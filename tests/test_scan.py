import subprocess
import tempfile
import unittest
from pathlib import Path

from agent import scan

# Built from pieces so that this file itself passes the scan.
FAKES = {
    "google-key": "AI" + "za" + "x" * 35,
    "openai-style-key": "s" + "k-" + "a" * 24,
    "groq-key": "gs" + "k_" + "a" * 24,
    "github-token": "gh" + "p_" + "a" * 36,
    "aws-key": "AK" + "IA" + "A" * 16,
    "slack-token": "xo" + "xb-" + "1" * 12,
    "private-key": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "assigned-secret": "pass" + "word = '" + "a1" * 10 + "'",
    "personal-email": "someone" + "@" + "mail.test",
    "home-path": "/Us" + "ers/someone/x",
}


class Scan(unittest.TestCase):
    def test_every_rule_finds_its_fake(self):
        self.assertEqual(set(FAKES), {name for name, _ in scan.RULES})
        for rule, text in FAKES.items():
            self.assertIn((1, rule), scan.scan_text(text), rule)

    def test_allowed_text(self):
        self.assertEqual(scan.scan_text("1+x@users.noreply.github.com a@example.com key = None"), [])

    def test_staged_secret_removed_from_the_working_file_is_still_found(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=d, check=True)
            (root / "a.txt").write_text(FAKES["google-key"])
            subprocess.run(["git", "add", "a.txt"], cwd=d, check=True)
            (root / "a.txt").write_text("clean")
            (root / ".env").write_text("x")
            found = scan.scan_repo(root)
        self.assertEqual(found, [".env: blocked-file-name", "a.txt:1: google-key"])
        self.assertNotIn(FAKES["google-key"], "".join(found))
