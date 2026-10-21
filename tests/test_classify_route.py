"""Invariants for the fork-only `classify` route (docs/adr/0001-fork-conventions.md §6).

`classify` is a short chain for tiny structured-output requests, in an
operator order (fork/classify_route.CLASSIFY_ORDER). The properties: it is a
subset of `standard`, only named deployments, in the named order, no
duplicates, every attempt short, and every name still exists in the template
(so an upstream rename is noticed instead of silently emptying the chain).
"""
import re
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fork import classify_route, standard
from tests._loader import REPO_ROOT, load_script

rc = load_script("render-config.py")
fork_render = load_script("fork/render.py")

TEMPLATE = REPO_ROOT / "config.template.yaml"
ROUTE = classify_route.ROUTE_NAME
PROVIDERS = rc.PROVIDERS
PROVIDER_KEYS = sorted({p.env_var for p in PROVIDERS.values() if p.env_var})


def _template_blocks() -> list[dict]:
    text = fork_render.merged_template_text(TEMPLATE)
    _, _, blocks = rc.parse_blocks(text.splitlines(keepends=True))
    return blocks


def _all_keys_env() -> dict[str, str]:
    env = {k: "x" for k in PROVIDER_KEYS}
    env["CLOUDFLARE_API_BASE"] = "https://cf.example/v1"
    return env


class ClassifyOrderTest(unittest.TestCase):
    def test_order_has_no_duplicates(self):
        self.assertEqual(len(classify_route.CLASSIFY_ORDER), len(set(classify_route.CLASSIFY_ORDER)))

    def test_every_named_deployment_exists_in_the_template(self):
        # Mutation: rename a model in CLASSIFY_ORDER -> this fails.
        known = {classify_route.order_key(b["model_id"], b.get("api_base", ""))
                 for b in _template_blocks() if b.get("mode", "chat") == "chat"}
        # Template api_base for Cloudflare is a placeholder; compare model ids there.
        known_models = {b["model_id"] for b in _template_blocks()}
        for key in classify_route.CLASSIFY_ORDER:
            model, _, _host = key.partition(classify_route.KEY_SEP)
            self.assertTrue(key in known or model in known_models,
                            f"{key!r} is not a deployment in the template or fork/models.yaml")

    def test_providers_alternate_at_the_head(self):
        # The first three slots must not share a provider: one burnt quota
        # must not take out the head of the chain.
        by_key = {classify_route.order_key(b["model_id"], b.get("api_base", "")): b["provider"]
                  for b in _template_blocks() if b.get("provider")}
        head = [by_key.get(k) for k in classify_route.CLASSIFY_ORDER[:3]]
        self.assertEqual(len(set(head)), len(head), head)


class ClassifyChainTest(unittest.TestCase):
    def test_full_env_chain_follows_the_operator_order(self):
        blocks = _template_blocks()
        got = [classify_route.order_key(b["model_id"], b.get("api_base", ""))
               for b in classify_route.chain(blocks, _all_keys_env())]
        want = [k for k in classify_route.CLASSIFY_ORDER if k in got]
        self.assertEqual(got, want)
        self.assertGreaterEqual(len(got), 8)

    def test_no_keys_means_no_route(self):
        self.assertEqual(classify_route.chain(_template_blocks(), {}), [])

    def test_missing_key_drops_only_that_provider(self):
        env = _all_keys_env()
        env["NVIDIA_API_KEY"] = ""
        got = classify_route.chain(_template_blocks(), env)
        self.assertTrue(got)
        self.assertNotIn("nvidia", {b["provider"] for b in got})

    @settings(max_examples=40, deadline=None)
    @given(st.fixed_dictionaries({k: st.sampled_from(["", "x"]) for k in PROVIDER_KEYS}))
    def test_subset_of_standard_unique_and_named(self, env):
        blocks = _template_blocks()
        std = {standard.deployment_key(b) for b in standard.chain(blocks, env)}
        got = classify_route.chain(blocks, env)
        keys = [standard.deployment_key(b) for b in got]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(set(keys) <= std)
        named = set(classify_route.CLASSIFY_ORDER)
        for b in got:
            self.assertIn(classify_route.order_key(b["model_id"], b.get("api_base", "")), named)


class ClassifyRenderTest(unittest.TestCase):
    def test_every_deployment_has_order_and_short_attempts(self):
        chain = classify_route.chain(_template_blocks(), _all_keys_env())
        text = "".join(classify_route.render_blocks(chain))
        n = len(chain)
        self.assertEqual(text.count(f"model_name: {ROUTE}"), n)
        self.assertEqual(re.findall(r"^\s+order: (\d+)$", text, re.M), [str(i) for i in range(1, n + 1)])
        self.assertEqual(re.findall(r"^\s+timeout: (\d+)$", text, re.M), [str(classify_route.CLASSIFY_TIMEOUT_S)] * n)
        self.assertEqual(re.findall(r"^\s+num_retries: (\d+)$", text, re.M), [str(classify_route.CLASSIFY_NUM_RETRIES)] * n)

    def test_rendered_config_pins_the_route_and_fallback_depth(self):
        with tempfile.TemporaryDirectory() as d:
            env_path, out = Path(d) / ".env", Path(d) / "config.yaml"
            env_path.write_text("".join(f"{k}=x\n" for k in PROVIDER_KEYS)
                                + "CLOUDFLARE_API_BASE=https://cf.example/v1\n", encoding="utf-8")
            self.assertEqual(fork_render.render(TEMPLATE, env_path, out, no_redis=True), 0)
            text = out.read_text(encoding="utf-8")
        self.assertIn('- {"classify": []}', text)
        self.assertIn("model_name: classify", text)
        depth = int(re.search(r"max_fallbacks: (\d+)", text).group(1))
        self.assertGreaterEqual(depth, text.count("model_name: classify"))


if __name__ == "__main__":
    unittest.main()
