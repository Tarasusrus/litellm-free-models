#!/usr/bin/env python3
"""Prints `export VAR='value'` for every provider variable found in `.env`.

Used by fork/docker-entrypoint.sh. The rendered config resolves provider
keys through `os.environ/<VAR>`, and compose fills the container's
environment only when the container is created — a plain
`docker compose restart` would keep stale keys. Exporting the file's
current values at every start makes `.env` the single source of truth,
so a key change needs a restart, not a recreate.

Only the variables providers_config.py knows (API keys and api_base
variables) are exported; passwords and the master key keep coming from
compose.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from providers_config import PROVIDERS  # noqa: E402

PROVIDER_VARS: tuple[str, ...] = tuple(sorted(
    {p.env_var for p in PROVIDERS.values() if p.env_var}
    | {p.api_base_env for p in PROVIDERS.values() if p.api_base_env}
))


def load(path: Path) -> dict[str, str]:
    """Same parsing as upstream's render-config.load_env (no import: that
    file is a script, and this runs before anything else is loaded)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def exports(env: dict[str, str]) -> str:
    lines = [f"export {var}={shlex.quote(env[var])}" for var in PROVIDER_VARS if var in env]
    return "\n".join(lines) + ("\n" if lines else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", type=Path, default=REPO_ROOT / ".env")
    args = ap.parse_args()
    sys.stdout.write(exports(load(args.env)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
