"""Other languages: find the project's language, its image and test command, and read its test reports.

A report is a claim by the project's own code, like every output. It is only used when it is well formed, holds
tests, agrees with the exit code, and (JUnit XML) was written to a file name chosen at random for this one run.
"""
import json
import re
import xml.etree.ElementTree as ET  # noqa: S405 - DOCTYPE and ENTITY are refused before parsing
from pathlib import Path
from typing import NamedTuple

from agent import box

REPORT = "{report}"  # in a command: where it writes JUnit XML
MOST = 5_000_000
ORDER = ["ok", "xfail", "skip", "fail", "error"]  # the worst result of a test counts


class Language(NamedTuple):
    name: str
    marker: str  # a file in the top folder
    image: str
    size: str  # to download, roughly
    command: tuple[str, ...]


LANGUAGES = (
    Language("JavaScript", "package.json", "node:22-alpine", "60 MB",
             ("node", "--test", "--test-reporter=junit", f"--test-reporter-destination={REPORT}")),
    Language("Go", "go.mod", "golang:1.23-alpine", "80 MB", ("go", "test", "-json", "./...")),
    Language("Rust", "Cargo.toml", "rust:1-slim", "300 MB", ("cargo", "test", "--offline")),
    Language("Java", "pom.xml", "maven:3-eclipse-temurin-21", "250 MB", ("mvn", "-o", "-q", "test")),
)
# Tools in these images write their caches to the home folder, the only writable place besides the project.
# Programs cannot run from /tmp, so Go builds its test program in the project copy (and removes it again).
ENV = {  # inside the container: its own /tmp, the only writable place besides the project copy
    "GOPATH": "/tmp/go", "GOCACHE": "/tmp/go-cache", "CARGO_HOME": "/tmp/cargo",  # noqa: S108
    "GOTMPDIR": "/work", "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod", "MAVEN_OPTS": "-Dmaven.repo.local=/tmp/m2",
}
PYTHON = re.compile(r"(^|/)([^/]+\.py|pyproject\.toml|setup\.py|requirements[^/]*\.txt)$")


def test_command(repo: Path, commit: str = "HEAD") -> tuple[str, ...]:
    """The test command when none is given: the language's own, or unittest or pytest for Python."""
    lang = language(repo, commit)
    if lang:
        return lang.command
    names = {e.name for e in box.list_commit(repo, commit)[0]}
    uses_pytest = bool({"pytest.ini", "conftest.py"} & names) or any(n.endswith("/conftest.py") for n in names)
    return ("python", "-m", "pytest") if uses_pytest else ("python", "-m", "unittest")


def language(repo: Path, commit: str = "HEAD") -> Language | None:
    """None for Python, which is also the answer when nothing else is found."""
    names = [e.name for e in box.list_commit(repo, commit)[0]]
    if any(PYTHON.search(n) for n in names):
        return None
    return next((x for x in LANGUAGES if x.marker in names), None)


def kind(command: tuple[str, ...]) -> str:
    """'junit' or 'gojson' when the command's report gives each test by name, else ''."""
    if any(REPORT in part for part in command):
        return "junit"
    if command[:2] == ("go", "test") and "-json" in command:
        return "gojson"
    return ""


def _worst(tests: dict[str, str], name: str, outcome: str) -> None:
    tests[name] = max(tests.get(name, "ok"), outcome, key=ORDER.index)


def read_junit(text: str, code: int) -> dict[str, str] | None:
    if not text.strip() or len(text) > MOST or re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        return None
    try:
        root = ET.fromstring(text)  # noqa: S314 - no DOCTYPE, so no entities
    except ET.ParseError:
        return None
    tests: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        if not name:
            return None
        group = case.get("classname") or ""
        tags = {child.tag for child in case}
        outcome = "error" if "error" in tags else "fail" if "failure" in tags else "skip" if "skipped" in tags else "ok"
        _worst(tests, f"{group}.{name}" if group else name, outcome)
    return _agrees(tests, code)


def read_gojson(output: str, code: int) -> dict[str, str] | None:
    """go test -json: what a test prints is wrapped in an "output" event, so it cannot pass for a result."""
    tests: dict[str, str] = {}
    outcomes = {"pass": "ok", "fail": "fail", "skip": "skip"}
    for line in output.splitlines():
        if not line.startswith("{"):
            continue  # build errors and the like
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if isinstance(event, dict) and event.get("Test") and event.get("Action") in outcomes:
            _worst(tests, f"{event.get('Package', '')}.{event['Test']}", outcomes[event["Action"]])
    return _agrees(tests, code)


def _agrees(tests: dict[str, str], code: int) -> dict[str, str] | None:
    """Tests, and failures if and only if the command failed."""
    if not tests:
        return None
    bad = bool({"fail", "error"} & set(tests.values()))
    return tests if bad == (code != 0) else None


def missing(image: str) -> str:
    """How to get a language image that is not on this computer; nothing is downloaded here."""
    size = next((x.size for x in LANGUAGES if x.image == image), "")
    return (f"the image {image} is not on this computer. Next: docker pull {image}"
            + (f"  (about {size}), then start again" if size else ", then start again"))
