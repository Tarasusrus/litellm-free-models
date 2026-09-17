"""Gemini in upstream's discovery (docs/adr/0001-fork-conventions.md §2).

Upstream's paid-vendor denylist drops every `gemini-*` id at every
provider, because aggregators resell the Gemini API. Google AI Studio is
the vendor itself: its free tier is what the fork routes to first, so its
own catalogue must survive the filter — while the same names at
`llm7io` / `opencode-zen` stay denied. The second half: deployments in
`fork/models.yaml` live outside the template, so the stale check has to
merge the fragment in or it never sees them.
"""
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from tests._loader import REPO_ROOT, load_script

fsm = load_script("find-shared-models.py")
rc = load_script("render-config.py")

FORK_MODELS = REPO_ROOT / "fork" / "models.yaml"
AGGREGATORS = ["llm7io", "opencode-zen"]
OTHER_PROVIDERS = ["", "huggingface", "openrouter", "groq"]

# A Gemini id as Google lists it: `gemini` + dashed suffix, optionally
# behind a vendor path segment (`google/gemini-...`).
_suffix = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789.-"),
    min_size=1, max_size=24,
).filter(lambda s: not s.startswith("-") and not s.endswith("-"))
gemini_ids = st.builds(lambda s: f"gemini-{s}", _suffix)
gemini_ids_pathed = st.one_of(gemini_ids, gemini_ids.map(lambda m: f"google/{m}"))

# Flagships that stay paid everywhere, Google AI Studio included.
foreign_paid = st.sampled_from([
    "claude-opus-4-8", "gpt-5.4", "gpt-4o", "grok-4.5", "anthropic/claude-3-opus",
])


class TestFirstPartyGemini(unittest.TestCase):
    @settings(max_examples=200)
    @given(model=gemini_ids_pathed)
    def test_google_ai_lists_gemini_as_free(self, model):
        self.assertFalse(fsm.is_paid_vendor_model(model, "google-ai"))

    @settings(max_examples=200)
    @given(model=gemini_ids_pathed, provider=st.sampled_from(AGGREGATORS))
    def test_aggregators_still_deny_gemini(self, model, provider):
        self.assertTrue(fsm.is_paid_vendor_model(model, provider))

    @settings(max_examples=200)
    @given(model=gemini_ids_pathed, provider=st.sampled_from(OTHER_PROVIDERS))
    def test_exemption_is_google_ai_only(self, model, provider):
        self.assertTrue(fsm.is_paid_vendor_model(model, provider))

    @given(model=foreign_paid)
    def test_exemption_is_gemini_only(self, model):
        self.assertTrue(fsm.is_paid_vendor_model(model, "google-ai"))

    def test_three_named_cases_from_the_task(self):
        self.assertFalse(fsm.is_paid_vendor_model("gemini-3.8-flash", "google-ai"))
        self.assertTrue(fsm.is_paid_vendor_model("gemini-3.8-flash", "llm7io"))
        self.assertTrue(fsm.is_paid_vendor_model("gemini-3.8-flash", "opencode-zen"))

    def test_every_fork_deployment_survives_its_own_providers_filter(self):
        """The sync must never deny a model the fork deploys."""
        lines = ["model_list:\n"] + FORK_MODELS.read_text(encoding="utf-8").splitlines(keepends=True)
        _, _, blocks = rc.parse_blocks(lines)
        self.assertTrue(blocks)
        for b in blocks:
            native = fsm._native_model_id(b["model_id"])
            with self.subTest(model=b["model_id"]):
                self.assertFalse(fsm.is_paid_vendor_model(native, b["provider"]))


# ─── Stale check covers fork/models.yaml ────────────────────────────────────

TEMPLATE_TEXT = (
    "model_list:\n"
    "\n"
    "  - model_name: gpt-oss-120b\n"
    "    litellm_params:\n"
    "      model: groq/openai/gpt-oss-120b\n"
    "      api_key: {{GROQ_API_KEY}}\n"
    "\n"
    "router_settings:\n"
    "  num_retries: 1\n"
)


def _fork_fragment(models: list[str]) -> str:
    out = []
    for m in models:
        out.append(
            f"  - model_name: {m}\n"
            "    litellm_params:\n"
            f"      model: gemini/{m}\n"
            "      api_key: os.environ/GEMINI_API_KEY\n"
            "    model_info:\n"
            "      mode: chat\n"
            "\n"
        )
    return "".join(out)


_fork_sets = st.lists(gemini_ids, min_size=1, max_size=6, unique=True)


class TestStaleCheckSeesForkDeployments(unittest.TestCase):
    @settings(max_examples=60)
    @given(fork_models=_fork_sets, data=st.data())
    def test_stale_equals_fork_minus_live_catalog(self, fork_models, data):
        live = data.draw(st.lists(st.sampled_from(fork_models), unique=True))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "fork").mkdir()
            (root / "fork" / "models.yaml").write_text(_fork_fragment(fork_models), encoding="utf-8")
            tmpl = root / "config.template.yaml"
            tmpl.write_text(TEMPLATE_TEXT, encoding="utf-8")
            raw = {"google-ai": live + ["gemini-embedding-001"], "groq": ["openai/gpt-oss-120b"]}
            stale = fsm.find_stale_deployments(tmpl, raw, partial=set())
        reported = {(s["provider"], s["native_id"]) for s in stale}
        expected = {("google-ai", m) for m in fork_models if m not in live}
        self.assertEqual(reported, expected)

    def test_template_without_fragment_is_unchanged(self):
        """Upstream's own tests pass a bare template; nothing merges in."""
        with tempfile.TemporaryDirectory() as d:
            tmpl = Path(d) / "tmpl.yaml"
            tmpl.write_text(TEMPLATE_TEXT, encoding="utf-8")
            stale = fsm.find_stale_deployments(tmpl, {"google-ai": [], "groq": ["x"]}, partial=set())
        self.assertEqual([s["native_id"] for s in stale], ["openai/gpt-oss-120b"])

    def test_real_fork_models_are_checked_against_google_ai(self):
        """Live shape: repo template + repo fragment, catalog without Gemini."""
        raw = {"google-ai": ["gemma-3-27b-it"]}
        stale = fsm.find_stale_deployments(REPO_ROOT / "config.template.yaml", raw, partial=set())
        lines = ["model_list:\n"] + FORK_MODELS.read_text(encoding="utf-8").splitlines(keepends=True)
        _, _, blocks = rc.parse_blocks(lines)
        fork_gemini = {fsm._native_model_id(b["model_id"]) for b in blocks if b["provider"] == "google-ai"}
        self.assertTrue(fork_gemini)
        self.assertTrue(fork_gemini <= {s["native_id"] for s in stale})


if __name__ == "__main__":
    unittest.main()
