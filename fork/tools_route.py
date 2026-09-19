"""The `tools` route: `standard` narrowed to deployments that really call tools.

An agent that wires an MCP server (or any function-calling loop) sends
`model: "tools"` with `tools` in the OpenAI format. The proxy runs with
`drop_params: true`, so on a backend that does not understand `tools` the
definitions are silently stripped and the model answers with prose -- the
agent's loop breaks without an error. `standard` deliberately keeps every
provider in its chain; `tools` keeps only those that returned a correct
`tool_calls` in a live run of tools/smoke-tool-calling.py.

Same mechanics as `standard` (fork/standard.py): generated at render time,
same provider order, unique `litellm_params.order`, closed with
`{"tools": []}` in fallbacks. The only difference is the allowlist below.
A new provider key puts its models into `standard` on the next render but
into `tools` only after the smoke has seen them pass.
"""
from __future__ import annotations

from fork import standard

ROUTE_NAME = "tools"

KEY_SEP = " @ "

# Deployments that returned a well-formed `tool_calls` (valid JSON
# arguments) and then a final text after the `role: tool` reply, on every
# attempt of a live run of tools/smoke-tool-calling.py. Key = model id plus
# host (`openai/<model>` is served by several hosts with different
# behaviour); value = date of the run. Re-run the smoke to extend or refresh:
#
#   python3 tools/smoke-tool-calling.py --all-deployments --n 5
TOOL_CALLING_VERIFIED: dict[str, str] = {
    # Google AI Studio (free tier), 5/5 addressed by deployment id
    "gemini/gemini-3.5-flash-lite": "2026-09-19",
    "gemini/gemini-3.1-flash-lite": "2026-09-19",
    "gemini/gemini-flash-lite-latest": "2026-09-19",
    # Groq, 5/5 addressed by deployment id
    "groq/openai/gpt-oss-120b": "2026-09-19",
    "groq/openai/gpt-oss-20b": "2026-09-19",
    "groq/openai/gpt-oss-safeguard-20b": "2026-09-19",
    # Seen failing the same run: gemma-4-26b (4/5, one Gemini 500),
    # gemma-4-31b (timeouts), lyria (quota), groq/qwen3.6-27b (404 at
    # Groq), LLM7 codestral (rate limit on step 2), LLM7 minimax (502s,
    # one second tool call instead of text), LLM7 mistral-nemo ("does
    # not support tools"). OVHcloud and the rest of LLM7: no usable key.
}


def verified_key(model_id: str, api_base: str = "") -> str:
    """Identity of a deployment as the allowlist spells it: `model @ host`."""
    return f"{model_id}{KEY_SEP}{api_base}" if api_base else model_id


def split_verified_key(key: str) -> tuple[str, str]:
    model, sep, api_base = key.partition(KEY_SEP)
    return model, api_base if sep else ""


def chain(blocks: list[dict], env: dict[str, str], providers: dict | None = None,
          verified: dict[str, str] | None = None) -> list[dict]:
    """The `standard` chain filtered to allowlisted deployments, order kept."""
    if verified is None:
        verified = TOOL_CALLING_VERIFIED
    return [b for b in standard.chain(blocks, env, providers)
            if verified_key(b["model_id"], b.get("api_base", "")) in verified]


def render_blocks(chain_blocks: list[dict]) -> list[str]:
    """YAML lines for the `tools` deployments, ready to append to model_list."""
    return standard.render_blocks(
        chain_blocks, route_name=ROUTE_NAME,
        what="keyed chat deployments verified for tool calling, tried in `order`")
