import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent import box, image
from agent.approve import Answer
from agent.checker import Checker, Contract
from tests.test_box import IMAGE, git

SITE = "/usr/local/lib/python3.12/site-packages/extra"
# A package the tests need, put in by the Dockerfile; no network is needed for it. The build also proves that a
# secret-looking file is not sent to Docker, and that the files are those of the commit.
DOCKERFILE = f"""FROM {IMAGE}
COPY . /ctx
RUN test ! -e /ctx/.env && test -e /ctx/calc.py && mkdir -p {SITE} && echo 'def two(): return 2' > {SITE}/__init__.py
"""
CALC = "from extra import two\ndef add(a, b):\n    return a - b\n"
TEST = ("import unittest\nfrom calc import add, two\nclass T(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n    def test_two(self): self.assertEqual(two(), 2)\n")


class ProjectImage(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q")
        # Each test its own files, so its own image name: tests run at the same time.
        files = {"calc.py": CALC, "test_calc.py": TEST, "Dockerfile": DOCKERFILE, ".env": "KEY=x\n",
                 "id.txt": f"{uuid.uuid4()}\n"}
        for name, text in files.items():
            (self.repo / name).write_text(text)
        git(self.repo, "add", "-f", ".")
        git(self.repo, "commit", "-qm", "start")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.asked = []

    def tearDown(self):
        subprocess.run(["docker", "rmi", "-f", image.tag(self.repo, self.base)], capture_output=True)

    def answer(self, yes):
        def ask(q):
            self.asked.append(q)
            return Answer(yes)
        return ask

    def test_no_dockerfile_means_plain_python(self):
        git(self.repo, "rm", "-q", "Dockerfile")
        git(self.repo, "commit", "-qm", "none")
        self.assertEqual(image.choose(self.repo, git(self.repo, "rev-parse", "HEAD"), "", self.answer(True)),
                         box.HELPER_IMAGE)
        self.assertEqual(self.asked, [])

    def test_given_image_wins(self):
        self.assertEqual(image.choose(self.repo, self.base, "mine:1", self.answer(True)), "mine:1")
        self.assertEqual(self.asked, [])

    def test_no_means_nothing_is_built(self):
        self.assertEqual(image.choose(self.repo, self.base, "", self.answer(False)), box.HELPER_IMAGE)
        self.assertIn("RUN test ! -e /ctx/.env", self.asked[0])  # the whole file was shown
        self.assertEqual(box.docker("images", "-q", image.tag(self.repo, self.base)), "")

    def test_a_package_from_the_dockerfile_makes_it_verified(self):
        name = image.choose(self.repo, self.base, "", self.answer(True))
        self.assertEqual(name, image.tag(self.repo, self.base))
        self.assertEqual(box.docker("image", "inspect", "-f", '{{index .Config.Labels "coding-agent-v2"}}', name),
                         "image")
        (self.repo / "calc.py").write_text(CALC.replace("a - b", "a + b"))
        git(self.repo, "commit", "-qam", "fix")
        result = git(self.repo, "rev-parse", "HEAD")
        contract = Contract("fix", ("calc.py",), ("python", "-m", "unittest"), ("test_calc.T.test_add",))
        self.assertEqual(Checker(self.repo, self.base, result, contract, name).check(), "VERIFIED")
        self.assertNotEqual(Checker(self.repo, self.base, result, contract, IMAGE).check(), "VERIFIED")
        # built once: the next time nothing is asked
        self.asked.clear()
        self.assertEqual(image.choose(self.repo, self.base, "", self.answer(True)), name)
        self.assertEqual(self.asked, [])

    def test_only_the_commits_dockerfile_counts(self):
        git(self.repo, "rm", "-q", "--cached", "Dockerfile")
        git(self.repo, "commit", "-qm", "untracked now")  # the file is still in the folder
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(image.choose(self.repo, head, "", self.answer(True)), box.HELPER_IMAGE)
        self.assertEqual(self.asked, [])

    def test_a_broken_dockerfile_says_why(self):
        (self.repo / "Dockerfile").write_text(f"FROM {IMAGE}\nRUN exit 3\n")
        git(self.repo, "commit", "-qam", "broken")
        head = git(self.repo, "rev-parse", "HEAD")
        try:
            with self.assertRaises(box.BoxError) as e:
                image.choose(self.repo, head, "", self.answer(True))
            self.assertIn("did not build", str(e.exception))
        finally:
            subprocess.run(["docker", "rmi", "-f", image.tag(self.repo, head)], capture_output=True)

    def test_verify_takes_the_image_from_the_base_not_from_the_branch(self):
        from agent import verify

        seen = []
        with mock.patch("agent.verify.image.choose", lambda repo, commit, given: seen.append(commit) or IMAGE), \
                mock.patch("agent.verify.Checker") as c:
            c.return_value.prepare.side_effect = SystemExit
            git(self.repo, "checkout", "-qb", "agent/x")
            (self.repo / "calc.py").write_text(CALC.replace("a - b", "a + b"))
            git(self.repo, "commit", "-qam", "fix")
            git(self.repo, "checkout", "-q", "-")
            with self.assertRaises(SystemExit):
                verify.verify(["agent/x"], self.repo)
        self.assertEqual(seen, [self.base])
