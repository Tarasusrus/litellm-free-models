#!/usr/bin/env python3
"""
Renders config.yaml = upstream render-config.py + the fork's additions.

Drop-in for `python3 render-config.py` (same flags).

  0. Prepends fork/models.yaml (deployments upstream does not carry) to
     the template's model_list, in a temporary copy of the template.
  1. Runs upstream's renderer on it, untouched.
  2. Appends the `standard` deployments to model_list -- every chat
     deployment that survived the provider filter, copied with a unique
     `order` (fork/standard.py).
  3. Pins `{"standard": []}` in router_settings.fallbacks, so the route
     ends explicitly instead of drifting into the catch-all '*'.
  4. Sets `router_settings.max_fallbacks` to the chain length: LiteLLM's
     order-based fallback walks one order level per hop and stops at
     max_fallbacks (default 5), which would leave most of the chain untried.

Usage:
    python3 fork/render.py
    python3 fork/render.py --env .env --template config.template.yaml --output config.yaml
    python3 fork/render.py --dry-run
    python3 fork/render.py --no-redis
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fork import standard  # noqa: E402


def _load_upstream_renderer():
    path = REPO_ROOT / "render-config.py"
    spec = importlib.util.spec_from_file_location("render_config", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load_upstream_renderer()

FORK_MODELS = REPO_ROOT / "fork" / "models.yaml"


def merged_template_text(template_path: Path, fragment_path: Path = FORK_MODELS) -> str:
    """Upstream template with the fork fragment first in model_list."""
    text = template_path.read_text(encoding="utf-8")
    if not fragment_path.exists():
        return text
    marker = "model_list:\n"
    if marker not in text:
        raise RuntimeError(f"model_list not found in {template_path}")
    return text.replace(marker, marker + fragment_path.read_text(encoding="utf-8"), 1)


def _model_list_end(lines: list[str]) -> int:
    """Index of the first top-level line after model_list (where the
    upstream template's router_settings banner begins)."""
    start = next(i for i, ln in enumerate(lines)
                 if ln.rstrip() == "model_list:" and not ln.startswith(" "))
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith(" "):
            return i
    return len(lines)


def add_standard_route(lines: list[str], env: dict[str, str]) -> tuple[list[str], list[dict]]:
    """Appends the generated `standard` deployments and returns (lines, chain)."""
    _, _, blocks = rc.parse_blocks(lines)
    chain = standard.chain(blocks, env)
    if not chain:
        return lines, chain
    end = _model_list_end(lines)
    return lines[:end] + standard.render_blocks(chain) + ["\n"] + lines[end:], chain


def pin_fallbacks(lines: list[str], chain_len: int) -> list[str]:
    """Explicit empty chain for `standard` + max_fallbacks deep enough."""
    if chain_len == 0:
        return lines
    out: list[str] = []
    in_router = False
    for line in lines:
        s = line.strip()
        if line.rstrip() == "router_settings:":
            in_router = True
            out.append(line)
            out.append(f"  max_fallbacks: {chain_len}\n")
            continue
        if in_router and re.match(r"\s*max_fallbacks:", line):
            continue  # ours is already in place
        if in_router and s == "fallbacks:":
            out.append(line)
            out.append(f'    - {{"{standard.ROUTE_NAME}": []}}\n')
            continue
        if in_router and re.match(rf'\s*-\s*\{{"{re.escape(standard.ROUTE_NAME)}":', line):
            continue  # ours is already in place
        if in_router and s and not line.startswith(" "):
            in_router = False
        out.append(line)
    return out


def render(template_path: Path, env_path: Path, output_path: Path,
           dry_run: bool = False, no_redis: bool = False) -> int:
    if not template_path.exists():
        print(f"ERROR: template not found: {template_path}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as d:
        merged = Path(d) / template_path.name
        merged.write_text(merged_template_text(template_path), encoding="utf-8")
        rcode = rc.render(merged, env_path, output_path, dry_run=dry_run, no_redis=no_redis)
    if rcode != 0 or dry_run:
        return rcode
    env = rc.load_env(env_path)
    lines = output_path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines, chain = add_standard_route(lines, env)
    lines = pin_fallbacks(lines, len(chain))
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, output_path)
    print(f"'{standard.ROUTE_NAME}' route: {len(chain)} deployment(s) in priority order")
    for n, b in enumerate(chain, start=1):
        print(f"  {n:>2}. {b['provider']:<13} {b['model_id']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, default=rc.DEFAULT_ENV)
    ap.add_argument("--template", type=Path, default=rc.DEFAULT_TEMPLATE)
    ap.add_argument("--output", type=Path, default=rc.DEFAULT_OUTPUT)
    ap.add_argument("--dry-run", action="store_true", help="preview only, don't write")
    ap.add_argument("--no-redis", action="store_true",
                    help="render without the Redis cache/router blocks")
    args = ap.parse_args()
    return render(args.template, args.env, args.output,
                  dry_run=args.dry_run, no_redis=args.no_redis)


if __name__ == "__main__":
    sys.exit(main())
