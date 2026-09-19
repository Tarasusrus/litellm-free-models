#!/usr/bin/env python3
"""Settings page: provider keys in the browser instead of `.env` by hand.

One process, standard library only. Serves `settings_ui.html` and a small
JSON API behind the proxy's master key:

  GET  /api/providers            every provider from providers_config.py:
                                 masked key, state, console link, last check
  POST /api/check {provider,     live catalogue query with the given key, or
                    value?}      the one stored in `.env` when `value` is
                                 absent (fetch_* from find-shared-models.py),
                                 cached per key value for the life of the
                                 process
  POST /api/apply {values}       write the keys to `.env` (temp + rename,
                                 0600, other lines untouched), restart the
                                 proxy through the Docker socket, wait for
                                 readiness, report the `standard` and
                                 `tools` chains
  GET  /api/proxy                readiness, model names, current chains

Secrets never leave the process: responses carry masks, error messages
are scrubbed, request bodies are not logged.

Runs as the `settings-ui` compose service (docker-compose.yaml) with the
repo mounted at /repo and the Docker socket read-only. Standalone:

    python3 fork/settings_ui.py --env .env --port 4445
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fork import standard, tools_route  # noqa: E402
from providers_config import PROVIDERS  # noqa: E402


def _load_script(name: str):
    """Upstream's scripts are not packages; load them as modules."""
    path = REPO_ROOT / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", "").replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fsm = _load_script("find-shared-models.py")   # fetch_* catalogue queries, PROVIDER_DISPLAY
onboard = _load_script("onboard.py")          # console URLs, hints, key_state

PAGE = Path(__file__).with_name("settings_ui.html")

# The variables the page may write: provider keys and api_base variables,
# nothing else. The master key and the passwords stay out of reach.
EDITABLE_VARS: frozenset[str] = frozenset(
    {p.env_var for p in PROVIDERS.values() if p.env_var}
    | {p.api_base_env for p in PROVIDERS.values() if p.api_base_env}
)

# What an API key or URL is made of: printable ASCII without whitespace,
# quotes, backslash or '#'. Keeps every value a one-line, quote-free
# `.env` entry that both compose and upstream's load_env read back as-is.
VALUE_RE = re.compile(r"\A[!$%&()*+,\-./0-9:;<=>?@A-Z\[\]^_`a-z{|}~]*\Z")

_OWNED_LINE_RE = re.compile(r"\A\s*(" + "|".join(sorted(EDITABLE_VARS)) + r")\s*=")

MASK = "••••"

# How long an apply waits for the restarted proxy before reporting it as
# not ready (image pull excluded: the container already exists).
RESTART_WAIT_SECONDS = 120


# ─── .env ───────────────────────────────────────────────────────────────────

def load_env(path: Path) -> dict[str, str]:
    return fsm.load_env(path)


def owned_line(line: str) -> bool:
    """True for a `VAR=` line of a variable the page may rewrite."""
    return bool(_OWNED_LINE_RE.match(line))


def validate(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        if key not in EDITABLE_VARS:
            raise ValueError(f"not an editable variable: {key}")
        if not isinstance(value, str) or not VALUE_RE.match(value):
            raise ValueError(f"{key}: value may only contain printable ASCII "
                             "without spaces, quotes, backslash or '#'")


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Sets `updates` in `.env` and leaves every other line byte-identical.

    Temp file in the same directory + rename: readers see the old file or
    the new one, never a half-written one. Mode 0600; owner copied from the
    existing file so a root-run container does not lock the operator out.
    """
    validate(updates)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    for i, line in enumerate(lines):
        m = _OWNED_LINE_RE.match(line)
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
    for key, value in pending.items():
        lines.append(f"{key}={value}")

    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        if path.exists() and os.geteuid() == 0:
            st = path.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── secrets ────────────────────────────────────────────────────────────────

def mask(value: str) -> str:
    if not value:
        return ""
    return MASK + value[-4:] if len(value) > 12 else MASK


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, MASK)
    return text


# ─── providers ──────────────────────────────────────────────────────────────

_CONSOLE: dict[str, tuple[str, str]] = {var: (url, hint) for var, _n, url, hint in onboard.PROVIDER_KEYS}


def providers() -> list[dict[str, Any]]:
    """Static provider rows in `standard` priority order."""
    rows = []
    for name in standard.PROVIDER_PRIORITY:
        p = PROVIDERS[name]
        url, hint = _CONSOLE.get(p.env_var or "", ("", ""))
        rows.append({
            "name": name,
            "display": fsm.PROVIDER_DISPLAY.get(name, name),
            "env_var": p.env_var,
            "api_base_env": p.api_base_env,
            "console_url": url,
            "hint": hint,
            "required": p.required,
            "checkable": name in fsm.PROVIDERS,
        })
    return rows


def key_state(value: str, var: str) -> str:
    return onboard.key_state(value, var)


# ─── live key check ─────────────────────────────────────────────────────────

class KeyChecker:
    """Runs a provider's catalogue query with the stored key.

    Results are cached per (provider, key material) for the life of the
    process: a page reload does not hit the provider again, a changed key
    does.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _needed(name: str) -> list[str]:
        p = PROVIDERS[name]
        return [v for v in (p.env_var, p.api_base_env) if v]

    def cached(self, name: str, env: dict[str, str]) -> dict[str, Any] | None:
        return self._cache.get(self._key(name, env))

    def _key(self, name: str, env: dict[str, str]) -> tuple[str, str]:
        material = "\x1f".join(env.get(v, "") for v in self._needed(name))
        return name, hashlib.sha256(material.encode("utf-8")).hexdigest()

    def check(self, name: str, env: dict[str, str]) -> dict[str, Any]:
        if name not in PROVIDERS:
            raise KeyError(name)
        fetch = fsm.PROVIDERS.get(name)
        if fetch is None:
            return {"status": "unsupported", "models": 0, "error": "no catalogue query for this provider"}
        needed = self._needed(name)
        states = [onboard.key_state(env.get(v, ""), v) for v in needed]
        if any(s == "empty" for s in states):
            return {"status": "missing", "models": 0, "error": "no key stored"}
        if any(s == "placeholder" for s in states):
            return {"status": "placeholder", "models": 0, "error": "placeholder value, not a real key"}
        key = self._key(name, env)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        secrets = [env[v] for v in needed]
        try:
            models = [m for m in fetch({v: env[v] for v in needed}) if m]
            result = {"status": "ok", "models": len(models), "sample": sorted(models)[:5], "error": ""}
        except Exception as exc:  # any provider/network failure is a result, not a crash
            result = {"status": "fail", "models": 0,
                      "error": scrub(f"{type(exc).__name__}: {exc}", secrets)}
        result["checked_at"] = time.time()
        with self._lock:
            self._cache[key] = result
        return result


# ─── docker + proxy ─────────────────────────────────────────────────────────

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float = 30) -> None:
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)


def docker_request(sock_path: str, method: str, path: str, timeout: float = 60) -> tuple[int, bytes]:
    conn = _UnixHTTPConnection(sock_path, timeout=timeout)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def docker_restart(container: str, sock_path: str = "/var/run/docker.sock") -> None:
    """`docker compose restart <service>` without the compose binary."""
    status, body = docker_request(sock_path, "POST", f"/containers/{container}/restart?t=10")
    if status != 204:
        raise RuntimeError(f"docker restart {container}: HTTP {status} {body[:200]!r}")


def _demux(stream: bytes) -> str:
    """Docker log stream frames: 8-byte header (type, 0, 0, 0, len BE32) + payload."""
    out, i = [], 0
    while i + 8 <= len(stream):
        n = int.from_bytes(stream[i + 4:i + 8], "big")
        out.append(stream[i + 8:i + 8 + n])
        i += 8 + n
    return b"".join(out).decode("utf-8", errors="replace")


_CHAIN_LINE = re.compile(r"^\s*(\d+)\.\s+(\S+)\s+(\S+)\s*$")
_ROUTE_HEADER = re.compile(r"'([a-z][a-z0-9_-]*)' route: \d+ deployment\(s\)")

# The routes fork/render.py prints at every start, in print order.
ROUTES: tuple[str, ...] = (standard.ROUTE_NAME, tools_route.ROUTE_NAME)


def parse_chains(log_text: str) -> dict[str, list[dict[str, Any]]]:
    """Every generated route's chain as fork/render.py printed it at the
    last start: `{'standard': [...], 'tools': [...]}`. A route printed
    twice (two starts in the log) keeps its last block."""
    chains: dict[str, list[dict[str, Any]]] = {}
    active: str | None = None
    for line in log_text.splitlines():
        m = _ROUTE_HEADER.search(line)
        if m:
            active = m.group(1)
            chains[active] = []
            continue
        if active is None:
            continue
        m = _CHAIN_LINE.match(line)
        if m:
            chains[active].append({"order": int(m.group(1)), "provider": m.group(2),
                                   "model": m.group(3)})
        else:
            active = None
    return chains


def parse_chain(log_text: str) -> list[dict[str, Any]]:
    """The `standard` chain fork/render.py printed at the last start."""
    return parse_chains(log_text).get(standard.ROUTE_NAME, [])


def _started_at(container: str, sock_path: str) -> int:
    """Unix time of the container's current run (State.StartedAt)."""
    status, body = docker_request(sock_path, "GET", f"/containers/{container}/json")
    if status != 200:
        return 0
    stamp = json.loads(body).get("State", {}).get("StartedAt", "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", stamp)
    if not m:
        return 0
    import calendar
    return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))


def docker_chains(container: str, sock_path: str = "/var/run/docker.sock",
                  max_bytes: int = 2 * 1024 * 1024) -> dict[str, list[dict[str, Any]]]:
    """The chains printed at the container's current start.

    fork/render.py prints them before LiteLLM boots, so the log is read from
    the run's start and only as far as the blocks go; the boot chatter
    after them (a thousand lines and growing with traffic) is never pulled.
    """
    since = _started_at(container, sock_path)
    conn = _UnixHTTPConnection(sock_path, timeout=30)
    try:
        conn.request("GET", f"/containers/{container}/logs?stdout=1&stderr=1&since={since}")
        resp = conn.getresponse()
        if resp.status != 200:
            return {}
        buf = b""
        while len(buf) < max_bytes:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
            text = _demux(buf)
            chains = parse_chains(text)
            # Complete once the last route's block is in and a line after
            # it (LiteLLM's own output) has arrived.
            last = text.rstrip().splitlines()[-1] if text.strip() else ""
            if (all(r in chains for r in ROUTES) and not _CHAIN_LINE.match(last)
                    and not _ROUTE_HEADER.search(last)):
                return chains
        return parse_chains(_demux(buf))
    finally:
        conn.close()


def docker_chain(container: str, sock_path: str = "/var/run/docker.sock",
                 max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    """The `standard` chain printed at the container's current start."""
    return docker_chains(container, sock_path, max_bytes).get(standard.ROUTE_NAME, [])


def proxy_status(base_url: str, master_key: str, container: str, sock_path: str,
                 wait_seconds: float = 0) -> dict[str, Any]:
    """Readiness (polled up to wait_seconds), model names, current chains
    (`chain` is `standard`, kept for the page's older readers; `chains`
    carries every generated route)."""
    deadline = time.time() + wait_seconds
    ready = False
    while True:
        try:
            with urllib.request.urlopen(f"{base_url}/health/readiness", timeout=5) as resp:
                ready = resp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            ready = False
        if ready or time.time() >= deadline:
            break
        time.sleep(2)
    models: list[str] = []
    if ready:
        req = urllib.request.Request(f"{base_url}/v1/models",
                                     headers={"Authorization": f"Bearer {master_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                models = sorted(m["id"] for m in json.load(resp).get("data", []))
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError):
            models = []
    try:
        chains = docker_chains(container, sock_path)
    except OSError:
        chains = {}
    return {"ready": ready, "models": models,
            "chain": chains.get(standard.ROUTE_NAME, []), "chains": chains}


# ─── application ────────────────────────────────────────────────────────────

class App:
    """The API behind the page. `restart` and `proxy` are injected so the
    logic runs without Docker (tests) and with it (compose)."""

    def __init__(self, env_path: Path, checker: KeyChecker,
                 restart: Callable[[], None],
                 proxy: Callable[[str, float], dict[str, Any]]) -> None:
        self.env_path = env_path
        self.checker = checker
        self.restart = restart
        self.proxy = proxy
        self._write_lock = threading.Lock()

    def env(self) -> dict[str, str]:
        return load_env(self.env_path)

    def authorized(self, header: str | None) -> bool:
        master = self.env().get("LITELLM_MASTER_KEY", "")
        if not master or not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].strip(), master)

    def list_providers(self) -> dict[str, Any]:
        env = self.env()
        rows = []
        for row in providers():
            value = env.get(row["env_var"], "") if row["env_var"] else ""
            r = dict(row)
            r["masked"] = mask(value)
            r["state"] = key_state(value, row["env_var"]) if row["env_var"] else "empty"
            if row["api_base_env"]:
                r["api_base"] = env.get(row["api_base_env"], "")
            r["check"] = self.checker.cached(row["name"], env)
            rows.append(r)
        return {"providers": rows}

    def check(self, name: str, value: str | None = None) -> dict[str, Any]:
        """Checks `value` when given (the field's live content, maybe not
        yet applied); falls back to the key stored in `.env`.
        """
        env = self.env()
        if value is not None:
            env = dict(env)
            var = PROVIDERS[name].env_var
            if var:
                env[var] = value
        return self.checker.check(name, env)

    def apply(self, values: dict[str, str]) -> dict[str, Any]:
        with self._write_lock:
            write_env(self.env_path, values)   # raises ValueError before touching the file
            self.restart()
        master = self.env().get("LITELLM_MASTER_KEY", "")
        return {"applied": sorted(values), "proxy": self.proxy(master, RESTART_WAIT_SECONDS)}


# ─── HTTP ───────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "settings-ui/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # path + status only, never bodies
        sys.stderr.write(f"{self.command} {self.path.split('?')[0]}\n")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _guard(self) -> bool:
        if self.app.authorized(self.headers.get("Authorization")):
            return True
        self._json(401, {"error": "master key required"})
        return False

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?")[0]
        if path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._guard():
            return
        if path == "/api/providers":
            self._json(200, self.app.list_providers())
        elif path == "/api/proxy":
            self._json(200, self.app.proxy(self.app.env().get("LITELLM_MASTER_KEY", ""), 0))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?")[0]
        if not self._guard():
            return
        try:
            data = self._body()
            if path == "/api/check":
                name = str(data.get("provider", ""))
                if name not in PROVIDERS:
                    self._json(404, {"error": "unknown provider"})
                    return
                value = data.get("value")
                if value is not None and not isinstance(value, str):
                    raise ValueError("value must be a string")
                self._json(200, self.app.check(name, value))
            elif path == "/api/apply":
                values = data.get("values")
                if not isinstance(values, dict):
                    raise ValueError("values must be an object")
                self._json(200, self.app.apply({str(k): v for k, v in values.items()}))
            else:
                self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # never a traceback with secrets in the response
            self._json(500, {"error": scrub(f"{type(exc).__name__}: {exc}", list(self.app.env().values()))})


def make_server(host: str, port: int, app: App) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, default=Path(os.environ.get("SETTINGS_ENV_FILE", REPO_ROOT / ".env")))
    ap.add_argument("--host", default=os.environ.get("SETTINGS_BIND_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SETTINGS_PORT", "4445")))
    ap.add_argument("--proxy-url", default=os.environ.get("PROXY_URL", "http://localhost:4444"))
    ap.add_argument("--container", default=os.environ.get("PROXY_CONTAINER", "litellm-free-models"))
    ap.add_argument("--docker-socket", default=os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"))
    args = ap.parse_args()

    app = App(
        env_path=args.env,
        checker=KeyChecker(),
        restart=lambda: docker_restart(args.container, args.docker_socket),
        proxy=lambda master, wait: proxy_status(args.proxy_url, master, args.container,
                                                args.docker_socket, wait_seconds=wait),
    )
    httpd = make_server(args.host, args.port, app)
    print(f"settings-ui: http://{args.host}:{args.port}  env={args.env}  proxy={args.container}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
