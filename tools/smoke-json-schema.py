#!/usr/bin/env python3
"""
Live smoke test: strict `response_format=json_schema` through the proxy.

The proxy runs with `drop_params: true`, so a provider that does not
understand `response_format` silently gets a plain chat request and answers
with prose. Nothing upstream notices. This script does: it sends a short
Russian job ad with a strict JSON schema, parses the answer as JSON, and
validates it against that schema. Any deviation is a failure.

Deployments that pass 5/5 are recorded in JSON_SCHEMA_VERIFIED below. It is
a report, not a filter: the `standard` route keeps every provider in its
chain, so a client that needs strict JSON validates the answer and retries
(docs/USAGE.md). The table tells which deployments have been seen to comply.

stdlib only. Examples:

  python3 tools/smoke-json-schema.py --model standard --n 3
  python3 tools/smoke-json-schema.py --all-chat --n 2 --no-fallback
  python3 tools/smoke-json-schema.py --model gpt-oss-20b --base-url http://host:4444

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

# Deployments that returned schema-valid JSON on every attempt of a live run
# through this script. Key = deployment_key(model, api_base): the same
# `openai/<model>` id is served by several hosts (LLM7, NVIDIA, OVHcloud)
# with different behaviour, so the host is part of the identity. Value =
# date of the last run.
JSON_SCHEMA_VERIFIED: dict[str, str] = {
    # Google AI Studio (free tier), 5/5 with --no-fallback
    "gemini/gemini-3.5-flash-lite": "2026-09-16",
    "gemini/gemini-3.1-flash-lite": "2026-09-16",
    "gemini/gemini-flash-lite-latest": "2026-09-16",
    # LLM7.io anonymous tier, 5/5 with --no-fallback
    "openai/codestral-latest @ https://api.llm7.io/v1": "2026-09-16",
    "openai/mistral-Nemo-Instruct-2407 @ https://api.llm7.io/v1": "2026-09-16",
}

KEY_SEP = " @ "


def deployment_key(model: str, api_base: str = "") -> str:
    """Identity of a deployment: litellm `model:` id plus its host."""
    return f"{model}{KEY_SEP}{api_base}" if api_base else model


def split_deployment_key(key: str) -> tuple[str, str]:
    model, sep, api_base = key.partition(KEY_SEP)
    return model, api_base if sep else ""

MAX_VACANCY_CHARS = 300

# Strict schema (OpenAI sense): every property required, no extras.
VACANCY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "company": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "remote": {"type": "boolean"},
        "salary_min": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "salary_max": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "currency": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "seniority": {"type": "string", "enum": ["junior", "middle", "senior", "lead", "unknown"]},
        "skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title", "company", "location", "remote", "salary_min", "salary_max",
        "currency", "seniority", "skills",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "Ты извлекаешь структурированные данные из текста вакансии. "
    "Отвечай только JSON по заданной схеме, без пояснений."
)

# Short RU job ads (well under the limit even with the nonce appended).
_VACANCIES = [
    "Ищем Go-разработчика (middle) в финтех, Москва, гибрид. Зарплата 250–320 тыс. руб. "
    "Нужны Go, PostgreSQL, Kafka, Docker. Компания «Финтех Лаб».",
    "Senior Python backend, удалённо из любой точки РФ. От 350 000 до 450 000 руб. "
    "Стек: Python, FastAPI, Redis, Kubernetes. Работодатель: ООО «Облако».",
    "Junior QA-инженер, Санкт-Петербург, офис. Оклад 80 тыс. руб. "
    "Ручное тестирование, Postman, SQL. Компания не указана.",
    "Тимлид мобильной разработки, Казань или удалёнка. 400–500 тыс. руб. "
    "Kotlin, Swift, CI/CD, управление командой 6 человек. «Мобайл Групп».",
    "DevOps-инженер, полностью удалённо, зарплата обсуждается. Terraform, AWS, "
    "GitLab CI, Prometheus. Стартап «Скайнет Ру».",
]


def build_vacancy(i: int, nonce: str) -> str:
    """Returns the i-th ad with a nonce so identical prompts never repeat
    (the proxy caches identical requests for 5 minutes)."""
    body = _VACANCIES[i % len(_VACANCIES)]
    suffix = f" Код: {nonce}"
    return body[: MAX_VACANCY_CHARS - len(suffix)] + suffix


def build_request(model: str, vacancy_text: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": vacancy_text},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "vacancy", "strict": True, "schema": VACANCY_SCHEMA},
        },
        "temperature": 0,
        "cache": {"no-cache": True},
    }


# ─── Minimal JSON-Schema validator (the subset VACANCY_SCHEMA uses) ─────────

def _type_ok(value, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    raise ValueError(f"unsupported type: {t}")


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Returns a list of human-readable violations; empty list = valid."""
    if "anyOf" in schema:
        branches = [validate(value, s, path) for s in schema["anyOf"]]
        if any(not b for b in branches):
            return []
        return [f"{path}: matches none of anyOf"]
    errors: list[str] = []
    t = schema.get("type")
    if t is not None and not _type_ok(value, t):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required property missing")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key}: additional property not allowed")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
    if t == "array" and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors


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


def _post_json(url: str, api_key: str, payload: dict, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")


def run_attempt(base_url: str, api_key: str, model: str, text: str, timeout: float) -> Attempt:
    t0 = time.monotonic()
    try:
        status, headers, body = _post_json(
            f"{base_url}/v1/chat/completions", api_key, build_request(model, text), timeout
        )
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return Attempt(False, f"HTTP {e.code}: {detail}", "", "", _ms(t0))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Attempt(False, f"transport: {e}", "", "", _ms(t0))

    model_id = headers.get("x-litellm-model-id", "")
    api_base = headers.get("x-litellm-model-api-base", "")
    group = headers.get("x-litellm-model-group", "")
    try:
        parsed = json.loads(body)
        served = parsed.get("model", "")
        content = parsed["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return Attempt(False, f"bad completion envelope: {e}", model_id, api_base, _ms(t0))
    if not isinstance(content, str) or not content.strip():
        return Attempt(False, "empty content", model_id, api_base, _ms(t0), served, group)
    try:
        instance = json.loads(content)
    except ValueError:
        return Attempt(False, f"content is not JSON: {content[:120]!r}", model_id, api_base,
                       _ms(t0), served, group)
    errors = validate(instance, VACANCY_SCHEMA)
    if errors:
        return Attempt(False, "schema: " + "; ".join(errors[:4]), model_id, api_base, _ms(t0),
                       served, group)
    return Attempt(True, "", model_id, api_base, _ms(t0), served, group)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def fetch_model_info(base_url: str, api_key: str, timeout: float) -> list[dict]:
    req = urllib.request.Request(
        f"{base_url}/model/info", headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data", [])


def chat_model_names(info: list[dict]) -> list[str]:
    """model_names with mode=chat, in proxy order, deduplicated."""
    names: list[str] = []
    for entry in info:
        mode = (entry.get("model_info") or {}).get("mode", "chat")
        name = entry.get("model_name", "")
        if mode == "chat" and name and name not in names:
            names.append(name)
    return names


def deployment_index(info: list[dict]) -> dict[str, str]:
    """x-litellm-model-id -> deployment_key (what JSON_SCHEMA_VERIFIED records)."""
    index: dict[str, str] = {}
    for entry in info:
        mid = (entry.get("model_info") or {}).get("id")
        params = entry.get("litellm_params") or {}
        model = params.get("model")
        if mid and model:
            index[mid] = deployment_key(model, params.get("api_base") or "")
    return index


def reject_fallback(model: str, a: Attempt) -> Attempt:
    """--no-fallback: an answer served by another model group does not count
    for `model` (the catch-all `*` chain would otherwise mask a broken route)."""
    if a.ok and a.group and a.group != model:
        a.ok = False
        a.reason = f"served by fallback group {a.group!r}, not by {model!r}"
    return a


def verified_deployments(results: dict[str, list[Attempt]]) -> set[str]:
    """Deployments with >= 1 OK and no failed attempt attributed to them."""
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("LITELLM_BASE_URL", "http://localhost:4444"))
    ap.add_argument("--api-key", default=None, help="default: LITELLM_MASTER_KEY from env or .env")
    ap.add_argument("--model", action="append", default=[], help="model_name to test (repeatable)")
    ap.add_argument("--all-chat", action="store_true", help="test every chat model_name the proxy lists")
    ap.add_argument("--n", type=int, default=5, help="attempts per model (default 5)")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--no-fallback", action="store_true",
                    help="count an answer only if the requested model group served it")
    args = ap.parse_args()

    api_key = args.api_key or _api_key_from_env()
    if not api_key:
        print("ERROR: no API key (--api-key or LITELLM_MASTER_KEY)", file=sys.stderr)
        return 2
    base_url = args.base_url.rstrip("/")

    models = list(args.model)
    try:
        info = fetch_model_info(base_url, api_key, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"ERROR: cannot read {base_url}/model/info: {e}", file=sys.stderr)
        return 2
    by_id = deployment_index(info)
    if args.all_chat:
        models += [m for m in chat_model_names(info) if m not in models]
    if not models:
        ap.error("give --model at least once or --all-chat")

    results: dict[str, list[Attempt]] = {}
    for model in models:
        print(f"== {model}")
        attempts: list[Attempt] = []
        for i in range(args.n):
            nonce = uuid.uuid4().hex[:8]
            a = run_attempt(base_url, api_key, model, build_vacancy(i, nonce), args.timeout)
            a.deployment = by_id.get(a.model_id, "")
            if args.no_fallback:
                a = reject_fallback(model, a)
            attempts.append(a)
            mark = "OK  " if a.ok else "FAIL"
            via = f" via {a.group}" if a.group and a.group != model else ""
            where = (f"deployment={a.deployment or '-'}{via} "
                     f"model_id={a.model_id or '-'} base={a.api_base or '-'}")
            print(f"  #{i + 1} {mark} {a.ms:>6}ms {where}" + ("" if a.ok else f"\n       {a.reason}"))
        results[model] = attempts
        ok = sum(1 for a in attempts if a.ok)
        print(f"  -> {ok}/{len(attempts)}")

    print()
    print("Summary:")
    for model, attempts in results.items():
        ok = sum(1 for a in attempts if a.ok)
        served = sorted({a.deployment or a.model_id for a in attempts if a.ok and a.model_id})
        print(f"  {'PASS' if ok == len(attempts) else 'FAIL'} {model}: {ok}/{len(attempts)}"
              + (f"  served by: {', '.join(served)}" if served else ""))
    print()
    print("Deployments that answered schema-valid JSON (candidates for JSON_SCHEMA_VERIFIED):")
    for dep in sorted(verified_deployments(results)):
        known = f"  (verified {JSON_SCHEMA_VERIFIED[dep]})" if dep in JSON_SCHEMA_VERIFIED else ""
        print(f"  {dep}{known}")
    return 0 if all_passed(results) else 1


if __name__ == "__main__":
    sys.exit(main())
