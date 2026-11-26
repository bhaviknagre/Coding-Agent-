"""agent verify BRANCH: the same proof for a branch made by anyone. No model, no key."""
import argparse
import difflib
import os
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from agent import box, gitsafe, receipt
from agent.approve import ask_human
from agent.checker import Change, Checker, Contract, changes, test_tool
from agent.log import Log, _masked
from agent.mask import mask

MAX_SHOWN = 5000  # lines of changed tests that a person can be asked to read
# Characters that Python calls printable, but that show as nothing or as a blank.
BLANK = re.compile("[\u034f\u115f\u1160\u17b4\u17b5\u2800\u3164\uffa0\ufe00-\ufe0f\U000e0100-\U000e01ef]")


def visible(text: object) -> str:
    return BLANK.sub(lambda m: f"<U+{ord(m.group()):04X}>", receipt.visible(text))


class Stop(Exception):
    """A message for the human, with the next step."""


def data_folder() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "coding-agent-v2"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "coding-agent-v2"


def confirm(question: str) -> bool:
    """Only a person at a terminal can say yes: 'y' and Enter. Keys typed before the question do not count."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"{question} Not asked: no person at a terminal, so the answer is no.")
        return False
    return ask_human(question).yes


def repository() -> Path:
    try:
        return Path(os.fsdecode(box.git_bytes(Path.cwd(), "rev-parse", "--show-toplevel")).rstrip("\n"))
    except box.BoxError:
        raise Stop("This folder is not in a Git repository. Next: open your project folder.") from None


def branch_commit(repo: Path, branch: str) -> str:
    """Only a branch: a tag with the same name must not be taken instead."""
    try:
        return box.commit_id(repo, f"refs/heads/{branch}")
    except box.BoxError:
        raise Stop(f"There is no branch '{visible(branch)}'. Next: see 'git branch'.") from None


def find_base(repo: Path, branch: str, base: str) -> tuple[str, str]:
    result = branch_commit(repo, branch)
    try:
        start = box.commit_id(repo, base) if base else ""
        common = box.git_bytes(repo, "merge-base", start or "HEAD", result).decode().strip()
    except box.BoxError:
        raise Stop("The branch and this base have nothing in common. Next: give another --base.") from None
    start = start or common
    if start == result:
        raise Stop("The branch has no change compared with here. Next: go to the branch it left, or give --base.")
    if common != start:
        raise Stop("The branch does not start from that base. Next: give another --base.")
    return start, result


def show_protected(repo: Path, start: str, names: Sequence[Change]) -> str:
    """The whole change of each file, made here from its exact content, so no Git setting can hide a part of it."""
    old = {e.name: e.oid for e in box.list_commit(repo, start)[0]}
    out: list[str] = []
    for c in names:
        texts: list[list[str]] = []
        for oid in (old.get(c.path), c.new_id):
            data = box.git_bytes(repo, "cat-file", "blob", oid) if oid else b""
            try:
                texts.append(data.decode().split("\n"))
            except UnicodeDecodeError:
                data = b"\0"
            if b"\0" in data:
                raise Stop(f"{visible(c.path)} is not text, so its change cannot be shown. Next: use --original-tests.")
            if mask(data.decode()) != data.decode():
                # An approval must show everything, and a secret must not be shown.
                raise Stop(f"{visible(c.path)} seems to hold a secret. Next: remove it, or use --original-tests.")
        out.append(f"== {visible(c.path)}  ({c.old_mode} -> {c.new_mode})")
        lines = list(difflib.unified_diff(texts[0], texts[1], lineterm="", n=3))[2:]
        out += [visible(x.replace("\t", "    ")) for x in lines] or ["   same text; only the mode is changed"]
    if len(out) > MAX_SHOWN:
        raise Stop(f"The changed tests are too long to show ({len(out)} lines). Next: use --original-tests.")
    return "\n".join(out)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent verify", description="Check a branch in fresh containers.")
    p.add_argument("branch")
    p.add_argument("--command", default="python -m unittest", help="the full test command")
    p.add_argument("--proof", action="append", default=[], help="a test that fails before and passes after; repeatable")
    p.add_argument("--base", default="", help="where the branch started (default: where it left the current branch)")
    p.add_argument("--image", default=box.HELPER_IMAGE, help="a Docker image that is already on this computer")
    p.add_argument("--original-tests", action="store_true", help="do not use the branch's changed tests and settings")
    return p


def verify(argv: Sequence[str]) -> int:
    a = parser().parse_args(argv)
    repo = repository()
    start, result = find_base(repo, a.branch, a.base)
    changed = changes(repo, start, result)
    command = tuple(shlex.split(a.command))
    if not command:
        raise Stop("The test command is empty.")
    kind = test_tool(command)[0]
    if kind == "command" and len(a.proof) > 1:
        raise Stop("For this command, give one --proof: a command that fails before and works after.")
    proof = tuple(a.proof) if kind != "command" else tuple(shlex.split(" ".join(a.proof)))
    what = f"check {a.branch}"
    contract = Contract(what, tuple(c.path for c in changed), command, proof)
    c = Checker(repo, start, result, contract, a.image)
    c.prepare()
    protected = [x for x in changed if x.path in c.restored and x.new_mode in ("100644", "100755")]
    if a.original_tests:
        protected = []

    print(f"Branch   {visible(a.branch)}  {start[:12]} -> {result[:12]}")
    print(f"Image    {visible(a.image)}  (no network; your folder is not mounted)")
    print(f"Command  {visible(a.command)}")
    print(f"Proof    {visible(' '.join(proof) or 'none: the result can then not be VERIFIED')}")
    print("Files    " + "\n         ".join(visible(x.path) for x in changed))
    unused = [x for x in c.restored if x not in {p.path for p in protected}]
    if unused:
        print("Not used: " + ", ".join(map(visible, unused)) + "  (the original version is tested)")
    if protected:
        print("\nThese tests or settings are changed. Saying yes means: use the branch's version.\n")
        print(show_protected(repo, start, protected))
    if not confirm("\nRun these checks?"):
        print("Nothing was run.")
        return 2

    approved = {x.path: x.new_id for x in protected}
    c = Checker(repo, start, result, contract._replace(approved=approved), a.image)
    run = uuid4().hex[:12]
    folder = data_folder()
    log = Log(folder / "events.db")
    try:
        log.add(run, "verify.started", base=start, result=result, contract=c.contract._asdict())
        print("Checking in fresh containers. This takes about a minute; Ctrl+C stops and cleans up.", flush=True)
        c.check()
        end = log.add(run, "verify.done", steps=[s._asdict() for s in c.steps])
        r = receipt.build(c, log_end=end)
    except KeyboardInterrupt:
        log.add(run, "verify.cancelled")
        raise
    finally:
        log.close()
    path = receipt.save(r, folder / "receipts" / run)
    text = receipt.as_text(_masked(r, ()))
    print("\n" + "\n".join(map(visible, text.split("\n"))) + f"\nReceipt  {path}")
    return {"VERIFIED": 0, "FAILED": 1}.get(r["verdict"], 2)


def merge(argv: Sequence[str]) -> int:
    """Show how to merge. Only the developer merges."""
    p = argparse.ArgumentParser(prog="agent merge")
    p.add_argument("branch")
    branch = p.parse_args(argv).branch
    repo = repository()
    start, result = find_base(repo, branch, "")
    names = [c for c in changes(repo, start, result) if c.new_mode != "000000"]
    notes = [f"{c.path}: {what}" for c in names for what in (
        "a link" if c.new_mode == "120000" else "", "a submodule" if c.new_mode == "160000" else "",
        "can be run as a program" if c.new_mode == "100755" != c.old_mode else "",
        "can make something run, or change how tools treat files" if gitsafe.RUNS_LATER.search(c.path) else "",
    ) if what]
    for n in notes:
        print("Look at   " + visible(n))
    print("Merging can run your own Git hooks. The code runs when you run it.")
    print(f"To merge  git merge --no-ff {result}")  # the exact commit: the branch could move
    print("To undo   git revert -m 1 <the merge commit>")
    return 0


def discard(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="agent discard")
    p.add_argument("branch")
    branch = p.parse_args(argv).branch
    if not confirm(f"Delete the branch {visible(branch)}? Your files stay as they are."):
        print("Nothing was deleted.")
        return 2
    old = gitsafe.discard(repository(), branch)
    print(f"Deleted. To bring it back: git branch {branch} {old}")
    return 0
