import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent import box, doctor, scan

IMAGE = box.HELPER_IMAGE


def git(repo, *args, **k):
    who = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
    p = subprocess.run(["git", *who, *args], cwd=repo, check=True, capture_output=True, text=True, **k)
    return p.stdout.strip()


def py(code):
    return ["python", "-c", code]


class BoxTest(unittest.TestCase):
    """Every test must leave no container and no volume behind (AC-12)."""

    @classmethod
    def setUpClass(cls):
        have = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
        if have.returncode:
            subprocess.run(["docker", "pull", "-q", IMAGE], check=True, capture_output=True)

    def setUp(self):
        self.addCleanup(self.nothing_left)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.repo = Path(d.name)
        git(self.repo, "init", "-q", "-b", "main")
        self.task = None

    def nothing_left(self):
        self.assertEqual(box.our_items(), [])

    def image(self, lines):
        tag = "coding-agent-v2/test:" + os.urandom(4).hex()
        text = f"FROM {IMAGE}\n{lines}\n"
        command = ["docker", "build", "-q", "--network=none", "-t", tag, "-"]
        subprocess.run(command, input=text.encode(), check=True, capture_output=True)
        self.addCleanup(box.docker, "rmi", "-f", tag)
        return tag

    def box(self, **k):
        return box.Box(IMAGE, **k)


class Isolation(BoxTest):
    def test_what_a_command_can_and_cannot_do(self):
        with self.box() as b, mock.patch.dict(os.environ, {"CODING_FAKE_KEY": "fake-marker"}):
            ok = b.run(["sh", "-c", "echo hi > a && cat a && id -u && pwd"])
            self.assertEqual((ok.status, ok.code, ok.output), ("EXITED", 0, "hi\n1000\n/work\n"))
            again = b.run(["cat", "a"], write=False)
            self.assertEqual(again.output, "hi\n")  # the volume carries the work to the next container
            for why, args in {
                "read-only mount": ["sh", "-c", "echo x > /work/b"],
                "root file system": ["sh", "-c", "echo x > /var/tmp/x"],  # open to all unless read-only
                "socket": ["ls", "/var/run/docker.sock"],
                "host home": ["ls", str(Path.home())],
                "network": py("import socket;socket.create_connection(('1.1.1.1',53),2)"),
                "names": py("import socket;socket.getaddrinfo('example.com',80)"),
                "become root": py("import os;os.setuid(0)"),
                "big tmp": ["sh", "-c", "head -c 600000000 /dev/zero > /tmp/f"],
            }.items():
                self.assertNotEqual(b.run(args, write=False).code, 0, why)
            env = b.run(["env"], env={"A": "1"}).output
            self.assertIn("A=1\n", env)
            self.assertNotIn("fake-marker", env)
            status = b.run(["cat", "/proc/self/status"]).output
            self.assertIn("CapEff:\t0000000000000000", status)
            self.assertIn("CapBnd:\t0000000000000000", status)
            self.assertIn("NoNewPrivs:\t1", status)

    def test_there_is_no_shell_between(self):
        with self.box() as b:
            self.assertEqual(b.run(["echo", "$HOME; ls `id`"]).output, "$HOME; ls `id`\n")

    def test_exit_code_and_missing_program(self):
        with self.box() as b:
            self.assertEqual(b.run(["sh", "-c", "exit 125"]).code, 125)  # not confused with Docker's own 125
            missing = b.run(["no-such-program"])
            self.assertEqual(missing.code, 127)
            self.assertIn("no-such-program", missing.output)

    def test_unknown_image_is_refused_and_nothing_is_downloaded(self):
        with self.assertRaisesRegex(box.BoxError, "No such image"):
            box.Box("no-such-image-xyz:1")

    def test_low_space_refuses_to_start(self):
        stop = doctor.Result("STOP", "Space", "1 GB free", "Free some space")
        with mock.patch.object(doctor, "space", return_value=stop), self.assertRaisesRegex(box.BoxError, "1 GB"):
            self.box()

    def test_a_real_image_volume_is_small(self):
        tag = self.image("RUN mkdir -m 777 /data\nVOLUME /data/")
        with box.Box(tag) as b:
            self.assertIn("No space", b.run(["sh", "-c", "head -c 100000000 /dev/zero > /data/f"]).output)

    def test_unusual_volume_names_are_refused(self):
        for path in ("/x/../data", "//data", "/a/./data", "/x/../tmp", "/x/../work", "/d:exec"):
            with mock.patch.object(box, "docker", return_value=json.dumps({path: {}})):
                with self.assertRaises(box.BoxError, msg=path):
                    box._image_volumes("x")

    def test_an_unexpected_disk_is_refused_even_if_the_name_check_fails(self):
        tag = self.image('VOLUME ["/x/../data"]')
        with mock.patch.object(box, "_image_volumes", return_value=[]), box.Box(tag) as b:
            with self.assertRaisesRegex(box.BoxError, "not ours"):
                b.run(["true"])

    def test_proxy_passwords_of_the_developer_do_not_enter(self):
        with tempfile.TemporaryDirectory() as d:
            context = box.docker("context", "inspect", "-f", "{{.Endpoints.docker.Host}}")
            (Path(d) / "config.json").write_text(
                json.dumps({"proxies": {"default": {"httpProxy": "http://a:FAKEPW@p:1", "noProxy": "FAKEPW"}}})
            )
            with mock.patch.dict(os.environ, {"DOCKER_CONFIG": d, "DOCKER_HOST": context}), self.box() as b:
                self.assertNotIn("FAKEPW", b.run(["env"]).output)

    def test_volumes_declared_by_the_image_become_small_memory_disks(self):
        with mock.patch.object(box, "docker", return_value='{"/data":{},"/work":{}}'):
            self.assertEqual(box._image_volumes("x"), ["--tmpfs", "/data:size=64m"])
        with mock.patch.object(box, "docker", return_value='{"/d:exec,size=9g":{}}'), self.assertRaises(box.BoxError):
            box._image_volumes("x")

    def test_a_hanging_docker_does_not_hang_us(self):
        real = box.docker

        def hang(*args, **k):
            if args[0] == "kill":
                raise box.BoxError("Docker did not answer (kill)")
            return real(*args, **k)

        with self.box() as b, mock.patch.object(box, "docker", hang):
            r = b.run(["sleep", "8"], timeout=0.5)
        self.assertEqual(r.status, "TIMED_OUT")


class Limits(BoxTest):
    def test_time_memory_processes_output(self):
        with self.box() as b:
            b.limits = ["--cpus", "2", "--memory", "128m", "--memory-swap", "128m"]
            t = time.monotonic()
            r = b.run(["sh", "-c", "echo begin; trap '' TERM; sleep 60 & sleep 60"], timeout=1)
            self.assertEqual((r.status, r.output), ("TIMED_OUT", "begin\n"))
            self.assertLess(time.monotonic() - t, 10)
            self.assertEqual(b.run(py("x=bytearray(400*1024*1024)")).status, "OUT_OF_MEMORY")
            many = b.run(["sh", "-c", "i=0; while [ $i -lt 600 ]; do sleep 5 & i=$((i+1)); done"])
            self.assertIn("fork", many.output)
            with mock.patch.object(box, "KEEP_OUTPUT", 1000):
                r = b.run(["sh", "-c", "echo first; head -c 100000 /dev/zero | tr '\\0' x; echo last"])
            self.assertTrue(r.truncated and r.output.startswith("first") and r.output.endswith("last\n"))
            self.assertLess(len(r.output), 1200)

    def test_too_many_files_is_stopped_while_running(self):
        with self.box() as b, mock.patch.object(box, "MAX_FILES", 20_000_000):
            r = b.run(["sh", "-c", "head -c 40000000 /dev/zero > big; sleep 60"])
            self.assertEqual(r.status, "TOO_MUCH_DISK")
            self.assertLess(r.seconds, 15)

    def test_a_fast_or_hidden_file_is_still_counted(self):
        with self.box() as b, mock.patch.object(box, "MAX_FILES", 20_000_000):
            r = b.run(["sh", "-c", "mkdir d; head -c 40000000 /dev/zero > d/big; chmod 000 d"])
            self.assertEqual(r.status, "TOO_MUCH_DISK")
            self.assertGreater(b.look().files, 40_000_000)

    def test_a_deleted_but_open_file_is_counted(self):
        code = "import os,time\nf=open('x','wb')\nos.remove('x')\nf.write(b'a'*120_000_000)\nf.flush()\ntime.sleep(30)"
        with self.box() as b, mock.patch.object(box, "MAX_FILES", 60_000_000):
            self.assertEqual(b.run(py(code)).status, "TOO_MUCH_DISK")

    def test_files_that_vanish_while_we_count_are_normal(self):
        code = (
            "import os,time\nt=time.time()\nwhile time.time()-t<7:\n"
            " for i in range(40): os.makedirs(f'd/{i}'); open(f'd/{i}/f','w').close()\n"
            " for i in range(40): os.remove(f'd/{i}/f'); os.rmdir(f'd/{i}')"
        )
        with self.box() as b:
            b.run(["mkdir", "d"])
            with mock.patch.object(box, "WATCH_SECONDS", 0.1):
                r = b.run(py(code))
        self.assertEqual((r.status, r.code), ("EXITED", 0))

    def test_low_space_while_running(self):
        with self.box() as b, mock.patch.object(box, "MIN_DISK", 10**18):
            self.assertEqual(b.run(["sleep", "30"]).status, "LOW_SPACE")

    def test_cancel(self):
        cancel = threading.Event()
        with self.box(cancel=cancel) as b:
            threading.Timer(1, cancel.set).start()
            r = b.run(["sh", "-c", "sleep 60"])
            self.assertEqual(r.status, "CANCELLED")
            self.assertLess(r.seconds, 15)


class CopyIn(BoxTest):
    def commit(self):
        git(self.repo, "add", "-A", "-f")
        git(self.repo, "commit", "-qm", "x")

    def test_only_the_commit_is_copied_and_the_folder_is_untouched(self):
        r = self.repo
        (r / "run.sh").write_text("#!/bin/sh\necho ran\n")
        (r / "run.sh").chmod(0o755)
        (r / "sub dir").mkdir()
        (r / "sub dir" / "ñ\n.txt").write_bytes(b"\0\xff")
        (r / "empty").touch()
        (r / "link").symlink_to("run.sh")
        (r / ".env").write_text("x")
        (r / ".env.example").write_text("x")
        (r / ".gitattributes").write_text("run.sh export-ignore\nempty export-subst\n")
        (r / ".gitignore").write_text("ignored\n")
        self.commit()
        (r / "untracked").write_text("x")
        (r / "ignored").write_text("x")
        (r / "run.sh").write_text("changed but not committed")
        before = {str(p): (p.lstat().st_mtime_ns, p.lstat().st_mode) for p in r.rglob("*")}
        with self.box() as b:
            copied = b.copy_in(r, "HEAD")
            names = b.run(py("import os;print(sorted(os.listdir('.')))")).output
            ran = b.run(["./link"])
            data = b.run(py("print(open('sub dir/ñ\\n.txt','rb').read())")).output
        self.assertEqual(copied.left_out, [".env: looks like a secret"])
        self.assertEqual(
            names, "['.env.example', '.gitattributes', '.gitignore', 'empty', 'link', 'run.sh', 'sub dir']\n"
        )
        self.assertEqual((ran.output, data), ("ran\n", "b'\\x00\\xff'\n"))
        self.assertEqual(before, {str(p): (p.lstat().st_mtime_ns, p.lstat().st_mode) for p in r.rglob("*")})

    def test_hostile_paths_and_submodules_are_left_out(self):
        blob = git(self.repo, "hash-object", "-w", "--stdin", input="x")
        (self.repo / "a").write_text("a")
        self.commit()
        head = git(self.repo, "rev-parse", "HEAD")
        inner = git(self.repo, "mktree", input=f"100644 blob {blob}\tconfig\n")
        tree = git(self.repo, "mktree", "--missing", input=(
            f"100644 blob {blob}\tgood\n040000 tree {inner}\t.GIT\n"
            f"040000 tree {inner}\t..\n160000 commit {head}\tsub\n"))
        commit = git(self.repo, "commit-tree", tree, "-m", "x")
        take, left = box.list_commit(self.repo, commit)
        self.assertEqual([e.name for e in take], ["good"])
        self.assertEqual(sorted(x.split(": ")[1] for x in left), ["submodule", "unsafe path", "unsafe path"])

    def test_link_with_files_below_it_is_refused(self):
        entries = [box.Entry("120000", "0", 1, "a"), box.Entry("100644", "0", 1, "a/b")]
        raw = b"".join(f"{e.mode} blob {e.oid} {e.size}\t{e.name}\0".encode() for e in entries)
        with mock.patch.object(box, "git_bytes", return_value=raw), self.assertRaisesRegex(box.BoxError, "conflict"):
            box.list_commit(self.repo, "x")

    def test_option_like_names_are_not_options(self):
        with self.assertRaises(box.BoxError):
            box.commit_id(self.repo, "--git-dir=/tmp")

    def test_missing_object_is_a_clear_error(self):
        raw = b"100644 blob abc BAD\ta\0"
        with mock.patch.object(box, "git_bytes", return_value=raw), self.assertRaisesRegex(box.BoxError, "partial"):
            box.list_commit(self.repo, "x")

    def test_too_large_project_is_refused_before_copying(self):
        (self.repo / "a").write_text("x" * 100)
        self.commit()
        with mock.patch.object(box, "MAX_FILES", 50), self.box() as b, self.assertRaisesRegex(box.BoxError, "larger"):
            b.copy_in(self.repo, "HEAD")

    def test_a_failing_copy_is_an_error(self):
        with self.box() as b, mock.patch.object(box, "_write_tar", side_effect=box.BoxError("broken")):
            (self.repo / "a").write_text("x")
            self.commit()
            with self.assertRaisesRegex(box.BoxError, "broken"):
                b.copy_in(self.repo, "HEAD")

    def test_secret_looking_names(self):
        for name in (".env", "a/.ENV.local", "id_rsa", "x.pem", ".netrc", "service-account-1.json", ".npmrc",
                     ".envrc", ".env/prod", ".env-prod", ".pgpass", "a/.kube/config", "id_rsa.bak", "x.kdbx",
                     "terraform.tfstate"):
            self.assertTrue(scan.BLOCKED_NAMES.search(name), name)
        for name in (".env.example", "id_map", "config", "keys.py", "environment", "a.env"):
            self.assertFalse(scan.BLOCKED_NAMES.search(name), name)


CRASH = """
import sys
from agent import box
b = box.Box(box.HELPER_IMAGE).__enter__()
print("up", flush=True)
b.run(["sleep", "60"])
"""


class Cleanup(BoxTest):
    def test_error_inside_removes_everything(self):
        with self.assertRaises(ZeroDivisionError), self.box() as b:
            b.run(["true"])
            raise ZeroDivisionError

    def test_ctrl_c_kill_and_closed_terminal_remove_everything(self):
        code = CRASH.replace("b = box.Box(box.HELPER_IMAGE).__enter__()", "with box.Box(box.HELPER_IMAGE) as b:")
        code = code.replace('\nprint("up", flush=True)\nb.run', '\n    print("up", flush=True)\n    b.run')
        code = code.replace("import box\n", "import box\nbox.signals_stop_cleanly()\n")
        for sig, wait in ((signal.SIGINT, 1.5), (signal.SIGTERM, 1.5), (signal.SIGHUP, 1.5), *[
            (signal.SIGINT, i * 0.05) for i in range(12)  # also while Docker is still making the container
        ]):
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p.stdout.readline()
            time.sleep(wait)
            p.send_signal(sig)
            self.assertNotEqual(p.wait(40), 0)
            p.stdout.close()
            self.assertEqual(box.our_items(), [], (sig, wait))

    def test_leftovers_of_a_killed_launcher_go_and_running_ones_stay(self):
        crash = CRASH.replace("HELPER_IMAGE)", "HELPER_IMAGE, task='same')")
        p = subprocess.Popen([sys.executable, "-c", crash], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p.stdout.readline()
        time.sleep(1.5)
        p.kill()
        p.wait()
        p.stdout.close()
        with self.box(task="same") as b:  # same task name, living owner
            self.assertEqual(box.clean_leftovers(), 2)
            self.assertEqual(b.run(["echo", "still here"]).output, "still here\n")

    def test_a_hostile_image_cannot_mark_the_developers_container_as_ours(self):
        labels = "LABEL coding-agent-v2=task coding-agent-v2.task=zzz coding-agent-v2.owner=99999999999999999999"
        tag = self.image(labels)
        name = box.docker("create", tag, "true")
        self.addCleanup(box.docker, "rm", "-f", name, ok=False)
        box.clean_leftovers()
        box.clean("zzz")
        self.assertIn(name, box.docker("ps", "-aq", "--no-trunc").split())
        with box.Box(tag) as b:  # and our own labels win in our own containers
            self.assertEqual(b.run(["true"]).code, 0)

    def test_other_docker_items_are_never_touched(self):
        for labels in ([], ["--label", "coding-agent-v2=dev"], ["--label", "coding-agent-v2.task=t1"]):
            name = "box-test-" + os.urandom(4).hex()
            box.docker("volume", "create", *labels, name)
            try:
                box.clean("t1")
                box.clean_leftovers()
                self.assertIn(name, box.docker("volume", "ls", "-q").split())
            finally:
                box.docker("volume", "rm", name)

    def test_input_is_fed(self):
        with self.box() as b:
            def feed(out: io.BufferedWriter) -> None:
                out.write(b"abc")
            self.assertEqual(b.run(["cat"], feed=feed).output, "abc")


del BoxTest  # the base class itself has no tests
