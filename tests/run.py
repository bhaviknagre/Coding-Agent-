"""Run all tests, several at the same time, and fail unless every test that exists has run and passed.

With 'key' it prints a fingerprint of the files and tools instead; ./check uses it to remember a pass.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKERS = 4
TIMES = Path(".git/coding-agent-v2-times")


def key() -> str:
    """Changes when any file that Git would see changes, or Python, Docker, the base image or the environment does."""
    h = hashlib.sha256()

    def add(*parts: bytes) -> None:
        for p in parts:
            h.update(str(len(p)).encode() + b":" + p)

    names = subprocess.run(["git", "ls-files", "-z", "-co", "--exclude-standard"], capture_output=True, check=True).stdout  # noqa: E501
    for name in sorted(set(names.split(b"\0")) - {b""}):
        path = Path(os.fsdecode(name))
        if path.is_symlink():
            add(name, b"link", os.fsencode(os.readlink(path)))
        elif path.is_file():
            add(name, b"x" if os.access(path, os.X_OK) else b"-", path.read_bytes())
        else:
            add(name, b"gone")
    add(sys.version.encode())
    for command in (["docker", "version"], ["docker", "images", "-q", "python:3.12-slim"],
                    ["git", "version"]):
        add(subprocess.run(command, capture_output=True, check=True).stdout)
    return h.hexdigest()


def flat(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flat(t)
        else:
            yield t


def jobs(tests) -> dict[str, int]:
    """One job for each test, or for each class that prepares something once for all its tests."""
    found: dict[str, int] = {}
    for t in tests:
        shared = type(t).setUpClass.__func__ is not unittest.TestCase.setUpClass.__func__
        name = t.id().rsplit(".", 1)[0] if shared else t.id()
        found[name] = found.get(name, 0) + 1
    return found


def run(name: str, expected: int) -> tuple[str, float, str]:
    """Return the problem, or "" if exactly the expected tests ran and passed."""
    start = time.monotonic()
    command = [sys.executable, "-m", "unittest", name]
    p = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    ran = re.findall(r"^Ran (\d+) tests? in ", p.stdout, re.M)
    good = p.returncode == 0 and ran == [str(expected)] and re.search(r"^OK$", p.stdout, re.M)
    return name, time.monotonic() - start, "" if good else p.stdout or "it ended without a report"


def main() -> int:
    todo = jobs(flat(unittest.defaultTestLoader.discover("tests", top_level_dir=".")))
    try:
        times = json.loads(TIMES.read_text())
    except (OSError, ValueError):
        times = {}
    order = sorted(todo, key=lambda n: -times.get(n, 999))  # longest first, so no worker is left with a long one
    start, bad = time.monotonic(), 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for name, seconds, problem in pool.map(lambda n: run(n, todo[n]), order):
            times[name] = round(seconds, 1)
            if problem:
                bad += 1
                print(f"\nFAILED {name}\n{problem[-6000:]}", flush=True)
    if TIMES.parent.is_dir():
        TIMES.write_text(json.dumps({n: times[n] for n in todo}))
    print(f"{sum(todo.values())} tests in {time.monotonic() - start:.0f} s: {f'{bad} job(s) FAILED' if bad else 'all passed'}")  # noqa: E501
    return 1 if bad or not todo else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["key"]:
        print(key())
    else:
        sys.exit(main())
