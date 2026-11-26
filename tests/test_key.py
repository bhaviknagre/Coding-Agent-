import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent import key, provider
from agent.approve import NO
from agent.checker import Contract
from agent.model import ModelError, call_message
from tests.test_box import git, own_items
from tests.test_checker import BUG, TEST

FAKE = "AIza" + "FakeKeyForTests0000000000000000000xyz"  # the shape of a Gemini key; never a real one
OTHER = "zzMarker.key-" + "1234567890abcdefxyz"  # a key that no general pattern finds: only exact masking can
ROOT = Path(__file__).parent.parent


@unittest.skipUnless(sys.platform == "darwin", "the Keychain is on macOS only")
class Keychain(unittest.TestCase):
    """A keychain of its own, so the developer's real key is never read or changed."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.chain = str(Path(d.name) / "test.keychain")
        subprocess.run(["security", "create-keychain", "-p", "test", self.chain], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["security", "delete-keychain", self.chain], capture_output=True)

    def test_store_load_remove(self):
        self.assertFalse(key.exists("gemini", self.chain))
        key.store("gemini", FAKE, self.chain)
        self.assertTrue(key.exists("gemini", self.chain))
        self.assertEqual(key.load("gemini", self.chain), FAKE)
        key.store("gemini", FAKE.replace("xyz", "abc"), self.chain)  # set again replaces it
        self.assertTrue(key.load("gemini", self.chain).endswith("abc"))
        self.assertTrue(key.remove("gemini", self.chain))
        self.assertEqual(key.load("gemini", self.chain), "")

    def test_odd_input_is_not_stored(self):
        for bad in ("", "short", "has space " + "x" * 30, "x" * 30 + "\nadd-generic-password -s evil", "ä" * 30):
            with self.assertRaises(key.KeyError_):
                key.store("gemini", bad, self.chain)
        self.assertFalse(key.exists("gemini", self.chain))

    def test_the_key_is_never_in_a_command_line(self):
        seen = []
        real = subprocess.run

        def spy(args, *a, **k):
            seen.append(" ".join(args))
            return real(args, *a, **k)

        with mock.patch.object(subprocess, "run", spy):
            key.store("gemini", FAKE, self.chain)
            key.load("gemini", self.chain)
            key.exists("gemini", self.chain)
        self.assertTrue(seen)
        self.assertNotIn(FAKE, " ".join(seen))

    def test_the_command_never_shows_it(self):
        env = {**os.environ, "CODING_AGENT_KEYCHAIN": self.chain, "PYTHONPATH": str(ROOT)}
        key.store("gemini", FAKE, self.chain)
        for args in (["status", "gemini"], ["remove", "gemini"], ["status", "gemini"], ["x"], ["set", "gemini"]):
            p = subprocess.run([sys.executable, "-m", "agent", "key", *args], env=env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
            self.assertNotIn(FAKE, p.stdout + p.stderr)
        self.assertIn("terminal", p.stdout)  # the last one: 'set' without a person


class FakeService(http.server.BaseHTTPRequestHandler):
    replies: list = []
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append((dict(self.headers), json.loads(body)))
        status, reply, headers = type(self).replies.pop(0)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())

    def log_message(self, *a):
        pass


def answer(message, finish="tool_calls", tokens=7):
    return 200, {"choices": [{"message": message, "finish_reason": finish}], "usage": {"total_tokens": tokens}}, {}


class Service(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        FakeService.replies, FakeService.seen = [], []
        self.waits = []
        address = f"http://127.0.0.1:{self.server.server_port}/v1beta/openai/chat/completions"
        limiter = provider.Limiter(600, sleep=self.waits.append)
        self.model = provider.OpenAIFormat(FAKE, "test-model", address, limiter=limiter, sleep=self.waits.append)

    def test_a_reply_is_returned_unchanged_with_its_own_fields(self):
        message = call_message("read", path="a", first=1)
        message["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "c2ln"}}
        FakeService.replies = [answer(message)]
        r = self.model.call([{"role": "user", "content": "hi"}], [{"type": "function"}])
        self.assertEqual((r.message, r.cut, r.tokens), (message, False, 7))
        headers, body = FakeService.seen[0]
        self.assertEqual(headers["Authorization"], f"Bearer {FAKE}")
        self.assertEqual((body["model"], body["messages"][0]["content"]), ("test-model", "hi"))

    def test_cut_reply(self):
        FakeService.replies = [answer({"role": "assistant", "content": "x"}, finish="length")]
        self.assertTrue(self.model.call([], []).cut)

    def test_busy_service_is_tried_again_then_given_up(self):
        busy = (429, {"error": "slow down"}, {"Retry-After": "7"})
        FakeService.replies = [busy, (503, {}, {}), answer({"role": "assistant", "content": "ok"})]
        self.assertEqual(self.model.call([], []).message["content"], "ok")
        self.assertIn(7.0, [round(w) for w in self.waits])
        FakeService.replies = [busy] * provider.TRIES
        with self.assertRaisesRegex(ModelError, "429"):
            self.model.call([], [])

    def test_refused_key_and_errors_never_show_the_key(self):
        FakeService.replies = [(401, {"error": f"bad key {FAKE}"}, {})]
        with self.assertRaisesRegex(ModelError, "agent key set gemini") as e:
            self.model.call([], [])
        self.assertNotIn(FAKE, str(e.exception))
        FakeService.replies = [(400, {"error": f"you sent {FAKE}"}, {})]
        with self.assertRaises(ModelError) as e:
            self.model.call([], [])
        self.assertNotIn(FAKE, str(e.exception))
        FakeService.replies = [(200, {"no": "choices"}, {})]
        with self.assertRaisesRegex(ModelError, "without a message"):
            self.model.call([], [])

    def test_part_of_the_key_at_the_edge_is_not_shown(self):
        FakeService.replies = [(400, {"e": " " * 1975 + "bad key " + FAKE}, {})]
        with self.assertRaises(ModelError) as e:
            self.model.call([], [])
        self.assertNotIn(FAKE[:10], str(e.exception))

    def test_odd_replies_give_a_clear_message(self):
        for reply in ({"choices": [{"message": "hi"}]}, {"choices": [{"message": None}]},
                      {"choices": [{"message": {"role": "assistant"}}], "usage": {"total_tokens": 1e400}}):
            FakeService.replies = [(200, reply, {})]
            with self.assertRaises(ModelError):
                self.model.call([], [])

    def test_a_slow_service_does_not_hold_us(self):
        class Slow:
            def read1(self, n):
                time.sleep(0.05)
                return b" "

        with mock.patch.object(provider, "CALL_SECONDS", 0.3), self.assertRaises(TimeoutError):
            self.model._read(Slow())

    def test_no_plain_address_and_no_redirect(self):
        for address in ("http://example.com/x", "http://127.0.0.1:1@localhost:80/x", "http://127.0.0.1:80#@[::1]:80/",
                        "https://user@example.com/", "ftp://127.0.0.1:1/"):
            with self.assertRaises(ModelError, msg=address):
                provider.OpenAIFormat(FAKE, address=address)
        FakeService.replies = [(302, {}, {"Location": "http://127.0.0.1:1/steal"})]
        with self.assertRaises(ModelError):
            self.model.call([], [])
        self.assertEqual(len(FakeService.seen), 1)

    def test_calls_are_spaced(self):
        t = [0.0]
        waits = []
        limiter = provider.Limiter(5, clock=lambda: t[0], sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)))
        for _ in range(3):
            limiter.wait()
        self.assertEqual([round(w) for w in waits], [12, 12])


class KeyNeverLeaks(unittest.TestCase):
    """AC-5: a full task, an error and a cancel leave no copy of the key anywhere it could be read later."""

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
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        FakeService.seen = []

    def run_task(self, secret, replies, cancel=None):
        from agent.log import Log
        from agent.loop import Task

        FakeService.replies = replies
        address = f"http://127.0.0.1:{self.server.server_port}/x"
        model = provider.OpenAIFormat(secret, "m", address, limiter=provider.Limiter(6000, sleep=lambda s: None),
                                      sleep=lambda s: None)
        log = Log(self.home / "data" / "events.db", (secret,))
        contract = Contract("fix", ("*",), ("python", "-m", "unittest"), ("test_calc.T.test_proof",))
        try:
            return Task(self.repo, contract, "python:3.12-slim", model, lambda q: NO, log, secrets=(secret,),
                        cancel=cancel).run()
        finally:
            log.close()

    def nowhere(self, secret):
        stored = b"".join(p.read_bytes() for p in (self.home / "data").rglob("*") if p.is_file())
        history = subprocess.run(["git", "-C", str(self.repo), "log", "--all", "-p"], capture_output=True).stdout
        self.assertNotIn(secret.encode(), stored + history)
        # The model's own replies go back unchanged, so only what we wrote is checked.
        ours = [m for _, body in FakeService.seen for m in body["messages"] if m["role"] != "assistant"]
        self.assertTrue(ours)
        self.assertNotIn(secret, json.dumps(ours))

    def test_full_task_error_and_cancel(self):
        from agent import receipt

        for secret in (FAKE, OTHER):
            with self.subTest(secret[:6]):
                steps = [call_message("read", path="calc.py", first=1), call_message("run_command", program=["echo", secret]),  # noqa: E501
                         call_message("edit", path="calc.py", old="a - b", new="a + b"), call_message("finish", summary=secret)]  # noqa: E501
                o = self.run_task(secret, [answer(m) for m in steps])
                self.assertEqual(o.state, "VERIFIED", o)
                receipt.save(o.receipt, self.home / "data" / "r", (secret,))
                o = self.run_task(secret, [answer(steps[0])] + [(500, {"e": secret}, {})] * 4)
                self.assertEqual(o.state, "BLOCKED")
                self.assertNotIn(secret, o.reason)
                cancel = threading.Event()
                cancel.set()  # a cancel at the very start, while the files are copied
                with self.assertRaises(KeyboardInterrupt):
                    self.run_task(secret, [answer(steps[0])], cancel)
                self.nowhere(secret)
