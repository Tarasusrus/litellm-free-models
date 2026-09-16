# Running the proxy by hand (fork notes)

How this fork is started, restarted and updated on a single machine with
Docker Desktop (compose v2). Everything below is run from the repo root.

## What is running

| Container | Purpose | Host port |
|---|---|---|
| `litellm-free-models` | the proxy | `4444` → 4000 |
| `litellm-redis` | response cache + router state | none |
| `litellm-postgres` | keys / spend log | none |

Only `4444` is published. Redis and Postgres are reachable inside the
compose network only. Other Postgres containers on the host (e.g. `55432`)
do not conflict.

## Keys

`.env` is never committed. It is a copy of `~/.config/litellm-free-models/.env`
plus the compose-only variables:

```bash
cp ~/.config/litellm-free-models/.env .env
chmod 600 .env
cat >> .env <<EOF
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=$(openssl rand -hex 16)
LLM7IO_API_KEY=unused      # LLM7 anonymous tier (10 RPM shared)
EOF
```

Filled today: `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `GEMINI_API_KEY`.
Every other provider is empty; `render-config.py` drops their deployments.

## Start

```bash
make render-config          # config.template.yaml + .env -> config.yaml
make docker-compose-up      # renders again, then docker compose up -d
curl -sf localhost:4444/health/readiness
# {"status":"healthy","db":"connected"}
```

Expected render output with the current keys: `Kept deployments: 26`,
`Available model_names: 23`. If a port is taken, change the host side of
`4444:4000` in `docker-compose.yaml`.

## Restart / apply a config change

`config.yaml` is bind-mounted read-only; the proxy reads it once at start.

```bash
make render-config
docker compose --env-file .env restart litellm-proxy
```

Key or password change → edit `.env`, then the same two commands. Changing
`POSTGRES_PASSWORD` after the first start also needs `docker compose down -v`
(drops the DB volume) or a manual `ALTER ROLE`.

## Update the fork

```bash
git pull
make test && make lint
make render-config
docker compose --env-file .env up -d      # recreates only what changed
curl -sf localhost:4444/health/readiness
```

## Stop

```bash
docker compose down          # keeps the Postgres volume
docker compose down -v       # also wipes keys/spend data
```

## `vacancy-parse`: strict JSON for job ads

`vacancy-parse` is a fork-only route (last section of `config.template.yaml`).
It exists because the proxy runs with `drop_params: true`: a provider that
does not understand `response_format` silently gets a plain chat request and
answers with prose. So the route is built only from deployments that passed
`tools/smoke-json-schema.py` live (`JSON_SCHEMA_VERIFIED` in that file), and
its fallback chain never reaches the catch-all `*`:

```
vacancy-parse           gemini/gemini-3.5-flash-lite, gemini/gemini-3.1-flash-lite
  └─ vacancy-parse-fallback   LLM7 codestral-latest, LLM7 mistral-Nemo-Instruct-2407
       └─ []                  (explicit end of chain)
```

`tests/test_vacancy_parse.py` fails if anything unverified becomes reachable,
if the chain is empty, or if the renderer ever appends `openrouter-free` to it.

### Example request

```bash
KEY=$(grep ^LITELLM_MASTER_KEY= .env | cut -d= -f2-)
curl -s localhost:4444/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -D - -o /tmp/vp.json \
  -d '{
    "model": "vacancy-parse",
    "messages": [
      {"role": "system", "content": "Извлеки данные вакансии. Только JSON по схеме."},
      {"role": "user", "content": "Ищем Go-разработчика (middle) в финтех, Москва, гибрид. 250–320 тыс. руб. Go, PostgreSQL, Kafka. Компания «Финтех Лаб»."}
    ],
    "response_format": {"type": "json_schema", "json_schema": {"name": "vacancy", "strict": true,
      "schema": {"type": "object", "additionalProperties": false,
        "required": ["title", "company", "location", "remote", "salary_min", "salary_max", "currency", "seniority", "skills"],
        "properties": {
          "title": {"type": "string"},
          "company": {"anyOf": [{"type": "string"}, {"type": "null"}]},
          "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
          "remote": {"type": "boolean"},
          "salary_min": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
          "salary_max": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
          "currency": {"anyOf": [{"type": "string"}, {"type": "null"}]},
          "seniority": {"type": "string", "enum": ["junior", "middle", "senior", "lead", "unknown"]},
          "skills": {"type": "array", "items": {"type": "string"}}
        }}}}
  }' | grep -i "^x-litellm-model"
python3 -c 'import json; print(json.loads(json.load(open("/tmp/vp.json"))["choices"][0]["message"]["content"]))'
```

The `x-litellm-model-id` / `x-litellm-model-api-base` headers say which
deployment answered; the second command prints the parsed object.

### Re-verify the route

```bash
python3 tools/smoke-json-schema.py --model vacancy-parse --n 5          # must be 5/5, exit 0
python3 tools/smoke-json-schema.py --all-chat --n 2 --no-fallback       # survey every chat route
```

A deployment may join `JSON_SCHEMA_VERIFIED` only after a `--no-fallback`
run with 5/5; then add it to the fork section and let the tests confirm.

## Known limits (2026-09-16)

- OVHcloud anonymous deployments fail inside LiteLLM v1.97.0 (`api_key: ""`
  is rejected by the OpenAI client before the request leaves). Upstream issue;
  they are not part of the `vacancy-parse` chain.
- The proxy's own rpm budget rejects the request that would reach the limit
  (`rpm: 2` allows one call per minute). Fork deployments use `rpm: 10` / `4`.
- Gemini `3.5-flash` / `3.6-flash` answer schema-valid JSON but hit 503 "high
  demand" often enough to miss 5/5; not verified yet.
