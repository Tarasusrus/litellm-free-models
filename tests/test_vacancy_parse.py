"""Invariants for the fork-only `vacancy-parse` route.

The proxy runs with `drop_params: true`: a provider that cannot do
`response_format=json_schema` silently answers with prose. So every
deployment that can serve `vacancy-parse` -- directly or through its
fallback chain -- must come from JSON_SCHEMA_VERIFIED, the set of
deployments that passed tools/smoke-json-schema.py live. The chain must
never end in the catch-all `*`, and the renderer must not widen it.
"""
import re
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from tests._loader import REPO_ROOT, load_script

rc = load_script("render-config.py")
smoke = load_script("tools/smoke-json-schema.py")

TEMPLATE = REPO_ROOT / "config.template.yaml"
VERIFIED = set(smoke.JSON_SCHEMA_VERIFIED)
ROUTE = "vacancy-parse"


def _chains(lines, section="fallbacks"):
    chains = {}
    in_section = False
    for line in lines:
        s = line.strip()
        if s == f"{section}:":
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


def _template():
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines(keepends=True)
    _, _, blocks = rc.parse_blocks(lines)
    return lines, blocks


def _reachable(chains, start):
    """Every model group a request to `start` can end up on via fallbacks."""
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(chains.get(name, []))
    return seen


class TestTemplateInvariants(unittest.TestCase):
    def setUp(self):
        self.lines, self.blocks = _template()
        self.by_name = {}
        for b in self.blocks:
            self.by_name.setdefault(b["model_name"], []).append(b)
        self.chains = _chains(self.lines)

    def test_route_exists_and_is_chat(self):
        self.assertIn(ROUTE, self.by_name)
        for b in self.by_name[ROUTE]:
            self.assertEqual(b.get("mode", "chat"), "chat")

    def test_every_reachable_deployment_is_verified(self):
        for name in _reachable(self.chains, ROUTE):
            self.assertIn(name, self.by_name, f"{name} has no deployments")
            for b in self.by_name[name]:
                self.assertIn(b["model_id"], VERIFIED,
                              f"{name} -> {b['model_id']} never passed the json_schema smoke test")

    def test_chain_is_explicit_non_empty_and_closed(self):
        self.assertIn(ROUTE, self.chains, "vacancy-parse needs an explicit fallback chain")
        self.assertTrue(self.chains[ROUTE], "vacancy-parse chain must not be empty")
        for name in _reachable(self.chains, ROUTE):
            self.assertIn(name, self.chains,
                          f"{name} has no explicit chain -> would fall through to '*'")
            self.assertNotIn("*", self.chains[name])
            self.assertNotIn("openrouter-free", self.chains[name])

    def test_route_is_pinned_in_renderer(self):
        for name in _reachable(self.chains, ROUTE):
            self.assertIn(name, rc.SCHEMA_PINNED_FALLBACKS)

    def test_fork_section_sits_after_upstream_blocks(self):
        # Upstream blocks are synced by find-shared-models.py; the fork
        # section stays at the very end so syncs never interleave with it.
        names = [b["model_name"] for b in self.blocks]
        first_fork = names.index(ROUTE)
        for name in names[first_fork:]:
            self.assertIn(name, rc.SCHEMA_PINNED_FALLBACKS,
                          f"upstream block '{name}' found after the fork section")


# ─── Renderer keeps the chain closed for every key set ───────────────────────

PROVIDER_KEYS = sorted({p.env_var for p in rc.PROVIDERS.values() if p.env_var})
env_st = st.fixed_dictionaries({k: st.sampled_from(["", "x"]) for k in PROVIDER_KEYS})


class TestRenderKeepsChainClosed(unittest.TestCase):
    @settings(max_examples=25, deadline=None)
    @given(env_st, st.booleans())
    def test_rendered_chain_never_widens(self, env, redis):
        env = dict(env, LITELLM_MASTER_KEY="k", CLOUDFLARE_API_BASE="https://cf/v1",
                   REDIS_HOST="redis" if redis else "", REDIS_PASSWORD="p", REDIS_PORT="6379")
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
            out = Path(d) / "config.yaml"
            self.assertEqual(rc.render(TEMPLATE, env_path, out), 0)
            lines = out.read_text(encoding="utf-8").splitlines(keepends=True)
        _, _, blocks = rc.parse_blocks(lines)
        by_name = {}
        for b in blocks:
            by_name.setdefault(b["model_name"], []).append(b)
        chains = _chains(lines)
        template_chains = _chains(_template()[0])
        if ROUTE not in by_name:
            # no GEMINI key -> the route is gone entirely, chain included
            self.assertNotIn(ROUTE, chains)
            return
        for name in _reachable(chains, ROUTE):
            self.assertIn(name, by_name, f"{name} is a chain target without deployments")
            self.assertIn(name, chains, f"{name} lost its explicit chain in render")
            self.assertTrue(set(chains[name]) <= set(template_chains[name]),
                            f"{name}: renderer added {set(chains[name]) - set(template_chains[name])}")
            self.assertNotIn("openrouter-free", chains[name])
            for b in by_name.get(name, []):
                self.assertIn(b["model_id"], VERIFIED)


if __name__ == "__main__":
    unittest.main()
