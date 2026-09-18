"""fork/ensure_secrets.py: the internal passwords generate themselves.

The property worth pinning: POSTGRES_PASSWORD, REDIS_PASSWORD and
LITELLM_MASTER_KEY are each generated exactly when they are not already a
real value (missing, empty, or the .env.example placeholder), never
otherwise -- regenerating POSTGRES_PASSWORD on a second run would lock the
proxy out of the existing postgres-data volume. Everything else in .env
must survive byte-for-byte, and the secret files written for postgres and
redis must carry the same value .env ends up with.
"""
import stat
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fork import ensure_secrets

GEN_VARS = sorted(ensure_secrets.GENERATED)

# What a generated secret is made of (hex, or "sk-" + hex for the master
# key) is already exercised by generating it; these strategies are for the
# .env content around it. Markers mirror onboard.PLACEHOLDER_MARKERS -- a
# "real" value must not accidentally read as one of them.
value_chars = st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="\"'#\\")
_PLACEHOLDER_MARKERS = ("change-me", "change_me", "your-", "-here", "placeholder")
real_values = st.text(value_chars, min_size=4, max_size=40).filter(
    lambda v: not any(m in v.lower() for m in _PLACEHOLDER_MARKERS))
placeholder_values = st.sampled_from([
    "", "sk-your-random-master-key-here", "change-me", "CHANGE_ME",
    "your-postgres-password-here", "placeholder",
])

foreign_lines = st.lists(
    st.one_of(
        st.just(""),
        st.builds(lambda s: "# " + s, st.text(value_chars, max_size=30)),
        st.builds(lambda k, v: f"{k}={v}",
                  st.from_regex(r"\A[A-Z][A-Z0-9_]{0,15}\Z").filter(
                      lambda k: k not in ensure_secrets.GENERATED),
                  real_values),
    ),
    max_size=8,
)


def _tmp_env(text: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / ".env"
    p.write_text(text, encoding="utf-8")
    return p


class TestNeedsGeneration(unittest.TestCase):
    @settings(max_examples=30, deadline=None)
    @given(value=placeholder_values)
    def test_placeholder_or_empty_needs_generation(self, value):
        self.assertTrue(ensure_secrets.needs_generation(value))

    @settings(max_examples=60, deadline=None)
    @given(value=real_values)
    def test_real_value_does_not_need_generation(self, value):
        self.assertFalse(ensure_secrets.needs_generation(value))


class TestEnsure(unittest.TestCase):
    @settings(max_examples=40, deadline=None)
    @given(present=st.dictionaries(st.sampled_from(GEN_VARS), real_values))
    def test_only_missing_vars_are_generated(self, present):
        generated = ensure_secrets.ensure(dict(present))
        self.assertEqual(set(generated), set(GEN_VARS) - set(present))

    def test_all_three_generated_from_empty_env(self):
        generated = ensure_secrets.ensure({})
        self.assertEqual(set(generated), set(GEN_VARS))

    def test_generated_values_are_distinct_and_well_formed(self):
        generated = ensure_secrets.ensure({})
        self.assertEqual(len(set(generated.values())), len(generated))
        self.assertTrue(generated["LITELLM_MASTER_KEY"].startswith("sk-"))
        for var in ("POSTGRES_PASSWORD", "REDIS_PASSWORD"):
            self.assertRegex(generated[var], r"\A[0-9a-f]{32}\Z")


# ─── writing .env ────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    @settings(max_examples=30, deadline=None)
    @given(foreign=foreign_lines)
    def test_first_run_generates_all_three_and_keeps_foreign_lines(self, foreign):
        path = _tmp_env("\n".join(foreign) + "\n")
        before = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                  if not any(ln.strip().startswith(f"{v}=") for v in GEN_VARS)]

        env = ensure_secrets.load(path)
        generated = ensure_secrets.ensure(env)
        ensure_secrets.write_updates(path, generated)

        after = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([ln for ln in after
                          if not any(ln.strip().startswith(f"{v}=") for v in GEN_VARS)], before)
        env2 = ensure_secrets.load(path)
        for var, value in generated.items():
            self.assertEqual(env2[var], value)

    @settings(max_examples=30, deadline=None)
    @given(values=st.dictionaries(st.sampled_from(GEN_VARS), real_values,
                                  min_size=len(GEN_VARS), max_size=len(GEN_VARS)))
    def test_second_run_changes_nothing(self, values):
        path = _tmp_env("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")

        env1 = ensure_secrets.load(path)
        ensure_secrets.write_updates(path, ensure_secrets.ensure(env1))
        first = path.read_text(encoding="utf-8")

        env2 = ensure_secrets.load(path)
        second_generated = ensure_secrets.ensure(env2)
        ensure_secrets.write_updates(path, second_generated)
        second = path.read_text(encoding="utf-8")

        self.assertEqual(second_generated, {})
        self.assertEqual(first, second)
        for var, value in values.items():
            self.assertEqual(ensure_secrets.load(path)[var], value)

    def test_secret_files_carry_the_env_value(self):
        path = _tmp_env("")
        with tempfile.TemporaryDirectory() as d:
            secrets_dir = Path(d)
            env = ensure_secrets.load(path)
            generated = ensure_secrets.ensure(env)
            ensure_secrets.write_updates(path, generated)
            env.update(generated)
            for var, filename in ensure_secrets.SECRET_FILES.items():
                ensure_secrets.write_secret_file(secrets_dir / filename, env[var])
                self.assertEqual((secrets_dir / filename).read_text(encoding="utf-8"), env[var])

    def test_env_file_stays_owner_only(self):
        path = _tmp_env("")
        ensure_secrets.write_updates(path, ensure_secrets.ensure({}))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
