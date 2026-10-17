"""tools/smoke-vision.py decides which deployments enter VISION_VERIFIED:
its pass/fail check must accept only the correct total of the fixture."""
import base64
import unittest

from tests._loader import REPO_ROOT, load_script

smoke = load_script("tools/smoke-vision.py")


class TestCheckAnswer(unittest.TestCase):
    def test_accepts_the_fixture_total_in_any_wrapping(self):
        for content in ['{"total": 247.5, "currency": "THB"}',
                        '```json\n{"total": "247.50", "currency": "thb"}\n```',
                        'Here: {"total": 247.50, "currency": "THB"} done']:
            self.assertEqual(smoke.check_answer(content), (True, ""), content)

    def test_rejects_wrong_or_missing_values(self):
        for content in ['{"total": 300, "currency": "THB"}',      # read CASH, not TOTAL
                        '{"total": 247.5, "currency": "USD"}',
                        '{"total": "n/a", "currency": "THB"}',
                        "I cannot see images.",
                        None, ""]:
            ok, reason = smoke.check_answer(content)
            self.assertFalse(ok, content)
            self.assertTrue(reason)


class TestBuildRequest(unittest.TestCase):
    def test_image_is_sent_as_png_data_url_uncached(self):
        req = smoke.build_request("vision", b"\x89PNGdata", "n1", no_fallback=True)
        parts = req["messages"][0]["content"]
        self.assertEqual([p["type"] for p in parts], ["text", "image_url"])
        url = parts[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), b"\x89PNGdata")
        self.assertIn("n1", parts[0]["text"])
        self.assertEqual(req["fallbacks"], [])
        self.assertEqual(req["cache"], {"no-cache": True})

    def test_fixture_exists_and_is_png(self):
        self.assertTrue(smoke.FIXTURE.read_bytes().startswith(b"\x89PNG"))
        self.assertTrue(str(smoke.FIXTURE).startswith(str(REPO_ROOT)))


if __name__ == "__main__":
    unittest.main()
