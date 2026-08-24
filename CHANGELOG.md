# Changelog

All notable changes to this project are documented here. The project follows
Semantic Versioning.

## [Unreleased]

## [0.3.0] - 2026-08-24

### Added

- Documented that HuggingFace Inference is credit-metered: once the monthly
  included budget is spent, every HF route answers HTTP 402 until the period
  resets, and backend pinning does not avoid it.
- Added a `DEAD_ROUTES` denylist to catalog discovery so live-verified
  unusable free routes are never re-proposed by the weekly sync, with a
  regression test.
- Added six free deployments from the 2026-08-24 catalog sync: the new
  `inkling-small` alias on HuggingFace, `kimi-k3` on NVIDIA, `laguna-xs-2.1`
  on Poolside, and `qwen3.6-35b-a3b` on HuggingFace (which restores provider
  redundancy for the former Hetzner exclusive).
- Added the current free LLM7 `gemini-3.1-flash-lite` and `minimax-m2.7`
  routes after live authenticated tests.
- Hetzner Experiments Inference as the sixteenth free provider, with
  `qwen3.6-35b-a3b` and multimodal `qwen3.8-27b`, OpenAI-compatible catalog
  discovery, onboarding/env/Kubernetes/multi-instance wiring, conservative
  unpublished-limit budgets, fallbacks, and estimated savings documentation.

### Fixed

- Stopped single-deployment model groups from stalling a full minute on a
  rate limit: `render-config.py` now generates a `model_group_retry_policy`
  with `RateLimitErrorRetries: 0` for every model_name left with one
  deployment, so the fallback chain is tried immediately (measured 61s ->
  1.3s on `lfm-2.5-2.6b`). Per-deployment `num_retries: 0` could not do this,
  because LiteLLM only stamps that override onto exceptions raised in the
  `(a)completion` path.
- Removed `allowed_fails: 1` from `router_settings`: it forced the router onto
  the legacy cooldown counter (cooldown only on the second failure per minute)
  and disabled the modern "HTTP 429 -> cool down immediately" rule. An
  invariant test now keeps the setting unset.
- Fixed the miscased HuggingFace repo IDs for `gemma-4-31b-it` and
  `gemma-4-26b-a4b-it` (the router serves `gemma-4-31B-it` /
  `gemma-4-26B-A4B-it` and answered HTTP 400 for the lowercase spelling),
  and taught the stale-deployment check to report case-only drift for
  case-sensitive catalogs instead of silently accepting it.
- Removed twelve dead deployments confirmed by live tests on 2026-08-24:
  NVIDIA end-of-life routes for `kimi-k2.6`, `deepseek-v4-flash`, `glm-5.2`,
  and `llama-4-maverick`; Cloudflare `@cf/moonshotai/kimi-k2.6` (excluded from
  the Workers Free plan); HuggingFace `nvidia/Nemotron-3-Nano-30B-A3B`;
  OpenCode Zen `north-mini-code-free`; the OpenRouter and LLM7 `gpt-oss-20b`
  routes (paid-only / unavailable); and the OpenRouter `inkling` and
  `inkling-small` free routes, which are gated to agentic harnesses (HTTP 403).
- Corrected three renamed provider model IDs: Cloudflare
  `llama-4-scout-17b-16e-instruct` and `llama-3.1-8b-instruct-fp8`, and
  HuggingFace `Llama-4-Maverick-17B-128E-Instruct-FP8`.
- Restricted LLM7 discovery and routing to live `turbo` models with
  `usage_based_only: false`; removed ten stale or paid LLM7 deployments and
  updated anonymous/free-token limits and dashboard URLs.
- Made OpenRouter `embedding-liquid` fail fast (`timeout: 5`, no retry) so a
  free-tier `Retry-After: 60` response cannot hide a one-minute stall behind
  the successful retry's sub-second provider duration. The per-deployment
  `num_retries` part of this turned out to be inert; the model-group retry
  policy above is what actually removes the stall.
- Prevented slow `gpt-oss-120b` providers from consuming the former 120-second
  per-attempt timeout under concurrent uncached load. Both GPT-OSS pools now
  use a 20-second deployment timeout and one retry; router defaults use a
  one-second retry delay, a 60-second cooldown, and a 30-second ceiling for
  other deployments.
- Documented that routing/load tests must use unique prompts and explicitly
  bypass the five-minute Redis response cache.
- Verified the fix with 24 unique requests at concurrency four: 24/24
  succeeded, maximum latency fell from over 135 seconds to 41.33 seconds,
  and the complete run fell from 315.07 seconds to 43.67 seconds.
- Extended the timeout policy by workload: live-tested Llama 3.3 70B and
  Gemma 4 31B pools now fail over after 20 seconds, embeddings after 15
  seconds, while speech (60s) and transcription (120s) retain media-safe
  limits. Other chat deployments inherit the 30-second global ceiling.

### Verified

- Live provider tests for every added, corrected, and removed deployment
  (HTTP 200/400/402/403/410 recorded per route on 2026-08-24).
- Rate-limit failover measured against a running proxy: a single-deployment
  group went from a 61-second stall to a 1.3-second fallback, a rate-limited
  embedding alias from 61 seconds to 50 milliseconds, and multi-deployment
  groups keep switching providers in under a second without an error.
- Lint, 141 tests, config render without redundancy warnings, LiteLLM boot
  against the rendered config, Compose validation, 23 Kubernetes resources,
  and generated documentation drift.

## [0.2.0] - 2026-08-19

### Added

- Z.AI with the zero-price `glm-4.5-flash`, `glm-4.7-flash`, and
  vision-capable `glm-4.6v-flash` aliases.
- ElevenLabs Scribe v2 as a second backend for `audio-transcription`, plus the
  provider-specific `elevenlabs-scribe-v2` alias.
- Poolside Laguna S 2.1 through Poolside, OpenCode Zen, and OpenRouter.
- Free embedding aliases for Gemini, Cohere, Mistral, OpenRouter/NVIDIA
  Nemotron text and vision models, and Liquid LFM2.5 Embedding.
- Groq speech/transcription aliases and an authenticated native Cloudflare
  image-generation route.
- Two newly verified OpenRouter chat aliases: `dots-3-note-preview` and
  `lfm-2.5-2.6b`.
- Generated `MODEL_PRICING.md` covering every alias with an official,
  LiteLLM-database, or clearly marked estimated reference price and saving.
- Live provider catalog synchronization for Poolside and Z.AI, plus the
  separate OpenRouter embedding catalog with modality-aware chat exclusion.
- Guided key setup for all current providers, GitHub Actions sync secrets,
  Kubernetes secret templates, and multi-instance key propagation.
- Research report on additional free providers under
  `research/free-model-providers-2026/`.
- Project banner and generated provider/deployment matrices.

### Changed

- Updated the pinned LiteLLM image from `v1.92.0` to `v1.97.0` in Docker,
  Compose, Kubernetes, and multi-instance manifests.
- Expanded the effective configuration to 15 providers, 69 aliases, and 155
  direct deployments; the generated master/slave setup contains 293 routes.
- Improved provider discrimination for OpenAI-compatible backends so a model
  vendor in the path cannot override the authoritative API base.
- Expanded catalog retries, stale-deployment reporting, paid-model filtering,
  pricing estimates, generated documentation, and invariant coverage.
- Increased the test suite from 109 to 122 passing tests.

### Removed

- GitHub Models after the service retired on 2026-07-30 and live catalog/API
  checks returned HTTP 404/410.
- Six OpenRouter `:free` deployments that now return HTTP 404. Their paid
  replacements were deliberately not adopted; affected aliases continue via
  other free providers where available.
- Retired or unavailable model routes discovered by the live catalog audit.

### Verified

- Live HTTP 200 inference through the LiteLLM proxy for Z.AI GLM Flash,
  ElevenLabs Scribe v2, Poolside Laguna S 2.1, both new OpenRouter chat models,
  and all three new OpenRouter embedding aliases.
- Docker readiness/health, Compose validation, 23 Kubernetes resources,
  generated documentation drift, lint, and all 122 tests.

## [0.1.0]

- Initial public release.

[0.3.0]: https://github.com/natorus87/litellm-free-models/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/natorus87/litellm-free-models/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/natorus87/litellm-free-models/releases/tag/v0.1.0
