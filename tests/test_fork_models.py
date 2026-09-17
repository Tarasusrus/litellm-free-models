"""fork/models.yaml against Google AI Studio's free tier.

The dashboard (https://aistudio.google.com/rate-limit, read 2026-09-17)
gives per-model, per-project budgets. A deployment whose `rpm`/`tpm`
exceeds them makes LiteLLM send requests Google will refuse; two
deployments that resolve to the same model share one budget. Both
invariants are cheap to pin and expensive to discover in production.
"""
import unittest
from collections import defaultdict

from tests._loader import REPO_ROOT, load_script

rc = load_script("render-config.py")
onboard = load_script("onboard.py")

FORK_MODELS = REPO_ROOT / "fork" / "models.yaml"

# Free tier, per model (RPM, TPM). Aliases map onto the model they resolve
# to (`modelVersion` in a generateContent response).
FREE_TIER = {
    "gemini-3.5-flash-lite": (15, 250_000),
    "gemini-3.1-flash-lite": (15, 250_000),
    "gemini-2.5-flash-lite": (10, 250_000),
    "gemini-2.5-flash": (5, 250_000),
    "gemini-3-flash-preview": (5, 250_000),
    "gemini-3.5-flash": (5, 250_000),
    "gemini-3.6-flash": (5, 250_000),
    "gemini-3.7-flash": (5, 250_000),
    "gemini-3.8-flash": (5, 250_000),
}
ALIASES = {
    "gemini-flash-lite-latest": "gemini-3.5-flash-lite",
    "gemini-flash-latest": "gemini-3.5-flash",
}


def _fork_blocks():
    lines = ["model_list:\n"] + FORK_MODELS.read_text(encoding="utf-8").splitlines(keepends=True)
    _, _, blocks = rc.parse_blocks(lines)
    return blocks


def _param(block, key):
    for ln in block["lines"]:
        s = ln.strip()
        if s.startswith(f"{key}:"):
            return int(s.split(":", 1)[1].split("#", 1)[0].strip())
    raise AssertionError(f"{key} missing in {block['model_id']}")


class TestGeminiBudgets(unittest.TestCase):
    def setUp(self):
        self.gemini = [b for b in _fork_blocks() if b["provider"] == "google-ai"]
        self.assertTrue(self.gemini)

    def _resolved(self, block) -> str:
        native = block["model_id"].split("/", 1)[1]
        return ALIASES.get(native, native)

    def test_every_gemini_deployment_has_a_known_budget(self):
        for b in self.gemini:
            with self.subTest(model=b["model_id"]):
                self.assertIn(self._resolved(b), FREE_TIER)

    def test_tpm_never_exceeds_the_dashboard(self):
        for b in self.gemini:
            with self.subTest(model=b["model_id"]):
                self.assertLessEqual(_param(b, "tpm"), FREE_TIER[self._resolved(b)][1])

    def test_rpm_summed_per_resolved_model_fits_the_dashboard(self):
        per_model = defaultdict(int)
        for b in self.gemini:
            per_model[self._resolved(b)] += _param(b, "rpm")
        for model, rpm in per_model.items():
            with self.subTest(model=model):
                self.assertLessEqual(rpm, FREE_TIER[model][0])

    def test_flash_tier_is_not_deployed(self):
        """20 requests a day: never a chain member (see fork/models.yaml)."""
        for b in self.gemini:
            with self.subTest(model=b["model_id"]):
                self.assertGreaterEqual(FREE_TIER[self._resolved(b)][0], 10)


class TestOnboardingTreatsGeminiAsRequired(unittest.TestCase):
    def test_gemini_key_is_not_optional(self):
        self.assertNotIn("GEMINI_API_KEY", onboard.EMPTY_IS_OK)

    def test_hint_does_not_claim_no_deployment(self):
        hint = next(h for var, _n, _u, h in onboard.PROVIDER_KEYS if var == "GEMINI_API_KEY")
        self.assertNotIn("no active deployment", hint)


if __name__ == "__main__":
    unittest.main()
