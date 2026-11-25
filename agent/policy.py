"""What may happen: DENY first, then ASK, then ALLOW. What matches nothing asks. No text from a model or a project
can change these rules, and a denial cannot be approved away."""
import hashlib
import json
import re
import unicodedata
from typing import Any, NamedTuple

from agent import box, gitsafe, scan
from agent.checker import PROTECTED, Contract, allowed

DENY, ASK, ALLOW = "DENY", "ASK", "ALLOW"

# tool: {argument: type}. There is no tool to push, merge, deploy, reach the network, or read keys.
TOOLS: dict[str, dict[str, type]] = {
    "list": {"path": str}, "search": {"text": str, "path": str}, "read": {"path": str, "first": int},
    "edit": {"path": str, "old": str, "new": str}, "create": {"path": str, "text": str}, "delete": {"path": str},
    "run_command": {"program": list}, "run_checks": {}, "ask": {"question": str}, "finish": {"summary": str},
}
CHANGES = ("edit", "create", "delete")
MAX_TEXT = 200_000
DEPENDENCIES = re.compile(
    r"""(?ix)(^|/)(
        requirements([^/]*|/[^/]*)\.(txt|in) | constraints[^/]*\.txt | pipfile(\.lock)? | poetry\.lock | uv\.lock
        | environment\.ya?ml
        | package(-lock)?\.json | npm-shrinkwrap\.json | yarn\.lock | pnpm-lock\.yaml | bun\.lockb? | go\.(mod|sum)
        | cargo\.(toml|lock) | gemfile(\.lock)? | composer\.(json|lock) | [^/]*\.csproj | packages\.lock\.json
        | gradle\.lockfile | (.*/)?(migrations?|alembic)/.* | [^/]*\.sql | \.cargo/.* | build\.rs | rakefile
        | \.yarnrc[^/]* | action\.ya?ml
    )$"""
)


class Action(NamedTuple):
    tool: str
    args: dict[str, Any]

    def hash(self) -> str:
        return hashlib.sha256(json.dumps([self.tool, self.args], sort_keys=True).encode()).hexdigest()


class Decision(NamedTuple):
    kind: str
    rule: str  # shown to the human and recorded


def _plain(text: str) -> bool:
    try:
        text.encode()
    except UnicodeEncodeError:  # half of a character
        return False
    return True


def spellings(path: str) -> set[str]:
    """Some computers and tools treat a name like 'Make\ufb01le' as 'Makefile', so rules look at both."""
    return {path, unicodedata.normalize("NFKC", path).casefold()}


class Policy:
    """The rules for one task."""

    def __init__(self, contract: Contract) -> None:
        self.contract = contract
        self.created: set[str] = set()

    def decide(self, action: Action) -> Decision:
        why = self._deny(action)
        if why:
            return Decision(DENY, why)
        why = self._ask(action)
        if why:
            return Decision(ASK, why)
        why = self._allow(action)
        return Decision(ALLOW, why) if why else Decision(ASK, "no rule allows this")

    def _deny(self, a: Action) -> str:
        wanted = TOOLS.get(a.tool) if isinstance(a.tool, str) else None
        if wanted is None:
            return "there is no such tool"
        if not isinstance(a.args, dict):
            return "the arguments must be named"
        if set(a.args) != set(wanted) or any(type(a.args[k]) is not t for k, t in wanted.items()):
            return f"{a.tool} needs exactly: " + (", ".join(wanted) or "nothing")
        texts = [v for v in a.args.values() if isinstance(v, str)]
        if any(len(v) > MAX_TEXT for v in texts) or not all(map(_plain, texts)):
            return "the text is too long, or is not valid text"
        if a.tool == "read" and not 1 <= a.args["first"] <= 10_000_000:
            return "first is the number of the first line, from 1"
        if a.tool == "run_command":
            p = a.args["program"]
            good = all(isinstance(x, str) and "\0" not in x and len(x) < 10_000 and _plain(x) for x in p)
            if not p or len(p) > 200 or not good:
                return "program must be a list of text: the program and its arguments"
        if "path" in wanted:
            return self._deny_path(a)
        return ""

    def _deny_path(self, a: Action) -> str:
        path = a.args["path"]
        if path == "" and a.tool in ("list", "search"):
            return ""  # the whole project
        if box.unsafe_path(path) or not path.isprintable() or "\\" in path or len(path) > 1000:
            return "the path is not inside the project"
        if any(scan.BLOCKED_NAMES.search(x) for x in spellings(path)):
            return "files with the name of a secret cannot be used"
        return ""

    def _ask(self, a: Action) -> str:
        if a.tool not in CHANGES:
            return ""
        path = a.args["path"]
        names = spellings(path)
        if not allowed(path, self.contract.where):
            return "outside the agreed files"
        if any(PROTECTED.search(x) for x in names):
            return "a test, a test setting, or a name that can change how tests run"
        if any(DEPENDENCIES.search(x) for x in names):
            return "changes what the project depends on, or its database"
        if any(gitsafe.RUNS_LATER.search(x) for x in names):
            return "can make something run later, or change how tools treat files"
        if a.tool == "delete" and path not in self.created:
            return "deletes a file that the agent did not make"
        return ""

    def _allow(self, a: Action) -> str:
        if a.tool in ("list", "search", "read", "ask", "finish"):
            return "reading, asking and finishing change nothing"
        if a.tool in CHANGES:
            return "inside the agreed files"
        if a.tool == "run_checks":
            return "the confirmed command"
        return ""
