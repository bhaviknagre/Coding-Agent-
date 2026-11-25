"""Decide what can be proven about a change. No model is asked, and the agent's own claims are not used.

Everything a project prints is a claim by untrusted code. Missing or unclear evidence never becomes VERIFIED.
VERIFIED means: the agreed checks passed here. It does not mean that the change is right in every case.
"""
import json
import re
import secrets
import sys
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import IO, NamedTuple

from agent import box

PASS, FAIL, FLAKY, UNCLEAR, NOT_CHECKED = "PASS", "FAIL", "FLAKY", "UNCLEAR", "NOT CHECKED"
TOTAL_SECONDS = 1800
OUTCOMES = {"ok", "xfail", "skip", "fail", "error"}

# Tests, test settings, and names that could replace the test tool. The base version of these is always used,
# unless the developer approved exactly this new content. Files named in the command or proof are added later.
PROTECTED = re.compile(
    r"""(?ix)(^|/)(
        tests?/.* | testing/.* | specs?/.* | __tests__/.* | tests?\.py | test_[^/]*(/.*)? | [^/]*_test\.[^/]+
        | conftest(\.[^/]*|/.*) | [^/]*\.(test|spec)\.[cm]?[jt]sx? | (?-i:[^/]*Tests?\.(java|kt|cs|php)) | [^/]*\.snap
        | \.?pytest\.(ini|toml) | tox\.ini | setup\.cfg | pyproject\.toml | noxfile\.py | \.coveragerc
        | (gnu)?makefile | [^/]*\.mk | justfile | package\.json | [^/]*\.config\.([cm]?[jt]s|json) | \.mocharc[^/]*
        | phpunit\.xml(\.dist)? | build\.gradle(\.kts)? | pom\.xml
        | (unittest|pytest|_pytest|pluggy|(site|user)customize)(\.[^/]*|/.*) | [^/]*\.pth
        | [^/]*\.(dist|egg)-info/.* | __pycache__/.* | [^/]*\.py[co]
    )$"""
)
# Base files that use a test tool, such as helpers with shared checks.
USES_TEST_TOOL = r"^[[:space:]]*(import|from)[[:space:]]+(unittest|pytest)"
REPEATS = 3


class Contract(NamedTuple):
    """What the developer agreed to. `where` holds file names, folders ending in /, or * patterns."""

    what: str
    where: tuple[str, ...]
    command: tuple[str, ...]
    proof: tuple[str, ...] = ()  # test names; for other commands: a proof command, or empty for the command itself
    approved: dict[str, str] = {}  # noqa: RUF012 - never changed; protected file -> exact content id


class Step(NamedTuple):
    name: str
    state: str
    text: str
    required: bool = True


class Change(NamedTuple):
    path: str
    old_mode: str
    new_mode: str
    new_id: str


class Run(NamedTuple):
    """One run in a fresh container. `tests` is None when there is no clear report."""

    code: int | None  # None: it did not end by itself
    tests: dict[str, str] | None
    text: str


def verdict(steps: list[Step]) -> str:
    """The whole decision. The order matters."""
    states = {s.state for s in steps}
    if FAIL in states:
        return "FAILED"
    if FLAKY in states:
        return "FLAKY"
    if not steps or any(s.required and s.state != PASS for s in steps):
        return "UNVERIFIABLE"
    return "VERIFIED"


def changes(repo: Path, base: str, result: str) -> list[Change]:
    flags = ("-r", "-z", "--no-renames", "--no-ext-diff", "--ignore-submodules=none")
    out = box.git_bytes(repo, "diff-tree", *flags, base, result).split(b"\0")
    found = []
    for info, name in zip(out[::2], out[1::2], strict=False):
        old_mode, new_mode, _, new_id, _ = info.decode().lstrip(":").split()
        found.append(Change(name.decode("utf-8", "surrogateescape"), old_mode, new_mode, new_id))
    return found


def allowed(path: str, where: tuple[str, ...]) -> bool:
    return any(path == w or (w.endswith("/") and path.startswith(w)) or fnmatch(path, w) for w in where)


def same_content(approved: dict[str, str], path: str, oid: str, mode: str) -> bool:
    return approved.get(path) == oid and mode in ("100644", "100755")


def failed_without(tests: dict[str, str], name: str) -> bool:
    """The test failed, or its file could not even be loaded (for example: it imports a new function)."""
    if name in tests:
        return tests[name] in ("fail", "error", "xfail")
    loader = "unittest.loader._FailedTest."
    files = [t for t, o in tests.items() if o == "error" and (t.startswith(loader) or "::" not in t)]
    broken = [t.removeprefix(loader) for t in files]
    return any(name.startswith((b + ".", b + "::")) or f".{b}." in name for b in broken)


def named_files(contract: Contract, files: set[str]) -> set[str]:
    """Files that the command or the proof names, also inside a shell line, and modules of the proof tests."""
    found = set()
    for text in (*contract.command, *contract.proof):
        for word in re.split(r"[\s;|&<>()'\"=]+|::.*", text):
            word = word.removeprefix("./")
            if word in files:
                found.add(word)
            parts = word.split(".")
            for n in range(len(parts), 0, -1):  # the longest dotted name that is a file: a.b.C.test -> a/b.py
                stem = "/".join(parts[:n])
                if stem + ".py" in files:
                    found |= {f for f in files if f == stem + ".py" or f.startswith((stem + "/", stem + "."))}
                    break
    return found


def test_tool(command: tuple[str, ...]) -> tuple[str, str, list[str]]:
    """Return (kind, Python program, arguments). Kind 'command' means: only the exit code is known."""
    if len(command) >= 3 and re.fullmatch(r"python[\d.]*", command[0]) and command[1] == "-m":
        if command[2] in ("unittest", "pytest"):
            return command[2], command[0], list(command[3:])
    if command and command[0] == "pytest":
        return "pytest", "python", list(command[1:])
    return "command", "", []


class Checker:
    def __init__(
        self, repo: Path, base: str, result: str, contract: Contract, image: str,
        cancel: threading.Event | None = None,
    ) -> None:
        self.repo, self.contract, self.cancel = repo, contract, cancel or threading.Event()
        self.base, self.result = box.commit_id(repo, base), box.commit_id(repo, result)
        self.image = box.image_id(image)  # by content, so every run uses the same one
        self.kind, self.python, self.args = test_tool(contract.command)
        self.level = "COMMAND-LEVEL" if self.kind == "command" else "TEST-LEVEL"
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.steps: list[Step] = []
        self.restored: list[str] = []
        self.counts: dict[str, int] = {}
        self.changes: list[Change] = []
        self.left_out: list[str] = []

    def step(self, name: str, state: str, text: str, required: bool = True) -> None:
        self.steps.append(Step(name, state, text, required))

    # ---- files

    def prepare(self) -> None:
        self.changes = changes(self.repo, self.base, self.result)
        base, left_base = box.list_commit(self.repo, self.base)
        result, left_result = box.list_commit(self.repo, self.result)
        self.left_out = sorted(set(left_base + left_result))
        old = {e.name: e for e in base}
        new = {e.name: e for e in result}
        everything = set(old) | set(new)
        protected = {n for n in everything if PROTECTED.search(n)} | named_files(self.contract, everything)
        protected |= self.helpers() & everything
        # A new file in the top folder that has the name of a standard Python module would be used instead of it.
        protected |= {n for n in new if n not in old and "/" not in n and n.split(".")[0] in sys.stdlib_module_names}
        approved = {n: e for n, e in new.items() if same_content(self.contract.approved, n, e.oid, e.mode)}
        # pure: the base. without: the base plus approved tests. with_change: the result, but protected files come
        # from the base unless approved. All are read from the developer's own repository.
        self.pure = base
        self.without = list({**old, **approved}.values())
        kept = {n: e for n, e in new.items() if n not in protected}
        safe = {n: e for n, e in old.items() if n in protected}
        self.with_change = list({**kept, **safe, **approved}.values())
        self.restored = sorted(n for n in protected if n not in approved and old.get(n) != new.get(n))
        box.refuse_conflicts(self.with_change)

    def helpers(self) -> set[str]:
        try:
            out = box.git_bytes(self.repo, "grep", "-l", "-z", "-E", USES_TEST_TOOL, self.base, "--", "*.py")
        except box.BoxError:  # git grep also fails when it finds nothing
            return set()
        return {x.decode("utf-8", "surrogateescape").split(":", 1)[1] for x in out.split(b"\0") if x}

    def check_changes(self) -> None:
        if not self.changes:
            self.step("change", FAIL, "the branch changes nothing")
            return
        agreed = self.contract
        outside = [c.path for c in self.changes if not allowed(c.path, agreed.where)
                   and not same_content(agreed.approved, c.path, c.new_id, c.new_mode)]
        if outside:
            self.step("change", FAIL, "outside the agreed files: " + ", ".join(outside))
        else:
            self.step("change", PASS, f"{len(self.changes)} file(s), all inside the agreed files")
        if self.restored:
            # What would be merged is not what was tested.
            self.step("original files", UNCLEAR, "changed without approval, so the base version was tested: "
                      + ", ".join(self.restored[:5]))
        skipped = {line.rsplit(": ", 1)[0] for line in self.left_out} & {c.path for c in self.changes}
        if skipped:
            self.step("copied", UNCLEAR, "changed but not checked: " + ", ".join(sorted(skipped)))

    # ---- running

    def run(self, entries: list[box.Entry], only: tuple[str, ...] = (), canary: str = "",
            command: tuple[str, ...] = ()) -> Run:
        """One run in a fresh volume and a fresh container."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            return Run(None, None, "the time for checking is used up")
        with box.Box(self.image, cancel=self.cancel) as b:
            b.copy_entries(self.repo, entries)
            env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": "/tmp/no-cache"}  # noqa: S108
            timeout = min(box.COMMAND_SECONDS, left)
            if self.kind == "command":
                r = b.run(list(command or self.contract.command), timeout=timeout, env=env)
            else:
                nonce = secrets.token_hex(16)
                want = {"nonce": nonce, "kind": self.kind, "args": self.args, "only": only, "canary": canary,
                        "proof": self.contract.proof}

                def feed(out: IO[bytes]) -> None:
                    out.write(json.dumps(want).encode() + b"\n")

                r = b.run([self.python, "-c", RUNNER], timeout=timeout, env=env, feed=feed)
        if r.status == "CANCELLED":
            raise KeyboardInterrupt
        if r.status != "EXITED":
            return Run(None, None, f"{r.status}: {r.output[-2000:]}")
        tests = None if self.kind == "command" else read_report(r.output, nonce, r.code)
        return Run(r.code, tests, r.output[-2000:])

    # ---- tests known by name

    def by_tests(self) -> None:
        before = self.run(self.pure).tests
        if before is None:
            self.step("baseline", UNCLEAR, "no clear test report without the change")
            return
        self.step("baseline", PASS, f"{len(before)} tests without the change, {ok(before)} ok")
        # Every run with the change holds canaries, so that code which changes reports is seen in every run.
        name = "Canary" + secrets.token_hex(8)
        runs = [self.run(self.with_change, canary=name) for _ in range(REPEATS)]
        if unclear := [r for r in runs if r.tests is None]:
            self.step("tests", UNCLEAR, "no clear test report with the change: " + last_line(unclear[0]))
            return
        reports = [r.tests for r in runs if r.tests is not None]
        self.canary([{t: o for t, o in x.items() if name in t} for x in reports])
        after = [{t: o for t, o in x.items() if name not in t} for x in reports]
        self.counts = {"before": len(before), "after": min(map(len, after))}
        # An "error" may be a whole file that could not be loaded before the change; that entry is no test.
        gone = sorted(t for t, o in before.items() if o != "error" and any(t not in x for x in after))
        if gone:
            self.step("test count", UNCLEAR, f"{len(gone)} test(s) no longer reported: " + ", ".join(gone[:5]))
        self.regressions(before, after)
        self.proof(after)

    def canary(self, found: list[dict[str, str]]) -> None:
        if not all(found):
            self.step("canary", UNCLEAR, "the tests that must fail did not run")
        elif any(len(x) < 3 or set(x.values()) != {"fail"} for x in found):
            self.step("canary", UNCLEAR, "tests that must fail were not all reported as failing, so reports are unsafe")
        else:
            self.step("canary", PASS, f"{len(found[0])} tests that must fail did fail, in each of {len(found)} runs")

    def regressions(self, before: dict[str, str], after: list[dict[str, str]]) -> None:
        names = sorted(set(before).union(*after) - set(self.contract.proof))
        good = {t: [x.get(t) == "ok" for x in after] for t in names}
        mixed = [t for t in names if len(set(good[t])) == 2]
        worse = tuple(t for t in names if before.get(t) == "ok" and not any(good[t]))
        new_bad = [t for t in names if before.get(t) not in ("ok", "fail", "error")
                   and all(x.get(t) in ("fail", "error") for x in after)]
        if worse:
            base = [self.run(self.pure, worse).tests for _ in range(REPEATS - 1)]
            if any(x is None for x in base):
                self.step("regressions", UNCLEAR, "a repeat gave no clear report: " + ", ".join(worse[:5]))
                return
            if broken := [t for t in worse if all(x is not None and x.get(t) == "ok" for x in base)]:
                self.step("regressions", FAIL, f"worked before, fails now ({REPEATS} of {REPEATS}): "
                          + ", ".join(broken[:5]))
                return
            mixed += worse
        if new_bad:
            self.step("regressions", FAIL, "new or changed tests fail: " + ", ".join(new_bad[:5]))
        elif mixed:
            self.step("regressions", FLAKY, "sometimes passes, sometimes fails: " + ", ".join(mixed[:5]))
        elif not ok(before):
            self.step("regressions", PASS, "no test worked before, so nothing here protects against new errors")
        else:
            self.step("regressions", PASS, f"all {ok(before)} tests that worked still work, in {len(after)} runs")

    def proof(self, after: list[dict[str, str]]) -> None:
        proof = self.contract.proof
        if not proof:
            self.step("proof", UNCLEAR, "no test was agreed that shows the change works")
            return
        results = {t: {x.get(t) for x in after} for t in proof}
        if bad := [t for t in proof if results[t] <= {"fail", "error"}]:
            self.step("proof", FAIL, "fails with the change: " + ", ".join(bad))
            return
        if sometimes := [t for t in proof if "ok" in results[t] and len(results[t]) > 1]:
            self.step("proof", FLAKY, "sometimes passes, sometimes not: " + ", ".join(sometimes))
            return
        if absent := [t for t in proof if results[t] != {"ok"}]:
            self.step("proof", UNCLEAR, "did not run with the change: " + ", ".join(absent))
            return
        runs = [self.run(self.without, proof).tests for _ in "12"]
        if runs[0] is None or runs[1] is None:
            self.step("proof", UNCLEAR, "no clear test report without the change")
            return
        if early := [t for t in proof if not failed_without(runs[0], t)]:
            self.step("proof", UNCLEAR, "did not fail without the change: " + ", ".join(early))
            return
        self.step("proof", PASS, f"{len(proof)} test(s) fail without the change and pass with it")
        if passed := [t for t in proof if not failed_without(runs[1], t)]:
            self.step("counter-check", FLAKY, "this time it did not fail without the change: " + ", ".join(passed))
        else:
            self.step("counter-check", PASS, "with the change removed, the proof fails again")

    # ---- other commands: only the exit code is known

    def by_command(self) -> None:
        before = self.run(self.pure)
        if before.code != 0:
            self.step("baseline", UNCLEAR, "the command must work before the change: " + last_line(before))
            return
        self.step("baseline", PASS, "the command works before the change")
        after = [self.run(self.with_change) for _ in range(REPEATS)]
        if any(r.code is None for r in after):
            self.step("regressions", UNCLEAR, "the command did not end by itself: " + last_line(after[-1]))
            return
        if any(r.code for r in after):
            mixed = any(r.code == 0 for r in after)
            text = "the command fails with the change: " + last_line(next(r for r in after if r.code))
            self.step("regressions", FLAKY if mixed else FAIL, text)
            return
        self.step("regressions", PASS, "the command works with the change")
        if not self.contract.proof and not self.contract.approved:
            self.step("proof", UNCLEAR, "no test was agreed that shows the change works")
            return
        command = self.contract.proof or self.contract.command
        without = [self.run(self.without, command=command) for _ in "12"]
        fixed = [self.run(self.with_change, command=command) for _ in "12"]
        if any(r.code is None for r in fixed + without):
            self.step("proof", UNCLEAR, "the proof command did not end by itself")
        elif all(r.code for r in fixed):
            self.step("proof", FAIL, "the proof command fails with the change: " + last_line(fixed[0]))
        elif any(r.code for r in fixed):
            self.step("proof", FLAKY, "the proof command sometimes fails with the change")
        elif without[0].code == 0:
            self.step("proof", UNCLEAR, "the proof command already works without the change")
        else:
            self.step("proof", PASS, "the proof command fails without the change and works with it")
            again = without[1].code != 0
            text = "with the change removed, it fails again" if again else "this time it worked without the change"
            self.step("counter-check", PASS if again else FLAKY, text)
        self.step("canary", NOT_CHECKED, "not possible for this command", required=False)

    def check(self) -> str:
        try:
            self.prepare()
            self.check_changes()
            if self.kind == "command":
                self.by_command()
            else:
                self.by_tests()
        except box.BoxError as e:  # never lose the receipt
            self.step("running", UNCLEAR, str(e))
        return verdict(self.steps)


def read_report(output: str, nonce: str, code: int) -> dict[str, str] | None:
    """The report must be there exactly once, be well formed, hold tests, and agree with the exit code."""
    found = re.findall(rf"^@@{nonce} (.*)@@$", output, re.M)
    if len(found) != 1:
        return None
    try:
        tests = json.loads(found[0])
    except ValueError:
        return None
    if not isinstance(tests, dict) or not tests or not set(map(str, tests.values())) <= OUTCOMES:
        return None
    bad = bool({"fail", "error"} & set(tests.values()))
    return tests if code == (1 if bad else 0) else None


def ok(tests: dict[str, str]) -> int:
    return sum(o == "ok" for o in tests.values())


def last_line(r: Run) -> str:
    lines = [x for x in r.text.splitlines() if x.strip() and not x.startswith("@@")]
    return (lines[-1] if lines else f"exit code {r.code}, no output")[:200]


RUNNER = r'''"""Runs INSIDE the project container, as text given to its Python. Reports each test by name.

Project code runs in the same process, so a report is only a claim. The checker adds tests that must fail
(canaries) to find forged reports. Kept simple enough for old Python 3 versions.
"""
import sys

# The project folder must not be able to replace unittest or pytest themselves.
sys.path[:] = [p for p in sys.path if p not in ("", "/work")]
import json
import os
import unittest

ORDER = ["ok", "xfail", "skip", "fail", "error"]  # the worst result of a test counts
seen = {}
want = {}


def note(name, outcome):
    old = seen.get(name, "ok")
    seen[name] = max(old, outcome, key=ORDER.index)


def add_project_folder():
    """After Python's own modules, so a project file cannot replace them; before installed packages."""
    places = [i for i, p in enumerate(sys.path) if "-packages" in p]
    sys.path.insert(places[0] if places else len(sys.path), "/work")


class Result(unittest.TextTestResult):
    def addSuccess(self, test):
        note(test.id(), "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        note(test.id(), "fail")

    def addError(self, test, err):
        super().addError(test, err)
        note(test.id(), "error")

    def addSkip(self, test, reason):
        note(test.id(), "skip")

    def addExpectedFailure(self, test, err):
        note(test.id(), "xfail")

    def addUnexpectedSuccess(self, test):
        note(test.id(), "fail")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            note(test.id(), "fail")


def canary_class(name):
    def raises(self):
        raise AssertionError("canary")

    return type(name, (unittest.TestCase,), {
        "test_a": lambda self: self.assertEqual(1, 2),
        "test_b": lambda self: self.assertTrue(False),
        "test_c": raises,
        "test_d": lambda self: self.fail("canary"),
    })


def run_unittest():
    found = []

    class Hold:  # takes the tests from unittest's own command line handling, without running them
        def run(self, suite):
            found.append(suite)

    unittest.main(module=None, argv=["unittest"] + want["args"], exit=False, testRunner=Hold())

    def flat(suite):
        for t in suite:
            if isinstance(t, unittest.TestSuite):
                yield from flat(t)
            else:
                yield t

    tests = [t for s in found for t in flat(s)]
    if want["only"]:
        # A file that cannot be loaded must show up as an error, not as "no tests".
        tests = [t for t in tests if t.id() in want["only"] or type(t).__name__ == "_FailedTest"]
    if want["canary"]:
        tests += list(unittest.TestLoader().loadTestsFromTestCase(canary_class(want["canary"])))
    unittest.TextTestRunner(resultclass=Result, verbosity=2).run(unittest.TestSuite(tests))


def run_pytest(pytest):
    class Plugin:
        def pytest_runtest_logreport(self, report):
            if report.failed:
                note(report.nodeid, "fail" if report.when == "call" else "error")
            elif report.skipped:
                note(report.nodeid, "xfail" if hasattr(report, "wasxfail") else "skip")
            elif report.when == "call":
                note(report.nodeid, "ok")

        def pytest_collectreport(self, report):
            if report.failed:
                note(report.nodeid, "error")

        @pytest.hookimpl(hookwrapper=True, trylast=True)
        def pytest_collection_modifyitems(self, items):
            yield
            if want["only"]:
                items[:] = [i for i in items if i.nodeid in want["only"]]
            # Added last, so the project's own selection options cannot remove them.
            near = [i for i in items if i.nodeid in want["proof"]] or items
            module = near[0].getparent(pytest.Module) if near and want["canary"] else None
            if module is not None:
                for name, f in (("a", lambda: compare(1, 2)), ("b", lambda: pytest.fail("canary")), ("c", raises)):
                    name = "test_" + name + want["canary"]
                    items.append(pytest.Function.from_parent(module, name=name, callobj=f))

    def compare(a, b):
        assert a == b

    def raises():
        raise AssertionError("canary")

    extra = ["-p", "no:cacheprovider", "--maxfail=0", "--continue-on-collection-errors"]
    pytest.main(list(want["args"]) + extra, plugins=[Plugin()])


def main():
    want.update(json.loads(sys.stdin.readline()))
    out = os.dup(1)
    if want["kind"] == "pytest":
        import pytest
        add_project_folder()
        run_pytest(pytest)
    else:
        add_project_folder()
        run_unittest()
    sys.stdout.flush()
    sys.stderr.flush()
    data = ("\n@@" + want["nonce"] + " " + json.dumps(seen) + "@@\n").encode()
    while data:
        data = data[os.write(out, data):]
    sys.exit(1 if {"fail", "error"} & set(seen.values()) or not seen else 0)


main()
'''
