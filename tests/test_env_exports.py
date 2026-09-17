"""fork/env_exports.py: provider keys travel from `.env` into the proxy process.

Compose interpolates `GROQ_API_KEY=${GROQ_API_KEY}` once, when the
container is created; a plain `docker compose restart` keeps the old
values. The rendered config resolves keys through `os.environ/...`, so the
entrypoint must export the current `.env` values itself before LiteLLM
starts. These tests pin two things: the shell sees exactly the value that
is in the file, whatever characters it holds, and nothing but provider
variables is exported.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fork import env_exports
from providers_config import PROVIDERS

PROVIDER_VARS = sorted(
    {p.env_var for p in PROVIDERS.values() if p.env_var}
    | {p.api_base_env for p in PROVIDERS.values() if p.api_base_env}
)

# Anything printable except control characters (a .env value is one line)
# and the quote characters that upstream's load_env strips from the ends;
# leading/trailing whitespace is stripped by that parser too, so values
# here carry none.
value_chars = st.characters(min_codepoint=32, max_codepoint=0x2FF,
                            blacklist_categories=("Cc", "Cs"),
                            blacklist_characters="'\"")
values = st.text(value_chars, min_size=1, max_size=40).filter(lambda v: v == v.strip())


def _shell_sees(env_text: str, var: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        env_file = Path(d) / ".env"
        env_file.write_text(env_text, encoding="utf-8")
        script = env_exports.exports(env_exports.load(env_file))
        out = subprocess.run(
            ["sh", "-c", f'eval "$1"; printf %s "${var}"', "sh", script],
            capture_output=True, text=True, check=True,
        )
        return out.stdout


class TestExportsRoundTrip(unittest.TestCase):
    @settings(max_examples=60, deadline=None)
    @given(var=st.sampled_from(PROVIDER_VARS), value=values)
    def test_shell_sees_the_file_value(self, var, value):
        self.assertEqual(_shell_sees(f"{var}={value}\n", var), value)

    @settings(max_examples=40, deadline=None)
    @given(var=st.sampled_from(PROVIDER_VARS), value=values,
           secret=st.text(value_chars, min_size=1, max_size=20))
    def test_only_provider_vars_are_exported(self, var, value, secret):
        text = f"POSTGRES_PASSWORD={secret}\nLITELLM_MASTER_KEY={secret}\n{var}={value}\n"
        script = env_exports.exports(env_exports.load(Path(self._write(text))))
        self.assertNotIn("POSTGRES_PASSWORD", script)
        self.assertNotIn("LITELLM_MASTER_KEY", script)
        self.assertIn(f"export {var}=", script)

    def test_empty_file_exports_nothing(self):
        self.assertEqual(env_exports.exports({}), "")

    def _write(self, text: str) -> str:
        d = tempfile.mkdtemp()
        p = Path(d) / ".env"
        p.write_text(text, encoding="utf-8")
        return str(p)


class TestEveryProviderVarIsExported(unittest.TestCase):
    def test_set_of_exported_vars(self):
        self.assertEqual(sorted(env_exports.PROVIDER_VARS), PROVIDER_VARS)
