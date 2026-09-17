# Running the proxy (fork notes)

Start, restart, update and stop on a single machine with Docker Compose v2.
Everything below is run from the repo root.

## What is running

| Container | Purpose | Host port |
|---|---|---|
| `litellm-free-models` | the proxy | `4444` → 4000 |
| `litellm-redis` | response cache + router state | none |
| `litellm-postgres` | keys / spend log | none |
| `litellm-settings-ui` | settings page (provider keys) | `127.0.0.1:4445` → 4445 |

Only the proxy port and the settings page are published; the page is bound
to loopback. Change them with `LITELLM_PORT=…` / `SETTINGS_UI_PORT=…` in
`.env`; container names with `LITELLM_CONTAINER_NAME`, `REDIS_CONTAINER_NAME`,
`POSTGRES_CONTAINER_NAME`, `SETTINGS_UI_CONTAINER_NAME` (useful for a second
stack on the same host, together with `COMPOSE_PROJECT_NAME`).

## Keys

`.env` is never committed. Start from the example:

```bash
cp .env.example .env
chmod 600 .env
```

Required: `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`
(`openssl rand -hex 16` for the passwords). Every provider key is optional;
a provider whose key is empty is left out of the config and of the
`standard` chain. `LLM7IO_API_KEY=unused` keeps LLM7's anonymous tier in
(any non-empty value works).

## Start

```bash
docker compose up -d
curl -sf localhost:4444/health/readiness
# {"status":"healthy","db":"connected"}
```

The config is rendered **inside** the proxy container on every start
(`fork/docker-entrypoint.sh` → `fork/render.py`): template + `.env` + the
generated `standard` route. Nothing is written to the host. The startup log
shows what was rendered:

```bash
docker compose logs litellm-proxy | grep -E "Kept deployments|Available model_names|'standard' route" -A0
docker compose logs litellm-proxy | grep -A200 "'standard' route" | grep -E "^\s+[0-9]+\."
```

`make docker-compose-up` is a thin wrapper around the same command.

## Restart / apply a config or key change

```bash
docker compose restart litellm-proxy      # re-reads keys from .env, re-renders, ~20 s
```

Or open the settings page (`http://localhost:4445`, master key), change the
keys and press **Apply** — same write + restart, and the page shows the
resulting `standard` chain. Provider keys reach LiteLLM from the file at
every start (`fork/docker-entrypoint.sh` exports them), so a restart is
enough; a recreate (`up -d`) is only needed for compose-level changes such
as ports or passwords.

Changing `POSTGRES_PASSWORD` after the first start also needs
`docker compose down -v` (drops the DB volume) or a manual `ALTER ROLE`.

## Render on the host (optional)

Only needed for Kubernetes (`make k8s-configmap`), the standalone
`make docker-run`, or to inspect the config:

```bash
make render-config                                     # -> config.yaml (git-ignored, contains keys)
python3 fork/render.py --output /tmp/config.yaml       # anywhere else
```

## Update the fork

```bash
git pull
make test && make lint
docker compose up -d      # recreates only what changed; the proxy re-renders on start
curl -sf localhost:4444/health/readiness
```

Syncing with upstream: `docs/adr/0001-fork-conventions.md` §4.

## Stop

```bash
docker compose down          # keeps the Postgres volume
docker compose down -v       # also wipes keys/spend data
```

## Smoke test

```bash
python3 tools/smoke-json-schema.py --model standard --n 3      # strict json_schema through the chain
python3 tools/smoke-json-schema.py --all-chat --n 2 --no-fallback   # survey every chat route
```

The first prints which deployment answered each attempt (headers
`x-litellm-model-id` / `x-litellm-model-api-base`) and whether the answer
matched the schema. `standard` keeps providers that cannot do strict JSON,
so a failed attempt there is information, not a defect — see
`docs/USAGE.md` → Structured output.

## Known limits (2026-09-16)

- OVHcloud anonymous deployments fail inside LiteLLM v1.97.0 (`api_key: ""`
  is rejected by the OpenAI client before the request leaves). Upstream
  issue; they join the `standard` chain only when `OVHCLOUD_API_KEY` is set.
- The proxy's own rpm budget rejects the request that would reach the limit
  (`rpm: 2` allows one call per minute per deployment). `standard` walks on
  to the next deployment, so the client only sees this when the whole chain
  is spent.
- Gemini `3.5-flash` / `3.6-flash` answer schema-valid JSON but hit 503
  "high demand" often; the verified table lists the `-lite` variants.
