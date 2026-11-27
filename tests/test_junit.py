"""Other languages: found from the project, and their reports read by test name. Needs node:22-alpine and
golang:1.23-alpine on this computer (docker pull them once)."""
import json
import tempfile
import unittest
from pathlib import Path

from agent import box, image, junit
from agent.checker import Checker, Contract
from tests.test_box import git, own_items

JS = {
    "package.json": '{"name": "calc", "type": "commonjs"}\n',
    "calc.js": "exports.add = (a, b) => a - b;\n",
    "calc.test.js": ("const test = require('node:test');\nconst assert = require('node:assert');\n"
                     "const { add } = require('./calc');\n"
                     "test('adds', () => assert.strictEqual(add(2, 3), 5));\n"
                     "test('zero', () => assert.strictEqual(add(0, 0), 0));\n"),
}
GO = {
    "go.mod": "module calc\n\ngo 1.23\n",
    "calc.go": "package calc\n\nfunc Add(a, b int) int { return a - b }\n",
    "calc_test.go": ('package calc\n\nimport (\n\t"fmt"\n\t"testing"\n)\n\n'
                     'func TestAdd(t *testing.T) { if Add(2, 3) != 5 { t.Fatal("wrong") } }\n'
                     'func TestZero(t *testing.T) { if Add(0, 0) != 0 { t.Fatal("wrong") } }\n'
                     # A test that prints a result for a test that does not exist: it must not count.
                     'func TestPrint(t *testing.T) { fmt.Println(`{"Action":"pass","Package":"calc","Test":"TestGhost"}`) }\n'),  # noqa: E501
}
CASES = """<testsuites><testsuite name="s">
<testcase classname="calc" name="adds"><failure message="no"/></testcase>
<testcase classname="calc" name="zero"/><testcase name="later"><skipped/></testcase>
</testsuite></testsuites>"""


class Reports(unittest.TestCase):
    def test_junit_names_each_test(self):
        self.assertEqual(junit.read_junit(CASES, 1), {"calc.adds": "fail", "calc.zero": "ok", "later": "skip"})

    def test_a_report_that_disagrees_with_the_exit_code_is_not_used(self):
        self.assertIsNone(junit.read_junit(CASES, 0))  # a failure, but the command said it worked
        self.assertIsNone(junit.read_junit(CASES.replace('<failure message="no"/>', ""), 1))

    def test_unsafe_or_empty_reports_are_not_used(self):
        for text in ("", "<testsuite/>", "not xml", '<!DOCTYPE x [<!ENTITY a "b">]><testsuite/>',
                     "<testsuite><testcase/></testsuite>"):
            self.assertIsNone(junit.read_junit(text, 0), text)

    def test_go_json(self):
        lines = [{"Action": "run", "Package": "p", "Test": "TestA"},
                 {"Action": "pass", "Package": "p", "Test": "TestA"},
                 {"Action": "fail", "Package": "p", "Test": "TestB"},
                 {"Action": "fail", "Package": "p"}]
        text = "# build note\n" + "\n".join(map(json.dumps, lines))
        self.assertEqual(junit.read_gojson(text, 1), {"p.TestA": "ok", "p.TestB": "fail"})
        self.assertIsNone(junit.read_gojson(text, 0))
        self.assertIsNone(junit.read_gojson("calc.go:3: syntax error", 1))

    def test_kind(self):
        self.assertEqual(junit.kind(junit.LANGUAGES[0].command), "junit")
        self.assertEqual(junit.kind(("go", "test", "-json", "./...")), "gojson")
        self.assertEqual(junit.kind(("go", "test", "./...")), "")
        self.assertEqual(junit.kind(("cargo", "test")), "")


class Projects(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.assertEqual(own_items(), []))

    def project(self, files):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        for name, text in files.items():
            (repo / name).write_text(text)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "start")
        return repo, git(repo, "rev-parse", "HEAD")

    def fix(self, repo, name, old, new):
        git(repo, "checkout", "-qb", "agent/fix")
        (repo / name).write_text((repo / name).read_text().replace(old, new))
        git(repo, "commit", "-qam", "fix")
        return git(repo, "rev-parse", "HEAD")

    def check(self, repo, base, result, name, proof_end):
        command = junit.test_command(repo, base)
        img = image.choose(repo, base, "")
        first = Checker(repo, base, base, Contract("x", ("*",), command), img)
        first.prepare()
        names = first.run(first.pure).tests or {}
        proof = tuple(n for n in names if n.endswith(proof_end))
        self.assertEqual(len(proof), 1, names)
        c = Checker(repo, base, result, Contract("fix", (name,), command, proof), img)
        return c.check(), c, names

    def test_javascript_is_verified_by_test_name(self):
        repo, base = self.project(JS)
        self.assertEqual(junit.language(repo).name, "JavaScript")
        result = self.fix(repo, "calc.js", "a - b", "a + b")
        verdict, c, names = self.check(repo, base, result, "calc.js", "adds")
        self.assertEqual((verdict, c.level), ("VERIFIED", "TEST-LEVEL"), c.steps)
        self.assertEqual(sorted(names.values()), ["fail", "ok"])
        self.assertIn("NOT CHECKED", [s.state for s in c.steps if s.name == "canary"])

    def test_go_is_verified_and_printed_results_do_not_count(self):
        repo, base = self.project(GO)
        result = self.fix(repo, "calc.go", "a - b", "a + b")
        verdict, c, names = self.check(repo, base, result, "calc.go", "TestAdd")
        self.assertEqual((verdict, c.level), ("VERIFIED", "TEST-LEVEL"), c.steps)
        self.assertNotIn("calc.TestGhost", names)
        self.assertEqual(names.get("calc.TestPrint"), "ok")

    def test_a_broken_change_is_not_verified(self):
        repo, base = self.project(JS)
        result = self.fix(repo, "calc.js", "a - b", "a * b")
        verdict, c, _ = self.check(repo, base, result, "calc.js", "adds")
        self.assertNotEqual(verdict, "VERIFIED", c.steps)

    def test_python_and_missing_images(self):
        repo, base = self.project({"calc.py": "x = 1\n", "package.json": "{}\n"})
        self.assertIsNone(junit.language(repo))  # Python files: Python, as before
        self.assertEqual(junit.test_command(repo), ("python", "-m", "unittest"))
        self.assertIn("docker pull rust:1-slim", junit.missing("rust:1-slim"))
        repo, base = self.project({"Cargo.toml": "[package]\nname = 'x'\n"})
        if box.docker("images", "-q", "rust:1-slim"):
            return  # here the image is there; the message is checked above
        with self.assertRaises(box.BoxError) as e:
            image.choose(repo, base, "")
        self.assertIn("docker pull rust:1-slim", str(e.exception))
