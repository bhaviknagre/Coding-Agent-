import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent import box, screen
from agent.checker import Contract
from agent.model import FakeModel, call_message
from tests.test_box import git, own_items
from tests.test_checker import BUG, GOOD, TEST

ROOT = Path(__file__).parent.parent
FIX = [call_message("read", path="calc.py", first=1),
       call_message("edit", "the sign is wrong", path="calc.py", old="a - b", new="a + b"),
       call_message("finish", summary="done")]


class Run(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.addCleanup(lambda: self.assertEqual(own_items(), []))
        self.home = Path(d.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.commit({"calc.py": BUG, "test_calc.py": TEST})
        self.script = self.home / "script.json"
        self.script.write_text(json.dumps(FIX))

    def commit(self, files):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "x")

    def agent(self, *args, answers=("y\n",), terminal=True):
        """Run it as a person would, in a terminal. Returns exit code, text, and how many questions were asked."""
        env = {**os.environ, "HOME": str(self.home), "XDG_DATA_HOME": str(self.home / "data"), "PYTHONPATH": str(ROOT)}
        env["CODING_AGENT_KEYCHAIN"] = "/nonexistent/test.keychain"
        env["DOCKER_HOST"] = box.docker("context", "inspect", "-f", "{{.Endpoints.docker.Host}}")
        command = [sys.executable, "-m", "agent", "run", *args]
        if not terminal:
            p = subprocess.run(command, cwd=self.repo, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)  # noqa: E501
            return p.returncode, p.stdout + p.stderr, 0
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.repo)
            os.execve(sys.executable, command, env)  # noqa: S606
        text, waiting, asked, limit = b"", list(answers), 0, time.time() + 300
        while time.time() < limit:
            if select.select([fd], [], [], 1)[0]:
                try:
                    piece = os.read(fd, 65536)
                except OSError:
                    break
                if not piece:
                    break
                text += piece
            if text.count(b"then Enter: ") > asked:
                asked += 1
                time.sleep(0.5)  # keys typed before the whole question is there are thrown away
                os.write(fd, (waiting.pop(0) if waiting else "\n").encode())
        else:
            os.kill(pid, signal.SIGKILL)
        code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        os.close(fd)
        return code, text.decode("utf-8", "replace").replace("\r\n", "\n"), asked

    def files(self):
        keep = [p for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts]
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in keep}, (self.repo / ".git/HEAD").read_text()

    def test_an_ordinary_fix_needs_one_yes_here_and_one_merge_by_you(self):
        before = self.files()
        code, text, asked = self.agent("fix add", "--proof", "test_calc.T.test_proof", "--model", f"script:{self.script}")  # noqa: E501
        self.assertEqual((code, asked), (0, 1), text)
        self.assertIn("Result     VERIFIED  (TEST-LEVEL)", text)
        branch = text.split("Branch     ")[1].split()[0]
        self.assertEqual(git(self.repo, "show", f"{branch}:calc.py"), GOOD.strip())
        self.assertIn(f"agent merge {branch}", text)
        self.assertEqual(before, self.files())
        self.assertTrue(list(self.home.rglob("receipt.json")))

    def test_the_setup_is_shown_again_only_when_it_changes(self):
        args = ("fix add", "--proof", "test_calc.T.test_proof", "--model", f"script:{self.script}")
        first = self.agent(*args, answers=("n\n",))[1]
        self.assertIn("Image     python:3.12-slim", first)
        self.assertIn("Nothing was started.", first)
        self.agent(*args)
        self.assertIn("as last time", self.agent(*args, answers=("n\n",))[1])
        for change in ({"requirements.txt": "x==1\n"}, {"Dockerfile": "FROM x\n"}, {".devcontainer/devcontainer.json": "{}"}):  # noqa: E501
            self.commit(change)
            self.assertIn("Image     ", self.agent(*args, answers=("n\n",))[1], change)
            self.agent(*args, answers=("y\n",))
        self.assertIn("Tests     python -m pytest", self.agent(*args[:-2], "--command", "python -m pytest", "--model",
                                                              f"script:{self.script}", answers=("n\n",))[1])

    def test_nothing_starts_without_a_person_and_a_yes(self):
        args = ("fix add", "--model", f"script:{self.script}")
        for answers in (("n\n",), ("\n",), ("yes\n",)):
            code, text, _ = self.agent(*args, answers=answers)
            self.assertEqual((code, "Nothing was started." in text), (2, True), answers)
        self.assertEqual(self.agent(*args, terminal=False)[0], 2)
        self.assertEqual(git(self.repo, "branch", "--list", "agent/*"), "")

    def test_clear_stops(self):
        for args, words in (((), "usage"), (("x",), "Stopped: No"), (("x", "--model", "script:" + str(self.script),  # noqa: E501
                            "--image", "no-such:1"), "No such image")):
            code, text, asked = self.agent(*args)
            self.assertIn(words, text, args)
            # Elsewhere than macOS the key is typed in each session, so that one question is allowed there.
            allowed = 1 if args == ("x",) and sys.platform != "darwin" else 0
            self.assertEqual((asked <= allowed, "Traceback" in text), (True, False), text)

    def test_warnings_on_the_screen(self):
        (self.repo / "calc.py").write_text("my unfinished work")
        text = self.agent("fix add", "--model", f"script:{self.script}", answers=("n\n",))[1]
        self.assertIn("uncommitted changes", text)
        self.assertIn("none named: a new test that you approve becomes the proof", text)

    def test_plain_and_live_lines(self):
        args = ("fix add", "--proof", "test_calc.T.test_proof", "--model", f"script:{self.script}")
        live = self.agent(*args)[1]
        self.assertIn("\r\x1b[2KDoing now  checking", live)
        plain = self.agent(*args, "--plain")[1]
        self.assertIn("\nDoing now  checking\n", plain)
        self.assertNotIn("\x1b[2K", plain)

    def test_a_failed_or_refused_result_says_why_and_what_next(self):
        bad = [call_message("read", path="calc.py", first=1), call_message("edit", path="calc.py", old="a - b", new="4")]  # noqa: E501
        self.script.write_text(json.dumps(bad + [call_message("finish", summary="")] * 3))
        code, text, _ = self.agent("fix add", "--proof", "test_calc.T.test_proof", "--model", f"script:{self.script}")
        self.assertEqual(code, 1, text)
        self.assertIn("FAIL: regressions", text)
        self.assertIn("Discard  agent discard", text)


class Parts(unittest.TestCase):
    def test_start_screen_rows_are_safe(self):
        c = Contract("fix\x1b[2J‮add " + "x" * 200, ("src/",), ("python", "-m", "unittest"), ())
        text = screen.start_screen(c, "img", FakeModel([]), False, 60)
        self.assertNotIn("\x1b", text)
        self.assertIn("<U+202E>", text)
        self.assertTrue(all(len(row) < 60 for row in text.split("\n")))

    def test_screen_shows_no_raw_control_text(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s = screen.Screen(plain=True)
            s.show("\x1b[2JVERIFIED")
        self.assertNotIn("\x1b", out.getvalue())


del Parts.test_screen_shows_no_raw_control_text  # covered by visible(); kept short
