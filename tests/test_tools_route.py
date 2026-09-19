"""Invariants for the fork-only `tools` route (docs/adr/0001-fork-conventions.md §5).

`tools` is `standard` narrowed to the deployments that returned a correct
`tool_calls` in a live run of tools/smoke-tool-calling.py. An agent that
sends `model: "tools"` must never land on a backend that ignores its tool
definitions, so the properties here are: every member is allowlisted, the
members are exactly the allowlisted part of `standard`, and their relative
order is the one `standard` has. A filter that silently lets an unverified
deployment through (or drops a verified one) is the bug these tests catch.
"""
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fork import standard, tools_route
from tests._loader import REPO_ROOT, load_script

rc = load_script("render-config.py")
fork_render = load_script("fork/render.py")

TEMPLATE = REPO_ROOT / "config.template.yaml"
FORK_MODELS = REPO_ROOT / "fork" / "models.yaml"
ROUTE = tools_route.ROUTE_NAME

PROVIDERS = rc.PROVIDERS
PROVIDER_NAMES = sorted(PROVIDERS)
PROVIDER_KEYS = sorted({p.env_var for p in PROVIDERS.values() if p.env_var})


# ─── Synthetic deployment blocks (same shape as tests/test_standard_route.py) ─

def _block(model_name, provider, model_id, api_base, mode, rpm):
    lines = [f"  - model_name: {model_name}\n", "    litellm_params:\n",
             f"      model: {model_id}\n", "      api_key: k\n"]
    if api_base:
        lines.append(f"      api_base: {api_base}\n")
    lines += [f"      rpm: {rpm}\n", "    model_info:\n", f"      mode: {mode}\n"]
    return {"model_name": model_name, "provider": provider, "model_id": model_id,
            "api_base": api_base, "mode": mode, "lines": lines, "start": 0, "end": 0}


MODEL_IDS = ["openai/x", "openai/y", "gemini/z", "groq/w"]
HOSTS = ["", "https://a/v1", "https://b/v1"]
ALL_KEYS = sorted({tools_route.verified_key(m, h) for m in MODEL_IDS for h in HOSTS})

block_st = st.builds(
    _block,
    model_name=st.sampled_from(["alpha", "beta", "gamma"]),
    provider=st.sampled_from(PROVIDER_NAMES + [""]),
    model_id=st.sampled_from(MODEL_IDS),
    api_base=st.sampled_from(HOSTS),
    mode=st.sampled_from(["chat", "chat", "embedding", "audio_transcription"]),
    rpm=st.integers(min_value=1, max_value=50),
)
env_st = st.fixed_dictionaries({k: st.sampled_from(["", "x"]) for k in PROVIDER_KEYS})
verified_st = st.dictionaries(st.sampled_from(ALL_KEYS), st.just("2026-01-01"))


def _key(b: dict) -> str:
    return tools_route.verified_key(b["model_id"], b.get("api_base", ""))


def _is_subsequence(short: list, long: list) -> bool:
    it = iter(long)
    return all(any(x == y for y in it) for x in short)


class TestChainProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st, verified_st)
    def test_chain_is_the_allowlisted_part_of_standard_in_standard_order(self, blocks, env, verified):
        base = standard.chain(blocks, env)
        got = tools_route.chain(blocks, env, verified=verified)
        self.assertEqual([_key(b) for b in got], [_key(b) for b in base if _key(b) in verified])

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st, verified_st)
    def test_every_member_is_allowlisted(self, blocks, env, verified):
        for b in tools_route.chain(blocks, env, verified=verified):
            self.assertIn(_key(b), verified)

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st, verified_st)
    def test_chain_is_a_subsequence_of_standard(self, blocks, env, verified):
        base = [(b["model_id"], b.get("api_base", "")) for b in standard.chain(blocks, env)]
        got = [(b["model_id"], b.get("api_base", ""))
               for b in tools_route.chain(blocks, env, verified=verified)]
        self.assertTrue(_is_subsequence(got, base), (got, base))
        self.assertEqual(len(got), len(set(got)))

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st)
    def test_empty_allowlist_means_empty_route(self, blocks, env):
        self.assertEqual(tools_route.chain(blocks, env, verified={}), [])

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st)
    def test_full_allowlist_means_the_whole_standard_chain(self, blocks, env):
        everything = dict.fromkeys(ALL_KEYS, "2026-01-01")
        self.assertEqual(tools_route.chain(blocks, env, verified=everything),
                         standard.chain(blocks, env))

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st, verified_st)
    def test_rendered_blocks_carry_route_name_and_unique_increasing_order(self, blocks, env, verified):
        chain = tools_route.chain(blocks, env, verified=verified)
        lines = tools_route.render_blocks(chain)
        _, _, parsed = rc.parse_blocks(["model_list:\n"] + lines)
        self.assertEqual([b["model_name"] for b in parsed], [ROUTE] * len(chain))
        orders = [int(m.group(1)) for ln in lines for m in [re.match(r"\s+order: (\d+)$", ln)] if m]
        self.assertEqual(orders, list(range(1, len(chain) + 1)))
        self.assertEqual([(b["model_id"], b["api_base"]) for b in parsed],
                         [(b["model_id"], b["api_base"]) for b in chain])


class TestVerifiedKey(unittest.TestCase):
    @given(st.sampled_from(MODEL_IDS), st.sampled_from(HOSTS))
    def test_key_round_trips(self, model, api_base):
        key = tools_route.verified_key(model, api_base)
        self.assertEqual(tools_route.split_verified_key(key), (model, api_base))

    @given(st.sampled_from(MODEL_IDS), st.sampled_from(HOSTS), st.sampled_from(HOSTS))
    def test_same_model_on_another_host_is_another_key(self, model, a, b):
        ka, kb = tools_route.verified_key(model, a), tools_route.verified_key(model, b)
        self.assertEqual(ka == kb, a == b)

    def test_key_matches_the_json_schema_smoke_format(self):
        # One vocabulary for both allowlists: `model @ host`.
        smoke = load_script("tools/smoke-json-schema.py")
        for m in MODEL_IDS:
            for h in HOSTS:
                self.assertEqual(tools_route.verified_key(m, h), smoke.deployment_key(m, h))


# ─── The real allowlist ─────────────────────────────────────────────────────

def _template_blocks(env: dict) -> list[dict]:
    text = TEMPLATE.read_text(encoding="utf-8").replace(
        "model_list:\n", "model_list:\n" + FORK_MODELS.read_text(encoding="utf-8"), 1)
    text, _ = rc.substitute_placeholders(text, env)
    _, _, blocks = rc.parse_blocks(text.splitlines(keepends=True))
    return blocks


def _full_env() -> dict:
    env = {p.env_var: "x" for p in PROVIDERS.values() if p.env_var}
    env["CLOUDFLARE_API_BASE"] = "https://cf/v1"
    return env


class TestAllowlist(unittest.TestCase):
    """TOOL_CALLING_VERIFIED is what the route trusts; keep it honest."""

    def test_non_empty_with_iso_dates_not_in_the_future(self):
        self.assertTrue(tools_route.TOOL_CALLING_VERIFIED)
        for key, day in tools_route.TOOL_CALLING_VERIFIED.items():
            model, _ = tools_route.split_verified_key(key)
            self.assertRegex(model, r"^[a-z0-9_-]+/.+", key)
            self.assertLessEqual(date.fromisoformat(day), date.today())

    def test_openai_compatible_entries_name_their_host(self):
        for key in tools_route.TOOL_CALLING_VERIFIED:
            model, api_base = tools_route.split_verified_key(key)
            if model.startswith("openai/"):
                self.assertTrue(api_base, f"{key}: api_base missing")

    def test_every_entry_is_a_deployment_the_template_can_render(self):
        # A stale entry (model retired by an upstream sync) would be dead
        # weight: the route is generated from the template, not the list.
        env = _full_env()
        keys = {_key(b) for b in _template_blocks(env) if b.get("mode", "chat") == "chat"}
        for key in tools_route.TOOL_CALLING_VERIFIED:
            self.assertIn(key, keys)

    def test_operator_verified_deployments_are_listed(self):
        # Confirmed by hand on 2026-09-19 before this route existed.
        self.assertIn("gemini/gemini-3.5-flash-lite", tools_route.TOOL_CALLING_VERIFIED)
        self.assertIn("groq/openai/gpt-oss-120b", tools_route.TOOL_CALLING_VERIFIED)

    def test_default_chain_uses_the_module_allowlist(self):
        env = _full_env()
        blocks = _template_blocks(env)
        self.assertEqual(tools_route.chain(blocks, env),
                         tools_route.chain(blocks, env, verified=tools_route.TOOL_CALLING_VERIFIED))
        self.assertTrue(tools_route.chain(blocks, env))


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


class TestRenderedToolsRoute(unittest.TestCase):
    @settings(max_examples=25, deadline=None)
    @given(env_st, st.booleans())
    def test_rendered_tools_is_the_allowlisted_subsequence_of_rendered_standard(self, env, redis):
        lines, _ = _render(env, redis)
        _, _, blocks = rc.parse_blocks(lines)
        std = [_key(b) for b in blocks if b["model_name"] == standard.ROUTE_NAME]
        got = [_key(b) for b in blocks if b["model_name"] == ROUTE]
        self.assertEqual(got, [k for k in std if k in tools_route.TOOL_CALLING_VERIFIED])

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
        self.assertEqual(chains.get(ROUTE), [])
        depth = [int(m.group(1)) for ln in lines
                 for m in [re.match(r"  max_fallbacks: (\d+)$", ln)] if m]
        self.assertEqual(len(depth), 1)
        self.assertGreaterEqual(depth[0], n)

    def test_tools_is_not_in_the_upstream_template(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(f"model_name: {ROUTE}\n", text)


if __name__ == "__main__":
    unittest.main()
