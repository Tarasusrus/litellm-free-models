# ADR 0001 — Fork conventions

Status: accepted · Date: 2026-09-16

## Context

This repository is a fork of [natorus87/litellm-free-models](https://github.com/natorus87/litellm-free-models).
Upstream ships a large hand-curated `config.template.yaml`, a line-based
renderer (`render-config.py`) and a weekly catalogue sync that edits the
template. We want to keep pulling those changes with `git merge upstream/main`
and never resolve conflicts inside upstream's files.

The fork adds one thing of substance: a `standard` route that puts every
keyed chat deployment behind one model name, tried in provider order. An
earlier attempt wrote a fork section into the template and a pinned-chain
rule into the renderer; every upstream sync would have had to merge around
it.

## Decision

### 1. Fork code lives in `fork/`, upstream files stay upstream

| Path | Role |
|---|---|
| `fork/render.py` | Drop-in for `render-config.py` (same flags). Prepends `fork/models.yaml` to a temporary copy of the template, runs upstream's renderer **unchanged** on it, then post-processes the output: appends the `standard` deployments, pins `{"standard": []}` in `fallbacks`, sets `router_settings.max_fallbacks`. |
| `fork/models.yaml` | Deployments upstream's catalogue does not carry (today: Gemini flash-lite tier), in upstream's block format so the same filter and validation apply. Placed first in `model_list`, so they lead their provider in the `standard` chain. Tests reject entries that duplicate an upstream backend or reuse a non-chat alias. |
| `fork/standard.py` | Provider priority (`PROVIDER_PRIORITY`), the chain builder (`chain`) and the YAML emitter (`render_blocks`). |
| `fork/docker-entrypoint.sh` | Proxy entrypoint for compose: export the provider keys from `.env` (`fork/env_exports.py`), render inside the container, then start LiteLLM. Keys are read from the file at every start, so a changed key needs `docker compose restart litellm-proxy`, not a recreate. |
| `fork/env_exports.py` | Prints `export VAR=…` for the provider variables in `.env`. Only variables `providers_config.py` knows; passwords and the master key keep coming from compose. |
| `fork/settings_ui.py`, `fork/settings_ui.html` | The `settings-ui` compose service: provider keys in the browser. Lists `providers_config.PROVIDERS` in chain order, checks a key with `find-shared-models.py`'s `fetch_*`, writes `.env` atomically (temp + rename, 0600, only provider variables), restarts the proxy through the Docker Engine API and reads the chain from the container log. Console links and hints come from `onboard.PROVIDER_KEYS`; nothing is typed twice. |
| `tests/test_standard_route.py` | Property tests for the chain and the rendered config. |
| `tests/test_settings_ui.py`, `tests/test_env_exports.py`, `tests/test_fork_renderer_entrypoints.py` | Property tests for the settings page (`.env` atomicity and ownership, masking, provider list, mocked key check, HTTP API), the key export and the render entry points. |
| `docs/USAGE.md`, `docs/run.md`, `docs/adr/` | Fork documentation. |
| `docs/upstream-README.md`, `docs/upstream/` | Upstream's README, review log and research, moved out of the root verbatim. |

`config.template.yaml`, `render-config.py`, `providers_config.py`,
`find-shared-models.py` (except one path, below) and everything else are
byte-identical to upstream and must stay so. Fork behaviour is added by
composing upstream's functions, never by editing them.

### 2. Points of contact with upstream files

These are the only upstream files the fork edits. Each edit is one hunk,
marked `# Fork:`; on a sync, keep ours.

| File | Edit | Why |
|---|---|---|
| `docker-compose.yaml` | proxy service: official image, repo mounted read-only at `/repo`, `entrypoint: fork/docker-entrypoint.sh`; container names and host port overridable from `.env`; the `settings-ui` service (loopback only, repo read-write for the `.env` replace, Docker socket read-only) | one-command start; compose has no other hook for "render before start" that survives a missing `config.yaml` (a bind mount of a missing file creates a directory) |
| `Makefile` | `render-config*` and `check-config` targets call `fork/render.py`; `docker-compose-up` no longer pre-renders | keep upstream's targets working with the fork's route |
| `onboard.py` | the render step calls `fork/render.py` | onboarding must produce the same config as compose and the Makefile |
| `find-shared-models.py` | `--write-docs` targets `docs/upstream-README.md` instead of `README.md` | the root README is the fork's; the generated matrix belongs to the upstream text |
| `.github/workflows/ci.yml` | installs `requirements-dev.txt` (hypothesis), renders through `fork/render.py`, drift check on `docs/upstream-README.md` | CI must exercise the fork's renderer |
| `pyproject.toml` | per-file ruff ignore for `find-shared-models.py` (`UP038`) | upstream code trips a rule newer ruff enables; ignoring it is a one-line, conflict-free fix |
| `AGENTS.md` | regenerated model matrix only | generated content |

### 3. The `standard` route

- Generated at render time, never written into the template. Source: the
  chat deployments that survive upstream's provider filter (a key present
  in `.env`). Anonymous tiers (`required=False`, today OVHcloud) join only
  with a key — an empty `api_key` is rejected by the OpenAI client inside
  LiteLLM before the request leaves.
- One backend = (`model`, `api_base`). When several aliases point at the
  same backend, the first occurrence is used; the chain has no duplicates.
  Within a provider the order is `fork/models.yaml` first, then upstream's
  template order.
- Each deployment is copied verbatim under `model_name: standard` with
  `litellm_params.order = 1..N`. LiteLLM routes to the lowest `order`
  first and, on failure, walks up order by order (order-based fallbacks).
  `max_fallbacks` is set to N because LiteLLM stops after 5 hops by
  default. `{"standard": []}` closes the chain: no catch-all `*`, no
  `openrouter-free` appended.
- Provider priority (first = tried first):

  | # | Provider | Reason |
  |---|---|---|
  | 1 | Google AI Studio (Gemini) | generous free tier, strong models, honours `json_schema` |
  | 2 | Groq | fastest answers, small daily budget |
  | 3 | OpenRouter (free models) | widest catalogue behind one key |
  | 4 | Mistral | |
  | 5 | NVIDIA NIM | |
  | 6–13 | Cerebras, HuggingFace, Cohere, Cloudflare, OpenCode Zen, Poolside, Hetzner, Z.AI | keyed providers, by free-tier request budget (`rpm` in `providers_config.py`), ties alphabetical |
  | 14 | ElevenLabs | no chat models; listed so the set stays complete |
  | 15 | LLM7.io | anonymous tier, 10 rpm shared by everyone |
  | 16 | OVHcloud | anonymous tier; only with a key |

  The first five are an operator decision; the rest follow the budget rule.
  `tests/test_standard_route.py` fails when a provider exists in
  `providers_config.PROVIDERS` without a slot here, so an upstream sync that
  adds a provider has to assign one deliberately.
- Structured output is passed through; providers that cannot do it stay
  in the chain. The client validates and retries (`docs/USAGE.md`). The
  live report of compliant deployments (`JSON_SCHEMA_VERIFIED` in
  `tools/smoke-json-schema.py`) is keyed by model **and** host and is
  informational, not a filter.

### 4. Syncing with upstream

```bash
git fetch upstream
git merge upstream/main          # conflicts, if any, only in the files of §2
python3 find-shared-models.py --write-docs
make test && make lint
```

After a merge, `git diff upstream/main -- config.template.yaml render-config.py providers_config.py`
must be empty. `test_every_provider_has_a_documented_priority` tells when a
new provider needs a slot in `PROVIDER_PRIORITY` and in the table above.

## Consequences

- Upstream syncs are mechanical; the template is never edited by hand here.
- `python3 render-config.py` still works but yields upstream's config
  without `standard`; the Makefile, compose and CI use `fork/render.py`.
- `standard` doubles the local rpm/tpm accounting for a backend (the alias
  and the `standard` copy count separately). Budgets in the template are
  conservative, so this is accepted.
- A request that walks the whole chain can take long (per-deployment
  timeout × N). Clients set a generous timeout; documented in USAGE.md.
