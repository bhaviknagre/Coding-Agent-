import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import run
from tests.test_box import git

ROOT = Path(__file__).parent.parent
# Lint, types and Docker facts are not what is tested here.
FAKE_DOCKER = "#!/bin/sh\n[ \"$1\" = ps ] || echo fixed\n"


class Check(unittest.TestCase):
    """A copy of ./check in a small project whose only test counts how often it runs."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name)
        self.repo = self.home / "repo"
        for name, text in {
            "repo/tests/__init__.py": "", "repo/agent/__init__.py": "", "repo/agent/box.py": "def clean_leftovers(): pass\n",  # noqa: E501
            "repo/agent/scan.py": f"open({str(self.home / 'scans')!r}, 'a').write('x')\n", "repo/Dockerfile": "x",
            "repo/tests/test_a.py": "import unittest\nclass A(unittest.TestCase):\n    def test_a(self):\n"
            f"        open({str(self.home / 'runs')!r}, 'a').write('x')\n",
            "bin/docker": FAKE_DOCKER,
        }.items():
            (self.home / name).parent.mkdir(parents=True, exist_ok=True)
            (self.home / name).write_text(text)
        (self.home / "bin/docker").chmod(0o755)
        shutil.copy(ROOT / "check", self.repo / "check")
        shutil.copy(ROOT / "tests/run.py", self.repo / "tests/run.py")
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".git/hooks").mkdir(exist_ok=True)
        (self.repo / ".git/hooks/pre-commit").symlink_to("../../check")

    def env(self):
        keep = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return keep | {"PATH": f"{self.home / 'bin'}:{os.environ['PATH']}"}

    def check(self, hook=False):
        program = ".git/hooks/pre-commit" if hook else "./check"
        p = subprocess.run([program], cwd=self.repo, env=self.env(), capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def runs(self, name="runs"):
        path = self.home / name
        return len(path.read_text()) if path.exists() else 0

    def prepare(self):
        git(self.repo, "add", "-A")
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.runs(), 1)

    def test_the_hook_does_not_repeat_but_check_always_runs(self):
        self.prepare()
        code, text = self.check(hook=True)
        self.assertEqual((code, self.runs(), "passed before" in text), (0, 1, True), text)
        self.assertEqual(self.runs("scans"), 2)  # the secret scan is never skipped
        self.assertEqual((self.check()[0], self.runs()), (0, 2))

    def test_any_difference_runs_again(self):
        def edit():
            with open(self.repo / "agent/box.py", "a") as f:
                f.write("#")

        for change in (
            edit, lambda: (self.repo / "new.txt").write_text("x"),
            lambda: (self.repo / "agent/box.py").chmod(0o755), lambda: (self.repo / "Dockerfile").unlink(),
            lambda: (self.home / "bin/docker").write_text("#!/bin/sh\n[ \"$1\" = ps ] || echo other\n"),
        ):
            self.tearDown()
            self.doCleanups()
            self.setUp()
            self.prepare()
            change()
            git(self.repo, "add", "-A")
            self.check(hook=True)
            self.assertGreaterEqual(self.runs() + (not (self.repo / "Dockerfile").exists()), 2)

    def test_the_hook_refuses_when_something_else_is_staged(self):
        self.prepare()
        (self.repo / "agent/box.py").write_text("bad staged content")
        git(self.repo, "add", "-A")
        (self.repo / "agent/box.py").write_text("def clean_leftovers(): pass\n")
        self.assertIn("git add -A", self.check(hook=True)[1])
        git(self.repo, "add", "-A")
        (self.repo / "new.txt").write_text("x")
        self.assertEqual(self.check(hook=True)[0], 1)

    def test_a_failure_is_not_remembered(self):
        self.prepare()
        (self.repo / "tests/test_a.py").write_text("import unittest\nclass A(unittest.TestCase):\n def test_a(self): self.fail()\n")  # noqa: E501
        git(self.repo, "add", "-A")
        for _ in "12":
            code, text = self.check(hook=True)
            self.assertEqual((code, "FAILED tests.test_a.A.test_a" in text), (1, True), text)

    def test_files_changed_during_the_tests_are_not_remembered(self):
        name = str(self.repo / "agent/box.py")
        (self.repo / "tests/test_a.py").write_text(
            f"import unittest\nclass A(unittest.TestCase):\n def test_a(self): open({name!r}, 'a').write('#')\n")
        git(self.repo, "add", "-A")
        code, text = self.check()
        self.assertEqual((code, "changed during" in text), (1, True), text)
        self.assertFalse((self.repo / ".git/coding-agent-v2-passed").exists())

    def test_a_test_that_does_not_run_is_a_failure(self):
        for text in ("import unittest, os\nclass A(unittest.TestCase):\n def test_a(self): os._exit(0)\n",
                     "import unittest\nclass A(unittest.TestCase):\n def test_a(self): self.skipTest('x')\n",
                     "import nothing_like_this\n", ""):
            (self.repo / "tests/test_a.py").write_text(text)
            git(self.repo, "add", "-A")
            self.assertNotEqual(self.check()[0], 0, text)

    def test_every_test_has_exactly_one_job(self):
        tests = list(run.flat(unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))))
        found = run.jobs(tests)
        self.assertEqual(sum(found.values()), len(tests))
        self.assertEqual(len({t.id() for t in tests}), len(tests))
        self.assertIn("tests.test_checker.Pytest", found)  # prepares an image once
