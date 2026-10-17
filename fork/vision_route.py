"""The `vision` route: `standard` narrowed to deployments that read images.

A client that sends an `image_url` part sends `model: "vision"`. PDF
`file` parts are not verified by the smoke and are not promised here. A text-only backend behind `drop_params: true` does not fail on
such a request: it answers about a picture it never saw, or hangs -- and
upstream's own fallbacks lead there (a named group ends in `"*"`, which is
text models). `vision` keeps only the deployments that read a synthetic
receipt correctly in a live run of tools/smoke-vision.py, and is closed
with `{"vision": []}` so a request never leaves them.

Same mechanics as `tools` (fork/tools_route.py); the differences are the
allowlist and `model_info.supports_vision: true` on every deployment:
LiteLLM fills that flag only for providers it knows, and clients read it
from /model/info to decide what they may send (docs/adr/0001 §6).
"""
from __future__ import annotations

from fork import standard

ROUTE_NAME = "vision"

KEY_SEP = standard.KEY_SEP

# Deployments that returned the correct total of the synthetic receipt on
# every attempt of a live run of tools/smoke-vision.py, addressed by
# deployment id. Key = `model @ host` as in fork/tools_route.py; value =
# date of the run. Re-run the smoke to extend or refresh:
#
#   python3 tools/smoke-vision.py --all-deployments --n 3
#
# Empty until a live run passes: while it is empty the route is not
# rendered, `vision` is absent from /v1/models and /model/info, and a client
# that reads /model/info (simpleAI) reports the feature as not configured.
VISION_VERIFIED: dict[str, str] = {}


def verified_key(model_id: str, api_base: str = "") -> str:
    """Identity of a deployment as the allowlist spells it: `model @ host`."""
    return standard.deployment_name(model_id, api_base)


def chain(blocks: list[dict], env: dict[str, str], providers: dict | None = None,
          verified: dict[str, str] | None = None,
          excluded: dict[str, str] | None = None) -> list[dict]:
    """The `standard` chain filtered to allowlisted deployments, order kept.

    Built on `standard.chain`, so standard.EXCLUDED applies here too."""
    if verified is None:
        verified = VISION_VERIFIED
    return [b for b in standard.chain(blocks, env, providers, excluded)
            if verified_key(b["model_id"], b.get("api_base", "")) in verified]


_FLAG = "      supports_vision: true\n"

# Images are personal data (receipts). LiteLLM passes deployment
# litellm_params into the call, where `turn_off_message_logging` is a
# per-request switch for every logging callback (Langfuse, spend logs):
# the trace keeps model, timing and status, the messages become
# "redacted-by-litellm". Set here, it holds no matter what the client sends.
_NO_MESSAGE_LOGGING = "      turn_off_message_logging: true\n"


def _key_line(line: str) -> str:
    """`    model_info:  # note` -> `model_info:`; indentation and an inline
    comment must not hide the key (standard.render_blocks matches on
    strip() too)."""
    return line.split("#", 1)[0].strip()


def _with_vision_flag(lines: list[str]) -> list[str]:
    """One deployment's lines with `model_info.supports_vision: true` and
    `litellm_params.turn_off_message_logging: true`.

    Existing lines for either key are replaced, not duplicated; a block
    without `model_info` gets one."""
    out = [ln for ln in lines
           if not ln.strip().startswith(("supports_vision:", "turn_off_message_logging:"))]
    for i, ln in enumerate(out):
        if _key_line(ln) == "litellm_params:":
            out = out[:i + 1] + [_NO_MESSAGE_LOGGING] + out[i + 1:]
            break
    for i, ln in enumerate(out):
        if _key_line(ln) == "model_info:":
            return out[:i + 1] + [_FLAG] + out[i + 1:]
    end = len(out)
    while end and not out[end - 1].strip():
        end -= 1  # keep the blank separator after the inserted lines
    return out[:end] + ["    model_info:\n", _FLAG] + out[end:]


def render_blocks(chain_blocks: list[dict]) -> list[str]:
    """YAML lines for the `vision` deployments, ready to append to model_list."""
    lines = standard.render_blocks(
        chain_blocks, route_name=ROUTE_NAME,
        what="keyed chat deployments verified to read images, tried in `order`")
    # Split the rendered text back into deployments to flag each one.
    out: list[str] = []
    block: list[str] = []
    for ln in lines:
        if ln.startswith("  - model_name: "):
            if block:
                out += _with_vision_flag(block)
            block = [ln]
        elif block:
            block.append(ln)
        else:
            out.append(ln)
    if block:
        out += _with_vision_flag(block)
    return out
