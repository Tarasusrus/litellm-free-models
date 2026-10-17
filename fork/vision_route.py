"""The `vision` route: `standard` narrowed to deployments that read images.

A client that sends an `image_url` (or a PDF `file`) part sends `model:
"vision"`. A text-only backend behind `drop_params: true` does not fail on
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


def _with_vision_flag(lines: list[str]) -> list[str]:
    """One deployment's lines with `model_info.supports_vision: true`.

    An existing `supports_vision` line is replaced, not duplicated; a block
    without `model_info` gets one."""
    out = [ln for ln in lines if not ln.strip().startswith("supports_vision:")]
    for i, ln in enumerate(out):
        if ln.rstrip() == "    model_info:":
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
