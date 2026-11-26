import contextlib
import io
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
from unittest import mock

from agent import box, demo, model
from tests.test_box import own_items

ROOT = Path(__file__).parent.parent


class Demo(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name)
        self.addCleanup(lambda: self.assertEqual(own_items(), []))

    def test_all_expected_results_without_a_model(self):
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home)}
        out = io.StringIO()
        no_model = mock.patch.object(model.FakeModel, "call", side_effect=AssertionError("a model was called"))
        with mock.patch.dict(os.environ, env), no_model, contextlib.redirect_stdout(out):
            self.assertEqual(demo.demo([]), 0, out.getvalue())
        text = out.getvalue()
        self.assertIn("The demo gave the expected results.", text)
        page = next(self.home.rglob("demo.html")).read_text()
        self.assertIn("the request matched the recording", page)
        for verdict in ("VERIFIED", "FAILED", "UNVERIFIABLE"):
            self.assertIn(f">{verdict}</td>", page)

    def test_model_test_marks_the_result(self):
        from agent.model import FakeModel

        out = io.StringIO()
        good = FakeModel(list(demo.SCRIPT))
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), contextlib.redirect_stdout(out), \
                mock.patch("agent.screen.choose_model", return_value=(good, ())):
            self.assertEqual(demo.model_test(["gemini"]), 0)
        self.assertIn("fake is TESTED", out.getvalue())
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), contextlib.redirect_stdout(out), \
                mock.patch("agent.screen.choose_model", return_value=(FakeModel([]), ())):
            self.assertEqual(demo.model_test(["gemini"]), 1)
        self.assertIn("EXPERIMENTAL", out.getvalue())
        self.assertIn('"status": "EXPERIMENTAL"', next(self.home.rglob("models.json")).read_text())

    def test_report_is_escaped(self):
        page = demo.page([("<script>x</script>", "VERIFIED", "a\x1b[2J & <b>")], 1)
        self.assertNotIn("<script>x", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("a&lt;U+001B&gt;[2J &amp; &lt;b&gt;", page)

    def test_the_recording_is_complete_and_safe(self):
        rows = demo.RECORDING.read_text().splitlines()
        self.assertEqual(len(rows), len(demo.SCRIPT))
        r = subprocess.run([sys.executable, "-m", "agent.scan"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_wrong_result_is_reported_as_wrong(self):
        out = io.StringIO()
        wrong = mock.patch.dict(demo.CHEATS, {"honest fix": demo.CHEATS["weakens the test"]})
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), wrong, contextlib.redirect_stdout(out):
            self.assertEqual(demo.demo([]), 1)
        self.assertIn("did NOT give the expected results", out.getvalue())

    def test_keep_shows_a_container_and_removes_it_after_enter(self):
        env = {**os.environ, "HOME": str(self.home), "XDG_DATA_HOME": str(self.home), "PYTHONPATH": str(ROOT)}
        env["DOCKER_HOST"] = box.docker("context", "inspect", "-f", "{{.Endpoints.docker.Host}}")
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-m", "agent", "demo", "--keep"], env)  # noqa: S606
        text, limit, seen = b"", time.time() + 400, None
        while time.time() < limit:
            if select.select([fd], [], [], 1)[0]:
                try:
                    piece = os.read(fd, 65536)
                except OSError:
                    break
                if not piece:
                    break
                text += piece
            if seen is None and b"Press Enter" in text:
                name = text.split(b"docker exec -it ")[1].split()[0].decode()
                seen = box.docker("exec", name, "sh", "-c", "cat shop.py; touch x 2>&1; echo net; "
                                  "python -c \"import socket;socket.create_connection(('1.1.1.1',53),2)\" 2>&1 | tail -1", ok=False)  # noqa: E501
                time.sleep(0.5)
                os.write(fd, b"\n")
        else:
            os.kill(pid, signal.SIGKILL)
        self.assertEqual(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), 0, text)
        os.close(fd)
        self.assertIn("max(0, base - discount)", seen)
        self.assertIn("Read-only", seen)
        self.assertIn("unreachable", seen)
        self.assertNotIn(name, box.docker("ps", "-a", "--format", "{{.Names}}"))
