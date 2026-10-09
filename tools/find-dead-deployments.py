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

* permanent -- retired, not found, paid only. Waiting does not heal
  these: candidates for fork/standard.py EXCLUDED.
* key or plan -- 401/403: the key is rejected or the plan lacks the model.
  Reported, never a candidate: fix the key, not the chain.
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
  python3 tools/find-dead-deployments.py --from-file health.json   # no quota spent

Exit status 0 when there is no candidate, 1 when there is, 2 on error.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fork import standard  # noqa: E402


# A bare number is never a permanent marker: "Requested 410 tokens" or "retry
# in 404 ms" would read as a status. A status counts next to a status word
# ("Error code: 410", "'status': 410", "\"code\":404" — the separator is quotes,
# colons, one '=' and spaces, never '==', so `status_code == 404` in a
# traceback does not count), or code-first with its reason phrase ("410 Gone",
# "HTTP Error 404", "404 Not Found").
def _status(codes: str) -> str:
    return rf"\b(?:status|code)(?:[\s'\":]|=(?!=)){{0,4}}(?:{codes})\b"


def _code_first(code: int, phrase: str) -> str:
    return rf"\b(?:HTTP\s*)?(?:Error\s*)?{code}\s*:?\s*{phrase}\b|\bHTTP Error {code}\b"


# First match wins; permanent markers come first because a provider's
# "not found" text can mention quotas or retries in the same message.
PERMANENT: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(rf"{_status('410')}|{_code_first(410, 'Gone')}|end of life|no longer available", re.I),
     "410 Gone: retired by the provider"),
    (re.compile(r"unavailable for free|paid version is available", re.I), "free tier withdrawn, paid only"),
    (re.compile(rf"insufficient credits|{_status('402')}|{_code_first(402, 'Payment Required')}", re.I),
     "402: paid model"),
    (re.compile(rf"NotFoundError|does not exist|no endpoints found|{_status('404')}|"
                rf"{_code_first(404, '(?:Not Found|page not found)')}", re.I),
     "404: model not found"),
)

# 401 and 403 are about the key or the account's plan, not the model: one
# expired key fails every deployment of a provider at once. Reported, never a
# candidate — otherwise a key problem would land in the model denylist and
# stay there after the key is rotated.
KEY_OR_PLAN = re.compile(
    rf"tier_not_allowed|not available in your subscription tier|AuthenticationError|api key is invalid|"
    rf"invalid api key|{_status('401|403')}|{_code_first(401, 'Unauthorized')}|{_code_first(403, 'Forbidden')}",
    re.I)

# Transient markers cannot cause a false permanent, so bare codes are fine here.
TRANSIENT = re.compile(
    r"RateLimitError|\b429\b|\b50[0-4]\b|quota|overloaded|ServiceUnavailable|Bad Gateway|"
    r"InternalServerError|timeout|timed out|Too many", re.I)

# Model ids that are not text chat models whatever /health says: music
# generation and safety classifiers answer, but not with what was asked.
NOT_TEXT: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"lyria", re.I), "not a text model: music generation"),
    (re.compile(r"content-safety|llama-guard", re.I), "not a text model: safety classifier"),
)


def classify(error: str) -> tuple[str, str]:
    """(`permanent` | `key` | `transient` | `unknown`, reason) for a /health error."""
    for pattern, reason in PERMANENT:
        if pattern.search(error):
            return "permanent", reason
    if KEY_OR_PLAN.search(error):
        return "key", "401/403: key or plan — fix the key, not the chain"
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
    report lines for key/plan, transient and unknown failures."""
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


def shape_problem(health: object) -> str | None:
    """Why this is not a /health report, or None. Exit 1 means "candidates",
    so a malformed report must stop here, not crash later with a traceback."""
    if not isinstance(health, dict):
        return "not a JSON object"
    lists = [health.get(k) for k in ("healthy_endpoints", "unhealthy_endpoints")]
    if not any(isinstance(v, list) for v in lists):
        return "no healthy_endpoints/unhealthy_endpoints list"
    for value in lists:
        if value is not None and (not isinstance(value, list) or not all(isinstance(e, dict) for e in value)):
            return "an endpoint list holds something other than objects"
    return None


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
    ap.add_argument("--from-file", type=Path, default=None,
                    help="classify a saved /health JSON instead of calling the proxy (spends no quota)")
    args = ap.parse_args()

    source = str(args.from_file) if args.from_file is not None else "/health"
    try:
        if args.from_file is not None:
            health = json.loads(args.from_file.read_text(encoding="utf-8"))
        else:
            api_key = args.api_key or _api_key_from_env()
            if not api_key:
                print("no API key: pass --api-key or set LITELLM_MASTER_KEY", file=sys.stderr)
                return 2
            health = fetch_health(args.base_url, api_key, args.timeout)
    # URLError/TimeoutError are OSError, JSONDecodeError is ValueError,
    # IncompleteRead and friends are HTTPException.
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"{source} failed: {exc}", file=sys.stderr)
        return 2
    problem = shape_problem(health)
    if problem:
        # An error body ({"error": ...}) is not "nothing is dead": exit 2, not 0.
        print(f"{source} is not a /health report: {problem}", file=sys.stderr)
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
