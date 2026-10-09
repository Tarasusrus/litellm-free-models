"""The `standard` route: every keyed chat deployment behind one model name.

A client that does not care which free model answers sends `model:
"standard"`. The route is generated at render time from the deployments
that survive upstream's provider filter: each chat deployment is copied
under `model_name: standard` with a unique `litellm_params.order`. LiteLLM
routes to the lowest order first and, when that level fails, walks up
order by order (order-based fallbacks, bounded by `max_fallbacks`), so the
request tries every provider in turn until one answers.

Priority follows docs/adr/0001-fork-conventions.md. A provider key that
appears in `.env` puts that provider's models into the chain on the next
render; nothing has to be edited by hand.
"""
from __future__ import annotations

ROUTE_NAME = "standard"

# Provider priority for the `standard` chain, first = tried first. The
# first five are the operator's call; the rest are keyed providers ordered
# by their free-tier request budget (providers_config.py rpm, ties
# alphabetical); anonymous tiers go last because their quota is shared by
# everyone. Every provider in providers_config.PROVIDERS must be listed
# (tests/test_standard_route.py) so a new upstream provider gets a
# deliberate slot instead of a silent one.
PROVIDER_PRIORITY: tuple[str, ...] = (
    "google-ai",      # Gemini: strongest json_schema, but 500 requests/day per model
    "mistral",        # ~1 request/s and ~1B tokens/month on the free tier
    "nvidia",         # ~40 rpm, no published daily cap
    "groq",           # fast, small daily budget
    "openrouter",     # many free models behind one key, 50 requests/day without credits
    "cerebras",       # rpm 30
    "huggingface",    # rpm 30
    "cohere",         # rpm 20
    "cloudflare",     # rpm 10
    "opencode-zen",   # rpm 10
    "poolside",       # rpm 10
    "hetzner",        # rpm 5
    "zai",            # rpm 1
    "elevenlabs",     # no chat models today; listed so the set stays complete
    "llm7io",         # anonymous tier, 10 rpm shared across all callers
    "ovhcloud",       # anonymous tier; joins the chain only with a key
)

_RANK = {name: i for i, name in enumerate(PROVIDER_PRIORITY)}

KEY_SEP = " @ "

# Deployments the chain leaves out although their provider has a key: dead
# for good (retired, withdrawn from the free tier, never served to this
# account) or not text models at all. A request walks the chain in order,
# so each of them cost an error and a round trip before a live deployment
# answered. Key = deployment_name(): model id plus host, as in
# fork/tools_route.py. Value = date of the live check and its evidence.
# Candidates come from tools/find-dead-deployments.py; only failures that
# waiting does not heal belong here, never a rate limit or a timeout.
# Drop an entry when the provider brings the model back.
EXCLUDED: dict[str, str] = {
    # Google AI Studio
    "gemini/lyria-3-clip-preview": "2026-10-09 not a text model: music generation",
    "gemini/lyria-3-pro-preview": "2026-10-09 not a text model: music generation",
    # Mistral
    "mistral/mistral-large-latest": "2026-10-09 403: not in the account's tier",
    # NVIDIA NIM
    "openai/openai/gpt-oss-120b @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/meta/llama-3.3-70b-instruct @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/meta/llama-3.1-8b-instruct @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/nvidia/nemotron-3-nano-30b-a3b @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/deepseek-ai/deepseek-v4-flash-0731 @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/thinkingmachines/inkling @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/minimaxai/minimax-m3 @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/nvidia/nemotron-nano-12b-v2-vl @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/nvidia/nvidia-nemotron-nano-9b-v2 @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/stepfun-ai/step-3.7-flash @ https://integrate.api.nvidia.com/v1": "2026-10-09 410 Gone: retired by the provider",
    "openai/google/gemma-3-12b-it @ https://integrate.api.nvidia.com/v1": "2026-10-09 404: model not found",
    "openai/google/gemma-3-4b-it @ https://integrate.api.nvidia.com/v1": "2026-10-09 404: model not found",
    "openai/meta/llama-guard-4-12b @ https://integrate.api.nvidia.com/v1": "2026-10-09 not a text model: safety classifier",
    "openai/nvidia/nemotron-3.5-content-safety @ https://integrate.api.nvidia.com/v1": "2026-10-09 not a text model: safety classifier",
    # Groq
    "groq/qwen/qwen3.6-27b": "2026-10-09 404: model not found",
    # OpenRouter
    "openrouter/nvidia/nemotron-3-nano-30b-a3b:free": "2026-10-09 free tier withdrawn, paid only",
    "openrouter/z-ai/glm-5.2:free": "2026-10-09 free tier withdrawn, paid only",
    "openrouter/nvidia/nemotron-nano-12b-v2-vl:free": "2026-10-09 404: model not found",
    "openrouter/nvidia/nemotron-nano-9b-v2:free": "2026-10-09 404: model not found",
    "openrouter/google/lyria-3-clip-preview": "2026-10-09 402: paid model; not a text model: music generation",
    "openrouter/google/lyria-3-pro-preview": "2026-10-09 402: paid model; not a text model: music generation",
    "openrouter/nvidia/nemotron-3.5-content-safety:free": "2026-10-09 not a text model: safety classifier",
    # LLM7.io: rejected on the anonymous tier (LLM7IO_API_KEY=unused); drop
    # the entry once a dashboard token is in .env
    "openai/gemini-3.1-flash-lite @ https://api.llm7.io/v1": "2026-10-09 401: key rejected",
}


def deployment_key(block: dict) -> tuple[str, str]:
    """What makes two deployments the same backend: model id + host."""
    return block["model_id"], block.get("api_base", "")


def deployment_name(model_id: str, api_base: str = "") -> str:
    """A deployment spelled as one string: `model @ host`, or the bare model
    id when the provider has a single fixed host."""
    return f"{model_id}{KEY_SEP}{api_base}" if api_base else model_id


def has_key(provider: str, env: dict[str, str], providers: dict) -> bool:
    """True when `.env` carries a non-empty key for the provider.

    Anonymous tiers (`required=False`) still need a key here: an empty
    `api_key` is rejected by the OpenAI client inside LiteLLM before the
    request leaves, so a keyless deployment would only burn an attempt.
    """
    prov = providers.get(provider)
    return bool(prov and prov.env_var and env.get(prov.env_var))


def chain(blocks: list[dict], env: dict[str, str], providers: dict | None = None,
          excluded: dict[str, str] | None = None) -> list[dict]:
    """Ordered, de-duplicated chat deployments that have a key in `env`
    and are not in `excluded` (EXCLUDED by default).

    `blocks` are parse_blocks() dicts (rendered or template). Order is
    provider priority first, then template order; the first occurrence of
    a backend wins when several aliases point at it.
    """
    if providers is None:
        from providers_config import PROVIDERS
        providers = PROVIDERS
    if excluded is None:
        excluded = EXCLUDED
    picked: list[tuple[int, int, dict]] = []
    seen: set[tuple[str, str]] = set()
    for pos, b in enumerate(blocks):
        if b.get("mode", "chat") != "chat" or not b.get("provider"):
            continue
        if not has_key(b["provider"], env, providers):
            continue
        key = deployment_key(b)
        if deployment_name(*key) in excluded:
            continue
        if key in seen:
            continue
        seen.add(key)
        picked.append((_RANK.get(b["provider"], len(_RANK)), pos, b))
    picked.sort(key=lambda t: (t[0], t[1]))
    return [b for _, _, b in picked]


def _body(block: dict) -> list[str]:
    """Block lines without the model_name line and without the trailing
    blank/comment lines that parse_blocks attributes to the block (they
    are the next block's header)."""
    lines = block["lines"][1:]
    while lines and (not lines[-1].strip() or lines[-1].strip().startswith("#")):
        lines.pop()
    return lines


def render_blocks(chain_blocks: list[dict], route_name: str = ROUTE_NAME,
                  what: str = "every keyed chat deployment, tried in `order`") -> list[str]:
    """YAML lines for a generated route's deployments, ready to append to
    model_list. `standard` by default; fork/tools_route.py reuses it."""
    if not chain_blocks:
        return []
    out: list[str] = [
        "\n",
        "  # ===========================================================================\n",
        f"  # {route_name}  –  {what}\n",
        "  # (generated by fork/render.py; see docs/adr/0001-fork-conventions.md)\n",
        "  # ===========================================================================\n",
    ]
    for n, b in enumerate(chain_blocks, start=1):
        out.append("\n")
        out.append(f"  - model_name: {route_name}\n")
        for line in _body(b):
            out.append(line if line.endswith("\n") else line + "\n")
            if line.strip() == "litellm_params:":
                out.append(f"      order: {n}\n")
    return out
