#!/usr/bin/env python3
"""
Live check: which deployments really read an image (fills VISION_VERIFIED).

Each attempt sends tools/fixtures/receipt-synthetic.png -- a synthetic
receipt, no personal data -- as an `image_url` data-URL and asks for the
total and currency as JSON. An attempt passes only if the answer is
247.5 THB. A text model behind `drop_params` does not fail on an image: it
invents a total or refuses, so the check is on the content, not the status.

  python3 tools/smoke-vision.py --all-deployments --n 3
  python3 tools/smoke-vision.py --deployment "gemini/gemini-3.5-flash-lite" --n 3
  python3 tools/smoke-vision.py --model vision --n 3

`--deployment` / `--all-deployments` address a backend by its deployment id
with `fallbacks: []`, so a fallback can neither answer for it nor hide its
error (same mechanics as tools/smoke-tool-calling.py). Deployments that
passed every attempt are printed as candidates for VISION_VERIFIED in
fork/vision_route.py.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import math
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fork import vision_route  # noqa: E402

FIXTURE = REPO_ROOT / "tools" / "fixtures" / "receipt-synthetic.png"
EXPECTED_TOTAL = 247.5
EXPECTED_CURRENCY = "THB"

PROMPT = ("This is a photo of a shop receipt. Return only JSON "
          '{"total": <final amount paid, number>, "currency": "<ISO 4217 code>"}. '
          "(request {nonce})")


def _load_tool_smoke():
    """tools/smoke-tool-calling.py carries the HTTP and /model/info helpers."""
    spec = importlib.util.spec_from_file_location(
        "smoke_tool_calling", REPO_ROOT / "tools" / "smoke-tool-calling.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


tc = _load_tool_smoke()


def build_request(model: str, image: bytes, nonce: str, no_fallback: bool = False) -> dict:
    data_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    req = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT.replace("{nonce}", nonce)},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "temperature": 0,
        # reasoning models spend hidden tokens before the answer
        "max_tokens": 1024,
        "cache": {"no-cache": True},
    }
    if no_fallback:
        req["fallbacks"] = []
    return req


def check_answer(content) -> tuple[bool, str]:
    """247.5 THB from whatever JSON the model wrapped in text or fences."""
    if not isinstance(content, str) or "{" not in content:
        return False, f"no JSON in answer: {str(content)[:80]!r}"
    m = re.search(r"\{.*\}", content, re.S)
    try:
        obj = json.loads(m.group(0)) if m else None
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        return False, f"unparsable JSON: {content[:80]!r}"
    try:
        total = float(str(obj.get("total")).replace(",", ""))
    except ValueError:
        return False, f"total is not a number: {obj.get('total')!r}"
    currency = str(obj.get("currency", "")).strip().upper()
    # NaN compares false with everything: without isfinite a model that
    # never saw the image would pass with {"total": NaN}.
    if not math.isfinite(total) or abs(total - EXPECTED_TOTAL) > 0.001 or currency != EXPECTED_CURRENCY:
        return False, f"read {total} {currency!r}, want {EXPECTED_TOTAL} {EXPECTED_CURRENCY}"
    return True, ""


@dataclass
class Attempt:
    ok: bool
    reason: str
    model_id: str
    ms: int
    served_model: str = ""
    deployment: str = ""


def run_attempt(base_url: str, api_key: str, model: str, image: bytes,
                timeout: float, no_fallback: bool) -> Attempt:
    t0 = time.monotonic()
    nonce = uuid.uuid4().hex[:8]
    message, headers, err = tc._call(base_url, api_key, build_request(model, image, nonce, no_fallback), timeout)
    model_id = headers.get("x-litellm-model-id", "")
    served = headers.get("x-litellm-model-name", "")
    if err:
        return Attempt(False, err, model_id, tc._ms(t0), served)
    ok, reason = check_answer(message.get("content"))
    return Attempt(ok, reason, model_id, tc._ms(t0), served)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:4444")
    ap.add_argument("--api-key", default=None, help="default: LITELLM_MASTER_KEY from env or .env")
    ap.add_argument("--model", action="append", default=[], help="model_name to test (repeatable)")
    ap.add_argument("--deployment", action="append", default=[], metavar="KEY",
                    help="`model @ host` key, addressed by deployment id (repeatable)")
    ap.add_argument("--all-deployments", action="store_true",
                    help="every chat backend the proxy lists, addressed by deployment id")
    ap.add_argument("--n", type=int, default=3, help="attempts per target (default 3)")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--pace", type=float, default=30.0, help="minimum seconds between rounds")
    args = ap.parse_args()

    api_key = args.api_key or tc._api_key_from_env()
    if not api_key:
        print("ERROR: no API key (--api-key or LITELLM_MASTER_KEY)", file=sys.stderr)
        return 2
    base_url = args.base_url.rstrip("/")
    image = FIXTURE.read_bytes()
    try:
        info = tc.fetch_model_info(base_url, api_key, args.timeout)
    except (OSError, ValueError) as e:
        print(f"ERROR: cannot read {base_url}/model/info: {e}", file=sys.stderr)
        return 2
    by_id = tc.deployment_index(info)
    deployments = dict(tc.unique_deployments(info))
    targets: list[tuple[str, str]] = [(m, m) for m in args.model]
    for key in args.deployment:
        if key not in deployments:
            print(f"ERROR: no chat deployment {key!r}", file=sys.stderr)
            return 2
        targets.append((key, deployments[key]))
    if args.all_deployments:
        targets += [(k, mid) for k, mid in deployments.items() if k not in {t[0] for t in targets}]
    if not targets:
        ap.error("give --model or --deployment at least once, or --all-deployments")

    results: dict[str, list[Attempt]] = {label: [] for label, _ in targets}
    for i in range(args.n):
        round_start = time.monotonic()
        print(f"== round {i + 1}/{args.n}")
        for label, model in targets:
            addressed = model if model != label else ""
            a = run_attempt(base_url, api_key, model, image, args.timeout, no_fallback=bool(addressed))
            if addressed and a.ok and a.model_id != addressed:
                a.ok, a.reason = False, f"served by fallback {a.served_model!r}"
            a.deployment = label if addressed else by_id.get(a.model_id, "")
            results[label].append(a)
            print(f"  {'OK  ' if a.ok else 'FAIL'} {a.ms:>6}ms {label}"
                  + ("" if a.ok else f"\n       {a.reason[:160]}"))
        if i + 1 < args.n:
            wait = args.pace - (time.monotonic() - round_start)
            if wait > 0:
                time.sleep(wait)
    print("\nDeployments that read the receipt every time (candidates for VISION_VERIFIED):")
    for dep in sorted(tc.verified_deployments(results)):
        known = vision_route.VISION_VERIFIED.get(dep)
        print(f"  {dep}" + (f"  (verified {known})" if known else "  (new)"))
    return 0 if tc.all_passed(results) else 1


if __name__ == "__main__":
    sys.exit(main())
