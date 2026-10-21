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
* Deployments keep the proxy's usual timeout and retry settings, like every
  other generated route; this route adds no limits of its own.

`CLASSIFY_ORDER` entries are `model @ host` keys (as in tools_route.py).
Entries marked "unverified" have not been through tools/smoke-json-schema.py
with a key yet; the client validates every answer (simpleAI's
GuardedClassifier), so a bad one costs one attempt, not a wrong label.
"""
from __future__ import annotations

from fork import standard

ROUTE_NAME = "classify"

KEY_SEP = " @ "

# Preference order, first = tried first. Providers alternate on purpose.
CLASSIFY_ORDER: tuple[str, ...] = (
    # Order of 2026-10-21, from live probes through this proxy (one request per
    # deployment, a ~750-token classification prompt):
    #   llm7 mistral-nemo 0.6 s, gemini 3.1 flash-lite 0.7 s, groq gpt-oss-120b
    #   0.8 s, gemini flash-lite-latest 0.4 s, gemini 3.5 flash-lite 0.5 s;
    #   llm7 DeepSeek 1-20 s (varies), NVIDIA gemma-4-31b 6 s, NVIDIA gpt-oss-20b
    #   4.5 s. Fast ones lead; slow ones stay as late fallbacks.
    # Gemini 3.5 flash-lite is `standard`'s first choice (job-hunt burns its
    # daily quota), so it goes last among the Gemini models.
    "openai/mistral-Nemo-Instruct-2407 @ https://api.llm7.io/v1",
    "gemini/gemini-3.1-flash-lite",
    # Groq: 100% (24/24) in the simpleAI golden set; a small budget (2 rpm,
    # 8000 tpm per deployment), so it is a mid-chain stop, not the head.
    "groq/openai/gpt-oss-120b",
    "gemini/gemini-flash-lite-latest",
    # Cerebras 30 rpm and HuggingFace 30 rpm: fast, free, need a key. Unverified.
    "cerebras/gpt-oss-120b",
    "huggingface/meta-llama/Llama-3.3-70B-Instruct",
    "openai/DeepSeek-V4-Flash-0731 @ https://api.llm7.io/v1",
    "gemini/gemini-3.5-flash-lite",
    "openai/google/gemma-4-31b-it @ https://integrate.api.nvidia.com/v1",
    "openai/openai/gpt-oss-20b @ https://integrate.api.nvidia.com/v1",
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


def render_blocks(chain_blocks: list[dict]) -> list[str]:
    """YAML lines for the `classify` deployments, ready to append to model_list."""
    return standard.render_blocks(
        chain_blocks, route_name=ROUTE_NAME,
        what="short structured-output chain for classification, tried in `order`")
