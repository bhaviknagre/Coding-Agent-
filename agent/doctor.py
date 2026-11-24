"""Check what the agent needs. Every problem comes with one next step. No secret is ever read."""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

KEY_ITEM = "coding-agent-v2"
NEED_FREE_GB = 8


class Result(NamedTuple):
    state: str  # OK, WARN or STOP
    name: str
    text: str
    next_step: str = ""


def run(*args: str) -> tuple[int, str]:
    """Run a short command. A missing program or a hang is a failure, never a crash."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return p.returncode, p.stdout.strip()


def python() -> Result:
    return Result("OK", "Python", sys.version.split()[0])  # __main__ has already refused an old one


def git() -> Result:
    code, out = run("git", "--version")
    return Result("OK", "Git", out) if code == 0 else Result("STOP", "Git", "not found", "Install Git.")


def repository() -> Result:
    code, out = run("git", "rev-parse", "--show-toplevel")
    if code == 0:
        return Result("OK", "Repository", Path(out).name)
    return Result("WARN", "Repository", "this folder is not in a Git repository", "Open a repository folder.")


def docker() -> Result:
    if shutil.which("docker") is None:
        return Result("STOP", "Docker", "not found", "Install Docker and open it.")
    code, out = run("docker", "version", "--format", "{{.Server.Version}}")
    if code == 0 and out:
        return Result("OK", "Docker", out)
    return Result("STOP", "Docker", "installed but not answering", "Open Docker Desktop and wait until it runs.")


def sqlite(version: tuple[int, ...] = sqlite3.sqlite_version_info) -> Result:
    text = ".".join(map(str, version))
    fixed = version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51) or (3, 44, 6) <= version < (3, 45)
    if fixed:
        return Result("OK", "SQLite", text)
    return Result("WARN", "SQLite", f"{text} has the WAL-reset bug", "Nothing to do: a slower safe mode is used.")


def space(path: Path | None = None) -> Result:
    free = shutil.disk_usage(path or Path.home()).free / 1e9
    text = f"{free:.0f} GB free"
    if free >= NEED_FREE_GB:
        return Result("OK", "Space", text)
    return Result("STOP", "Space", text, f"Free some space; at least {NEED_FREE_GB} GB is needed.")


def key() -> Result:
    """Ask the Keychain whether an item exists. Without -w or -g it never returns the secret."""
    if sys.platform != "darwin":
        return Result("WARN", "Key", "key store not supported here yet", "Type the key when asked.")
    code, _ = run("security", "find-generic-password", "-s", KEY_ITEM)
    if code == 0:
        return Result("OK", "Key", "stored in the Keychain")
    return Result("WARN", "Key", "none stored", "Only needed for a live model; verify and demo work without it.")


def check_all() -> list[Result]:
    return [python(), git(), repository(), docker(), sqlite(), space(), key()]


def main() -> int:
    results = check_all()
    for r in results:
        print(f"{r.state:5}{r.name:12}{r.text}")
        if r.next_step:
            print(f"     next: {r.next_step}")
    stops = sum(r.state == "STOP" for r in results)
    print("Ready." if stops == 0 else f"Not ready: fix {stops} STOP line(s).")
    return 1 if stops else 0
