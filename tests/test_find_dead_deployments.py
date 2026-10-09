"""Tests for tools/find-dead-deployments.py (pure parts, no network).

The script proposes EXCLUDED entries for the `standard` route. Its one job
that can hurt is the split between "dead for good" and "will heal": a rate
limit taken for a dead model would strip a live model from the chain. So
the classifier is checked on the error texts providers really returned
(live /health, 2026-10-09), and the candidate list on generated reports.
"""
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from fork import standard
from tests._loader import load_script

dead = load_script("tools/find-dead-deployments.py")

NVIDIA = "https://integrate.api.nvidia.com/v1"

# (error text as /health returned it, shortened; expected kind)
OBSERVED: list[tuple[str, str]] = [
    ("litellm.APIError: APIError: OpenAIException - Error code: 410 - {'title': 'Gone', 'status': 410, "
     "'detail': \"The model 'openai/gpt-oss-120b' has reached its end of life\"} LiteLLM Retried: 1 times",
     "permanent"),
    ("litellm.NotFoundError: GroqException - {\"error\":{\"message\":\"The model `qwen/qwen3.6-27b` does not "
     "exist or you do not have access to it.\"", "permanent"),
    ("litellm.NotFoundError: NotFoundError: OpenrouterException - {\"error\":{\"message\":\"This model is "
     "unavailable for free. The paid version is available now", "permanent"),
    ("litellm.NotFoundError: NotFoundError: OpenrouterException - {\"error\":{\"message\":\"No endpoints found "
     "for nvidia/nemotron-nano-9b-v2:free.\",\"code\":404}", "permanent"),
    ("litellm.APIError: APIError: OpenrouterException - {\"error\":{\"message\":\"Insufficient credits. This "
     "account never purchased credits.", "permanent"),
    ("litellm.APIError: APIError: MistralException - {\"message\":\"This model is not available in your "
     "subscription tier\",\"type\":\"tier_not_allowed\",\"raw_status_code\":403}", "permanent"),
    ("litellm.AuthenticationError: AuthenticationError: OpenAIException - Your API key is invalid, expired, "
     "or revoked.", "permanent"),
    ("litellm.NotFoundError: NotFoundError: OpenAIException - Error code: 404 - {'status': 404, 'title': "
     "'Not Found', 'detail': \"Function 'ee47df99' not found\"}", "permanent"),
    ("litellm.RateLimitError: geminiException - {\"error\": {\"code\": 429, \"message\": \"You exceeded your "
     "current quota, please check your plan and billing details.\"", "transient"),
    ("litellm.RateLimitError: RateLimitError: OpenrouterException - {\"error\":{\"message\":\"Rate limit "
     "exceeded: free-models-per-day. Add 10 credits to unlock", "transient"),
    ("litellm.InternalServerError: GeminiException InternalServerError - {\"error\": {\"code\": 500, "
     "\"message\": \"Internal error encountered.\"", "transient"),
    ("litellm.RateLimitError: RateLimitError: ZaiException - The service may be temporarily overloaded, "
     "please try again later", "transient"),
    ("litellm.RateLimitError: RateLimitError: OpenAIException - Too many concurrent requests for this "
     "client. Retry after 10 seconds.", "transient"),
    ("Timeout exceeded", "transient"),
    ("litellm.ServiceUnavailableError: ServiceUnavailableError: OpenAIException - Service temporarily "
     "unavailable", "transient"),
]


class TestClassify(unittest.TestCase):
    def test_observed_provider_errors(self):
        for text, kind in OBSERVED:
            with self.subTest(text=text[:60]):
                self.assertEqual(dead.classify(text)[0], kind)

    @settings(max_examples=200, deadline=None)
    @given(st.sampled_from([t for t, k in OBSERVED if k == "transient"]),
           st.sampled_from([t for t, k in OBSERVED if k == "permanent"]))
    def test_a_permanent_marker_wins_over_retry_words_in_the_same_text(self, transient, permanent):
        self.assertEqual(dead.classify(f"{transient} ... {permanent}")[0], "permanent")

    def test_unrecognised_text_is_not_a_candidate_kind(self):
        self.assertEqual(dead.classify("something new")[0], "unknown")


# ─── candidates() on generated /health reports ──────────────────────────────

TEXT_MODELS = ["openai/x", "groq/y", "gemini/z"]
NOT_TEXT_MODELS = ["gemini/lyria-3-pro-preview", "openai/meta/llama-guard-4-12b",
                   "openrouter/nvidia/nemotron-3.5-content-safety:free"]
ERRORS = [t for t, _ in OBSERVED] + ["something new"]

endpoint_st = st.fixed_dictionaries({
    "model": st.sampled_from(TEXT_MODELS + NOT_TEXT_MODELS),
    "api_base": st.sampled_from([None, "", NVIDIA]),
    "error": st.sampled_from(ERRORS),
})
report_st = st.fixed_dictionaries({
    "healthy_endpoints": st.lists(endpoint_st.map(lambda e: {k: v for k, v in e.items() if k != "error"}),
                                  max_size=8),
    "unhealthy_endpoints": st.lists(endpoint_st, max_size=8),
})


def _name(ep):
    return standard.deployment_name(ep["model"], ep.get("api_base") or "")


class TestCandidates(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(report_st, st.data())
    def test_candidates_are_exactly_the_dead_and_the_non_text_not_yet_excluded(self, report, data):
        all_names = sorted({_name(e) for e in report["healthy_endpoints"] + report["unhealthy_endpoints"]})
        already = set(data.draw(st.lists(st.sampled_from(all_names), unique=True))) if all_names else set()
        found, _ = dead.candidates(report, {n: "x" for n in already}, "2026-10-09")

        expected = set()
        for ep in report["unhealthy_endpoints"]:
            if dead.classify(ep["error"])[0] == "permanent" or dead.not_text_reason(ep["model"]):
                expected.add(_name(ep))
        for ep in report["healthy_endpoints"]:
            if dead.not_text_reason(ep["model"]):
                expected.add(_name(ep))
        self.assertEqual(set(found), expected - already)
        for value in found.values():
            self.assertTrue(value.startswith("2026-10-09 "))

    def test_a_rate_limited_text_model_is_reported_but_never_a_candidate(self):
        ep = {"model": "gemini/gemini-3.5-flash-lite", "api_base": None,
              "error": OBSERVED[8][0]}
        found, other = dead.candidates({"unhealthy_endpoints": [ep]}, {}, "2026-10-09")
        self.assertEqual(found, {})
        self.assertEqual(len(other), 1)

    def test_endpoint_key_is_the_excluded_spelling(self):
        ep = {"model": "openai/openai/gpt-oss-120b", "api_base": NVIDIA}
        self.assertIn(dead.endpoint_key(ep), standard.EXCLUDED)
        self.assertEqual(dead.endpoint_key({"model": "groq/qwen/qwen3.6-27b", "api_base": None}),
                         "groq/qwen/qwen3.6-27b")


if __name__ == "__main__":
    unittest.main()
