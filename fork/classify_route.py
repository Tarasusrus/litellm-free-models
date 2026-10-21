"""The `classify` route: a short chain for tiny structured-output requests.

Built for a client that classifies one short text into one of a few labels
(simpleAI's expense-category classifier): ~750 tokens in, <=150 out, strict
`response_format: json_schema`, temperature 0, a person waiting for the
answer. `standard` is the wrong tool for it: it walks every keyed provider in
catalogue order, so one exhausted free-tier quota costs seconds (measured
2026-10-21: `standard` ~8 s average, direct models 0.1-1.5 s) and it keeps
models that cannot hold a strict schema.

Same mechanics as `standard` and `tools` (fork/standard.py): generated at
render time from the keyed chat deployments, unique `litellm_params.order`,
closed with `{"classify": []}`. Differences:

* The order is an operator list (CLASSIFY_ORDER), not provider priority:
  providers alternate, so a quota that one provider burnt (Gemini free tier:
  500 requests per day per model; Groq: tokens per day) is skipped after one
  fast failure instead of eating the whole chain.
* Only deployments named in CLASSIFY_ORDER take part. A deployment without a
  key drops out at render time, so adding NVIDIA/Cerebras/... keys later
  puts their entries into the chain at the slot written here.
* Each attempt is short (CLASSIFY_TIMEOUT_S, no retries of its own): the
  next deployment is the retry.

`CLASSIFY_ORDER` entries are `model @ host` keys (as in tools_route.py).
Entries marked "unverified" have not been through tools/smoke-json-schema.py
with a key yet; the client validates every answer (simpleAI's
GuardedClassifier), so a bad one costs one attempt, not a wrong label.
"""
from __future__ import annotations

import re

from fork import standard

ROUTE_NAME = "classify"

KEY_SEP = " @ "

# Per-attempt timeout and retries written into every `classify` deployment.
# The client waits at most ~10 s in total; 4 s per deployment leaves room for
# two fallbacks. `num_retries: 0`: the next deployment is the retry.
CLASSIFY_TIMEOUT_S = 4
CLASSIFY_NUM_RETRIES = 0

# Preference order, first = tried first. Providers alternate on purpose.
CLASSIFY_ORDER: tuple[str, ...] = (
    # LLM7.io: 40 rpm anonymous tier; DeepSeek-V4-Flash scored 19/19 in the
    # simpleAI golden set (2026-10-21), ~1 s.
    "openai/DeepSeek-V4-Flash-0731 @ https://api.llm7.io/v1",
    # NVIDIA NIM, 40 rpm, a plain (non-reasoning) instruct model. Unverified.
    "openai/google/gemma-4-31b-it @ https://integrate.api.nvidia.com/v1",
    # Google AI Studio: strong json_schema support, verified 5/5; its daily
    # request quota is shared with other consumers, hence not first.
    "gemini/gemini-3.1-flash-lite",
    # Cerebras, 30 rpm, very fast. Unverified.
    "cerebras/gpt-oss-120b",
    # HuggingFace, 30 rpm. Unverified.
    "huggingface/meta-llama/Llama-3.3-70B-Instruct",
    "gemini/gemini-flash-lite-latest",
    "openai/mistral-Nemo-Instruct-2407 @ https://api.llm7.io/v1",
    # Groq: 100% (24/24) in the golden set, but a small daily token budget.
    "groq/openai/gpt-oss-120b",
    "openai/openai/gpt-oss-20b @ https://integrate.api.nvidia.com/v1",
    "gemini/gemini-3.5-flash-lite",
    "huggingface/deepseek-ai/DeepSeek-V4-Flash-0731",
    "cerebras/gemma-4-31b",
    "groq/openai/gpt-oss-20b",
)
# Left out on purpose: OpenRouter (1 rpm), OVHcloud (2 rpm), Mistral large and
# Cohere (slow for a waiting user), Cloudflare (its api_base is a template
# placeholder, so it has no stable key here), reasoning models (answer in thought tokens).


def order_key(model_id: str, api_base: str = "") -> str:
    """Identity of a deployment as CLASSIFY_ORDER spells it: `model @ host`."""
    return f"{model_id}{KEY_SEP}{api_base}" if api_base else model_id


def chain(blocks: list[dict], env: dict[str, str], providers: dict | None = None,
          order: tuple[str, ...] | None = None) -> list[dict]:
    """Keyed chat deployments named in `order`, in that order, no duplicates."""
    if order is None:
        order = CLASSIFY_ORDER
    rank = {key: i for i, key in enumerate(order)}
    picked = [(rank[k], b) for b in standard.chain(blocks, env, providers)
              if (k := order_key(b["model_id"], b.get("api_base", ""))) in rank]
    picked.sort(key=lambda t: t[0])
    return [b for _, b in picked]


_TIMEOUT = re.compile(r"^(\s+)timeout:\s*\S+\s*$")
_RETRIES = re.compile(r"^(\s+)num_retries:\s*\S+\s*$")


def _short_attempts_block(block: list[str]) -> list[str]:
    """One deployment: its copied `timeout` / `num_retries` become the route's
    values; a missing one is added right after `order:`."""
    out: list[str] = []
    seen_t = seen_r = False
    for line in block:
        text = line.rstrip("\n")
        if m := _TIMEOUT.match(text):
            out.append(f"{m.group(1)}timeout: {CLASSIFY_TIMEOUT_S}\n")
            seen_t = True
        elif m := _RETRIES.match(text):
            out.append(f"{m.group(1)}num_retries: {CLASSIFY_NUM_RETRIES}\n")
            seen_r = True
        else:
            out.append(line)
    if seen_t and seen_r:
        return out
    fixed: list[str] = []
    for line in out:
        fixed.append(line)
        if line.strip().startswith("order:"):
            indent = line[: len(line) - len(line.lstrip())]
            if not seen_t:
                fixed.append(f"{indent}timeout: {CLASSIFY_TIMEOUT_S}\n")
            if not seen_r:
                fixed.append(f"{indent}num_retries: {CLASSIFY_NUM_RETRIES}\n")
    return fixed


def _short_attempts(lines: list[str]) -> list[str]:
    """Apply _short_attempts_block to every `- model_name:` block."""
    out: list[str] = []
    block: list[str] = []
    for line in lines:
        if line.startswith("  - model_name:"):
            out.extend(_short_attempts_block(block))
            block = []
        block.append(line)
    out.extend(_short_attempts_block(block))
    return out


def render_blocks(chain_blocks: list[dict]) -> list[str]:
    """YAML lines for the `classify` deployments, ready to append to model_list."""
    lines = standard.render_blocks(
        chain_blocks, route_name=ROUTE_NAME,
        what="short structured-output chain for classification, tried in `order`")
    return _short_attempts(lines)
