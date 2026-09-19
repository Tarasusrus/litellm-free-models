# Using the proxy from a client

Everything a client needs to call the proxy. Written for a developer or an
agent that has to produce working code right away.

## Endpoint and authentication

| | |
|---|---|
| Base URL | `http://localhost:4444/v1` (host and port of the compose stack; `LITELLM_PORT` in `.env` changes the port) |
| Auth | `Authorization: Bearer <key>` — `LITELLM_MASTER_KEY` from `.env`, or a virtual key created via `POST /key/generate` with the master key |
| Protocol | OpenAI Chat Completions API. Any OpenAI SDK works with `base_url` set to the proxy. |
| Health | `GET /health/readiness` → `200 {"status":"healthy", ...}`; no auth |
| Models | `GET /v1/models` (auth) lists every model name the proxy serves |

## The `standard` model

`model: "standard"` is the route for "any free model that answers". Behind
it are all chat deployments of every provider that has a key in `.env`,
tried in a fixed priority order (Gemini → Groq → OpenRouter → Mistral →
NVIDIA → Cerebras → HuggingFace → Cohere → Cloudflare → OpenCode Zen →
Poolside → Hetzner → Z.AI → LLM7 → OVHcloud; see
[adr/0001-fork-conventions.md](adr/0001-fork-conventions.md)). If the first
deployment fails — rate limit, timeout, provider error — the proxy moves to
the next one and so on. The client sees one request and one answer.

Every upstream alias (`gpt-oss-120b`, `llama-3.3-70b-instruct`,
`embedding-general`, `whisper-large-v3`, …) still works by name when you
need a specific model; the list is in
[upstream-README.md → Models](upstream-README.md#-models).

## Request and response

Request — standard OpenAI chat completion:

```bash
curl -s http://localhost:4444/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -D /tmp/headers.txt \
  -d '{
    "model": "standard",
    "messages": [
      {"role": "system", "content": "Answer briefly."},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "temperature": 0
  }'
grep -i '^x-litellm-model' /tmp/headers.txt
```

Response — standard OpenAI shape. `model` echoes the name you asked for;
the backend that actually answered is in the response headers (below):

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "model": "standard",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 17, "completion_tokens": 2, "total_tokens": 19}
}
```

Python with the OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:4444/v1", api_key="<LITELLM_MASTER_KEY>")

raw = client.chat.completions.with_raw_response.create(
    model="standard",
    messages=[{"role": "user", "content": "What is the capital of France?"}],
    temperature=0,
)
reply = raw.parse()
print(reply.choices[0].message.content)
print("served by:", raw.headers.get("x-litellm-model-name"), raw.headers.get("x-litellm-model-api-base"))
```

Streaming (`"stream": true`) is supported as usual.

## Who answered

Response headers tell which deployment served the request:

| Header | Meaning |
|---|---|
| `x-litellm-model-name` | LiteLLM model id of the backend, e.g. `gemini/gemini-3.5-flash-lite`, `openai/codestral-latest` |
| `x-litellm-model-api-base` | provider endpoint that answered (tells the host apart for `openai/*` ids) |
| `x-litellm-model-id` | stable id of the deployment; `GET /model/info` maps ids to `litellm_params` |
| `x-litellm-model-group` | the model name you asked for (`standard`, `tools`, …) |
| `x-litellm-attempted-fallbacks` | how many deployments failed before this one (absent when the first one answered) |

The response body's `model` field is the requested name (`standard`, `tools`), not the backend.

## Structured output (`response_format`)

`response_format` is passed through to the provider, both
`{"type": "json_object"}` and the strict form:

```json
"response_format": {
  "type": "json_schema",
  "json_schema": {"name": "answer", "strict": true, "schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": false}}
}
```

**Limitation.** The proxy runs with `drop_params: true`: a provider that
does not support `response_format` receives the request *without* it and
answers with prose. `standard` deliberately keeps every provider in the
chain, so a structured request may land on such a provider. Therefore:

1. Put the schema into the system prompt as well ("Answer only with JSON
   matching …").
2. Parse and validate the answer on the client side.
3. On an invalid answer, retry. Identical requests are cached for 5 minutes;
   send `"cache": {"no-cache": true}` in the body (or change the prompt) so
   the retry is not served from cache.

Which deployments have been seen to honour a strict schema is recorded in
`JSON_SCHEMA_VERIFIED` in [`tools/smoke-json-schema.py`](../tools/smoke-json-schema.py)
(a report from live runs, keyed by model + host). Re-check any time:

```bash
python3 tools/smoke-json-schema.py --model standard --n 3
```

If your client needs guaranteed schema compliance on every call, ask for a
verified model by name (e.g. `gemini-3.5-flash-lite`) instead of `standard`.

## Tool calling / MCP agents (`model: "tools"`)

`standard` does not guarantee tool calling. The proxy runs with
`drop_params: true`: a backend that does not understand `tools` receives the
request *without* them and answers with prose — no error, the agent loop
just stops. `model: "tools"` is the same chain as `standard`, in the same
provider order, narrowed to the deployments that completed a live two-step
tool call on every attempt (`TOOL_CALLING_VERIFIED` in
[`fork/tools_route.py`](../fork/tools_route.py), keyed by model + host, with
the date of the run). A new provider key puts its models into `standard` on
the next start but into `tools` only after the smoke has seen them pass.

Send `tools`/`tool_calls` in the usual OpenAI format. One tool, two steps:

```python
import json
from openai import OpenAI

client = OpenAI(base_url="http://localhost:4444/v1", api_key="<LITELLM_MASTER_KEY>")

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]

def get_weather(city: str) -> dict:
    return {"city": city, "temperature_c": 21, "sky": "clear"}  # your implementation

messages = [{"role": "user", "content": "What is the weather in Paris right now?"}]

# Step 1: the model asks for the tool.
first = client.chat.completions.create(model="tools", messages=messages, tools=tools)
call = first.choices[0].message.tool_calls[0]
args = json.loads(call.function.arguments)

# Step 2: answer the call by id, the model writes the final text.
messages += [first.choices[0].message,
             {"role": "tool", "tool_call_id": call.id, "content": json.dumps(get_weather(**args))}]
final = client.chat.completions.create(model="tools", messages=messages, tools=tools)
print(final.choices[0].message.content)
```

`x-litellm-model-name` in the response headers says which backend served
each step ([Who answered](#who-answered)). Keep the tool definitions in the
second request too: the two steps are independent requests, and a backend
needs the definitions to read the `role: tool` message.

**With an MCP server.** An MCP client lists the server's tools and hands
them to the model in the same format; the loop above is the whole
integration. With the official `mcp` package:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async with stdio_client(StdioServerParameters(command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "."])) as (r, w):
    async with ClientSession(r, w) as mcp:
        await mcp.initialize()
        tools = [{"type": "function",
                  "function": {"name": t.name, "description": t.description or "",
                               "parameters": t.inputSchema}}
                 for t in (await mcp.list_tools()).tools]
        reply = client.chat.completions.create(model="tools", messages=messages, tools=tools)
        for call in reply.choices[0].message.tool_calls or []:
            result = await mcp.call_tool(call.function.name, json.loads(call.function.arguments))
            messages += [reply.choices[0].message,
                         {"role": "tool", "tool_call_id": call.id,
                          "content": "".join(c.text for c in result.content if hasattr(c, "text"))}]
        # ... then call the model again with the tool results, as in step 2
```

Re-check the route, or a single backend, any time:

```bash
python3 tools/smoke-tool-calling.py --model tools --n 5
python3 tools/smoke-tool-calling.py --all-deployments --n 5   # candidates for the allowlist
```

`tools` is empty (and absent from `/v1/models`) until at least one verified
deployment has a key in `.env`; today that means a Gemini or Groq key.

## Limits

- Each deployment carries its own `rpm`/`tpm` budget (the provider's free
  tier, conservative). When a deployment's budget is spent the proxy simply
  skips to the next one in the chain — the client is not throttled until
  *every* keyed deployment is exhausted.
- A single request can take a while when several providers fail in a row:
  every attempt has a per-deployment timeout (20–30 s) and one retry. Set a
  generous client timeout (≥ 120 s) or use streaming.
- Identical requests (same model, messages, parameters) are served from
  cache for 5 minutes; opt out per request with `"cache": {"no-cache": true}`
  or the header `Cache-Control: no-cache`.

## Errors

The proxy answers with the OpenAI error envelope:

```json
{"error": {"message": "…", "type": "…", "param": null, "code": "429"}}
```

| HTTP | When |
|---|---|
| `401` | missing or wrong API key |
| `400` | malformed request, unknown model name |
| `429` | every deployment in the chain was exhausted or rate-limited (the last failure was a rate limit) |
| `500`/`502`/`503`/`504` | the chain ended on a provider error or timeout |

When the whole `standard` chain fails, the status is that of the *last*
attempt and `message` carries that provider's error plus
`Received Model Group=standard`. Retry after a minute:
cooled-down deployments return to the chain automatically
(`cooldown_time: 60`).
