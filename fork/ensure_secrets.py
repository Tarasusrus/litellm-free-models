#!/usr/bin/env python3
"""Generates the internal secrets the compose network needs for itself.

`POSTGRES_PASSWORD`, `REDIS_PASSWORD` and `LITELLM_MASTER_KEY` are not
provider keys the operator has to obtain anywhere -- they only have to
exist. Quickstart should not ask for them. Run once, before `postgres`
and `redis` start (the `env-init` compose service, `depends_on:
condition: service_completed_successfully`):

  * any of the three that is missing, empty, or still the placeholder
    `.env.example` ships (`onboard.is_placeholder`) gets a fresh value
    (`secrets.token_hex`, the format `onboard.py`'s interactive setup
    already uses) and is written back, atomically, touching no other
    line;
  * a value already in `.env` is left alone -- generating a new
    `POSTGRES_PASSWORD` on a second `docker compose up` would lock the
    proxy out of the existing `postgres-data` volume;
  * `POSTGRES_PASSWORD` and `REDIS_PASSWORD` are also copied into plain
    secret files under `--secrets-dir`, because the official `postgres`
    image and `redis-server`'s `--requirepass` read a value at their own
    container start, before `fork/docker-entrypoint.sh` gets a chance to
    export anything -- compose interpolates `${POSTGRES_PASSWORD}` once,
    when the whole file is parsed, which is before this script has run.

Standalone (`make docker-run`, no compose): run `python3
fork/ensure_secrets.py --env .env` once by hand, or set the three
variables yourself; nothing else reads `--secrets-dir` in that path.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from onboard import is_placeholder  # noqa: E402

# Re-generating an existing value would leave `postgres-data` encrypted
# with a password nothing remembers, so the format matches onboard.py's
# interactive generator exactly (that module drives a CLI wizard on top --
# more than this one-shot step needs, so only the placeholder check is
# imported from it).
GENERATED: dict[str, Callable[[], str]] = {
    "LITELLM_MASTER_KEY": lambda: "sk-" + secrets.token_hex(24),
    "REDIS_PASSWORD": lambda: secrets.token_hex(16),
    "POSTGRES_PASSWORD": lambda: secrets.token_hex(16),
}

# Which generated variables also need a plain-text file, for images/CLI
# flags that cannot read `.env` themselves.
SECRET_FILES: dict[str, str] = {
    "POSTGRES_PASSWORD": "postgres_password",
    "REDIS_PASSWORD": "redis_password",
}


def load(path: Path) -> dict[str, str]:
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


def write_updates(path: Path, updates: dict[str, str]) -> None:
    """Sets `updates` in `.env`, leaving every other line byte-identical.

    Temp file in the same directory + rename: a reader sees the old file
    or the new one, never a half-written one.
    """
    if not updates:
        return
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    for key, value in pending.items():
        lines.append(f"{key}={value}")

    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_secret_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secret.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def needs_generation(value: str) -> bool:
    return not value or is_placeholder(value)


def ensure(env: dict[str, str]) -> dict[str, str]:
    """The subset of GENERATED that `env` does not already hold a real
    value for, each mapped to a freshly generated value."""
    return {var: gen() for var, gen in GENERATED.items()
            if needs_generation(env.get(var, ""))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, required=True)
    ap.add_argument("--secrets-dir", type=Path, default=None)
    args = ap.parse_args()

    env = load(args.env)
    generated = ensure(env)
    write_updates(args.env, generated)
    env.update(generated)

    if args.secrets_dir is not None:
        for var, filename in SECRET_FILES.items():
            write_secret_file(args.secrets_dir / filename, env[var])

    for var in generated:
        print(f"ensure-secrets: generated {var} (see .env)", flush=True)
    if not generated:
        print("ensure-secrets: nothing to generate", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
