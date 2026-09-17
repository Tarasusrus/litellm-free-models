"""Invariants of the settings page (fork/settings_ui.py).

The page edits the operator's `.env` and restarts the proxy, so the
properties worth pinning are the ones whose violation costs a key or
leaks one: an apply either lands completely or leaves the file untouched,
no line the page does not own is altered, the file stays private (0600),
and no secret ever appears in an API response or an error message. The
provider list is derived from providers_config.py, never typed twice.
"""
import json
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from fork import settings_ui, standard
from providers_config import PROVIDERS
import onboard

PROVIDER_VARS = sorted(settings_ui.EDITABLE_VARS)

# Values the page accepts: printable ASCII without whitespace, quotes,
# backslash or '#', i.e. what an API key or URL is made of.
value_chars = st.sampled_from(sorted(set(map(chr, range(33, 127))) - set("\"'#\\")))
values = st.text(value_chars, min_size=0, max_size=48)
secrets_ = st.text(value_chars, min_size=8, max_size=48)

# Lines the page must never touch: comments, blanks and foreign variables.
foreign_lines = st.lists(
    st.one_of(
        st.just(""),
        st.builds(lambda s: "# " + s, st.text(value_chars, max_size=30)),
        st.builds(lambda k, v: f"{k}={v}",
                  st.from_regex(r"\A[A-Z][A-Z0-9_]{0,15}\Z").filter(
                      lambda k: k not in settings_ui.EDITABLE_VARS),
                  values),
    ),
    max_size=8,
)

updates_ = st.dictionaries(st.sampled_from(PROVIDER_VARS), values, max_size=6)


def _tmp_env(text: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / ".env"
    p.write_text(text, encoding="utf-8")
    return p


# ─── .env writing ───────────────────────────────────────────────────────────

class TestWriteEnv(unittest.TestCase):
    @settings(max_examples=80, deadline=None)
    @given(foreign=foreign_lines, present=updates_, updates=updates_)
    def test_updates_land_and_foreign_lines_survive_verbatim(self, foreign, present, updates):
        lines = list(foreign) + [f"{k}={v}" for k, v in present.items()]
        path = _tmp_env("\n".join(lines) + "\n")
        before = path.read_text(encoding="utf-8").splitlines()

        settings_ui.write_env(path, updates)

        after = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([ln for ln in after if not settings_ui.owned_line(ln)],
                         [ln for ln in before if not settings_ui.owned_line(ln)])
        env = settings_ui.load_env(path)
        for k, v in {**present, **updates}.items():
            self.assertEqual(env.get(k, ""), v)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [".env"])

    @settings(max_examples=40, deadline=None)
    @given(foreign=foreign_lines, updates=updates_)
    def test_failed_rename_leaves_file_and_no_temp(self, foreign, updates):
        original = "\n".join(foreign) + "\n"
        path = _tmp_env(original)
        with mock.patch.object(settings_ui.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                settings_ui.write_env(path, updates)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [".env"])

    @settings(max_examples=40, deadline=None)
    @given(var=st.sampled_from(PROVIDER_VARS),
           bad=st.text(st.sampled_from(list("\"'#\\ \t\n")), min_size=1, max_size=3),
           good=values)
    def test_rejected_value_never_touches_the_file(self, var, bad, good):
        original = f"{var}=before\n"
        path = _tmp_env(original)
        with self.assertRaises(ValueError):
            settings_ui.write_env(path, {var: good + bad})
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    @settings(max_examples=30, deadline=None)
    @given(key=st.from_regex(r"\A[A-Z][A-Z0-9_]{0,15}\Z").filter(
        lambda k: k not in settings_ui.EDITABLE_VARS), value=values)
    def test_unknown_variable_is_rejected(self, key, value):
        path = _tmp_env("")
        with self.assertRaises(ValueError):
            settings_ui.write_env(path, {key: value})
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_owned_line_recognises_provider_vars_only(self):
        self.assertTrue(settings_ui.owned_line("GROQ_API_KEY=x"))
        self.assertTrue(settings_ui.owned_line("  GROQ_API_KEY = x"))
        self.assertFalse(settings_ui.owned_line("GROQ_API_KEY_2=x"))
        self.assertFalse(settings_ui.owned_line("# GROQ_API_KEY=x"))
        self.assertFalse(settings_ui.owned_line("POSTGRES_PASSWORD=x"))


# ─── masking ────────────────────────────────────────────────────────────────

class TestMask(unittest.TestCase):
    @settings(max_examples=100)
    @given(value=st.text(value_chars, min_size=1, max_size=64))
    def test_mask_never_contains_the_value(self, value):
        masked = settings_ui.mask(value)
        self.assertNotIn(value, masked)
        self.assertLessEqual(len(masked), 8)

    def test_empty_masks_to_empty(self):
        self.assertEqual(settings_ui.mask(""), "")

    @settings(max_examples=60)
    @given(text=st.text(max_size=40), secret=secrets_)
    def test_scrub_removes_every_occurrence(self, text, secret):
        out = settings_ui.scrub(text + secret + text + secret, [secret, ""])
        self.assertNotIn(secret, out)


# ─── provider list ──────────────────────────────────────────────────────────

class TestProviderList(unittest.TestCase):
    def setUp(self):
        self.rows = settings_ui.providers()

    def test_same_set_as_providers_config(self):
        self.assertEqual(sorted(r["name"] for r in self.rows), sorted(PROVIDERS))

    def test_ordered_by_standard_priority(self):
        self.assertEqual([r["name"] for r in self.rows], list(standard.PROVIDER_PRIORITY))

    def test_every_row_has_env_var_display_and_console_url(self):
        for r in self.rows:
            with self.subTest(provider=r["name"]):
                self.assertEqual(r["env_var"], PROVIDERS[r["name"]].env_var)
                self.assertEqual(r["api_base_env"], PROVIDERS[r["name"]].api_base_env)
                self.assertTrue(r["display"])
                self.assertTrue(r["console_url"].startswith("https://"))

    def test_editable_vars_cover_every_provider_variable(self):
        expected = {p.env_var for p in PROVIDERS.values() if p.env_var}
        expected |= {p.api_base_env for p in PROVIDERS.values() if p.api_base_env}
        self.assertEqual(set(settings_ui.EDITABLE_VARS), expected)


# ─── live key check (HTTP mocked) ───────────────────────────────────────────

class TestKeyCheck(unittest.TestCase):
    @settings(max_examples=40, deadline=None)
    @given(key=secrets_, n=st.integers(0, 40))
    def test_ok_reports_model_count(self, key, n):
        assume(not onboard.is_placeholder(key))
        data = {"data": [{"id": f"m{i}"} for i in range(n)]}
        checker = settings_ui.KeyChecker()
        with mock.patch.object(settings_ui.fsm, "http_get_json", return_value=data):
            res = checker.check("groq", {"GROQ_API_KEY": key})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["models"], n)

    @settings(max_examples=40, deadline=None)
    @given(key=secrets_, noise=st.text(max_size=20))
    def test_failure_message_never_contains_the_key(self, key, noise):
        assume(not onboard.is_placeholder(key))
        checker = settings_ui.KeyChecker()
        err = urllib.error.HTTPError("https://x/?key=" + key, 401, noise + key, {}, None)
        with mock.patch.object(settings_ui.fsm, "http_get_json", side_effect=err):
            res = checker.check("google-ai", {"GEMINI_API_KEY": key})
        self.assertEqual(res["status"], "fail")
        self.assertNotIn(key, json.dumps(res))

    @settings(max_examples=30, deadline=None)
    @given(key=secrets_)
    def test_result_is_cached_per_key_value(self, key):
        assume(not onboard.is_placeholder(key))
        assume(not onboard.is_placeholder(key + "x"))
        checker = settings_ui.KeyChecker()
        data = {"data": [{"id": "a"}]}
        with mock.patch.object(settings_ui.fsm, "http_get_json", return_value=data) as get:
            checker.check("groq", {"GROQ_API_KEY": key})
            checker.check("groq", {"GROQ_API_KEY": key})
            self.assertEqual(get.call_count, 1)
            checker.check("groq", {"GROQ_API_KEY": key + "x"})
            self.assertEqual(get.call_count, 2)

    def test_missing_key_is_not_a_network_call(self):
        checker = settings_ui.KeyChecker()
        with mock.patch.object(settings_ui.fsm, "http_get_json") as get:
            res = checker.check("groq", {})
        self.assertEqual(res["status"], "missing")
        get.assert_not_called()

    def test_placeholder_value_is_not_a_network_call(self):
        checker = settings_ui.KeyChecker()
        with mock.patch.object(settings_ui.fsm, "http_get_json") as get:
            res = checker.check("groq", {"GROQ_API_KEY": "gsk_your-groq-api-key-here"})
        self.assertEqual(res["status"], "placeholder")
        get.assert_not_called()

    def test_provider_without_catalogue_query(self):
        checker = settings_ui.KeyChecker()
        res = checker.check("elevenlabs", {"ELEVENLABS_API_KEY": "abc"})
        self.assertEqual(res["status"], "unsupported")


# ─── chain from the container log ───────────────────────────────────────────

model_ids = st.from_regex(r"\A[a-z0-9]+/[A-Za-z0-9.\-]+\Z")
chain_entries = st.lists(st.tuples(st.sampled_from(list(standard.PROVIDER_PRIORITY)), model_ids),
                         max_size=12)
noise_lines = st.lists(st.from_regex(r"\A[A-Za-z][A-Za-z0-9 :.\-]{0,40}\Z"), max_size=6)


def _frames(text: str, chunk: int) -> bytes:
    """Docker's multiplexed log stream: 8-byte header + payload per frame."""
    raw = text.encode("utf-8")
    out = b""
    for i in range(0, len(raw), chunk):
        payload = raw[i:i + chunk]
        out += b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
    return out


class TestChainFromLog(unittest.TestCase):
    @settings(max_examples=60, deadline=None)
    @given(before=noise_lines, entries=chain_entries, after=noise_lines,
           stale=chain_entries, chunk=st.integers(1, 64))
    def test_last_printed_chain_is_recovered(self, before, entries, after, stale, chunk):
        def block(items):
            lines = [f"'standard' route: {len(items)} deployment(s) in priority order"]
            lines += [f"  {n:>2}. {prov:<13} {mid}" for n, (prov, mid) in enumerate(items, 1)]
            return lines
        log = "\n".join(before + block(stale) + after + block(entries) + after) + "\n"
        chain = settings_ui.parse_chain(settings_ui._demux(_frames(log, chunk)))
        self.assertEqual([(c["order"], c["provider"], c["model"]) for c in chain],
                         [(n, p, m) for n, (p, m) in enumerate(entries, 1)])

    def test_no_route_header_means_no_chain(self):
        self.assertEqual(settings_ui.parse_chain("  1. groq groq/x\nsomething\n"), [])


# ─── HTTP API ───────────────────────────────────────────────────────────────

class _Server:
    def __init__(self, env_text: str):
        self.env_path = _tmp_env(env_text)
        self.restarts: list[str] = []
        self.app = settings_ui.App(
            env_path=self.env_path,
            checker=settings_ui.KeyChecker(),
            restart=lambda: self.restarts.append("restart"),
            proxy=lambda master, wait: {"ready": True, "models": ["standard"], "chain": []},
        )
        self.httpd = settings_ui.make_server("127.0.0.1", 0, self.app)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method: str, path: str, body=None, key=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if key is not None:
            req.add_header("Authorization", f"Bearer {key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestHttpApi(unittest.TestCase):
    MASTER = "sk-master-0123456789abcdef"

    def setUp(self):
        self.srv = _Server(f"LITELLM_MASTER_KEY={self.MASTER}\nPOSTGRES_PASSWORD=pgsecret\n"
                           "GROQ_API_KEY=gsk_live_0123456789\n")

    def tearDown(self):
        self.srv.close()

    def test_page_is_public_but_api_needs_the_master_key(self):
        status, body = self.srv.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())
        self.assertEqual(self.srv.request("GET", "/api/providers")[0], 401)
        self.assertEqual(self.srv.request("GET", "/api/providers", key="wrong")[0], 401)
        self.assertEqual(self.srv.request("GET", "/api/providers", key=self.MASTER)[0], 200)

    def test_provider_listing_is_masked(self):
        status, body = self.srv.request("GET", "/api/providers", key=self.MASTER)
        self.assertEqual(status, 200)
        self.assertNotIn("gsk_live_0123456789", body)
        self.assertNotIn("pgsecret", body)
        self.assertNotIn(self.MASTER, body)
        rows = {r["name"]: r for r in json.loads(body)["providers"]}
        self.assertEqual(rows["groq"]["state"], "ok")
        self.assertEqual(rows["cohere"]["state"], "empty")

    def test_apply_writes_env_and_restarts_once(self):
        status, body = self.srv.request(
            "POST", "/api/apply", {"values": {"COHERE_API_KEY": "co-abc123"}}, key=self.MASTER)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.srv.restarts, ["restart"])
        env = settings_ui.load_env(self.srv.env_path)
        self.assertEqual(env["COHERE_API_KEY"], "co-abc123")
        self.assertEqual(env["GROQ_API_KEY"], "gsk_live_0123456789")
        self.assertEqual(env["POSTGRES_PASSWORD"], "pgsecret")
        self.assertNotIn("co-abc123", body)

    def test_apply_rejects_bad_value_without_restart(self):
        status, _ = self.srv.request(
            "POST", "/api/apply", {"values": {"COHERE_API_KEY": "has space"}}, key=self.MASTER)
        self.assertEqual(status, 400)
        self.assertEqual(self.srv.restarts, [])

    def test_apply_rejects_master_key_edit(self):
        status, _ = self.srv.request(
            "POST", "/api/apply", {"values": {"LITELLM_MASTER_KEY": "x"}}, key=self.MASTER)
        self.assertEqual(status, 400)
        self.assertEqual(settings_ui.load_env(self.srv.env_path)["LITELLM_MASTER_KEY"], self.MASTER)

    def test_check_uses_the_stored_key_and_masks_errors(self):
        err = urllib.error.HTTPError("https://api.groq.com", 401, "bad gsk_live_0123456789", {}, None)
        with mock.patch.object(settings_ui.fsm, "http_get_json", side_effect=err):
            status, body = self.srv.request("POST", "/api/check", {"provider": "groq"}, key=self.MASTER)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "fail")
        self.assertNotIn("gsk_live_0123456789", body)

    def test_unknown_provider_check_is_404(self):
        status, _ = self.srv.request("POST", "/api/check", {"provider": "nope"}, key=self.MASTER)
        self.assertEqual(status, 404)

