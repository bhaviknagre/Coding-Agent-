"""The agent's tools. The launcher decides and runs each one; the agent only asks.

Files are read and changed by a small fixed program of ours in a helper container. It walks folder by folder and never
follows a link, so a link cannot lead a read or a change anywhere else, not even when it appears at the last moment.
"""
import hashlib
import posixpath
import re
from collections.abc import Callable
from typing import Any, NamedTuple

from agent import approve, box, scan
from agent.approve import Answer
from agent.checker import Contract
from agent.mask import mask
from agent.policy import ASK, DENY, Action, Decision, Policy

MAX_RESULT = 30_000  # characters, about 8,000 tokens

FILES = r"""
import hashlib, json, os, re, stat, sys
BIG, SEEN = 1000000, 20000
class No(Exception): pass
def folder(parts, make=False):
    fd = os.open("/work", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for p in parts:
            if make:
                try: os.mkdir(p, dir_fd=fd)
                except FileExistsError: pass
            new = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = new
        return fd
    except OSError:
        os.close(fd)
        raise No("there is no such folder, or it is a link")
def split(path):
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts): raise No("the path is not inside the project")
    return parts[:-1], parts[-1]
def load(path):
    parts, name = split(path)
    d = folder(parts)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
    except FileNotFoundError: raise No("there is no such file")
    except OSError: raise No("this is a link, or cannot be read")
    finally: os.close(d)
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode):
        os.close(fd)
        raise No("this is not a normal file")
    with os.fdopen(fd, "rb") as f:
        data = f.read(BIG + 1)
    if len(data) > BIG: raise No("the file is too large")
    return data, s
def text(data):
    if b"\0" in data: raise No("this is not text")
    try: return data.decode()
    except UnicodeDecodeError: raise No("this is not UTF-8 text")
def digest(data): return hashlib.sha256(data).hexdigest()
def put(path, data, mode, new):
    parts, name = split(path)
    d = folder(parts, make=new)
    temp = ".coding-agent-" + os.urandom(8).hex()
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=d)
        try:
            with os.fdopen(fd, "wb") as f: f.write(data)
            os.chmod(temp, mode, dir_fd=d)
            if new:
                try: os.link(temp, name, src_dir_fd=d, dst_dir_fd=d, follow_symlinks=False)
                except FileExistsError: raise No("the file exists already")
            else: os.rename(temp, name, src_dir_fd=d, dst_dir_fd=d); temp = None
        finally:
            if temp: os.unlink(temp, dir_fd=d)
    except OSError as e: raise No("the file could not be written: " + e.strerror)
    finally: os.close(d)
def walk(path, blocked):
    parts = path.split("/") if path else []
    todo, seen = [parts], 0
    while todo:
        here = todo.pop()
        try: d = folder(here)
        except No:
            if here == parts: raise
            continue
        try:
            for e in os.scandir(d):
                seen += 1
                if seen > SEEN: raise No("too many files; give a smaller folder")
                name = "/".join(here + [e.name])
                if e.name == ".git" or blocked.search(name): continue
                s = e.stat(follow_symlinks=False)
                if stat.S_ISDIR(s.st_mode): todo.append(here + [e.name])
                elif stat.S_ISREG(s.st_mode): yield name, s.st_size
                elif stat.S_ISLNK(s.st_mode): yield name + " (link, not followed)", 0
        finally: os.close(d)
def do(q):
    op = q["op"]
    if op == "list":
        found = sorted(walk(q["path"], re.compile(q["blocked"])))
        return {"files": [[n[:1000], s] for n, s in found[:500]], "total": len(found)}
    if op == "search":
        hits, files, total, skipped, left = [], 0, 0, 0, 100000000
        for name, size in sorted(walk(q["path"], re.compile(q["blocked"]))):
            if name.endswith(" (link, not followed)"): continue
            left -= min(size, BIG) + 1000
            if left < 0: skipped += 1; continue
            try: lines = text(load(name)[0]).split("\n")
            except No: skipped += 1; continue
            here = [(name[:1000], i, x if len(x) <= 5000 else "[a very long line]")
                    for i, x in enumerate(lines, 1) if q["text"] in x]
            files += bool(here); total += len(here); hits += here[:max(0, 50 - len(hits))]
        return {"hits": hits, "files": files, "total": total, "skipped": skipped}
    data, s = load(q["path"]) if op != "create" else (b"", None)
    if op == "read": return {"text": text(data), "hash": digest(data)}
    if op == "create":
        put(q["path"], q["text"].encode(), 0o644, True)
        return {"hash": digest(q["text"].encode())}
    if digest(data) != q["hash"]: raise No("the file changed since it was read; read it again")
    if op == "delete":
        parts, name = split(q["path"]); d = folder(parts)
        try: os.unlink(name, dir_fd=d)
        finally: os.close(d)
        return {}
    if op == "edit":
        whole, old, new = text(data), q["old"], q["new"]
        if "\r\n" in whole and "\n" not in whole.replace("\r\n", ""):
            old, new = (x.replace("\r\n", "\n").replace("\n", "\r\n") for x in (old, new))
        at = whole.find(old)
        if not old or at < 0: raise No("the old text was not found")
        if whole.find(old, at + 1) >= 0: raise No("the old text is there more than once; give more of it")
        made = (whole[:at] + new + whole[at + len(old):]).encode()
        if len(made) > BIG: raise No("the file would be too large")
        put(q["path"], made, stat.S_IMODE(s.st_mode) & 0o755 | 0o600, False)
        return {"hash": digest(made)}
    raise No("unknown")
for line in sys.stdin:
    try: answer = do(json.loads(line))
    except No as e: answer = {"no": str(e)}
    except (OSError, ValueError, KeyError, MemoryError, RecursionError) as e:
        answer = {"no": "not possible: " + type(e).__name__}
    print(json.dumps(answer), flush=True)
"""


def word_is_here(word: str) -> bool:
    return posixpath.normpath(posixpath.join("/work", word)) == "/work" and word != ""


class Result(NamedTuple):
    """What the model gets to see. `status` is a fact from the launcher; `text` may be untrusted data."""

    status: str  # OK, FAILED, DENIED, REJECTED or ERROR
    text: str
    code: int | None = None
    truncated: bool = False


Human = Callable[[str], Answer]
Record = Callable[..., Any]


class Toolbox:
    """The tools of one task, working in one box."""

    def __init__(
        self, b: box.Box, contract: Contract, human: Human, record: Record = lambda *a, **k: None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.box, self.policy, self.human, self.record, self.secrets = b, Policy(contract), human, record, secrets
        self.contract = contract
        self.files = box.Session(b, FILES)
        self.read_hash: dict[str, str] = {}  # what the agent last saw of each file
        self.edited: set[str] = set()
        self.finished = ""

    def close(self) -> None:
        self.files.close()

    def ask_files(self, question: dict[str, Any]) -> dict[str, Any]:
        if self.files.closed:  # one odd file must not end the task
            self.files = box.Session(self.box, FILES)
        return self.files.ask(question)

    def call(self, action: Action, reason: str = "") -> Result:
        """Decide, ask if needed, check again that nothing changed, and only then do it."""
        decision = self.policy.decide(action)
        if decision.kind == DENY:  # the arguments may be anything, so they are not recorded
            name = action.tool[:50] if isinstance(action.tool, str) else "?"
            self.record("tool.denied", tool=name, rule=decision.rule)
            return Result("DENIED", decision.rule)
        reason = reason if isinstance(reason, str) else ""
        self.record("tool.asked", tool=action.tool, args=action.args, decision=decision.kind, rule=decision.rule)
        result = self._decided(action, decision, reason)
        text = mask(result.text, self.secrets)
        if len(text) > MAX_RESULT:
            half = MAX_RESULT // 2
            text = f"{text[:half]}\n[{len(text) - 2 * half} characters left out]\n{text[-half:]}"
            result = result._replace(truncated=True)
        result = result._replace(text=text)
        self.record("tool.done", tool=action.tool, status=result.status, code=result.code, text=text)
        return result

    def _decided(self, action: Action, decision: Decision, reason: str) -> Result:
        try:
            if decision.kind == ASK:
                refused = self._approve(action, decision, reason)
                if refused:
                    return refused
            return self._do(action)
        except box.BoxError as e:
            return Result("ERROR", str(e))

    def _approve(self, action: Action, decision: Decision, reason: str) -> Result | None:
        shown, state = self._shown(action)
        bound = (action.hash(), state)
        question = approve.draw(action, decision, reason, shown, self.secrets)
        if question is None:
            return Result("REJECTED", "this cannot be shown completely to the human; make it smaller or simpler")
        answer = self.human(question)
        self.record("human.answered", yes=answer.yes, reason=answer.reason, action=bound[0])
        if not answer.yes:
            return Result("REJECTED", "the human said no" + (f": {answer.reason}" if answer.reason else ""))
        # While the question waited, the action or what it uses may have changed. Then the yes was for something else.
        if (action.hash(), self._shown(action)[1]) != bound:
            return Result("REJECTED", "something changed while the question waited; ask again")
        return None

    def _shown(self, action: Action) -> tuple[dict[str, str | None], str]:
        """Project files that a command names: the human should see the script, not only its name. It cannot find
        every one, and the question says so."""
        shown: dict[str, str | None] = {}
        state = hashlib.sha256()
        for name in sorted(self._named(action)):
            answer = self.ask_files({"op": "read", "path": name})
            state.update(f"{name}\0{answer.get('hash')}\0{answer.get('no')}\0".encode())
            if action.tool == "run_command" and ("text" in answer or "no such" not in answer.get("no", "")):
                shown[name] = answer.get("text")
        return shown, state.hexdigest()

    def _named(self, action: Action) -> set[str]:
        if action.tool in ("edit", "delete"):
            return {action.args["path"]}
        found = set()
        for part in action.args.get("program", ()):
            for word in [part, *re.split(r"[\s;|&<>()'\"`$=,]+", part)][:200]:
                path = posixpath.normpath(posixpath.join("/work", word)).removeprefix("/work").lstrip("/")
                names = (path, f"{path}/__main__.py", f"{path}/__init__.py", path.replace(".", "/") + ".py",
                         path.replace(".", "/") + "/__main__.py")
                found |= {n for n in names if word and not box.unsafe_path(n) and len(n) < 1000 and n.isprintable()}
            if word_is_here(part):
                found.add("__main__.py")
        return found if len(found) <= 1000 else set(sorted(found)[:1000])

    def _do(self, action: Action) -> Result:
        a = action.args
        if action.tool in ("list", "search", "read", "edit", "create", "delete"):
            return self._file(action.tool, a)
        if action.tool == "run_command":
            return self._run(a["program"])
        if action.tool == "run_checks":
            return self._run(list(self.contract.command))
        if action.tool == "ask":
            text = mask(a["question"], self.secrets)[:1000]
            answer = self.human("\n".join(["", *approve.rows(text, "THE AGENT ASKS (its words): ", 100)]))
            return Result("OK", answer.reason or ("yes" if answer.yes else "the human gave no answer"))
        self.finished = a["summary"]
        return Result("OK", "the checks start now")

    def _run(self, program: list[str]) -> Result:
        r = self.box.run(program)
        self.record("command.ended", program=program, status=r.status, code=r.code, seconds=round(r.seconds, 2))
        good = r.status == "EXITED" and r.code == 0
        head = "" if r.status == "EXITED" else f"[{r.status}] "
        return Result("OK" if good else "FAILED", head + r.output, r.code, r.truncated)

    def _file(self, tool: str, a: dict[str, Any]) -> Result:
        path = a["path"]
        if tool in ("edit", "delete") and path not in self.read_hash:
            return Result("ERROR", "read the file first")
        question = {"op": tool, **a, "hash": self.read_hash.get(path, "")}
        if tool == "read":
            question.pop("first")
        if tool in ("list", "search"):
            question["blocked"] = scan.BLOCKED_NAMES.pattern
        x = self.ask_files(question)
        if "no" in x:
            return Result("ERROR", str(x["no"]))
        if tool == "list":
            more = "" if x["total"] <= 500 else f"\n[{x['total'] - 500} more]"
            return Result("OK", "\n".join(f"{n}  {s}" for n, s in x["files"]) + more, truncated=bool(more))
        if tool == "search":
            head = f"{x['total']} line(s) in {x['files']} file(s)"
            head += f"; {x['skipped']} file(s) not searched" if x["skipped"] else ""
            # Mask whole lines first: a cut secret would no longer be found.
            rows = [f"{n}:{i}: {mask(t, self.secrets)[:300]}" for n, i, t in x["hits"]]
            return Result("OK", "\n".join([head, *rows]), truncated=x["total"] > len(rows))
        if tool == "read":
            self.read_hash[path] = x["hash"]
            return self._page(mask(x["text"], self.secrets), a["first"])
        if tool == "delete":
            self.read_hash.pop(path)
            self.policy.created.discard(path)
            self.edited.add(path)
            return Result("OK", "deleted")
        self.read_hash[path] = x["hash"]
        self.edited.add(path)
        if tool == "create":
            self.policy.created.add(path)
        return Result("OK", "done")

    def _page(self, text: str, first: int) -> Result:
        rows = text.split("\n")
        page: list[str] = []
        size = 0
        for row in rows[first - 1:]:
            if not page and len(row) > MAX_RESULT - 2000:
                row = row[: MAX_RESULT - 2000] + " [the rest of this long line is left out]"
            elif size + len(row) > MAX_RESULT - 2000 or len(page) == 400:
                break
            page.append(row)
            size += len(row) + 1
        last = first - 1 + len(page)
        head = f"[lines {first} to {last} of {len(rows)}]\n"
        return Result("OK", head + "\n".join(page), truncated=last < len(rows) or first > 1)
