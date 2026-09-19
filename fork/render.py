#!/usr/bin/env python3
"""
Renders config.yaml = upstream render-config.py + the fork's additions.

Drop-in for `python3 render-config.py` (same flags).

  0. Prepends fork/models.yaml (deployments upstream does not carry) to
     the template's model_list, in a temporary copy of the template.
  1. Runs upstream's renderer on it, untouched.
  2. Appends the `standard` deployments to model_list -- every chat
     deployment that survived the provider filter, copied with a unique
     `order` (fork/standard.py) -- and the `tools` deployments: the same
     chain narrowed to the tool-calling allowlist (fork/tools_route.py).
  3. Pins `{"standard": []}` and `{"tools": []}` in router_settings.fallbacks,
     so each route ends explicitly instead of drifting into the catch-all '*'.
  4. Sets `router_settings.max_fallbacks` to the longest chain: LiteLLM's
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

from fork import standard, tools_route  # noqa: E402


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


def add_tools_route(lines: list[str], env: dict[str, str]) -> tuple[list[str], list[dict]]:
    """Appends the generated `tools` deployments and returns (lines, chain).

    Built from the same parsed blocks as `standard` (the `standard` copies
    carry the same backends, so they are de-duplicated away)."""
    _, _, blocks = rc.parse_blocks(lines)
    chain = tools_route.chain(blocks, env)
    if not chain:
        return lines, chain
    end = _model_list_end(lines)
    return lines[:end] + tools_route.render_blocks(chain) + ["\n"] + lines[end:], chain


def pin_fallbacks(lines: list[str], chain_len: int,
                  routes: tuple[str, ...] = (standard.ROUTE_NAME,)) -> list[str]:
    """Explicit empty chain for each generated route + max_fallbacks deep enough.

    `chain_len` is the longest chain; `routes` the names to close (only
    routes that were actually rendered -- a fallback key that is not a
    model_name would fail LiteLLM's config validation)."""
    if chain_len == 0 or not routes:
        return lines
    ours = re.compile(r'\s*-\s*\{"(' + "|".join(re.escape(r) for r in routes) + r')":')
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
            for route in routes:
                out.append(f'    - {{"{route}": []}}\n')
            continue
        if in_router and ours.match(line):
            continue  # ours is already in place
        if in_router and s and not line.startswith(" "):
            in_router = False
        out.append(line)
    return out


def print_chain(route: str, chain: list[dict]) -> None:
    """One block per route in the start-up log; fork/settings_ui.py parses it."""
    print(f"'{route}' route: {len(chain)} deployment(s) in priority order")
    for n, b in enumerate(chain, start=1):
        print(f"  {n:>2}. {b['provider']:<13} {b['model_id']}")


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
    lines, tools_chain = add_tools_route(lines, env)
    routes = tuple(name for name, c in ((standard.ROUTE_NAME, chain),
                                        (tools_route.ROUTE_NAME, tools_chain)) if c)
    lines = pin_fallbacks(lines, max(len(chain), len(tools_chain)), routes)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, output_path)
    print_chain(standard.ROUTE_NAME, chain)
    print_chain(tools_route.ROUTE_NAME, tools_chain)
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
