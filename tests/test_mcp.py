import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import mcp, verify
from tests.test_box import git

BUG = "def add(a, b):\n    return a - b\n"
TEST = ("import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n")


def talk(messages, here):
    out = io.StringIO()
    mcp.serve(io.StringIO("".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)), out, here)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(n, name, args):
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": args}}


class Protocol(unittest.TestCase):
    def test_hello_and_the_tools(self):
        got = talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"}], Path("."))
        self.assertEqual([g["id"] for g in got], [1, 2, 3])  # the notification gets no answer
        self.assertEqual(got[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in got[1]["result"]["tools"]], ["verify", "receipt"])
        old = talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999"}}],
                   Path("."))
        self.assertEqual(old[0]["result"]["protocolVersion"], mcp.PROTOCOLS[0])

    def test_bad_messages_get_errors_not_a_crash(self):
        got = talk(["{not json", "[1, 2]", {"jsonrpc": "2.0", "id": 4, "method": "nosuch"},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "rm", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": []},
                    "x" * (mcp.MOST + 10), {"jsonrpc": "2.0", "id": 7, "method": "ping"}], Path("."))
        codes = [g.get("error", {}).get("code") for g in got]
        self.assertEqual(codes, [-32700, -32600, -32601, -32602, -32602, -32600, None])
        self.assertEqual(got[-1]["id"], 7)  # still answering after all that

    def test_arguments_cannot_become_options(self):
        for args in ({"branch": "--help"}, {"branch": "a b"}, {"branch": "x", "proof": ["-k"]},
                     {"branch": "x", "proof": "t"}, {"branch": "x", "command": "a\nb"}, {"branch": "x", "rm": 1},
                     {}, {"branch": "x", "repository": "/nonexistent"}):
            got = talk([call(1, "verify", args)], Path("."))[0]["result"]
            self.assertTrue(got["isError"], args)
            self.assertIn("Not checked", got["content"][0]["text"])
        for rid in ("../../etc", "ABCDEF123456", 5):
            self.assertTrue(talk([call(1, "receipt", {"id": rid})], Path("."))[0]["result"]["isError"])


class Check(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "calc.py").write_text(BUG)
        (self.repo / "test_calc.py").write_text(TEST)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "start")
        for branch, files in (("agent/good", {"calc.py": BUG.replace("a - b", "a + b")}),
                              ("agent/cheat", {"test_calc.py": TEST.replace("add(2, 3), 5", "5, 5")})):
            git(self.repo, "checkout", "-qb", branch, "main")
            for name, text in files.items():
                (self.repo / name).write_text(text)
            git(self.repo, "commit", "-qam", branch)
        git(self.repo, "checkout", "-q", "main")
        self.before = git(self.repo, "status", "--porcelain"), git(self.repo, "rev-parse", "HEAD")

    def test_verify_by_mcp_gives_the_verdict_of_agent_verify_and_changes_nothing(self):
        proof = ["test_calc.T.test_add"]
        got = talk([call(1, "verify", {"branch": "agent/good", "proof": proof}),
                    call(2, "verify", {"branch": "agent/cheat", "proof": proof, "repository": str(self.repo)})],
                   self.repo)
        good, cheat = (g["result"]["content"][0]["text"] for g in got)
        self.assertIn("VERIFIED  (TEST-LEVEL)", good)
        self.assertNotIn("VERIFIED", cheat.split("\n\n")[-1].split("Checks")[0])  # the original test is used
        with mock.patch("agent.verify.confirm", return_value=True), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(verify.verify(["agent/good", "--proof", proof[0]], self.repo), 0)
        self.assertEqual((git(self.repo, "status", "--porcelain"), git(self.repo, "rev-parse", "HEAD")), self.before)
        rid = good.split("receipts/")[1].split("/")[0]
        text = talk([call(3, "receipt", {"id": rid})], self.repo)[0]["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("VERIFIED"))

    def test_the_real_program_speaks_only_json_on_its_output(self):
        root = Path(__file__).parent.parent
        p = subprocess.run([sys.executable, "-m", "agent", "mcp"], cwd=self.repo, capture_output=True, text=True,
                           input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n", timeout=120,
                           env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                "HOME": tempfile.mkdtemp()})
        lines = p.stdout.splitlines()
        self.assertEqual(len(lines), 1, p.stdout + p.stderr)
        self.assertEqual(json.loads(lines[0])["id"], 1)
