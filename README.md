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
- **One-command start** — `docker compose up -d` renders the config inside
  the proxy container. No `make render-config` on the host.
- **Client guide** — [docs/USAGE.md](docs/USAGE.md): everything a client
  (or an agent writing one) needs to send requests, including structured
  output and its limits.
- **Upstream stays upstream** — fork code lives in [`fork/`](fork/) and
  post-processes upstream's renderer; the template and renderer are byte-
  identical to upstream. How and why: [docs/adr/0001-fork-conventions.md](docs/adr/0001-fork-conventions.md).

## Quickstart

```bash
git clone https://github.com/Tarasusrus/litellm-free-models.git && cd litellm-free-models
cp .env.example .env      # set LITELLM_MASTER_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD + provider keys
docker compose up -d
```

Check: `curl -sf localhost:4444/health/readiness` → `{"status":"healthy",…}`.

Only the proxy port (`4444`, change with `LITELLM_PORT` in `.env`) is
published; Postgres and Redis stay inside the compose network. Providers
whose key is empty are simply left out. See [docs/run.md](docs/run.md) for
restart, update and stop.

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
structured output, limits and error codes: [docs/USAGE.md](docs/USAGE.md).

## Add a provider key

1. Get a free key from the provider (links and limits: [upstream README →
   Providers](docs/upstream-README.md#-providers)).
2. Put it into `.env` (variable names are in `.env.example`).
3. `docker compose restart litellm-proxy` — the config is re-rendered on
   start and the provider's chat models join the `standard` chain at their
   priority slot.

The current chain is printed in the proxy's startup log
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
