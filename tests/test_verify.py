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

from agent import __main__, box, gitsafe, verify
from agent.gitsafe import File, Refused
from tests.test_box import IMAGE, git, own_items
from tests.test_checker import BUG, GOOD, TEST
from tests.test_scan import FAKES

ROOT = Path(__file__).parent.parent
PROOF = "test_calc.T.test_proof"


class Case(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.addCleanup(lambda: self.assertEqual(own_items(), []))
        self.home = Path(d.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.write({"calc.py": BUG, "test_calc.py": TEST})
        self.base = git(self.repo, "rev-parse", "HEAD")

    def write(self, files, branch="main"):
        if branch != "main":
            git(self.repo, "checkout", "-q", "-b", branch)
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        git(self.repo, "add", "-A", "-f")
        git(self.repo, "commit", "-qm", "x")
        git(self.repo, "checkout", "-q", "main")

    def snapshot(self):
        """Everything in the folder and in .git that a person would notice, except the new branch and objects."""
        keep = [p for p in self.repo.rglob("*") if p.is_file() and not {"objects", "agent"} & set(p.relative_to(self.repo).parts)]  # noqa: E501
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in keep}


class Commit(Case):
    def test_the_folder_index_and_branch_are_untouched_and_no_hook_runs(self):
        hooks = self.repo / ".git" / "hooks"
        for h in ("post-commit", "pre-commit", "reference-transaction", "post-checkout", "post-index-change"):
            (hooks / h).write_text(f"#!/bin/sh\ntouch {self.home}/PWNED\n")
            (hooks / h).chmod(0o755)
        git(self.repo, "config", "core.fsmonitor", f"touch {self.home}/PWNED")
        git(self.repo, "config", "hook.x.command", f"touch {self.home}/PWNED")
        for event in ("reference-transaction", "post-index-change"):
            git(self.repo, "config", "--add", "hook.x.event", event)
        git(self.repo, "config", "filter.x.clean", f"touch {self.home}/PWNED; cat")
        (self.home / "PWNED").unlink(missing_ok=True)
        (self.repo / "calc.py").write_text("not committed")
        before = self.snapshot()
        files = [File("calc.py", "100644", GOOD.encode()), File("new/ñ.py", "100755", b"x"),
                 File(".gitattributes", "100644", b"* filter=x\n"), File("l", "120000", b"calc.py")]
        new = gitsafe.commit(self.repo, "main", "agent/t1", files, "fix\x1b[2J")
        self.assertFalse((self.home / "PWNED").exists())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(git(self.repo, "-c", "core.fsmonitor=false", "show", "agent/t1:calc.py"), GOOD.strip())
        tree = git(self.repo, "ls-tree", "-r", new)
        self.assertEqual([x.split()[0] for x in tree.splitlines()], ["100644", "100644", "120000", "100755", "100644"])
        self.assertEqual(git(self.repo, "rev-parse", "agent/t1^"), self.base)

    def test_more_commits_and_unexpected_moves(self):
        one = gitsafe.commit(self.repo, "main", "agent/t", [File("a", "100644", b"1")], "m")
        two = gitsafe.commit(self.repo, "main", "agent/t", [File("a", "", b""), File("b", "100644", b"")], "m", one)
        self.assertEqual(git(self.repo, "diff", "--name-status", self.base, two), "A\tb")
        for parent in ("", one):  # the branch exists already, or somebody moved it
            with self.assertRaises(Refused):
                gitsafe.commit(self.repo, "main", "agent/t", [File("c", "100644", b"1")], "m", parent)
        self.assertEqual(git(self.repo, "rev-parse", "agent/t"), two)

    def test_refused(self):
        cases = {
            "links out": [File("l", "120000", b"../../x")], "absolute link": [File("l", "120000", b"/etc/passwd")],
            "link to .git": [File("l", "120000", b".git/config")], "deep link out": [File("a/b/l", "120000", b"../../../x")],  # noqa: E501
            "git folder": [File(".git/hooks/x", "100755", b"")], "git by case": [File("a/.GIT/config", "100644", b"")],
            "HFS trick": [File(".g‌it/config", "100644", b"")], "NTFS trick": [File("git~1/config", "100644", b"")],
            "parent": [File("../x", "100644", b"")], "absolute": [File("/x", "100644", b"")],
            "screen control": [File("a\x1b[2J", "100644", b"")], "backslash": [File("a\\..\\b", "100644", b"")],
            "submodule": [File("s", "160000", b"")], "secret name": [File(".env", "100644", b"x")],
            "secret": [File("a", "100644", FAKES["google-key"].encode())],
            "same on a Mac": [File("CALC.py", "100644", b"")], "folder same on a Mac": [File("D/x", "100644", b""),
                                                                                  File("d/y", "100644", b"")],
            "below a file": [File("calc.py/x", "100644", b"")], "below a link": [File("l", "120000", b"d"),
                                                                                 File("l/x", "100644", b"")],
            "link through a link": [File("x/y/l", "120000", b"../.."), File("m", "120000", b"x/y/l/../outside")],
            "link through a link to .git": [File("x/l", "120000", b".."), File("h", "120000", b"x/l/.git/hooks")],
            "link to .GIT": [File("l", "120000", b".GIT/config")], "link to git~1": [File("l", "120000", b"git~1/config")],  # noqa: E501
            "link with dots inside": [File("l", "120000", b"a/../b")], "link to itself": [File("l", "120000", b"l")],
            "Git drops this": [File(".gitmodules", "120000", b"x")], "key in a name": [File(FAKES["aws-key"], "100644", b"")],  # noqa: E501
            "key in a link": [File("l", "120000", FAKES["aws-key"].encode())],
            "key in UTF-16": [File("a", "100644", FAKES["aws-key"].encode("utf-16"))],
            "nothing": [], "same content": [File("calc.py", "100644", BUG.encode())],
            "too large": [File("a", "100644", b"x" * (gitsafe.MAX_CHANGE + 1))],
        }
        for name, files in cases.items():
            good = [] if name in ("nothing", "same content") else [File("good", "100644", b"1")]
            with self.subTest(name), self.assertRaises((Refused, box.BoxError)):
                gitsafe.commit(self.repo, "main", "agent/t", files + good, "m")
        for branch in ("main", "agent/../main", "agent/", "agent/A", "agent/x.lock", "agent/a b", "-d", "agent/" + "x" * 41):  # noqa: E501
            with self.subTest(branch), self.assertRaises(Refused):
                gitsafe.commit(self.repo, "main", branch, [File("a", "100644", b"1")], "m")
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_good_links_are_taken(self):
        files = [File("a/b/l", "120000", b"../../calc.py"), File("k", "120000", b"test_calc.py")]
        gitsafe.commit(self.repo, "main", "agent/t", files, "m")

    def test_exact_secret_and_message(self):
        with self.assertRaises(Refused):
            gitsafe.commit(self.repo, "main", "agent/t", [File("a", "100644", b"x MARKER y")], "m", secrets=("MARKER",))
        new = gitsafe.commit(self.repo, "main", "agent/t", [File("a", "100644", b"x")], "MARKER", secrets=("MARKER",))
        self.assertNotIn("MARKER", git(self.repo, "log", "-1", "--format=%B", new))

    def test_other_branches_are_never_reached(self):
        git(self.repo, "branch", "precious")
        for target in ("precious", "main", "unborn"):
            git(self.repo, "symbolic-ref", "refs/heads/agent/t", f"refs/heads/{target}")
            with self.assertRaises(Refused):
                gitsafe.discard(self.repo, "agent/t")
            with self.assertRaises(Refused):
                gitsafe.commit(self.repo, "main", "agent/t", [File("a", "100644", b"1")], "m")
        git(self.repo, "symbolic-ref", "-d", "refs/heads/agent/t")
        git(self.repo, "checkout", "-q", "-b", "Agent/u")
        with self.assertRaises(Refused):
            gitsafe.discard(self.repo, "agent/u")
        git(self.repo, "checkout", "-q", "main")
        for action in (lambda: gitsafe.discard(self.repo, "agent/u"),
                       lambda: gitsafe.commit(self.repo, "main", "agent/u", [File("a", "100644", b"1")], "m")):
            with self.assertRaisesRegex(Refused, "almost the same name"):
                action()
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname:short)").split(), ["Agent/u", "main", "precious"])  # noqa: E501

    def test_a_checked_out_agent_branch_is_not_moved(self):
        one = gitsafe.commit(self.repo, "main", "agent/t", [File("a", "100644", b"1")], "m")
        git(self.repo, "checkout", "-q", "agent/t")
        with self.assertRaisesRegex(Refused, "checked out"):
            gitsafe.commit(self.repo, "main", "agent/t", [File("b", "100644", b"1")], "m", one)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_an_old_finding_does_not_block_but_a_new_one_does(self):
        old = f"a = 1\n# {FAKES['aws-key']}\n"
        self.write({"c.py": old})
        gitsafe.commit(self.repo, "main", "agent/t", [File("c.py", "100644", (old + "b = 2\n").encode())], "m")
        with self.assertRaises(Refused):
            gitsafe.commit(self.repo, "main", "agent/u", [File("c.py", "100644", (old + FAKES["groq-key"]).encode())], "m")  # noqa: E501

    def test_notes(self):
        files = [File("run.sh", "100755", b""), File(".github/workflows/x.yml", "100644", b""), File("l", "120000", b"calc.py"),  # noqa: E501
                 File("calc.py", "100644", GOOD.encode())]
        self.assertEqual(len(gitsafe.check_files(self.repo, "main", files)), 3)

    def test_discard(self):
        new = gitsafe.commit(self.repo, "main", "agent/t", [File("a", "100644", b"1")], "m")
        for bad in ("main", "agent/none", "refs/heads/main", "--all"):
            with self.assertRaises(Refused):
                gitsafe.discard(self.repo, bad)
        git(self.repo, "checkout", "-q", "agent/t")
        with self.assertRaisesRegex(Refused, "checked out"):
            gitsafe.discard(self.repo, "agent/t")
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(gitsafe.discard(self.repo, "agent/t"), new)
        self.assertEqual(git(self.repo, "branch", "--list", "agent/*"), "")


class FromBox(Case):
    def test_only_tracked_and_edited_files_are_taken(self):
        self.write({"gone.py": "x", ".env": "keep me", "exec.sh": "x"})
        entries, _ = box.list_commit(self.repo, "main")
        with box.Box(IMAGE) as b:
            b.copy_entries(self.repo, entries)
            b.run(["sh", "-c", "rm gone.py; chmod +x exec.sh; mkdir __pycache__; echo c > __pycache__/c; echo n > new.py;"  # noqa: E501
                               "echo '" + GOOD.replace("\n", "\\n") + "' > calc.py; mkfifo pipe; ln -s /etc link"])
            found = gitsafe.read_box(b, entries, {"new.py"})
        self.assertEqual([(f.path, f.mode) for f in found.files],
                         [("calc.py", "100644"), ("exec.sh", "100755"), ("gone.py", ""), ("new.py", "100644")])
        self.assertEqual(found.not_taken, ["__pycache__/c", "link", "pipe (not a normal file)"])
        gitsafe.commit(self.repo, "main", "agent/t", found.files, "m")
        self.assertEqual(git(self.repo, "show", "agent/t:.env"), "keep me")  # never copied, so never deleted

    def test_nothing_changed(self):
        entries, _ = box.list_commit(self.repo, "main")
        with box.Box(IMAGE) as b:
            b.copy_entries(self.repo, entries)
            self.assertEqual(gitsafe.read_box(b, entries, set()), gitsafe.Found([], []))

    def test_an_incomplete_or_broken_list_is_refused(self):
        whole = '{"path": "YQ==", "mode": "100644", "data": ""}\n{"end": true}\n'
        for output, truncated in ((whole.split("\n")[0], False), ("", False), ("oops", False), ("{}\n", False),
                                  ('{"path": 1}\n', False), (whole, True)):
            b = mock.Mock()
            b.run.return_value = box.Result("EXITED", 0, output, truncated, 1)
            with self.subTest(output), self.assertRaises(Refused):
                gitsafe.read_box(b, [], set())
        b.run.return_value = box.Result("EXITED", 0, whole, False, 1)
        self.assertEqual(gitsafe.read_box(b, [], set()).files, [File("a", "100644", b"")])

    def test_hostile_boxes_are_refused(self):
        entries, _ = box.list_commit(self.repo, "main")
        for name, script in {
            "unreadable file": "chmod 000 calc.py", "unreadable folder": "mkdir d; touch d/x; chmod 000 d",
            "large": "head -c 6000000 /dev/zero > calc.py",
        }.items():
            with self.subTest(name), box.Box(IMAGE) as b:
                b.copy_entries(self.repo, entries)
                b.run(["sh", "-c", script])
                with self.assertRaises(Refused):
                    gitsafe.read_box(b, entries, set())
        with box.Box(IMAGE) as b:  # a link in place of a folder is not followed
            b.copy_entries(self.repo, entries)
            b.run(["sh", "-c", "rm calc.py; ln -s /etc/passwd calc.py; ln -s /proc p"])
            found = gitsafe.read_box(b, entries, {"p"})
        self.assertEqual([(f.path, f.data) for f in found.files], [("calc.py", b"/etc/passwd"), ("p", b"/proc")])
        with self.assertRaises(Refused):
            gitsafe.commit(self.repo, "main", "agent/t", found.files, "m")


class Command(Case):
    def run_agent(self, *args, answer=None, early="", cwd=None):
        """Run the program as a person would: in a terminal, when there is an answer."""
        env = {**os.environ, "HOME": str(self.home), "XDG_DATA_HOME": str(self.home / "data"), "PYTHONPATH": str(ROOT)}
        env["CODING_AGENT_KEYCHAIN"] = "/nonexistent/test.keychain"
        env["DOCKER_HOST"] = box.docker("context", "inspect", "-f", "{{.Endpoints.docker.Host}}")
        command = [sys.executable, "-m", "agent", *args]
        if answer is None:
            p = subprocess.run(command, cwd=cwd or self.repo, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)  # noqa: E501
            return p.returncode, p.stdout + p.stderr
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd or self.repo)
            os.execve(sys.executable, command, env)  # noqa: S606
        os.write(fd, early.encode())
        text, sent, limit = b"", False, time.time() + 300
        while time.time() < limit:
            if select.select([fd], [], [], 1)[0]:
                try:
                    piece = os.read(fd, 65536)
                except OSError:
                    break
                if not piece:
                    break
                text += piece
            if not sent and b"Enter: " in text:
                time.sleep(0.5)  # keys typed before the whole question is there are thrown away
                os.write(fd, answer.encode())
                sent = True
        else:
            os.kill(pid, signal.SIGKILL)  # never wait for ever
        code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        os.close(fd)
        return code, text.decode("utf-8", "replace").replace("\r\n", "\n")

    def test_good_and_bad_branch(self):
        self.write({"calc.py": GOOD}, "good")
        self.write({"calc.py": "def add(a, b):\n    return 7\n"}, "bad")
        before = self.snapshot()
        code, text = self.run_agent("verify", "good", "--proof", PROOF, answer="y\n")
        self.assertEqual(code, 0, text)
        self.assertIn("VERIFIED  (TEST-LEVEL)", text)
        receipts = list(self.home.rglob("receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertNotIn("repo", receipts[0].parts)
        code, text = self.run_agent("verify", "bad", "--proof", PROOF, answer="y\n")
        self.assertEqual((code, "FAILED  (TEST-LEVEL)" in text), (1, True), text)
        self.assertEqual(before, self.snapshot())

    def test_changed_tests_are_shown_and_used_only_after_yes(self):
        new = TEST + "    def test_more(self): self.assertEqual(add(1, 2), 3)  # \x1b[2J‮\n"
        self.write({"calc.py": GOOD, "test_calc.py": new}, "more")
        code, text = self.run_agent("verify", "more", "--proof", "test_calc.T.test_more", answer="y\n")
        self.assertEqual(code, 0, text)
        self.assertIn("+    def test_more(self)", text)
        self.assertIn("<U+001B>[2J<U+202E>", text)
        self.assertNotIn("\x1b[2J", text)
        code, text = self.run_agent("verify", "more", "--proof", PROOF, "--original-tests", answer="y\n")
        self.assertEqual((code, "UNVERIFIABLE" in text), (2, True), text)

    def test_no_content_is_approved_unseen(self):
        name = "tests/test_e\u0301.py"  # written in the form that Git on a Mac changes
        self.write({"calc.py": GOOD, name: "UNSEEN = 1\n", ".gitattributes": "*.py -diff\n"}, "hidden")
        text = self.run_agent("verify", "hidden", "--proof", PROOF, answer="n\n")[1]
        self.assertIn("+UNSEEN = 1", text)
        self.write({"tests/test_b.py": "a\0b\n"}, "binary")
        self.assertIn("not text", self.run_agent("verify", "binary", answer="y\n")[1])
        self.write({"tests/test_b.py": "x\n" * 6000}, "long")
        self.assertIn("too long", self.run_agent("verify", "long", answer="y\n")[1])
        self.assertEqual(verify.visible("a\u3164\u2800\ufe0fb"), "a<U+3164><U+2800><U+FE0F>b")

    def test_colour_settings_do_not_switch_off_protection(self):
        helper = "import unittest\ndef same(t, a, b): t.assertEqual(a, b)\n"
        self.write({"helper.py": helper, "test_calc.py": TEST.replace("self.assertEqual(add(2, 2), 4)", "import helper; helper.same(self, add(2, 2), 4)")})  # noqa: E501
        self.write({"helper.py": "import unittest\ndef same(*a): pass\n", "calc.py": "def add(a, b): return 5\n"}, "cheat")  # noqa: E501
        git(self.repo, "config", "color.ui", "always")
        code, text = self.run_agent("verify", "cheat", "--proof", PROOF, "--original-tests", answer="y\n")
        self.assertNotEqual(code, 0, text)
        self.assertIn("Not used: helper.py", text)

    def test_screen_shows_no_secret_and_no_escape(self):
        key = FAKES["openai-style-key"]
        self.write({"a.sh": "exit 0\n"})
        self.write({"a.sh": f"echo {key}\nexit 1\n"}, "leak2")
        args = ("verify", "leak2", "--command", "sh a.sh", "--base", "main")
        self.assertIn("seems to hold a secret", self.run_agent(*args, answer="y\n")[1])
        text = self.run_agent(*args, "--original-tests", answer="y\n")[1]
        self.assertIn("the command works with the change", text)
        self.write({"data1": key, "bad1": ""}, "leak3")
        command = "sh -c 'cat data* 2>/dev/null; ls bad* >/dev/null 2>&1 && exit 1; exit 0'"
        text = self.run_agent("verify", "leak3", "--command", command, answer="y\n")[1]
        self.assertIn("the command fails with the change", text)
        self.assertNotIn(key, text)
        with mock.patch.object(sys, "argv", ["agent", "merge", "x"]), mock.patch.object(
            verify, "repository", side_effect=box.BoxError(f"\x1b[2J {key}")
        ), mock.patch.object(box, "signals_stop_cleanly"), contextlib.redirect_stdout(io.StringIO()) as out:
            __main__.main()
        self.assertEqual(out.getvalue(), "Stopped: <U+001B>[2J [MASKED]\n")

    def test_tag_with_the_same_name_and_unrelated_history(self):
        self.write({"calc.py": GOOD}, "b")
        git(self.repo, "tag", "b", "main")
        self.assertIn(git(self.repo, "rev-parse", "refs/heads/b")[:12], self.run_agent("verify", "b", answer="n\n")[1])
        git(self.repo, "checkout", "-q", "--orphan", "other")
        git(self.repo, "commit", "-qm", "another message, so it is never the same commit")
        git(self.repo, "checkout", "-q", "main")
        self.assertIn("nothing in common", self.run_agent("verify", "other")[1])
        self.assertIn("one --proof", self.run_agent("verify", "b", "--command", "sh x", "--proof", "a", "--proof", "b")[1])  # noqa: E501

    def test_data_folder_that_cannot_be_made(self):
        self.write({"calc.py": GOOD}, "good")
        for name in ("Library", "data"):
            (self.home / name).write_text("a file")
        code, text = self.run_agent("verify", "good", answer="y\n")
        self.assertEqual((code, "Traceback" in text, "Stopped" in text), (2, False, True), text)

    def test_nothing_runs_without_a_yes(self):
        self.write({"calc.py": GOOD}, "good")
        for k in ({}, {"answer": "n\n"}, {"answer": "\n"}, {"answer": "yes\n"}, {"answer": "\n", "early": "y\ny\n"}):
            with self.subTest(k):
                code, text = self.run_agent("verify", "good", "--proof", PROOF, **k)
                self.assertEqual((code, "Nothing was run." in text), (2, True), text)
        self.assertEqual(list(self.home.rglob("receipt.json")), [])

    def test_clear_messages(self):
        self.write({"calc.py": GOOD}, "good")
        for args, word in (
            (("verify", "none"), "no branch"), (("verify", "main"), "no change"), (("verify", "--base=--x", "good"), "Stopped"),  # noqa: E501
            (("verify", "good", "--image", "no-such:1"), "No such image"), (("verify", "good", "--command", ""), "empty"),  # noqa: E501
            (("what",), "usage"), ((), "terminal"), (("merge", "good"), "git merge --no-ff " + git(self.repo, "rev-parse", "good")),  # noqa: E501
        ):
            code, text = self.run_agent(*args)
            self.assertIn(word, text, args)
            self.assertNotIn("Traceback", text)
        code, text = self.run_agent("verify", "good", cwd=self.home)
        self.assertIn("not in a Git repository", text)

    def test_discard_asks(self):
        gitsafe.commit(self.repo, "main", "agent/t", [File("a", "100644", b"1")], "m")
        self.assertEqual(self.run_agent("discard", "agent/t")[0], 2)
        self.assertEqual(self.run_agent("discard", "agent/t", answer="n\n")[0], 2)
        self.assertNotEqual(git(self.repo, "branch", "--list", "agent/*"), "")
        self.assertEqual(self.run_agent("discard", "agent/t", answer="y\n")[0], 0)
        self.assertEqual(git(self.repo, "branch", "--list", "agent/*"), "")

    def test_merge_shows_what_to_look_at(self):
        self.write({"calc.py": GOOD, ".github/workflows/x.yml": "x"}, "good")
        self.assertIn(".github/workflows/x.yml: can make something run", self.run_agent("merge", "good")[1])

    def test_ctrl_c_message(self):
        with mock.patch.object(sys, "argv", ["agent", "verify", "x"]), mock.patch.object(box, "clean_leftovers"):
            with mock.patch.object(verify, "repository", side_effect=KeyboardInterrupt), mock.patch.object(
                box, "signals_stop_cleanly"
            ), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(__main__.main(), 130)
        self.assertIn("Cancelled", out.getvalue())


del Case
