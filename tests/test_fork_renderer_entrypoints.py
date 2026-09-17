"""Every path that renders a config must go through fork/render.py.

docs/adr/0001-fork-conventions.md §2 promises that the Makefile targets and
onboarding render with the fork's renderer; upstream's render-config.py
yields a config without the `standard` route, so a caller that slipped
back to it would silently ship a proxy without the fork's one feature.
"""
import re
import unittest
from unittest import mock

from tests._loader import REPO_ROOT, load_script

ob = load_script("onboard.py")


class TestMakefileRendersWithFork(unittest.TestCase):
    def test_no_target_calls_upstream_renderer(self):
        text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        offenders = [ln for ln in text.splitlines()
                     if re.search(r"python3\s+render-config\.py", ln)]
        self.assertEqual(offenders, [], "Makefile still renders with upstream render-config.py")

    def test_check_config_uses_fork_renderer(self):
        text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        body = text.split("check-config:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("fork/render.py", body)


class TestOnboardRendersWithFork(unittest.TestCase):
    def test_step_render_invokes_fork_renderer(self):
        with mock.patch.object(ob.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(ob.step_render())
        argv = run.call_args.args[0]
        self.assertEqual(str(argv[1]), str(REPO_ROOT / "fork" / "render.py"))

    def test_step_render_reports_failure(self):
        with mock.patch.object(ob.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1)
            self.assertFalse(ob.step_render())
