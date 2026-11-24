import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import __main__, doctor


class Doctor(unittest.TestCase):
    def test_missing_program_is_a_failure_not_a_crash(self):
        self.assertEqual(doctor.run("no-such-program-xyz"), (127, ""))

    def test_hang_is_a_failure(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 15)):
            self.assertEqual(doctor.run("x"), (127, ""))

    def test_docker_missing_and_stopped(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(doctor.docker().state, "STOP")
        with mock.patch("shutil.which", return_value="/x"), mock.patch.object(doctor, "run", return_value=(1, "")):
            self.assertIn("Open Docker", doctor.docker().next_step)

    def test_sqlite_versions(self):
        want = {(3, 53, 4): "OK", (3, 51, 2): "WARN", (3, 51, 3): "OK", (3, 50, 7): "OK", (3, 50, 6): "WARN",
                (3, 44, 6): "OK", (3, 45, 0): "WARN"}
        self.assertEqual({v: doctor.sqlite(v).state for v in want}, want)

    def test_low_space_stops(self):
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=7.9e9)):
            self.assertEqual(doctor.space().state, "STOP")

    def test_key_check_never_asks_for_the_secret(self):
        with mock.patch("sys.platform", "darwin"), mock.patch.object(doctor, "run", return_value=(0, "SECRET")) as r:
            result = doctor.key()
        self.assertNotIn("SECRET", " ".join(result))
        self.assertFalse({"-w", "-g"} & set(r.call_args.args))

    def test_outside_a_repository(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            doctor, "run", side_effect=lambda *a: (
                (p := subprocess.run(a, capture_output=True, text=True, cwd=d)).returncode, p.stdout)):
            self.assertEqual(doctor.repository().state, "WARN")

    def test_stop_gives_exit_1(self):
        bad = [doctor.Result("STOP", "Docker", "x", "y")]
        with mock.patch.object(doctor, "check_all", return_value=bad), mock.patch("builtins.print"):
            self.assertEqual(doctor.main(), 1)

    def test_old_python_is_refused_in_one_line(self):
        with mock.patch("sys.version_info", (3, 9, 6)), mock.patch("builtins.print") as p:
            self.assertEqual(__main__.main(), 2)
        self.assertEqual(p.call_count, 1)

    def test_main_file_can_be_read_by_old_python(self):
        import ast
        ast.parse(Path(__main__.__file__).read_text(), feature_version=(3, 9))
