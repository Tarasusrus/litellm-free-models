#!/usr/bin/env python3
"""
Live smoke test: OpenAI-style tool calling through the proxy, two steps.

The proxy runs with `drop_params: true`, so a backend that does not
understand `tools` silently gets a plain chat request and answers with
prose; an agent's "model -> tool -> model" loop breaks without an error.
This script sends one function (`get_weather(city)`) with `tool_choice:
auto` and passes only if

  1. the answer carries `tool_calls` naming that function with arguments
     that parse to a JSON object holding a non-empty `city`, and
  2. after the `role: tool` reply the model returns a final text (and no
     further call).

Deployments that pass every attempt of a run are what TOOL_CALLING_VERIFIED
in fork/tools_route.py lists; unlike the json_schema smoke this list IS a
filter: the `tools` route is generated from it. Every answer prints its
`x-litellm-model-name` header, so a run against `--model tools` shows which
backend served each attempt.

stdlib only. Examples:

  python3 tools/smoke-tool-calling.py --model tools --n 5
  python3 tools/smoke-tool-calling.py --all-deployments --n 5
  python3 tools/smoke-tool-calling.py --deployment "groq/openai/gpt-oss-20b" --n 3
  python3 tools/smoke-tool-calling.py --model gpt-oss-120b --base-url http://host:4444

`--deployment` and `--all-deployments` address a chat backend by its deployment id (the
proxy accepts `model_info.id` as `model`), so shared model names cannot
mask a backend that fails, and with `fallbacks: []` so the backend's own
error comes back instead of an answer from the catch-all `*` chain (an
answer that still arrives from another backend is rejected). Attempts run round-robin over the targets
with `--pace` seconds between rounds: the template caps most deployments
at 2 rpm and one attempt is two requests, so one round per minute.

Exit status 0 only if every attempt of every model passed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fork import tools_route  # noqa: E402

TOOL_NAME = "get_weather"

# Routes fork/render.py generates: copies of other backends, never a
# deployment of their own, so --all-deployments skips them.
GENERATED_ROUTES = frozenset({"standard", tools_route.ROUTE_NAME})

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Current weather for a city. Call it whenever the user asks about weather.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = (
    "You are a weather assistant. You have no weather knowledge of your own: "
    f"to answer a weather question you must call the `{TOOL_NAME}` tool first."
)

CITIES = ["Paris", "Tokyo", "Lima", "Oslo", "Cairo", "Perth"]

deployment_key = tools_route.verified_key
split_deployment_key = tools_route.split_verified_key


def build_request(model: str, city: str, nonce: str, no_fallback: bool = False) -> dict:
    """Step one: the question plus the single tool, uncached (the proxy
    caches identical requests; the nonce keeps every attempt distinct).

    `no_fallback` sends `fallbacks: []`, which LiteLLM honours per request:
    the addressed deployment's own error comes back instead of an answer
    from the catch-all `*` chain."""
    req = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"What is the weather in {city} right now? (request {nonce})"},
        ],
        "tools": [TOOL_DEF],
        "tool_choice": "auto",
        "temperature": 0,
        "cache": {"no-cache": True},
    }
    if no_fallback:
        req["fallbacks"] = []
    return req


def check_tool_call(message: dict) -> tuple[bool, str, dict | None]:
    """(ok, reason, call): the first tool call must be get_weather with a
    JSON object holding a non-empty string `city`."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return False, "no tool_calls in the answer (prose instead of a call)", None
    call = calls[0]
    fn = call.get("function") if isinstance(call, dict) else None
    if not isinstance(fn, dict):
        return False, "tool_calls[0] has no function", None
    name = fn.get("name")
    if name != TOOL_NAME:
        return False, f"called {name!r}, expected {TOOL_NAME!r}", None
    raw = fn.get("arguments")
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except ValueError:
            return False, f"arguments are not valid JSON: {raw[:80]!r}", None
    else:
        args = raw
    if not isinstance(args, dict):
        return False, f"arguments are not a JSON object: {args!r}", None
    city = args.get("city")
    if not isinstance(city, str) or not city.strip():
        return False, f"arguments lack a non-empty city: {args!r}", None
    if not isinstance(call.get("id"), str) or not call["id"]:
        return False, "tool call has no id to answer to", None
    return True, "", call


def build_followup(first: dict, assistant_message: dict, call: dict) -> dict:
    """Step two: replay the conversation, append the model's call and the
    tool result addressed to its id. `first` is left untouched."""
    raw = call["function"]["arguments"]
    args = json.loads(raw) if isinstance(raw, str) else raw
    result = {"city": args["city"], "temperature_c": 21, "sky": "clear"}
    second = {k: v for k, v in first.items() if k != "messages"}
    second["messages"] = [*first["messages"], assistant_message,
                          {"role": "tool", "tool_call_id": call["id"],
                           "content": json.dumps(result, ensure_ascii=False)}]
    return second


def check_final(message: dict) -> tuple[bool, str]:
    """After the tool reply the model must answer in text, not call again."""
    if message.get("tool_calls"):
        return False, "answered with tool_calls again instead of text"
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return False, "empty final content"
    return True, ""


# ─── Live run ───────────────────────────────────────────────────────────────

@dataclass
class Attempt:
    ok: bool
    reason: str
    model_id: str
    api_base: str
    ms: int
    served_model: str = ""
    group: str = ""
    deployment: str = ""
    final_text: str = ""
    served_model2: str = ""  # backend of step two when it differs from step one


def _post_json(url: str, api_key: str, payload: dict, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _call(base_url: str, api_key: str, payload: dict, timeout: float):
    """Returns (message, headers, error). A transport/HTTP problem is an error."""
    try:
        _, headers, body = _post_json(f"{base_url}/v1/chat/completions", api_key, payload, timeout)
    except urllib.error.HTTPError as e:
        return None, {}, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, {}, f"transport: {e}"
    try:
        message = json.loads(body)["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return None, headers, f"bad completion envelope: {e}"
    return message, headers, ""


def run_attempt(base_url: str, api_key: str, model: str, city: str, nonce: str,
                timeout: float, no_fallback: bool = False) -> Attempt:
    t0 = time.monotonic()
    first = build_request(model, city, nonce, no_fallback)
    message, headers, err = _call(base_url, api_key, first, timeout)
    model_id = headers.get("x-litellm-model-id", "")
    api_base = headers.get("x-litellm-model-api-base", "")
    served = headers.get("x-litellm-model-name", "")
    group = headers.get("x-litellm-model-group", "")
    if err:
        return Attempt(False, err, model_id, api_base, _ms(t0), served, group)
    ok, reason, call = check_tool_call(message)
    if not ok:
        return Attempt(False, "step 1: " + reason, model_id, api_base, _ms(t0), served, group)

    second = build_followup(first, message, call)
    message2, headers2, err = _call(base_url, api_key, second, timeout)
    if err:
        return Attempt(False, "step 2: " + err, model_id, api_base, _ms(t0), served, group)
    served2 = headers2.get("x-litellm-model-name", "")
    if no_fallback and served2 and served2 != served:
        # Addressed by id, both steps must come from that backend. Through
        # a route, a different verified backend on step two is what the
        # route promises; it is reported, not rejected.
        return Attempt(False, f"step 2 served by another backend: {served2}", model_id, api_base,
                       _ms(t0), served, group)
    ok, reason = check_final(message2)
    if not ok:
        return Attempt(False, "step 2: " + reason, model_id, api_base, _ms(t0), served, group)
    return Attempt(True, "", model_id, api_base, _ms(t0), served, group,
                   final_text=message2["content"].strip(), served_model2=served2)


def reject_fallback(expected_id: str, a: Attempt) -> Attempt:
    """An answer served by another deployment (the catch-all `*` chain took
    over after the addressed one failed) does not count for it."""
    if expected_id and a.ok and a.model_id != expected_id:
        a.ok = False
        a.reason = f"served by fallback {a.served_model or a.model_id!r}, not the addressed deployment"
    return a


def fetch_model_info(base_url: str, api_key: str, timeout: float) -> list[dict]:
    req = urllib.request.Request(f"{base_url}/model/info",
                                 headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data", [])


def deployment_index(info: list[dict]) -> dict[str, str]:
    """x-litellm-model-id -> deployment key (what TOOL_CALLING_VERIFIED records)."""
    index: dict[str, str] = {}
    for entry in info:
        mid = (entry.get("model_info") or {}).get("id")
        params = entry.get("litellm_params") or {}
        model = params.get("model")
        if mid and model:
            index[mid] = deployment_key(model, params.get("api_base") or "")
    return index


def unique_deployments(info: list[dict]) -> list[tuple[str, str]]:
    """(deployment key, one deployment id) per chat backend, proxy order.

    Generated routes are copies of these backends and are skipped."""
    picked: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in info:
        if entry.get("model_name") in GENERATED_ROUTES:
            continue
        mi = entry.get("model_info") or {}
        if mi.get("mode", "chat") != "chat" or not mi.get("id"):
            continue
        params = entry.get("litellm_params") or {}
        if not params.get("model"):
            continue
        key = deployment_key(params["model"], params.get("api_base") or "")
        if key in seen:
            continue
        seen.add(key)
        picked.append((key, mi["id"]))
    return picked


def verified_deployments(results: dict[str, list[Attempt]]) -> set[str]:
    """Deployments where every attributed attempt passed (>= 1 attempt)."""
    good: set[str] = set()
    bad: set[str] = set()
    for attempts in results.values():
        for a in attempts:
            if not a.deployment:
                continue
            (good if a.ok else bad).add(a.deployment)
    return good - bad


def all_passed(results: dict[str, list[Attempt]]) -> bool:
    if not results:
        return False
    return all(attempts and all(a.ok for a in attempts) for attempts in results.values())


def _api_key_from_env() -> str:
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("LITELLM_BASE_URL", "http://localhost:4444"))
    ap.add_argument("--api-key", default=None, help="default: LITELLM_MASTER_KEY from env or .env")
    ap.add_argument("--model", action="append", default=[], help="model_name to test (repeatable)")
    ap.add_argument("--deployment", action="append", default=[], metavar="KEY",
                    help="one backend by its allowlist key (`model` or `model @ host`), "
                         "addressed by deployment id (repeatable)")
    ap.add_argument("--all-deployments", action="store_true",
                    help="test every chat backend the proxy lists, addressed by deployment id")
    ap.add_argument("--n", type=int, default=5, help="attempts per model (default 5)")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--pace", type=float, default=60.0,
                    help="minimum seconds between rounds (default 60: rpm 2 = one 2-step attempt/min)")
    args = ap.parse_args()

    api_key = args.api_key or _api_key_from_env()
    if not api_key:
        print("ERROR: no API key (--api-key or LITELLM_MASTER_KEY)", file=sys.stderr)
        return 2
    base_url = args.base_url.rstrip("/")

    try:
        info = fetch_model_info(base_url, api_key, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"ERROR: cannot read {base_url}/model/info: {e}", file=sys.stderr)
        return 2
    by_id = deployment_index(info)
    targets: list[tuple[str, str]] = [(m, m) for m in args.model]  # (label, model to send)
    deployments = dict(unique_deployments(info))
    for key in args.deployment:
        if key not in deployments:
            print(f"ERROR: no chat deployment {key!r}; known: {', '.join(deployments)}",
                  file=sys.stderr)
            return 2
        targets.append((key, deployments[key]))
    if args.all_deployments:
        targets += [(key, mid) for key, mid in deployments.items()
                    if key not in {t[0] for t in targets}]
    if not targets:
        ap.error("give --model or --deployment at least once, or --all-deployments")

    results: dict[str, list[Attempt]] = {label: [] for label, _ in targets}
    for i in range(args.n):
        round_start = time.monotonic()
        print(f"== round {i + 1}/{args.n}")
        for label, model in targets:
            addressed_id = model if model != label else ""
            nonce = uuid.uuid4().hex[:8]
            a = run_attempt(base_url, api_key, model, CITIES[i % len(CITIES)], nonce, args.timeout,
                            no_fallback=bool(addressed_id))
            a = reject_fallback(addressed_id, a)
            # Addressed by id, the verdict belongs to that deployment even
            # when a fallback answered; by name, to whoever served it.
            a.deployment = label if addressed_id else by_id.get(a.model_id, "")
            results[label].append(a)
            mark = "OK  " if a.ok else "FAIL"
            hop = f" -> {a.served_model2}" if a.served_model2 and a.served_model2 != a.served_model else ""
            print(f"  {mark} {a.ms:>6}ms {label}  x-litellm-model-name={a.served_model or '-'}{hop}"
                  + (f"\n       -> {a.final_text[:100]!r}" if a.ok else f"\n       {a.reason[:160]}"))
        if i + 1 < args.n:
            wait = args.pace - (time.monotonic() - round_start)
            if wait > 0:
                print(f"  (pacing: {wait:.0f}s until the next round)")
                time.sleep(wait)
    print()
    print("Summary:")
    for label, attempts in results.items():
        ok = sum(1 for a in attempts if a.ok)
        served = sorted({m for a in attempts if a.ok for m in (a.served_model, a.served_model2) if m})
        print(f"  {'PASS' if ok == len(attempts) else 'FAIL'} {label}: {ok}/{len(attempts)}"
              + (f"  served by: {', '.join(served)}" if served else ""))
    print()
    print("Deployments that completed both steps every time (candidates for TOOL_CALLING_VERIFIED):")
    for dep in sorted(verified_deployments(results)):
        known = tools_route.TOOL_CALLING_VERIFIED.get(dep)
        print(f"  {dep}" + (f"  (verified {known})" if known else "  (new)"))
    return 0 if all_passed(results) else 1


if __name__ == "__main__":
    sys.exit(main())
