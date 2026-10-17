"""Invariants for the fork-only `vision` route (docs/adr/0001-fork-conventions.md §6).

`vision` is `standard` narrowed to the deployments that read a synthetic
receipt in a live run of tools/smoke-vision.py. A client that sends an
image must never land on a text model, so the properties here are: every
member is allowlisted, the members are exactly the allowlisted part of
`standard` in its order, every member says `supports_vision: true` in
model_info (clients read it from /model/info), and the rendered route is
closed so that upstream's `"*"` fallback -- text models -- is unreachable.
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from fork import standard, vision_route
from tests._loader import REPO_ROOT, load_script

rc = load_script("render-config.py")
fork_render = load_script("fork/render.py")

TEMPLATE = REPO_ROOT / "config.template.yaml"
ROUTE = vision_route.ROUTE_NAME

PROVIDERS = rc.PROVIDERS
PROVIDER_NAMES = sorted(PROVIDERS)
PROVIDER_KEYS = sorted({p.env_var for p in PROVIDERS.values() if p.env_var})


def _block(model_name, provider, model_id, api_base, mode, rpm, info):
    lines = [f"  - model_name: {model_name}\n", "    litellm_params:\n",
             f"      model: {model_id}\n", "      api_key: k\n"]
    if api_base:
        lines.append(f"      api_base: {api_base}\n")
    lines.append(f"      rpm: {rpm}\n")
    if info is not None:
        lines += ["    model_info:\n", f"      mode: {mode}\n"]
        if info:
            lines.append(f"      supports_vision: {info}\n")
    return {"model_name": model_name, "provider": provider, "model_id": model_id,
            "api_base": api_base, "mode": mode, "lines": lines, "start": 0, "end": 0}


MODEL_IDS = ["openai/x", "openai/y", "gemini/z", "groq/w"]
HOSTS = ["", "https://a/v1", "https://b/v1"]
ALL_KEYS = sorted({vision_route.verified_key(m, h) for m in MODEL_IDS for h in HOSTS})

block_st = st.builds(
    _block,
    model_name=st.sampled_from(["alpha", "beta", "gamma"]),
    provider=st.sampled_from(PROVIDER_NAMES + [""]),
    model_id=st.sampled_from(MODEL_IDS),
    api_base=st.sampled_from(HOSTS),
    mode=st.sampled_from(["chat", "chat", "embedding"]),
    rpm=st.integers(min_value=1, max_value=50),
    # no model_info / model_info without the flag / a stale false flag
    info=st.sampled_from([None, "", "false"]),
)
env_st = st.fixed_dictionaries({k: st.sampled_from(["", "x"]) for k in PROVIDER_KEYS})
verified_st = st.dictionaries(st.sampled_from(ALL_KEYS), st.just("2026-01-01"))


def _key(b: dict) -> str:
    return vision_route.verified_key(b["model_id"], b.get("api_base", ""))


def _deployments(lines: list[str]) -> list[dict]:
    """The rendered route as LiteLLM will read it."""
    parsed = yaml.safe_load("model_list:\n" + "".join(lines))["model_list"] or []
    return parsed


class TestChainProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st, verified_st)
    def test_chain_is_the_allowlisted_part_of_standard_in_standard_order(self, blocks, env, verified):
        base = standard.chain(blocks, env)
        got = vision_route.chain(blocks, env, verified=verified)
        self.assertEqual([_key(b) for b in got], [_key(b) for b in base if _key(b) in verified])

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st, verified_st, verified_st)
    def test_an_excluded_deployment_leaves_vision_even_when_allowlisted(self, blocks, env, verified, excluded):
        got = [_key(b) for b in vision_route.chain(blocks, env, verified=verified, excluded=excluded)]
        self.assertFalse(set(got) & set(excluded))

    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st)
    def test_empty_allowlist_means_empty_route(self, blocks, env):
        self.assertEqual(vision_route.chain(blocks, env, verified={}), [])


class TestRenderedBlocks(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.lists(block_st, max_size=30), env_st, verified_st)
    def test_every_deployment_is_flagged_named_and_ordered(self, blocks, env, verified):
        chain = vision_route.chain(blocks, env, verified=verified)
        deps = _deployments(vision_route.render_blocks(chain))
        self.assertEqual(len(deps), len(chain))
        for n, (d, b) in enumerate(zip(deps, chain), start=1):
            self.assertEqual(d["model_name"], ROUTE)
            self.assertIs(d["model_info"]["supports_vision"], True)
            # Receipts are personal data: messages never reach Langfuse.
            self.assertIs(d["litellm_params"]["turn_off_message_logging"], True)
            self.assertEqual(d["litellm_params"]["order"], n)
            self.assertEqual(d["litellm_params"]["model"], b["model_id"])

    def test_flag_is_added_to_existing_model_info_once(self):
        b = _block("a", "google-ai", "gemini/z", "", "chat", 5, "false")
        lines = vision_route.render_blocks([b])
        self.assertEqual(sum("supports_vision" in ln for ln in lines), 1)
        self.assertEqual(sum("turn_off_message_logging" in ln for ln in lines), 1)
        d = _deployments(lines)[0]
        self.assertEqual(d["model_info"], {"mode": "chat", "supports_vision": True})

    def test_block_without_model_info_gets_one(self):
        b = _block("a", "google-ai", "gemini/z", "", "chat", 5, None)
        self.assertEqual(_deployments(vision_route.render_blocks([b]))[0]["model_info"],
                         {"supports_vision": True})


class TestKeyLineMatching(unittest.TestCase):
    def test_commented_model_info_is_reused_not_duplicated(self):
        b = _block("a", "google-ai", "gemini/z", "", "chat", 5, "")
        b["lines"] = [ln.replace("    model_info:\n", "    model_info:  # costs are documentary\n")
                      .replace("    litellm_params:\n", "    litellm_params:   # call params\n")
                      for ln in b["lines"]]
        d = _deployments(vision_route.render_blocks([b]))[0]
        self.assertEqual(d["model_info"], {"mode": "chat", "supports_vision": True})
        self.assertIs(d["litellm_params"]["turn_off_message_logging"], True)


class TestRouteListsAgree(unittest.TestCase):
    def test_settings_ui_and_render_list_the_same_routes(self):
        settings_ui = load_script("fork/settings_ui.py")
        self.assertEqual(settings_ui.ROUTES,
                         (standard.ROUTE_NAME,) + tuple(r.ROUTE_NAME for r in fork_render.NARROWED_ROUTES))

    def test_settings_page_does_not_hardcode_routes(self):
        html = (REPO_ROOT / "fork" / "settings_ui.html").read_text(encoding="utf-8")
        self.assertNotIn('["standard", "tools"]', html)


# ─── Full render against the real template ──────────────────────────────────

GEMINI_VISION = {
    "gemini/gemini-3.5-flash-lite": "2026-10-17",
    "gemini/gemini-3.1-flash-lite": "2026-10-17",
}


def _render(env: dict) -> dict:
    env = dict(env, LITELLM_MASTER_KEY="k", CLOUDFLARE_API_BASE="https://cf/v1",
               REDIS_HOST="", REDIS_PASSWORD="p", REDIS_PORT="6379")
    with tempfile.TemporaryDirectory() as d:
        env_path = Path(d) / ".env"
        env_path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        out = Path(d) / "config.yaml"
        assert fork_render.render(TEMPLATE, env_path, out) == 0
        return yaml.safe_load(out.read_text(encoding="utf-8"))


def _fallbacks(cfg: dict) -> dict:
    out = {}
    for item in cfg["router_settings"].get("fallbacks") or []:
        out.update(item)
    return out


class TestRenderedVisionRoute(unittest.TestCase):
    @settings(max_examples=15, deadline=None)
    @given(env_st)
    def test_route_is_closed_flagged_and_deep_enough(self, env):
        with mock.patch.object(vision_route, "VISION_VERIFIED", GEMINI_VISION):
            cfg = _render(env)
        deps = [d for d in cfg["model_list"] if d["model_name"] == ROUTE]
        fallbacks = _fallbacks(cfg)
        if not deps:
            self.assertNotIn(ROUTE, fallbacks)
            return
        # `{"vision": []}` is what keeps a failed vision request away from
        # upstream's "*" (text models).
        self.assertEqual(fallbacks[ROUTE], [])
        self.assertTrue(all(d["model_info"]["supports_vision"] is True for d in deps))
        self.assertTrue(all(d["litellm_params"]["turn_off_message_logging"] is True for d in deps))
        self.assertTrue({d["litellm_params"]["model"] for d in deps} <= set(GEMINI_VISION))
        self.assertGreaterEqual(cfg["router_settings"]["max_fallbacks"], len(deps))

    def test_route_with_a_gemini_key_contains_the_allowlisted_gemini_models(self):
        env = {k: "" for k in PROVIDER_KEYS}
        env[PROVIDERS["google-ai"].env_var] = "x"
        with mock.patch.object(vision_route, "VISION_VERIFIED", GEMINI_VISION):
            cfg = _render(env)
        got = [d["litellm_params"]["model"] for d in cfg["model_list"] if d["model_name"] == ROUTE]
        self.assertEqual(sorted(got), sorted(GEMINI_VISION))

    def test_upstream_catch_all_still_leads_to_text_models(self):
        # The reason the route must be closed: "*" exists and is text-only.
        env = {k: "" for k in PROVIDER_KEYS}
        env[PROVIDERS["google-ai"].env_var] = "x"
        self.assertIn("*", _fallbacks(_render(env)))

    def test_vision_is_not_in_the_upstream_template(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(f"model_name: {ROUTE}\n", text)
        self.assertIsNone(re.search(r"^\s+supports_vision:", text, re.M),
                          "flags belong to the render step, not the template (ADR-0001 §6)")


if __name__ == "__main__":
    unittest.main()


class TestReservedFlash(unittest.TestCase):
    """gemini-flash-latest has 20 requests a day: it serves `vision` only."""

    def test_reserved_deployment_is_in_vision_but_not_in_standard(self):
        env = {k: "" for k in PROVIDER_KEYS}
        env[PROVIDERS["google-ai"].env_var] = "x"
        cfg = _render(env)
        by_route = {}
        for d in cfg["model_list"]:
            by_route.setdefault(d["model_name"], []).append(d["litellm_params"]["model"])
        self.assertIn("gemini/gemini-flash-latest", by_route.get(ROUTE, []))
        self.assertNotIn("gemini/gemini-flash-latest", by_route.get(standard.ROUTE_NAME, []))
        for model in standard.RESERVED:
            self.assertNotIn(model, by_route.get(standard.ROUTE_NAME, []))

    def test_every_allowlisted_deployment_exists_in_the_catalogue(self):
        env = {k: "x" for k in PROVIDER_KEYS}
        cfg = _render(env)
        models = {d["litellm_params"]["model"] for d in cfg["model_list"]}
        for key in vision_route.VISION_VERIFIED:
            self.assertIn(key.split(" @ ")[0], models)
