# litellm-free-models (fork)

**English** · [Русский](README.ru.md)

One OpenAI-compatible endpoint in front of free LLM providers. Send
`model: "standard"` and the proxy tries the providers one after another
until one answers.

## About this fork

This is a fork of [natorus87/litellm-free-models](https://github.com/natorus87/litellm-free-models)
(v0.3.0, MIT). Upstream does the hard part: a curated catalogue of free
providers and models, rendered into a [LiteLLM](https://github.com/BerriAI/litellm)
proxy config with load balancing, cooldowns and fallback chains. Its README
is kept verbatim in [docs/upstream-README.md](docs/upstream-README.md) and
remains the reference for providers, models, Kubernetes and multi-instance
setups.

What the fork adds:

- **`standard` route** — every chat deployment you have a key for, behind
  one model name, tried in a fixed provider order (Gemini → Groq →
  OpenRouter → Mistral → NVIDIA → … → anonymous tiers last). A new key in
  `.env` joins the chain on the next start; nothing to edit by hand.
- **`tools` route** — the same chain narrowed to the deployments that
  completed a live two-step tool call on every attempt. For agents with
  MCP servers: `standard` does not guarantee tool calling.
  See [Tool calling / MCP agents](#tool-calling--mcp-agents).
- **One-command start** — `docker compose up -d` renders the config inside
  the proxy container. No `make render-config` on the host.
- **Settings UI** — a local page for provider keys: status, live check,
  Apply writes `.env` and restarts the proxy. See [Settings UI](#settings-ui).
- **Client guide** — [docs/USAGE.md](docs/USAGE.md): everything a client
  (or an agent writing one) needs to send requests, including structured
  output and its limits.
- **Upstream stays upstream** — fork code lives in [`fork/`](fork/) and
  post-processes upstream's renderer; the template and renderer are byte-
  identical to upstream. How and why: [docs/adr/0001-fork-conventions.md](docs/adr/0001-fork-conventions.md).

## Quickstart

```bash
git clone https://github.com/Tarasusrus/litellm-free-models.git && cd litellm-free-models
cp .env.example .env      # set LITELLM_MASTER_KEY + provider keys
docker compose up -d
```

Check: `curl -sf localhost:4444/health/readiness` → `{"status":"healthy",…}`.

Only the proxy port (`4444`, change with `LITELLM_PORT` in `.env`) and the
settings page (`127.0.0.1:4445`) are published; Postgres and Redis stay
inside the compose network. Providers
whose key is empty are simply left out. See [docs/run.md](docs/run.md) for
restart, update and stop.

`POSTGRES_PASSWORD` and `REDIS_PASSWORD` are internal, compose-network-only
credentials — nothing to set by hand; the first `docker compose up`
generates them into `.env` and later runs leave them alone. Left
`LITELLM_MASTER_KEY` empty too? Same thing — generated once, printed to
`docker compose logs env-init`.

## Connect a client

- **Base URL:** `http://localhost:4444/v1`
- **API key:** the `LITELLM_MASTER_KEY` from `.env` (or a virtual key created through the proxy)
- **Model:** `standard`

```bash
curl -s localhost:4444/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model": "standard", "messages": [{"role": "user", "content": "Say hi in one word."}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:4444/v1", api_key="<LITELLM_MASTER_KEY>")
reply = client.chat.completions.create(
    model="standard",
    messages=[{"role": "user", "content": "Say hi in one word."}],
)
print(reply.choices[0].message.content)
```

Every upstream alias (`gpt-oss-120b`, `llama-3.3-70b-instruct`, embeddings,
audio, …) is still available by name — see the model matrix in the
[upstream README](docs/upstream-README.md#-models). Request/response format,
structured output, tool calling, limits and error codes: [docs/USAGE.md](docs/USAGE.md).

## Tool calling / MCP agents

An agent that calls tools (its own functions or an MCP server's) sends
`model: "tools"` with `tools` in the usual OpenAI format. Behind the name
are the `standard` providers in the same order, but only the models that
returned a correct `tool_calls` in a live check; the verified list with
dates is `TOOL_CALLING_VERIFIED` in [`fork/tools_route.py`](fork/tools_route.py).
`standard` keeps every provider, including ones that silently drop the
tool definitions and answer with prose — do not use it for agents.

```python
first = client.chat.completions.create(model="tools", messages=messages, tools=tools)
call = first.choices[0].message.tool_calls[0]           # step 1: the model asks for a tool
messages += [first.choices[0].message,
             {"role": "tool", "tool_call_id": call.id, "content": run(call)}]
final = client.chat.completions.create(model="tools", messages=messages, tools=tools)  # step 2
```

Full two-step example, an MCP client that feeds a server's tool list into
`tools`, and how to re-check a backend:
[docs/USAGE.md → Tool calling / MCP agents](docs/USAGE.md#tool-calling--mcp-agents-model-tools).

## Settings UI

`docker compose up -d` also starts `settings-ui`, a page on
`http://localhost:4445` (change with `SETTINGS_UI_PORT` in `.env`; it
listens on loopback only). Log in with `LITELLM_MASTER_KEY`.

What it shows — one row per provider from `providers_config.py`, in
`standard` priority order:

- the stored key, masked, and its state (empty / set / example value from
  `.env.example` / default tier);
- **Check** — a live catalogue query with the stored key (same requests as
  `find-shared-models.py`), reporting the model count or the error. Results
  are cached until the key changes or the service restarts;
- **where to get** — the provider's console.

**Apply** writes the changed keys to `.env` (atomic replace, mode 0600,
every other line untouched), restarts the proxy through the Docker socket,
waits for readiness and prints the `standard` and `tools` chains the
proxy rendered.
Keys are only ever masked in API responses and never logged. The page
edits provider variables only; the master key and the passwords stay
hands-on in `.env`.

The service mounts the repo read-write (for the `.env` replace) and the
Docker socket read-only; that socket still allows restarting any container
on the host, which is why the page is bound to loopback and gated by the
master key.

## Add a provider key by hand

1. Get a free key from the provider (links and limits: [upstream README →
   Providers](docs/upstream-README.md#-providers)).
2. Put it into `.env` (variable names are in `.env.example`).
3. `docker compose restart litellm-proxy` — the proxy reads the keys from
   `.env` at every start, re-renders the config and the provider's chat
   models join the `standard` chain at their priority slot (and `tools`
   once they are in its allowlist).

The current chains are printed in the proxy's startup log
(`docker compose logs litellm-proxy | grep -A200 "'standard' route"`), or
locally with `python3 fork/render.py --output /tmp/config.yaml`.

## Development

```bash
pip install -r requirements-dev.txt
make test     # unit + property tests (hypothesis)
make lint     # ruff
```

Fork conventions — where fork code goes, which upstream files are touched
and how to sync: [docs/adr/0001-fork-conventions.md](docs/adr/0001-fork-conventions.md).

## License

MIT, same as upstream — see [LICENSE](LICENSE).
