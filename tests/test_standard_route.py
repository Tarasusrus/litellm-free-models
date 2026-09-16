"""Invariants for the fork-only `standard` route (docs/adr/0001-fork-conventions.md).

`standard` is generated at render time: every chat deployment whose
provider key is present in `.env` is copied under one model_name, with a
unique `order` that follows the provider priority from the ADR. LiteLLM
tries the lowest order first and walks up on failure, so the rendered
chain must contain every keyed chat deployment exactly once, in priority
order — a deployment silently dropped from the chain is the bug these
tests exist to catch.
"""
import re
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fork import standard
from tests._loader import REPO_ROOT, load_script

rc = load_script("render-config.py")
fork_render = load_script("fork/render.py")

TEMPLATE = REPO_ROOT / "config.template.yaml"
FORK_MODELS = REPO_ROOT / "fork" / "models.yaml"
ROUTE = standard.ROUTE_NAME

PROVIDERS = rc.PROVIDERS
PROVIDER_NAMES = sorted(PROVIDERS)
PROVIDER_KEYS = sorted({p.env_var for p in PROVIDERS.values() if p.env_var})


# ─── Expected chain, computed independently of fork.standard.chain ──────────

def _has_key(provider: str, env: dict) -> bool:
    prov = PROVIDERS[provider]
    return bool(prov.env_var and env.get(prov.env_var))


def _expected_chain(blocks: list[dict], env: dict) -> list[tuple[str, str]]:
    """Ordered, de-duplicated (model, api_base) of every keyed chat block."""
    rank = {name: i for i, name in enumerate(standard.PROVIDER_PRIORITY)}
    picked: list[tuple[int, int, tuple[str, str]]] = []
    seen: set[tuple[str, str]] = set()
    for pos, b in enumerate(blocks):
        if b.get("mode", "chat") != "chat" or not b["provider"]:
            continue
        if not _has_key(b["provider"], env):
            continue
        key = (b["model_id"], b.get("api_base", ""))
        if key in seen:
            continue
        seen.add(key)
        picked.append((rank.get(b["provider"], len(rank)), pos, key))
    picked.sort()
    return [key for _, _, key in picked]


# ─── Synthetic deployment blocks ────────────────────────────────────────────

def _block(model_name, provider, model_id, api_base, mode, rpm):
    lines = [f"  - model_name: {model_name}\n", "    litellm_params:\n",
             f"      model: {model_id}\n", "      api_key: k\n"]
    if api_base:
        lines.append(f"      api_base: {api_base}\n")
    lines += [f"      rpm: {rpm}\n", "    model_info:\n", f"      mode: {mode}\n"]
    return {"model_name": model_name, "provider": provider, "model_id": model_id,
            "api_base": api_base, "mode": mode, "lines": lines, "start": 0, "end": 0}


block_st = st.builds(
    _block,
    model_name=st.sampled_from(["alpha", "beta", "gamma"]),
    provider=st.sampled_from(PROVIDER_NAMES + [""]),
    model_id=st.sampled_from(["openai/x", "openai/y", "gemini/z", "groq/w"]),
    api_base=st.sampled_from(["", "https://a/v1", "https://b/v1"]),
    mode=st.sampled_from(["chat", "chat", "embedding", "audio_transcription"]),
    rpm=st.integers(min_value=1, max_value=50),
)
env_st = st.fixed_dictionaries({k: st.sampled_from(["", "x"]) for k in PROVIDER_KEYS})


class TestChainProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st)
    def test_chain_is_every_keyed_chat_deployment_once_in_priority_order(self, blocks, env):
        chain = standard.chain(blocks, env)
        got = [(b["model_id"], b.get("api_base", "")) for b in chain]
        self.assertEqual(got, _expected_chain(blocks, env))

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st)
    def test_chain_members_keep_their_source_provider_and_mode(self, blocks, env):
        for b in standard.chain(blocks, env):
            self.assertEqual(b.get("mode", "chat"), "chat")
            self.assertTrue(_has_key(b["provider"], env))

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st)
    def test_rendered_blocks_carry_unique_increasing_order(self, blocks, env):
        chain = standard.chain(blocks, env)
        lines = standard.render_blocks(chain)
        _, _, parsed = rc.parse_blocks(["model_list:\n"] + lines)
        self.assertEqual([b["model_name"] for b in parsed], [ROUTE] * len(chain))
        orders = [int(m.group(1)) for ln in lines for m in [re.match(r"\s+order: (\d+)$", ln)] if m]
        self.assertEqual(orders, list(range(1, len(chain) + 1)))
        self.assertEqual([(b["model_id"], b["api_base"]) for b in parsed],
                         [(b["model_id"], b["api_base"]) for b in chain])

    def test_every_provider_has_a_documented_priority(self):
        # A provider without a slot in the ADR order would land at the end
        # silently; make the upstream sync that adds one update the ADR.
        self.assertEqual(set(standard.PROVIDER_PRIORITY), set(PROVIDER_NAMES))
        self.assertEqual(len(standard.PROVIDER_PRIORITY), len(set(standard.PROVIDER_PRIORITY)))


# ─── Full render against the real template ──────────────────────────────────

def _chains(lines):
    chains = {}
    in_section = False
    for line in lines:
        s = line.strip()
        if s == "fallbacks:":
            in_section = True
            continue
        if in_section:
            if s and not line.startswith("    "):
                break
            m = re.match(r'\s*-\s*\{"([^"]+)":\s*\[(.*?)\]\}\s*$', line)
            if m:
                chains[m.group(1)] = [x.strip().strip('"') for x in m.group(2).split(",")
                                      if x.strip().strip('"')]
    return chains


def _render(env: dict, redis: bool) -> tuple[list[str], dict]:
    env = dict(env, LITELLM_MASTER_KEY="k", CLOUDFLARE_API_BASE="https://cf/v1",
               REDIS_HOST="redis" if redis else "", REDIS_PASSWORD="p", REDIS_PORT="6379")
    with tempfile.TemporaryDirectory() as d:
        env_path = Path(d) / ".env"
        env_path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        out = Path(d) / "config.yaml"
        assert fork_render.render(TEMPLATE, env_path, out) == 0
        return out.read_text(encoding="utf-8").splitlines(keepends=True), env


def _template_blocks(env: dict) -> list[dict]:
    """Fork fragment + upstream template with {{VAR}} substituted, so
    api_base values are comparable with the rendered output. The fragment
    goes first: fork-chosen deployments lead within their provider."""
    text = TEMPLATE.read_text(encoding="utf-8").replace(
        "model_list:\n", "model_list:\n" + FORK_MODELS.read_text(encoding="utf-8"), 1)
    text, _ = rc.substitute_placeholders(text, env)
    _, _, blocks = rc.parse_blocks(text.splitlines(keepends=True))
    return blocks


class TestForkModelsFragment(unittest.TestCase):
    """fork/models.yaml: deployments upstream does not carry, in upstream's
    block format so the same renderer filters and validates them."""

    def setUp(self):
        text = "model_list:\n" + FORK_MODELS.read_text(encoding="utf-8")
        _, _, self.blocks = rc.parse_blocks(text.splitlines(keepends=True))
        _, _, self.upstream = rc.parse_blocks(
            TEMPLATE.read_text(encoding="utf-8").splitlines(keepends=True))

    def test_fragment_is_well_formed_chat_with_known_provider(self):
        self.assertTrue(self.blocks)
        for b in self.blocks:
            self.assertEqual(b.get("mode", "chat"), "chat", b["model_name"])
            self.assertIn(b["provider"], PROVIDERS, b["model_id"])
            self.assertTrue(any("rpm:" in ln for ln in b["lines"]), b["model_id"])

    def test_fragment_adds_backends_upstream_lacks(self):
        upstream_keys = {(b["model_id"], b["api_base"]) for b in self.upstream}
        for b in self.blocks:
            self.assertNotIn((b["model_id"], b["api_base"]), upstream_keys,
                             f"{b['model_id']} already in upstream -- drop it from the fragment")

    def test_fragment_never_reuses_a_non_chat_upstream_alias(self):
        non_chat = {b["model_name"] for b in self.upstream if b.get("mode", "chat") != "chat"}
        for b in self.blocks:
            self.assertNotIn(b["model_name"], non_chat)

    def test_fragment_deployments_lead_their_provider_in_the_chain(self):
        env = {p.env_var: "x" for p in PROVIDERS.values() if p.env_var}
        chain = standard.chain(_template_blocks(env), env)
        fragment = {(b["model_id"], b["api_base"]) for b in self.blocks}
        seen_upstream: set[str] = set()
        for b in chain:
            if (b["model_id"], b["api_base"]) in fragment:
                self.assertNotIn(b["provider"], seen_upstream,
                                 f"{b['model_id']} comes after an upstream {b['provider']} deployment")
            else:
                seen_upstream.add(b["provider"])


class TestRenderedStandardRoute(unittest.TestCase):
    @settings(max_examples=25, deadline=None)
    @given(env_st, st.booleans())
    def test_rendered_chain_matches_template_expectation(self, env, redis):
        lines, full_env = _render(env, redis)
        _, _, blocks = rc.parse_blocks(lines)
        got = [(b["model_id"], b["api_base"]) for b in blocks if b["model_name"] == ROUTE]
        self.assertEqual(got, _expected_chain(_template_blocks(full_env), full_env))

    @settings(max_examples=25, deadline=None)
    @given(env_st, st.booleans())
    def test_rendered_route_is_closed_and_deep_enough(self, env, redis):
        lines, _ = _render(env, redis)
        _, _, blocks = rc.parse_blocks(lines)
        n = sum(1 for b in blocks if b["model_name"] == ROUTE)
        chains = _chains(lines)
        if n == 0:
            self.assertNotIn(ROUTE, chains)
            return
        # explicit end of chain: never the catch-all '*', never openrouter-free
        self.assertEqual(chains.get(ROUTE), [])
        depth = [int(m.group(1)) for ln in lines
                 for m in [re.match(r"  max_fallbacks: (\d+)$", ln)] if m]
        self.assertEqual(len(depth), 1)
        self.assertGreaterEqual(depth[0], n)

    def test_standard_is_not_in_the_upstream_template(self):
        # The route is generated; the template stays upstream's.
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(f"model_name: {ROUTE}", text)
        self.assertNotIn("vacancy-parse", text)


if __name__ == "__main__":
    unittest.main()
