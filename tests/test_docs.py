import re
import unittest
from pathlib import Path

from agent import __main__

ROOT = Path(__file__).parent.parent
PAGES = [ROOT / "README.md", ROOT / "CASE-STUDY.md", *sorted((ROOT / "docs").glob("*.md"))]


class Docs(unittest.TestCase):
    def test_every_command_in_the_readme_exists(self):
        text = (ROOT / "README.md").read_text()
        usage = __main__.USAGE + " key "
        used = set(re.findall(r"`agent ([a-z][a-z-]*)", text)) | set(re.findall(r"^agent ([a-z][a-z-]*)", text, re.M))
        self.assertTrue(used)
        for word in used:
            self.assertIn(f" {word} ", usage, word)

    def test_links_point_to_files_that_exist(self):
        for page in PAGES:
            for link in re.findall(r"\]\(([^)#:]+)\)", page.read_text()):
                self.assertTrue((page.parent / link).exists(), (page.name, link))

    def test_nothing_private_and_limits_said(self):
        for page in PAGES:
            text = page.read_text()
            self.assertNotRegex(text, r"/Users/\w|AIza[0-9A-Za-z_-]{30}|@[a-z]+\.com", page.name)
        readme = (ROOT / "README.md").read_text()
        said = ("Not built in this version", "never changed", "a promise that the code is right", "kernel")
        for words in said:
            self.assertIn(words, readme)
        self.assertIn("No user study", (ROOT / "CASE-STUDY.md").read_text())

    def test_every_chat_command_is_described(self):
        from agent import chat

        readme = (ROOT / "README.md").read_text()
        for command in re.findall(r"(/[a-z]+)", chat.HELP):
            self.assertIn(f"`{command}", readme, command)
