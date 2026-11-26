"""Turn the files in a box into one commit on a new agent branch, without trusting what is in the box.

No container holds Git. The launcher reads plain files, checks them, and makes the commit itself with Git's low-level
commands: no checkout, no hooks, no filters, and the developer's files and current branch are never touched.
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import IO, Any, NamedTuple

from agent import box, scan
from agent.mask import mask

MAX_CHANGE = 5_000_000  # bytes of all changed files together
BRANCH = re.compile(r"agent/[a-z0-9][a-z0-9-]{0,39}")
RUNS_LATER = re.compile(  # files that can make something run after a merge, or change how Git treats files
    r"""(?ix)(^|/)(
        \.git(attributes|modules|ignore) | \.github/.* | \.gitlab-ci\.yml | \.circleci/.* | jenkinsfile
        | azure-pipelines\.yml | \.claude/.* | \.cursor/.* | \.mcp\.json | \.npmrc | justfile
        | tox\.ini | build\.gradle(\.kts)?
        | \.husky/.* | \.pre-commit-config\.yaml | \.vscode/.* | \.idea/.* | \.devcontainer(/.*|\.json) | \.envrc
        | (gnu)?makefile | dockerfile[^/]* | (docker-)?compose[^/]*\.ya?ml | setup\.py | pyproject\.toml | package\.json
        | (agents|claude|gemini)\.md | conftest\.py | [^/]*\.pth
    )$"""
)


class Refused(Exception):
    """The result is not safe to take. The message is shown to the human."""


class File(NamedTuple):
    path: str
    mode: str  # 100644, 100755, 120000, or "" for a deleted file
    data: bytes = b""


class Found(NamedTuple):
    files: list[File]  # changed files to commit
    not_taken: list[str]  # new files that only a command made, such as caches


# Runs in our own helper container over untrusted files, without following links.
READER = r"""
import base64, hashlib, json, os, stat, sys
want = json.loads(sys.stdin.readline())
known, limit, total, seen = want["known"], want["limit"], 0, 0
def out(**k): print(json.dumps(k), flush=True)
def stop(why): out(refuse=why); sys.exit(0)
def show(p): return base64.b64encode(os.fsencode(p)).decode()
for folder, dirs, names in os.walk("/work", onerror=lambda e: stop("a folder cannot be read")):
    for d in list(dirs):
        if os.path.islink(os.path.join(folder, d)):
            dirs.remove(d); names.append(d)
    for n in names:
        p = os.path.join(folder, n); rel = show(os.path.relpath(p, "/work")); s = os.lstat(p)
        seen += 1
        if seen > 200000: stop("too many files")
        if stat.S_ISLNK(s.st_mode): mode, data, size = "120000", os.fsencode(os.readlink(p)), None
        elif stat.S_ISREG(s.st_mode):
            mode, data, size = ("100755" if s.st_mode & 0o100 else "100644"), None, s.st_size
        else:
            out(special=rel); continue
        h = hashlib.new(want["hash"])
        h.update(b"blob %d\0" % (len(data) if data is not None else size))
        if data is not None: h.update(data)
        else:
            try: f = open(p, "rb")
            except OSError: stop("a file cannot be read")
            with f:
                while True:
                    b = f.read(1 << 20)
                    if not b: break
                    h.update(b)
        old = known.pop(rel, None)
        if old == [mode, h.hexdigest()]: continue
        if old is None and rel not in want["edited"]:
            out(new=rel); continue
        if data is None:
            total += size
            if total > limit: stop("the changed files are too large")
            with open(p, "rb") as f: data = f.read(limit + 1)
        out(path=rel, mode=mode, data=base64.b64encode(data).decode())
for rel in known: out(path=rel, mode="", data="")
out(end=True)
"""


def _name(text: str) -> str:
    return base64.b64decode(text, validate=True).decode("utf-8", "surrogateescape")


def _code(name: str) -> str:
    return base64.b64encode(name.encode("utf-8", "surrogateescape")).decode()


def read_box(b: box.Box, copied: list[box.Entry], edited: set[str]) -> Found:
    """Compare the box with what was copied in. Tracked files count; of new files only those in `edited`."""
    want = {
        "known": {_code(e.name): [e.mode, e.oid] for e in copied}, "edited": sorted(map(_code, edited)),
        "limit": MAX_CHANGE, "hash": "sha256" if copied and len(copied[0].oid) == 64 else "sha1",
    }

    def feed(out: IO[bytes]) -> None:
        out.write(json.dumps(want).encode() + b"\n")

    r = b.run(["python", "-I", "-c", READER], image=b.helper, write=False, feed=feed, timeout=300)
    if r.status != "EXITED" or r.code or r.truncated:
        raise Refused(f"the files could not be read: {r.status}")
    try:
        return _parse([json.loads(line) for line in r.output.splitlines()])
    except (ValueError, KeyError, TypeError) as e:
        raise Refused("the list of files is not well formed") from e


def _parse(lines: list[dict[str, Any]]) -> Found:
    files, not_taken = [], []
    for x in lines:
        if "refuse" in x:
            raise Refused(x["refuse"])
        if "special" in x:
            not_taken.append(_name(x["special"]) + " (not a normal file)")
        elif "new" in x:
            not_taken.append(_name(x["new"]))
        elif "path" in x:
            files.append(File(_name(x["path"]), x["mode"], base64.b64decode(x["data"])))
    if not lines or lines[-1] != {"end": True}:
        raise Refused("the list of files is incomplete")
    return Found(sorted(files), sorted(not_taken))


def check_files(repo: Path, base: str, files: list[File], secrets: tuple[str, ...] = ()) -> list[str]:
    """Refuse what is not safe to take; return what the human should look at before merging."""
    old = {e.name: e for e in _tree(repo, base)}
    final = {n: e.mode for n, e in old.items()}
    notes = []
    if sum(len(f.data) for f in files) > MAX_CHANGE:
        raise Refused("the changed files are too large")
    for f in files:
        shown = ascii(f.path)
        if box.unsafe_path(f.path) or not f.path.isprintable() or "\\" in f.path:
            raise Refused(f"unsafe file name {shown}")
        if scan.BLOCKED_NAMES.search(f.path):
            raise Refused(f"{shown} has the name of a secret file")
        if f.mode == "":
            final.pop(f.path, None)
            continue
        if f.mode not in ("100644", "100755", "120000"):
            raise Refused(f"{shown} is not a normal file")
        final[f.path] = f.mode
        if f.mode == "120000":
            notes.append(f"{f.path}: a link to {f.data.decode('utf-8', 'surrogateescape')}")
        new = _secrets(f.data) | _secrets(f.path.encode("utf-8", "surrogateescape"))
        new -= _secrets(_content(repo, old.get(f.path)))
        if new or any(x and x.encode() in (f.data + b"\0" + f.path.encode("utf-8", "replace")) for x in secrets):
            kinds = ", ".join(sorted({n for n, _ in new})) or "key"
            raise Refused(f"{shown} would add what looks like a secret ({kinds})")
        if f.mode == "100755" and (f.path not in old or old[f.path].mode != "100755"):
            notes.append(f"{f.path}: can be run as a program")
        if RUNS_LATER.search(f.path):
            notes.append(f"{f.path}: can make something run, or change how tools treat files")
    _refuse_collisions(final, {f.path for f in files})
    links = {_key(n) for n, mode in final.items() if mode == "120000"}
    for f in files:
        if f.mode == "120000":
            _check_link(f, links)
    return notes


def _key(name: str) -> str:
    """Names with the same key are one file on macOS and Windows."""
    return unicodedata.normalize("NFC", name).casefold()


def _check_link(f: File, links: set[str]) -> None:
    """A link must stay inside and may not go through another link: 'l/../x' is not what the text suggests."""
    target = f.data.decode("utf-8", "surrogateescape")
    parts = target.split("/")
    up = next((i for i, p in enumerate(parts) if p != ".."), len(parts))
    folder = f.path.split("/")[:-1]
    rest = parts[up:]
    place = [_key(p) for p in folder[: len(folder) - up] + rest]
    through = {"/".join(place[:i]) for i in range(1, len(place) + 1)}
    git_like = any(re.fullmatch(r"\.git[ .]*|git~\d+", p) for p in place)
    if (
        not target.isprintable() or "\\" in target or up > len(folder) or not rest or git_like
        or any(p in ("", ".", "..") for p in rest) or through & links
    ):
        raise Refused(f"{ascii(f.path)} is a link that leaves the project or cannot be checked")


def _tree(repo: Path, commit: str) -> list[box.Entry]:
    found = []
    for line in box.git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if line:
            info, name = line.split(b"\t", 1)
            mode, _, oid = info.decode().split()
            found.append(box.Entry(mode, oid, 0, name.decode("utf-8", "surrogateescape")))
    return found


def _content(repo: Path, e: box.Entry | None) -> bytes:
    return box.git_bytes(repo, "cat-file", "blob", e.oid) if e and e.mode.startswith("100") else b""


def _secrets(data: bytes) -> set[tuple[str, str]]:
    # Also without NUL bytes, which is how UTF-16 text looks.
    text = data.decode("utf-8", "replace") + "\n" + data.replace(b"\0", b"").decode("utf-8", "replace")
    private = ("personal-email", "home-path")
    return {(name, m.group()) for name, rx in scan.RULES if name not in private for m in rx.finditer(text)}


def _refuse_collisions(final: dict[str, str], changed: set[str]) -> None:
    """On macOS and Windows 'A' and 'a' are the same file; a link or file cannot also be a folder."""
    seen: dict[str, str] = {}
    for name in final:
        key = _key(name)
        parts = key.split("/")
        for i in range(1, len(parts) + 1):
            part = "/".join(parts[:i])
            real = "/".join(name.split("/")[:i])
            other = seen.setdefault(part, real)
            if other != real and changed & {name, other}:
                raise Refused(f"{ascii(real)} and {ascii(other)} are the same file on some computers")
    keys = {_key(n) for n in final}
    for name in final:
        parts = _key(name).split("/")
        if any("/".join(parts[:i]) in keys for i in range(1, len(parts))):
            raise Refused(f"{ascii(name)} is inside something that is not a folder")


SAFE = {
    "core.hooksPath": "/dev/null", "core.fsmonitor": "false", "core.splitIndex": "false",
    "core.sparseCheckout": "false", "index.sparse": "false", "core.protectHFS": "true", "core.protectNTFS": "true",
    "commit.gpgSign": "false",
    "gc.auto": "0", "maintenance.auto": "false", "color.ui": "false",
    "user.name": "coding-agent", "user.email": "coding-agent@example.invalid",
}


class Git:
    """Git in the developer's repository, with everything off that could run something."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.env = box.git_env()
        settings = dict(SAFE)
        # Newer Git versions can also run hooks that are written in the configuration.
        command = ["git", "-C", str(repo), "config", "-z", "--name-only", "--get-regexp", r"^hook\."]
        found = subprocess.run(command, capture_output=True, env=self.env)
        if found.returncode not in (0, 1):
            raise Refused("the Git settings could not be read")
        for name in found.stdout.decode("utf-8", "surrogateescape").split("\0"):
            if name.count(".") > 1:
                settings[name.rsplit(".", 1)[0] + ".enabled"] = "false"
        self.env["GIT_CONFIG_COUNT"] = str(len(settings))
        for i, (name, value) in enumerate(settings.items()):
            self.env |= {f"GIT_CONFIG_KEY_{i}": name, f"GIT_CONFIG_VALUE_{i}": value}

    def __call__(self, *args: str, data: bytes | None = None, index: str = "", ok: bool = True) -> str:
        env = self.env | ({"GIT_INDEX_FILE": index} if index else {})
        command = ["git", "-C", str(self.repo), *args]
        with box.uninterrupted():
            p = subprocess.run(command, input=data, capture_output=True, env=env, start_new_session=True)
        if ok and p.returncode:
            raise Refused(f"Git refused '{args[0]}': {p.stderr.decode('utf-8', 'replace').strip()[-200:]}")
        return p.stdout.decode("utf-8", "surrogateescape").strip()

    def check_branch(self, branch: str) -> str:
        """Return the full name of an agent branch that is a plain branch and is not checked out anywhere."""
        if not BRANCH.fullmatch(branch):
            raise Refused("an agent branch must be named agent/ and then a-z, 0-9 or -")
        ref = f"refs/heads/{branch}"
        if self("symbolic-ref", "-q", ref, ok=False):
            raise Refused("this branch is only another name for a different branch")
        names = self("for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
        if any(_key(n) == _key(ref) and n != ref for n in names):
            raise Refused("a branch with almost the same name exists")
        used = self("worktree", "list", "--porcelain").splitlines()
        if any(_key(x) == _key(f"branch {ref}") for x in used):
            raise Refused("this branch is checked out; switch to another branch first")
        return ref


def commit(
    repo: Path, base: str, branch: str, files: list[File], message: str, parent: str = "",
    secrets: tuple[str, ...] = (), replace: str = "",
) -> str:
    """Make one commit and create the branch, or move it on from `parent`, our own last commit on it. With `replace`,
    the commit starts again from the base and takes the place of that earlier one of ours."""
    git = Git(repo)
    ref = git.check_branch(branch)
    base = box.commit_id(repo, base)
    start = parent or base
    check_files(repo, start, files, secrets)
    if not files:
        raise Refused("there is no change")
    null = "0" * len(base)
    wanted = {e.name: (e.mode, e.oid) for e in _tree(repo, start)}
    with tempfile.TemporaryDirectory() as d:
        index = os.path.join(d, "index")
        git("read-tree", start, index=index)
        rows = []
        # Deletions first, so a file can take the place of a folder.
        for f in sorted(files, key=lambda f: f.mode != ""):
            oid = git("hash-object", "-w", "--no-filters", "--stdin", data=f.data) if f.mode else null
            rows.append(f"{f.mode or '0'} {oid}\t{f.path}".encode("utf-8", "surrogateescape"))
            if f.mode:
                wanted[f.path] = (f.mode, oid)
            else:
                wanted.pop(f.path, None)
        git("update-index", "-z", "--index-info", data=b"\0".join(rows) + b"\0", index=index)
        tree = git("write-tree", index=index)
    # Git leaves out some names without an error, so compare what it made with what was asked for.
    if {e.name: (e.mode, e.oid) for e in _tree(repo, tree)} != wanted:
        raise Refused("Git did not accept every file name")
    if tree == git("rev-parse", f"{start}^{{tree}}"):
        raise Refused("there is no change")
    new = git("commit-tree", tree, "-p", start, data=mask(message, secrets).encode("utf-8", "replace"))
    git("update-ref", "--no-deref", ref, new, replace or parent or null)  # only if it is still what we expect
    return new


def move_back(repo: Path, branch: str, to: str) -> None:
    """Put an agent branch back on an earlier commit of ours, or delete it when there is none."""
    git = Git(repo)
    ref = git.check_branch(branch)
    tip = git("rev-parse", "-q", "--verify", "--end-of-options", ref, ok=False)
    if not tip or tip == to:
        return
    if to:
        git("update-ref", "--no-deref", ref, to, tip)
    else:
        git("update-ref", "--no-deref", "-d", ref, tip)


def discard(repo: Path, branch: str) -> str:
    """Delete one agent branch and return the commit it pointed to, so that it can be brought back."""
    git = Git(repo)
    ref = git.check_branch(branch)
    if ref not in git("for-each-ref", "--format=%(refname)", ref).splitlines():
        raise Refused("there is no such agent branch")
    old = git("rev-parse", "--verify", "--end-of-options", ref)
    git("update-ref", "--no-deref", "-d", ref, old)
    return old
