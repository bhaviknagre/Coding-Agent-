"""Run project commands in short-lived, limited Docker containers.

The developer's folder is never mounted. Files of one commit are copied into a Docker volume, each command gets
a new container without network, and everything made here is labelled, so cleanup never touches anything else.
Containers share the computer's kernel: this limits harm, it is not complete protection.
"""
import contextlib
import functools
import json
import os
import posixpath
import queue
import re
import signal
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import IO, Any, NamedTuple
from uuid import uuid4

from agent import doctor, scan

LABEL = "coding-agent-v2"
HELPER_IMAGE = "python:3.12-slim"
GB = 10**9
MAX_FILES = 3 * GB  # everything in one volume
MIN_DISK, MIN_NAMES = 2 * GB, 200_000  # kept free on Docker's disk
KEEP_OUTPUT = 10_000_000
COMMAND_SECONDS = 600
WATCH_SECONDS = 2.0


class BoxError(Exception):
    """Docker or Git did not do what was asked. The message is shown to the human."""


class Result(NamedTuple):
    status: str  # EXITED, TIMED_OUT, CANCELLED, OUT_OF_MEMORY, TOO_MUCH_DISK or LOW_SPACE
    code: int
    output: str
    truncated: bool
    seconds: float


class Look(NamedTuple):
    files: int  # bytes in the volume
    disk_used: int  # bytes used on Docker's whole disk: also sees files that are deleted but still open
    disk_free: int
    names_free: int  # how many more files Docker's disk can hold


class Copied(NamedTuple):
    files: int
    left_out: list[str]  # "name: reason"


class _Hold:
    depth = 0
    pending = False


@contextlib.contextmanager
def uninterrupted() -> Iterator[None]:
    """Ctrl+C inside this block takes effect at its end. Without it, an interrupt could leave Docker making something
    that we no longer know about and never remove. Needs signals_stop_cleanly(); only the main thread gets signals."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _Hold.depth += 1
    try:
        yield
    finally:
        _Hold.depth -= 1
        if _Hold.depth == 0 and _Hold.pending:
            _Hold.pending = False
            raise KeyboardInterrupt


def signals_stop_cleanly() -> None:
    """Closing the terminal or 'kill' should clean up like Ctrl+C does. Call once, at program start."""

    def stop(*_: object) -> None:
        if _Hold.depth:
            _Hold.pending = True
        else:
            raise KeyboardInterrupt

    # Ctrl+C too: a program started in the background inherits 'ignore Ctrl+C' from the shell.
    for name in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(name, stop)


def docker(*args: str, timeout: float = 60, ok: bool = True) -> str:
    with uninterrupted():
        try:
            p = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=timeout, start_new_session=True
            )
        except subprocess.TimeoutExpired as e:
            raise BoxError(f"Docker did not answer ({args[0]}). Next: run 'agent doctor'.") from e
        except OSError as e:
            raise BoxError("Docker was not found. Next: run 'agent doctor'.") from e
    if ok and p.returncode:
        raise BoxError(f"Docker refused '{args[0]}': {p.stderr.strip()[-300:]}")
    return p.stdout.strip()


# ---------------------------------------------------------------- cleanup


class Item(NamedTuple):
    kind: str
    name: str
    labels: dict[str, str]


def our_items() -> list[Item]:
    """Our containers, then our volumes. An image can set labels on a container, but not its name: both must match."""
    # Docker's list already holds the labels we need. Asking each container would wait for one that is stuck.
    wanted = ("task", "owner")
    fields = "".join(f'\t{{{{.Label "{LABEL}.{x}"}}}}' for x in wanted)
    items = []
    for kind, listing, name in (("container", ("ps", "-a"), "{{.Names}}"), ("volume", ("volume", "ls"), "{{.Name}}")):
        for line in docker(*listing, "--filter", f"label={LABEL}=task", "--format", name + fields).split("\n"):
            found, *values = line.split("\t")
            if found.startswith(f"{LABEL}-"):
                items.append(Item(kind, found, {f"{LABEL}.{x}": v for x, v in zip(wanted, values, strict=False)}))
    return items


def _delete(item: Item) -> None:
    if item.kind == "container":
        docker("rm", "-f", "-v", item.name, ok=False)
    else:
        docker("volume", "rm", "-f", item.name, ok=False)


def clean(task: str) -> None:
    """Remove the containers and volumes of one task, and nothing else."""
    for item in (mine := [i for i in our_items() if i.labels.get(f"{LABEL}.task") == task]):
        _delete(item)
    if left := {i.name for i in mine} & {i.name for i in our_items()}:
        raise BoxError(f"could not remove {len(left)} Docker item(s) of task {task}")


def clean_leftovers() -> int:
    """Remove items whose own launcher is no longer running, for example after a crash."""
    dead = [i for i in our_items() if not _alive(i.labels.get(f"{LABEL}.owner", ""))]
    for item in dead:
        _delete(item)
    return len(dead)


def _alive(owner: str) -> bool:
    # ponytail: a reused process number hides a leftover until that program ends; store the start time if it matters
    if not re.fullmatch(r"[1-9]\d{0,8}", owner):
        return False
    try:
        os.kill(int(owner), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- reading one commit


def git_bytes(repo: Path, *args: str) -> bytes:
    """Read the developer's own repository. Nothing is fetched, replaced or written."""
    # The developer's colour setting would put escape codes into the answers that we read.
    plain = ("-c", "color.ui=false", "-c", "color.grep=false", "-c", "color.diff=false")
    p = subprocess.run(["git", "-C", str(repo), *plain, *args], capture_output=True, env=git_env())
    if p.returncode:
        raise BoxError(f"Git refused '{args[0]}': {p.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return p.stdout


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return env | {"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"}


def commit_id(repo: Path, name: str) -> str:
    return git_bytes(repo, "rev-parse", "--verify", "--end-of-options", f"{name}^{{commit}}").decode().strip()


class Entry(NamedTuple):
    mode: str
    oid: str
    size: int
    name: str


def list_commit(repo: Path, commit: str) -> tuple[list[Entry], list[str]]:
    """Return the files to copy and the files left out, with the reason."""
    take: list[Entry] = []
    left_out: list[str] = []
    for line in git_bytes(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit).split(b"\0"):
        if not line:
            continue
        info, raw = line.split(b"\t", 1)
        mode, _, oid, size = info.decode().split()
        name = raw.decode("utf-8", "surrogateescape")
        if not (size.isdigit() or size == "-"):
            raise BoxError(f"Git has no content for '{name}'. Next: fetch the missing objects (partial clone?).")
        entry = Entry(mode, oid, int(size) if size != "-" else 0, name)
        reason = _reason_to_leave_out(entry)
        if reason:
            left_out.append(f"{name}: {reason}")
        else:
            take.append(entry)
    refuse_conflicts(take)
    return take, left_out


def refuse_conflicts(entries: list[Entry]) -> None:
    names = {e.name for e in entries}
    for e in entries:
        if len(names) != len(entries) or any(str(p) in names for p in PurePosixPath(e.name).parents):
            raise BoxError(f"the commit has conflicting paths near '{e.name}'")


def unsafe_path(name: str) -> bool:
    return any(p in ("", ".", "..") or p.lower().rstrip(". ") == ".git" for p in name.split("/"))


def _reason_to_leave_out(e: Entry) -> str:
    if unsafe_path(e.name):
        return "unsafe path"
    if e.mode == "160000":
        return "submodule"
    if scan.BLOCKED_NAMES.search(e.name):
        return "looks like a secret"
    return "" if e.mode in ("100644", "100755", "120000") else f"unknown type {e.mode}"


def _write_tar(repo: Path, entries: list[Entry], out: IO[bytes]) -> None:
    command = ["git", "-C", str(repo), "cat-file", "--batch"]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=git_env()) as p:
        assert p.stdin and p.stdout  # noqa: S101 - for the type checker
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for e in entries:
                p.stdin.write(e.oid.encode() + b"\n")
                p.stdin.flush()
                head = p.stdout.readline().split()
                if len(head) != 3 or head[1] != b"blob" or int(head[2]) != e.size:
                    raise BoxError(f"Git could not read '{e.name}'")
                item = tarfile.TarInfo(e.name)
                if e.mode == "120000":
                    item.type = tarfile.SYMTYPE
                    item.linkname = os.fsdecode(p.stdout.read(e.size))
                    tar.addfile(item)
                else:
                    item.size, item.mode = e.size, 0o755 if e.mode == "100755" else 0o644
                    tar.addfile(item, p.stdout)
                p.stdout.read(1)
        p.stdin.close()


# ---------------------------------------------------------------- limits


def image_id(name: str) -> str:
    """The exact image, by its content. Nothing is downloaded."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", name):
        return name
    return docker("image", "inspect", "-f", "{{.Id}}", "--", name)


_helper_id = functools.cache(lambda: image_id(HELPER_IMAGE))


@functools.cache
def _limits() -> list[str]:
    cpus, memory = docker("info", "-f", "{{.NCPU}} {{.MemTotal}}").split()
    size = min(3 * GB, int(memory) // 2)
    return ["--cpus", str(min(4, int(cpus))), "--memory", str(size), "--memory-swap", str(size)]


@functools.cache
def _image_volumes(image: str) -> list[str]:
    """Places the image declares as volumes would be unlimited disk; give them small memory disks instead."""
    flags: list[str] = []
    declared = json.loads(docker("image", "inspect", "-f", "{{json .Config.Volumes}}", image)) or {}
    for path in declared:
        clean_path = path.removesuffix("/")
        # Docker tidies '//a' or '/a/../b' later and would then mount a real disk over our limit.
        if not re.fullmatch(r"(/[\w.-]+)+", clean_path) or posixpath.normpath(clean_path) != clean_path:
            raise BoxError("the image declares a volume with an unusual name")
        if clean_path not in ("/work", "/tmp"):  # noqa: S108
            flags += ["--tmpfs", f"{clean_path}:size=64m"]
    return flags


HARDENED = (
    "--network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --pids-limit 512 "
    "--ulimit core=0 --tmpfs /tmp:size=512m --init --log-driver none --no-healthcheck --pull never "
    "--workdir /work --env HOME=/tmp"
).split()
# The docker program adds the developer's proxy settings, which may hold a password. Empty values replace them.
for _name in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "NO_PROXY", "ALL_PROXY"):
    HARDENED += ["--env", f"{_name}=", "--env", f"{_name.lower()}="]


# ---------------------------------------------------------------- the box


class Box:
    """One volume with one copy of the project. Use with 'with'; leaving removes everything."""

    def __init__(self, image: str, task: str | None = None, cancel: threading.Event | None = None) -> None:
        self.task = task or uuid4().hex[:12]
        if not re.fullmatch(r"[a-z0-9-]{1,40}", self.task):
            raise BoxError("a task name may only use a-z, 0-9 and -")
        if (space := doctor.space()).state == "STOP":
            raise BoxError(f"Only {space.text}. Next: {space.next_step}")
        self.cancel = cancel or threading.Event()
        self.image, self.helper = image_id(image), _helper_id()
        self.limits = _limits()
        self.places = _image_volumes(self.image)
        self.volume = f"{LABEL}-{self.task}-{uuid4().hex[:8]}"

    def __enter__(self) -> "Box":
        try:
            docker("volume", "create", *self._labels(), self.volume)
            # Only our own fixed helper commands run as root: here, give the empty volume to the ordinary user.
            self.disk_before = self.look("chown 1000:1000 /work; ").disk_used
        except BaseException:
            self._remove()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self._remove()

    def _remove(self) -> None:
        with uninterrupted():  # a second Ctrl+C must not stop the cleanup
            self._remove_all()

    def _remove_all(self) -> None:
        problem = ""
        for pause in (0, 0.5, 1, 2):
            time.sleep(pause)
            try:
                # Normally each command has removed its container already, so this one call is enough.
                if problem:
                    for item in our_items():
                        if item.kind == "container" and item.name.startswith(self.volume):
                            _delete(item)
                docker("volume", "rm", "-f", self.volume)
                return
            except BoxError as e:
                problem = str(e)
        raise BoxError(f"cleanup failed, the next start tries again: {problem}")

    def _labels(self) -> list[str]:
        values = (f"{LABEL}=task", f"{LABEL}.task={self.task}", f"{LABEL}.owner={os.getpid()}")
        return [x for v in values for x in ("--label", v)]

    def _name(self) -> list[str]:
        return ["--name", f"{self.volume}-{uuid4().hex[:8]}"]

    def _mount(self, write: bool) -> list[str]:
        # nocopy: without it Docker fills an empty volume with files from the image.
        return ["--mount", f"type=volume,src={self.volume},dst=/work,volume-nocopy{'' if write else ',readonly'}"]

    def _helper(self, args: list[str], capability: str, ok: bool = True) -> str:
        flags = [*HARDENED, "--user", "0:0", "--cap-add", capability, "--memory", "256m", "--rm"]
        flags += [*self._name(), *self._labels(), *self._mount(True), "--entrypoint", ""]
        return docker("run", *flags, self.helper, *args, ok=ok)

    def copy_in(self, repo: Path, commit: str) -> Copied:
        entries, left_out = list_commit(repo, commit_id(repo, commit))
        self.copy_entries(repo, entries)
        return Copied(len(entries), left_out)

    def copy_entries(self, repo: Path, entries: list[Entry]) -> None:
        refuse_conflicts(entries)
        if sum(e.size for e in entries) > MAX_FILES:
            raise BoxError("the project is larger than the 3 GB limit")
        r = self.run(
            ["tar", "-x", "--no-same-owner", "-C", "/work"], image=self.helper,
            feed=lambda out: _write_tar(repo, entries, out),
        )
        if r.status != "EXITED" or r.code:
            raise BoxError(f"copying the files failed: {r.status} {r.output[-300:]}")

    def look(self, first: str = "") -> Look:
        """Measure the volume and the disk behind it. May look into folders that project code has closed."""
        measure = first + "du -sk /work; df -k --output=used,avail,iavail /work"
        # du complains when a file vanishes while it counts; that is normal, so only its total matters.
        out = self._helper(["sh", "-c", measure], "CHOWN" if first else "DAC_READ_SEARCH", ok=False)
        m = re.search(r"^(\d+)\t/work$[\s\S]*^\s*(\d+)\s+(\d+)\s+(\d+)\s*$", out, re.M)
        if not m:
            raise BoxError("could not measure the disk use of the project")
        files, used, free, names = map(int, m.groups())
        return Look(files * 1024, used * 1024, free * 1024, names)

    def run(
        self,
        args: list[str],
        *,
        write: bool = True,
        image: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = COMMAND_SECONDS,
        feed: Callable[[IO[bytes]], None] | None = None,
    ) -> Result:
        """Run one program, without a shell, in a new container, and remove the container."""
        own = image is None or image == self.image
        flags = [*HARDENED, *self.limits, *(self.places if own else []), *self._labels(), *self._mount(write)]
        flags += [x for k, v in (env or {}).items() for x in ("--env", f"{k}={v}")]
        if feed:
            flags.append("--interactive")
        container = self._name()
        name = container[1]
        try:
            docker("create", *container, *flags, "--entrypoint", "", image or self.image, *args)
            if own:  # our own helper image declares no disks
                mounts = json.loads(docker("inspect", "-f", "{{json .Mounts}}", name))
                if [m.get("Name") for m in mounts] != [self.volume]:
                    raise BoxError("the container would get a disk that is not ours; refused")
            return self._watch(name, timeout, feed)
        finally:
            docker("rm", "-f", "-v", name, ok=False)

    def _watch(self, name: str, timeout: float, feed: Callable[[IO[bytes]], None] | None) -> Result:
        started, next_look, status = time.monotonic(), time.monotonic() + WATCH_SECONDS, "EXITED"
        # A session of its own: Ctrl+C reaches us, not the docker program, so we decide how to stop.
        command = ["docker", "start", "--attach", *(["--interactive"] if feed else []), name]
        pipes: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "start_new_session": True}
        errors: list[Exception] = []
        kept = Kept()
        with uninterrupted():
            p = subprocess.Popen(command, stdin=subprocess.PIPE if feed else subprocess.DEVNULL, **pipes)
        with p:
            threads = [threading.Thread(target=kept.read, args=(p.stdout,), daemon=True)]
            if feed:
                threads.append(threading.Thread(target=_feed, args=(feed, p.stdin, errors), daemon=True))
            for t in threads:
                t.start()
            try:
                while p.poll() is None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        p.wait(0.05)
                        break
                    if self.cancel.is_set():
                        status = "CANCELLED"
                        docker("stop", "--time", "10", name, ok=False)
                    elif time.monotonic() - started > timeout:
                        status = "TIMED_OUT"
                    elif time.monotonic() > next_look:
                        status = self._too_much()
                        next_look = time.monotonic() + WATCH_SECONDS
                    if status != "EXITED":
                        break
            finally:
                if p.poll() is None:
                    with contextlib.suppress(BoxError):  # if Docker hangs, we must still get away
                        docker("kill", name, ok=False, timeout=20)
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    p.kill()
                for t in threads:
                    t.join(20)
        state = json.loads(docker("inspect", "-f", "{{json .State}}", name))
        if state["Error"] or state["StartedAt"].startswith("0001"):
            raise BoxError(f"the command could not start: {state['Error'] or kept.text()[-300:]}")
        if state["Status"] != "exited" and status == "EXITED":
            raise BoxError("the connection to Docker broke while the command was running")
        if errors:
            raise errors[0]
        if status == "EXITED":
            # Also when only a child was killed: its missing work makes the result doubtful.
            # 137 means killed, and here it was not us: on Linux, Docker may set OOMKilled a moment too late.
            status = "OUT_OF_MEMORY" if state["OOMKilled"] or state["ExitCode"] == 137 else self._too_much()
        return Result(status, state["ExitCode"], kept.text(), kept.truncated, time.monotonic() - started)

    def _too_much(self) -> str:
        now = self.look()
        # Other Docker work at the same time also counts here; stopping too early is the safe side.
        if now.files > MAX_FILES or now.disk_used - self.disk_before > MAX_FILES:
            return "TOO_MUCH_DISK"
        low = now.disk_free < MIN_DISK or now.names_free < MIN_NAMES or doctor.space().state == "STOP"
        return "LOW_SPACE" if low else "EXITED"


class Session:
    """One of our own fixed programs in a helper container, asked one line at a time. Use with 'with'.

    The program is ours, but the files it works on are not: every answer has a time limit and a size limit.
    """

    LINE = 10_000_000

    def __init__(self, b: Box, program: str, write: bool = True) -> None:
        self.name = b._name()
        flags = [*HARDENED, *b.limits, *b._labels(), *b._mount(write), "--interactive"]
        try:
            docker("create", *self.name, *flags, "--entrypoint", "", b.helper, "python", "-I", "-c", program)
            with uninterrupted():
                self.process = subprocess.Popen(
                    ["docker", "start", "--attach", "--interactive", self.name[1]], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                )
        except BaseException:
            docker("rm", "-f", "-v", self.name[1], ok=False)
            raise
        self.closed = False
        self.answers: queue.Queue[bytes] = queue.Queue(2)  # more waits in the container, not in our memory
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout  # noqa: S101 - for the type checker
        while True:
            line = self.process.stdout.readline(self.LINE)
            while not self.closed:
                with contextlib.suppress(queue.Full):
                    self.answers.put(line, timeout=0.5)
                    break
            if self.closed or not line.endswith(b"\n"):  # ended, or far too long
                return

    def ask(self, question: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        end = time.monotonic() + timeout
        try:
            # In a thread: a helper that does not read must not block us for ever.
            w = threading.Thread(target=self._write, args=(json.dumps(question).encode() + b"\n",), daemon=True)
            w.start()
            w.join(timeout)
            answer = json.loads(self.answers.get(timeout=max(0.01, end - time.monotonic())))
            if not isinstance(answer, dict):
                raise ValueError
            return answer
        except BaseException as e:
            self.close()  # after a late or missing answer, nothing that follows could be matched to its question
            if isinstance(e, ValueError | queue.Empty | RecursionError):
                raise BoxError("the file helper gave no clear answer") from e
            raise

    def _write(self, data: bytes) -> None:
        with contextlib.suppress(OSError, ValueError):
            if self.process.stdin:
                self.process.stdin.write(data)
                self.process.stdin.flush()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with uninterrupted():
            self.closed = True
            # First leave, then remove: on Linux, Docker cannot remove a container while output to us is blocked.
            self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(10)
            docker("rm", "-f", "-v", self.name[1], ok=False)
            self.reader.join(5)
            with contextlib.suppress(OSError):
                if self.process.stdin:
                    self.process.stdin.close()
            if self.process.stdout and not self.reader.is_alive():
                self.process.stdout.close()


def _feed(feed: Callable[[IO[bytes]], None], out: IO[bytes], errors: list[Exception]) -> None:
    try:
        with contextlib.suppress(BrokenPipeError), out:  # the program may end without reading everything
            feed(out)
    except Exception as e:
        errors.append(e)


class Kept:
    """Keep the first and last half of the output, and say when the middle is missing."""

    def __init__(self) -> None:
        self.first, self.last, self.missing = bytearray(), bytearray(), 0

    def read(self, pipe: IO[bytes]) -> None:
        half = KEEP_OUTPUT // 2
        for piece in _pieces(pipe):
            room = half - len(self.first)
            self.first += piece[:room]
            self.last += piece[room:]
            if len(self.last) > half:
                self.missing += len(self.last) - half
                del self.last[: len(self.last) - half]

    @property
    def truncated(self) -> bool:
        return self.missing > 0

    def text(self) -> str:
        middle = f"\n[{self.missing} bytes not kept]\n".encode() if self.missing else b""
        return bytes(self.first + middle + self.last).decode("utf-8", "replace")


def _pieces(pipe: IO[bytes]) -> Iterator[bytes]:
    while piece := os.read(pipe.fileno(), 65536):
        yield piece
