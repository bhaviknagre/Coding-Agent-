import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import chat
from agent.approve import NO, Answer
from agent.model import FakeModel, call_message
from tests.test_box import git, own_items
from tests.test_checker import BUG, TEST

READ = call_message("read", path="calc.py", first=1)
FIX = call_message("edit", "the sign is wrong", path="calc.py", old="a - b", new="a + b")
DONE = call_message("finish", summary="done")
MUL_TEST = "import unittest\nfrom calc import mul\nclass M(unittest.TestCase):\n    def test_mul(self): self.assertEqual(mul(2, 3), 6)\n"  # noqa: E501


class Chat(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.addCleanup(lambda: self.assertEqual(own_items(), []))
        self.home = Path(d.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "calc.py").write_text(BUG)
        (self.repo / "test_calc.py").write_text(TEST)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "x")
        self.questions = []

    def chat(self, typed, script, answers=(), proof=("test_calc.T.test_proof",)):
        """Run a whole chat: `typed` are the lines at the prompt, `answers` the replies to questions."""
        model = script if isinstance(script, FakeModel) else FakeModel(script)
        answers = [Answer(True), *answers]  # the first one starts the chat

        def human(question):
            self.questions.append(question)
            a = answers.pop(0) if answers else NO
            return a() if callable(a) else a

        lines = iter(typed)

        def prompt(text=""):
            line = next(lines, None)
            if line is None:
                raise EOFError
            if callable(line):
                return line()
            return line

        out = io.StringIO()
        args = argparse.Namespace(model="script", files=[], proof=list(proof), command="", image="python:3.12-slim")
        with mock.patch("agent.chat.screen.choose_model", return_value=(model, ())), \
                mock.patch("agent.chat.ask_human", human), mock.patch("agent.verify.confirm", lambda q: human(q).yes), \
                mock.patch("agent.chat.ask_open", human), mock.patch("builtins.input", prompt), \
                mock.patch("agent.chat.repository", return_value=self.repo), \
                mock.patch.dict("os.environ", {"HOME": str(self.home)}), contextlib.redirect_stdout(out):
            code = chat.Chat(args).run()
        return code, out.getvalue(), model

    def test_a_follow_up_remembers_and_continues_the_same_branch(self):
        def second(messages):
            # The conversation is remembered: the first request and the agent's own fix are still there.
            text = str(messages)
            assert "fix add" in text and "the sign is wrong" in text, text
            return call_message("edit", path="calc.py", old="    return a + b\n", new="    return a + b\ndef mul(a, b):\n    return a * b\n")  # noqa: E501

        script = [READ, FIX, DONE, second, call_message("create", path="test_mul.py", text=MUL_TEST), DONE]
        code, text, _ = self.chat(["fix add", "also add mul, with a test", "/diff", "/verify", "/quit"], script,
                                  [Answer(True), Answer(True)])
        self.assertEqual(code, 0, text)
        self.assertEqual(text.count("Independent check: VERIFIED (TEST-LEVEL)"), 2, text)
        branches = git(self.repo, "branch", "--list", "agent/*").split()
        self.assertEqual(len(branches), 1, branches)
        self.assertIn("def mul", git(self.repo, "show", f"{branches[0]}:calc.py"))
        self.assertIn("+def mul", text)  # /diff
        self.assertIn("VERIFIED  (TEST-LEVEL)", text)  # /verify checks the whole branch again
        self.assertIn("⏺ edit calc.py", text)
        self.assertIn("MAKE A FILE", self.questions[1])
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual((self.repo / "calc.py").read_text(), BUG)  # your folder is never changed

    def test_without_a_proof_the_approved_new_test_proves_it(self):
        new = "import unittest\nfrom calc import add\nclass N(unittest.TestCase):\n    def test_n(self): self.assertEqual(add(1, 1), 2)\n"  # noqa: E501
        script = [READ, call_message("create", path="test_new.py", text=new), FIX, DONE]
        code, text, _ = self.chat(["fix add"], script, [Answer(True)], proof=())
        self.assertIn("Independent check: VERIFIED", text)
        script = [READ, FIX, DONE]
        code, text, _ = self.chat(["fix add"], script, proof=())
        self.assertIn("UNVERIFIABLE", text)
        self.assertIn("no new test was approved", text)

    def test_commands(self):
        code, text, _ = self.chat(["/help", "/diff", "/x", "/proof a.b", "/quit"], [])
        self.assertIn("/merge", text)
        self.assertIn("no branch yet", text)
        self.assertIn("Unknown: /x", text)
        self.assertIn("Proof for the next request: a.b", text)
        script = [READ, FIX, DONE, READ, FIX]
        code, text, _ = self.chat(["fix add", "/merge", "/discard", "/quit"], script, [Answer(True)])
        self.assertIn("git merge --no-ff", text)
        self.assertIn("Deleted. To bring it back", text)
        self.assertEqual(git(self.repo, "branch", "--list", "agent/*"), "")

    def test_new_starts_another_branch(self):
        script = [READ, FIX, DONE, READ, call_message("edit", path="calc.py", old="a - b", new="b + a"), DONE]
        self.chat(["fix add", "/new", "fix it another way"], script)
        self.assertEqual(len(git(self.repo, "branch", "--list", "agent/*").split()), 2)

    def test_ctrl_c_stops_the_step_and_the_next_request_goes_on_from_the_checked_work(self):
        def interrupt():
            raise KeyboardInterrupt

        run = call_message("run_command", program=["ls"])
        mul = call_message("edit", path="calc.py", old="    return a + b\n", new="    return a + b\ndef mul(a, b):\n    return a * b\n")  # noqa: E501
        script = [READ, FIX, DONE, run, READ, mul, DONE]
        code, text, _ = self.chat(["fix add", "now list files", "add mul"], script, [interrupt])
        self.assertIn("Stopped. Its containers are removed", text)
        branches = git(self.repo, "branch", "--list", "agent/*").split()
        self.assertEqual(len(branches), 1)
        self.assertIn("def mul", git(self.repo, "show", f"{branches[0]}:calc.py"))
        self.assertIn("a + b", git(self.repo, "show", f"{branches[0]}:calc.py"))

    def test_no_start_means_nothing_runs(self):
        patches = (mock.patch("agent.chat.ask_human", lambda q: NO),
                   mock.patch.dict("os.environ", {"HOME": str(self.home)}),
                   mock.patch("agent.chat.repository", return_value=self.repo),
                   mock.patch("agent.chat.screen.choose_model", return_value=(FakeModel([READ]), ())))
        with patches[0], patches[1], patches[2], patches[3], contextlib.redirect_stdout(io.StringIO()) as out:
            args = argparse.Namespace(model="x", files=[], proof=[], command="", image="python:3.12-slim")
            self.assertEqual(chat.Chat(args).run(), 2)
        self.assertIn("Nothing was started", out.getvalue())

    def test_the_agents_words_are_shown_safely(self):
        talk = {"role": "assistant", "content": "I will fix it \x1b[2J now", "tool_calls": FIX["tool_calls"]}
        code, text, _ = self.chat(["fix add"], [READ, talk, DONE])
        self.assertIn("The agent says: I will fix it <U+001B>[2J now", text)

    def test_a_follow_up_without_new_work_is_not_verified(self):
        two = {"role": "assistant", "content": None, "tool_calls": DONE["tool_calls"] + [
            {**DONE["tool_calls"][0], "id": "second"}]}
        seen = []

        def look(messages):
            seen.append("reject strings" in str(messages))
            return DONE

        code, text, model = self.chat(["fix add", "make add reject strings, with a new test"], [READ, FIX, two, look, DONE, DONE])  # noqa: E501
        self.assertEqual(text.count("VERIFIED"), 1, text)
        self.assertIn("FAILED", text.split("VERIFIED", 1)[1])
        self.assertIn("changes nothing", text)
        self.assertEqual(seen, [True])  # the model saw the follow-up before anything else happened

    def test_a_new_test_in_an_existing_test_file_proves_a_follow_up(self):
        add = call_message("edit", path="test_calc.py", old="class T(unittest.TestCase):\n",
                           new="class T(unittest.TestCase):\n    def test_neg(self): self.assertEqual(add(-1, -1), -2)\n")  # noqa: E501
        neg = call_message("edit", path="calc.py", old="    return a + b", new="    return a + b if a >= 0 else a + b")
        script = [READ, FIX, DONE, call_message("read", path="test_calc.py", first=1), add, READ, neg, DONE, DONE, DONE]
        code, text, _ = self.chat(["fix add", "make negatives work, with a test"], script, [Answer(True)])
        # test_neg passes already after the first fix, so it is no proof of this follow-up: honest UNVERIFIABLE
        self.assertIn("did not fail without the change: test_calc.T.test_neg", text)
        self.assertNotIn("test_calc.T.test_old", text.split("make negatives")[-1])

    def test_a_stop_does_not_turn_a_small_yes_into_a_whole_file(self):
        weak = "import unittest\nfrom calc import add, helper\nclass T(unittest.TestCase):\n    def test_proof(self): self.assertTrue(True)\n"  # noqa: E501
        rewrite = call_message("run_command", program=["python", "-c", f"open('test_calc.py','w').write({weak!r})"])
        helper = call_message("edit", path="calc.py", old="    return a - b\n", new="    return a - b\ndef helper(): pass\n")  # noqa: E501
        bad = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "read", "arguments": "{oops"}}]}
        comment = call_message("edit", path="test_calc.py", old="import unittest\n", new="import unittest  # reviewed\n")  # noqa: E501
        script = [READ, helper, rewrite, DONE, DONE, DONE, bad, bad, bad,
                  call_message("read", path="test_calc.py", first=1), comment, DONE, DONE, DONE]
        code, text, _ = self.chat(["add a helper", "go on", "add a comment to the tests"], script,
                                  [Answer(True), Answer(True)], proof=())
        self.assertNotIn("Independent check: VERIFIED", text)

    def test_ctrl_c_during_the_check_takes_the_unchecked_commit_off_the_branch(self):
        from agent import checker

        real, calls = checker.Checker.check, []

        def check(self):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(self)

        mul = call_message("edit", path="calc.py", old="    return a + b\n", new="    return a + b\ndef mul(a, b):\n    return a * b\n")  # noqa: E501
        with mock.patch.object(checker.Checker, "check", check):
            code, text, _ = self.chat(["fix add", "add mul"], [READ, FIX, DONE, mul, DONE])
        branch = git(self.repo, "branch", "--list", "agent/*").split()[0]
        self.assertNotIn("def mul", git(self.repo, "show", f"{branch}:calc.py"))
        self.assertIn("goes on from the last checked work", text)

    def test_open_questions_and_odd_proof_text(self):
        q = call_message("ask", question="Which function?")
        code, text, _ = self.chat(["fix something", "/proof 'a", "/quit"], [q, READ, FIX, DONE],
                                  [Answer(True, reason="add")])
        self.assertIn("Which function?", self.questions[1])
        args = argparse.Namespace(model="script", files=[], proof=[], command="sh t.sh", image="python:3.12-slim")
        with mock.patch("agent.chat.repository", return_value=self.repo), \
                mock.patch("agent.chat.screen.choose_model", return_value=(FakeModel([]), ())), \
                mock.patch("builtins.input", side_effect=["/proof 'a", "/quit"]), contextlib.redirect_stdout(io.StringIO()) as out:  # noqa: E501
            c = chat.Chat(args)
            self.assertIsNone(c.ask())
        self.assertIn("Stopped: No closing quotation", out.getvalue())

