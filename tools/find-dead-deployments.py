#!/usr/bin/env python3
"""
Live check: which deployments of the `standard` route are dead for good.

`standard` takes every keyed chat deployment from the catalogue. Providers
retire models faster than the catalogue follows (NVIDIA answers 410 Gone
for a model past its end of life, OpenRouter moves a `:free` model to paid
only), so the chain keeps deployments that can never answer. Every request
walks the chain in order and pays an error and a round trip for each of
them before it reaches a live one.

This script asks the proxy's /health for every `standard` deployment and
sorts what it sees:

* permanent -- retired, not found, paid only, not in the account's tier,
  key rejected. Waiting does not heal these: candidates for
  fork/standard.py EXCLUDED.
* not text -- answers, but is a music generator or a safety classifier by
  its name. Also a candidate: its answer is not what a `standard` client
  asked for.
* transient -- rate limit, daily quota, overload, 5xx, timeout. Heals by
  itself. Never a candidate, or one bad hour would strip live models from
  the chain.

Candidates come out as ready-to-paste EXCLUDED lines. The list itself is
edited by hand; the script never changes the chain.

/health sends one short request to every deployment, so each run spends
one request of every free quota (OpenRouter's free tier is 50 a day). Run
it when a provider key changes or the chain looks slow, not on a schedule.

stdlib only. Examples:

  python3 tools/find-dead-deployments.py
  python3 tools/find-dead-deployments.py --base-url http://host:4444

Exit status 0 when there is no candidate, 1 when there is, 2 on error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fork import standard  # noqa: E402

# First match wins; permanent markers come first because a provider's
# "not found" text can mention quotas or retries in the same message.
PERMANENT: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b410\b|end of life|no longer available", re.I), "410 Gone: retired by the provider"),
    (re.compile(r"unavailable for free|paid version is available", re.I), "free tier withdrawn, paid only"),
    (re.compile(r"insufficient credits|\b402\b", re.I), "402: paid model"),
    (re.compile(r"tier_not_allowed|not available in your subscription tier", re.I), "403: not in the account's tier"),
    (re.compile(r"AuthenticationError|api key is invalid|invalid api key|\b401\b", re.I), "401: key rejected"),
    (re.compile(r"NotFoundError|does not exist|no endpoints found|\b404\b", re.I), "404: model not found"),
)

TRANSIENT = re.compile(
    r"RateLimitError|\b429\b|quota|overloaded|ServiceUnavailable|\b50[0-4]\b|"
    r"InternalServerError|timeout|timed out|Too many", re.I)

# Model ids that are not text chat models whatever /health says: music
# generation and safety classifiers answer, but not with what was asked.
NOT_TEXT: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"lyria", re.I), "not a text model: music generation"),
    (re.compile(r"content-safety|llama-guard", re.I), "not a text model: safety classifier"),
)


def classify(error: str) -> tuple[str, str]:
    """(`permanent` | `transient` | `unknown`, reason) for a /health error."""
    for pattern, reason in PERMANENT:
        if pattern.search(error):
            return "permanent", reason
    if TRANSIENT.search(error):
        return "transient", "rate limit, quota, overload or timeout"
    return "unknown", re.sub(r"\s+", " ", error)[:120]


def not_text_reason(model: str) -> str | None:
    for pattern, reason in NOT_TEXT:
        if pattern.search(model):
            return reason
    return None


def endpoint_key(endpoint: dict) -> str:
    """The EXCLUDED key for a /health endpoint record: `model @ host`."""
    return standard.deployment_name(endpoint.get("model", ""), endpoint.get("api_base") or "")


def candidates(health: dict, excluded: dict[str, str], today: str) -> tuple[dict[str, str], list[str]]:
    """EXCLUDED candidates (key -> "date reason") not yet excluded, plus
    report lines for transient and unknown failures."""
    found: dict[str, str] = {}
    other: list[str] = []
    for ep in health.get("unhealthy_endpoints", []):
        key = endpoint_key(ep)
        kind, reason = classify(str(ep.get("error", "")))
        not_text = not_text_reason(ep.get("model", ""))
        if kind == "permanent" or not_text:
            if key not in excluded:
                found[key] = f"{today} {reason if kind == 'permanent' else not_text}"
        else:
            other.append(f"{kind:9} {key}  ({reason})")
    for ep in health.get("healthy_endpoints", []):
        key = endpoint_key(ep)
        reason = not_text_reason(ep.get("model", ""))
        if reason and key not in excluded:
            found[key] = f"{today} {reason}"
    return found, other


def _api_key_from_env() -> str:
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    env_file = REPO_ROOT / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def fetch_health(base_url: str, api_key: str, timeout: float) -> dict:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/health?model={standard.ROUTE_NAME}",
                                 headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("LITELLM_BASE_URL", "http://localhost:4444"))
    ap.add_argument("--api-key", default=None, help="default: LITELLM_MASTER_KEY from env or .env")
    ap.add_argument("--timeout", type=float, default=600.0, help="for the whole /health call")
    args = ap.parse_args()

    api_key = args.api_key or _api_key_from_env()
    if not api_key:
        print("no API key: pass --api-key or set LITELLM_MASTER_KEY", file=sys.stderr)
        return 2
    try:
        health = fetch_health(args.base_url, api_key, args.timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"/health failed: {exc}", file=sys.stderr)
        return 2

    found, other = candidates(health, standard.EXCLUDED, date.today().isoformat())
    total = len(health.get("healthy_endpoints", [])) + len(health.get("unhealthy_endpoints", []))
    print(f"{standard.ROUTE_NAME}: {total} deployments, "
          f"{len(health.get('unhealthy_endpoints', []))} failed the check")
    if other:
        print("\nNot candidates (heal by themselves, or look at them by hand):")
        for line in other:
            print(f"  {line}")
    if not found:
        print("\nNo new candidates for EXCLUDED.")
        return 0
    print(f"\nCandidates for fork/standard.py EXCLUDED ({len(found)}):")
    for key, value in found.items():
        print(f"    {json.dumps(key)}: {json.dumps(value)},")
    return 1


if __name__ == "__main__":
    sys.exit(main())
