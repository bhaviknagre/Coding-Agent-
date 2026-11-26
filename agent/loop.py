"""One task, from request to verdict. The launcher owns this loop: it always ends, and no text from a model or a
project can make it say that something was proven."""
import hashlib
import json
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

from agent import box, gitsafe, receipt
from agent.approve import Answer, rows, screen_width
from agent.checker import FAIL, Checker, Contract
from agent.log import Log
from agent.mask import mask
from agent.model import Message, Model, ModelError, Reply, check_reply, tool_list
from agent.policy import TOOLS, Action
from agent.tools import Result, Toolbox
from agent.worker import SYSTEM, WORKER

MAX_REQUEST = 3_000_000


class Limits(NamedTuple):
    model_calls: int = 100
    tool_calls: int = 1000
    log_bytes: int = 50_000_000
    minutes: float = 45  # without the time spent waiting for the human
    tokens: int = 2_000_000
    tries: int = 3  # the first, and two more after FAILED
    malformed: int = 3
    cut: int = 2


class Stopped(Exception):
    """The task ends without a verdict. The message is shown to the human."""


class Outcome(NamedTuple):
    state: str  # a verdict, or STOPPED or BLOCKED
    reason: str
    branch: str = ""
    commit: str = ""
    receipt: dict[str, Any] | None = None


def short(args: object) -> str:
    """The file or the program, for the line on the screen."""
    if not isinstance(args, dict):
        return ""
    path = args.get("path")
    if isinstance(path, str):
        return path[:80]
    program = args.get("program")
    if isinstance(program, list) and program and all(isinstance(x, str) for x in program):
        return " ".join(program)[:80]
    text = args.get("text")
    return text[:40] if isinstance(text, str) else ""


class Repeats:
    """Notices when nothing new happens: one warning, then the end."""

    def __init__(self) -> None:
        self.past: list[tuple[str, str, str]] = []  # call, result, error text
        self.warned = False

    def add(self, call: str, result: str, error: str) -> str:
        """Return "" or the warning; raise at the second time."""
        self.past.append((call, result, error))
        recent = self.past[-12:]
        calls = [c for c, _, _ in recent]
        errors = [e for _, _, e in self.past[-3:]]
        found = (
            recent.count((call, result, error)) >= 4
            or (len(errors) == 3 and errors[0] != "" and len(set(errors)) == 1)
            or (len(calls) == 12 and len(set(calls)) == 2 and all(calls[i] != calls[i + 1] for i in range(11)))
        )
        if not found:
            return ""
        if self.warned:
            raise Stopped("the agent repeated itself after a warning")
        self.warned = True
        self.past.clear()
        return "You are repeating yourself. Try another way, call ask, or call finish."


class Task:
    def __init__(
        self, repo: Path, contract: Contract, image: str, model: Model, human: Callable[[str], Answer], log: Log, *,
        base: str = "HEAD", limits: Limits | None = None, show: Callable[[str], None] = lambda text: None,
        secrets: tuple[str, ...] = (), cancel: threading.Event | None = None,
        next_request: Callable[[Outcome], str | None] | None = None, say: Callable[[str], None] = lambda text: None,
        branch: str = "", continue_from: str = "", open_question: Callable[[str], Answer] | None = None,
    ) -> None:
        self.open_question = open_question
        self.repo, self.contract, self.image, self.model, self.log = repo, contract, image, model, log
        self.base = box.commit_id(repo, base)
        self.limits, self.show, self.secrets = limits or Limits(), show, secrets
        self.cancel = cancel or threading.Event()
        self.id = uuid4().hex[:12]
        self.branch = branch or f"agent/{self.id}"
        # A chat: after each verdict, ask for the next request; None ends it.
        self.next_request, self.say, self.pending = next_request, say, ""
        # After a stop in a chat: begin from our own last commit on the branch, not from the base.
        self.continue_from = continue_from
        self.checked_from = self.base  # a follow-up is checked against the last verdict, not against the base
        self._human, self.waited = human, 0.0
        self.calls = self.tool_calls = self.tokens = self.malformed = self.empty = self.tries = self.logged = 0
        self.given: set[str] = set()  # the replies that the model really gave
        self.refused: list[str] = []
        self.repeats = Repeats()
        self.commit = ""
        self.checker: Checker | None = None

    def record(self, kind: str, **body: Any) -> None:
        self.logged += len(json.dumps(body, default=str))
        if self.logged > self.limits.log_bytes:
            raise Stopped("the task wrote too much to the log")
        self.log.add(self.id, kind, **body)

    def human(self, question: str) -> Answer:
        return self._waiting(self._human, question)

    def _waiting(self, ask: Callable[[str], Answer], question: str) -> Answer:
        self.show("NEEDS YOU")
        start = time.monotonic()
        try:
            return ask(question)
        finally:
            self.waited += time.monotonic() - start

    def check_time(self) -> None:
        if self.cancel.is_set():
            raise KeyboardInterrupt
        if time.monotonic() - self.started - self.waited > self.limits.minutes * 60:
            raise Stopped(f"the limit of {self.limits.minutes:g} active minutes is reached")

    def check_model_limits(self) -> None:
        self.check_time()
        if self.calls >= self.limits.model_calls:
            raise Stopped(f"the limit of {self.limits.model_calls} model calls is reached")
        if self.tokens >= self.limits.tokens:
            raise Stopped(f"the limit of {self.limits.tokens} tokens is reached")

    # ---- the whole task

    def run(self) -> Outcome:
        self.started = time.monotonic()
        self.record("task.started", contract=self.contract._asdict(), base=self.base, model=self.model.name)
        try:
            outcome = self._work()
        except Stopped as e:
            outcome = Outcome("STOPPED", str(e))
        except (ModelError, box.BoxError, gitsafe.Refused) as e:
            outcome = Outcome("BLOCKED", str(e))
        except KeyboardInterrupt:
            self.log.add(self.id, "task.cancelled")
            raise
        except Exception as e:  # nothing a model or project sends may end without an outcome
            outcome = Outcome("BLOCKED", f"internal error ({type(e).__name__})")
        outcome = outcome._replace(reason=mask(outcome.reason, self.secrets))
        if self.checker and outcome.receipt is None and self.commit:
            outcome = outcome._replace(branch=self.branch, commit=self.commit)
        end = self.log.add(self.id, "task.ended", state=outcome.state, reason=outcome.reason, commit=outcome.commit)
        if outcome.receipt is not None:
            outcome.receipt["log_end"] = list(end)
        return outcome

    def _work(self) -> Outcome:
        entries, self.left_out = box.list_commit(self.repo, self.continue_from or self.base)
        with box.Box(self.image, cancel=self.cancel) as b:
            b.copy_entries(self.repo, entries)
            # What the human has seen begins at the developer's own commit, also when work goes on from ours.
            oids = {e.name: e.oid for e in box.list_commit(self.repo, self.base)[0]}

            def base_hash(path: str) -> str:
                data = box.git_bytes(self.repo, "cat-file", "blob", oids[path]) if path in oids else b""
                return hashlib.sha256(data).hexdigest() if path in oids else ""

            ask = None
            if self.open_question is not None:
                open_question = self.open_question
                ask = lambda q: self._waiting(open_question, q)  # noqa: E731
            tools = Toolbox(b, self.contract, self.human, self.record, self.secrets, base_hash, ask)
            try:
                with box.Session(b, WORKER, write=False) as worker:
                    return self._talk(b, entries, tools, worker)
            finally:
                tools.close()

    def _talk(self, b: box.Box, entries: list[box.Entry], tools: Toolbox, worker: box.Session) -> Outcome:
        c = self.contract
        task = (
            f"Request: {c.what}\nFiles you may change: {', '.join(c.where)}\nTest command: {' '.join(c.command)}\n"
            f"Must pass at the end: {', '.join(c.proof) or 'not agreed'}"
        )
        answer: dict[str, Any] = {"system": SYSTEM, "task": mask(task, self.secrets)}
        while True:
            want = worker.ask(answer)
            kind = want.get("want")
            if kind == "model" and self.pending:
                answer, self.pending = {"user": mask(self.pending, self.secrets)}, ""
            elif kind == "tool" and self.pending:
                # Calls that were in the same reply as finish: the model has not seen the new request yet.
                answer = {"status": "SKIPPED", "text": "the human has a new request; it comes next"}
            elif kind == "model":
                answer = {"message": self._model(want.get("messages"))}
            elif kind == "tool":
                answer = self._tool(tools, want)
                if tools.finished:
                    tools.finished = False
                    outcome, feedback = self._check(b, entries, tools)
                    if outcome and self.next_request:
                        self.record("request.ended", state=outcome.state, commit=outcome.commit)
                        more = self.next_request(outcome)
                        if more is None:
                            return outcome
                        self._again(more)
                        answer = {"status": "OK", "text": f"The independent check said: {outcome.state}."}
                    elif outcome:
                        return outcome
                    else:
                        answer = {"status": "FAILED", "text": feedback}
            elif kind == "malformed":
                self.malformed += 1
                if self.malformed >= self.limits.malformed:
                    raise Stopped("the model sent tool calls that cannot be read")
                answer = {"status": "ERROR", "text": "the arguments are not a JSON object"}
            elif kind == "nothing":
                answer = {"text": self._nothing(str(want.get("text")))}
            else:
                raise Stopped("the agent asked for something that does not exist")

    def _again(self, request: str) -> None:
        """A follow-up in the chat: the same conversation and branch, fresh limits, and the requests so far."""
        self.pending = request
        self.checked_from = self.commit or self.checked_from
        self.contract = self.contract._replace(what=f"{self.contract.what}; then: {request}"[-2000:])
        self.calls = self.tool_calls = self.tokens = self.malformed = self.empty = self.tries = 0
        self.started, self.waited = time.monotonic(), 0.0
        self.repeats = Repeats()

    def _model(self, messages: object) -> Message:
        if not isinstance(messages, list) or len(json.dumps(messages)) > MAX_REQUEST:
            raise Stopped("the conversation is too long, or not a conversation")
        safe = [self._safe(m) for m in messages]
        for _ in range(self.limits.cut + 1):
            self.check_model_limits()
            self.calls += 1
            self.show("thinking")
            self.record("model.asked", number=self.calls, model=self.model.name, messages=len(safe))
            reply: Reply = self.model.call(safe, tool_list())
            message = check_reply(reply.message)
            self.tokens += max(0, reply.tokens)
            self.record("model.replied", number=self.calls, tokens=reply.tokens, cut=reply.cut, message=message)
            if not reply.cut:
                self.given.add(json.dumps(message, sort_keys=True))
                if isinstance(message.get("content"), str) and message["content"].strip():
                    self.say(mask(message["content"], self.secrets))
                return message
        raise ModelError("the model's reply was cut off each time, so nothing in it was run")

    def _safe(self, m: object) -> Message:
        """What goes out is masked. The model's own messages go back unchanged, because a provider may refuse them
        otherwise, so each must be one that it really gave: the agent's program could put a secret in."""
        role = m.get("role") if isinstance(m, dict) else None
        if not isinstance(m, dict) or role not in ("system", "user", "assistant", "tool"):
            raise Stopped("the conversation is not well formed")
        if role == "assistant":
            if json.dumps(m, sort_keys=True) not in self.given:
                raise Stopped("the conversation holds a reply that the model did not give")
            return m
        clean: Message = {"role": role, "content": mask(str(m.get("content") or ""), self.secrets)}
        if role == "tool":
            clean["tool_call_id"] = mask(str(m.get("tool_call_id"))[:200], self.secrets)
        return clean

    def _nothing(self, text: str) -> str:
        """A reply without a tool call: one reminder, then the human decides."""
        self.check_time()
        self.empty += 1
        if self.empty > 10:
            raise Stopped("the agent stopped too often without finishing")
        if self.empty == 1:
            return "Please continue by calling a tool, or call finish."
        words = rows(mask(text, self.secrets)[:1000], "It says (its words): ", screen_width())
        answer = self.human("\n".join(["", "The agent stopped without finishing.", *words]))
        if answer.yes or answer.reason:
            return answer.reason or "Continue."
        raise Stopped("the agent stopped and the human did not continue")

    def _tool(self, tools: Toolbox, want: dict[str, Any]) -> dict[str, Any]:
        self.check_time()
        self.tool_calls += 1
        if self.tool_calls > self.limits.tool_calls:
            raise Stopped(f"the limit of {self.limits.tool_calls} tool calls is reached")
        tool: Any = want.get("tool")  # the policy checks the types
        name = tool if isinstance(tool, str) and tool in TOOLS else "an unknown tool"
        args: Any = want.get("args")
        action = Action(tool, args)
        self.show(name + (f" {short(args)}" if name in TOOLS else ""))
        r: Result = tools.call(action, want.get("reason", ""))
        self.malformed = 0
        if r.status in ("DENIED", "REJECTED"):
            self.refused.append(f"{name}: {r.text[:100]}")
        if name == "finish":
            summary = args.get("summary") if isinstance(args, dict) else ""
            if isinstance(summary, str) and summary.strip():
                self.say(mask(summary, self.secrets))
            return {"status": r.status, "text": r.text}
        call = hashlib.sha256(repr(action).encode("utf-8", "replace")).hexdigest()
        result = hashlib.sha256(f"{r.status}\0{r.text}".encode("utf-8", "replace")).hexdigest()
        note = self.repeats.add(call, result, r.text if r.status in ("ERROR", "DENIED", "REJECTED") else "")
        answer: dict[str, Any] = {"status": r.status, "text": r.text}
        if r.code is not None:
            answer["exit_code"] = r.code
        if r.truncated:
            answer["truncated"] = True
        return answer | ({"notice": note} if note else {})

    # ---- checking

    def _check(self, b: box.Box, entries: list[box.Entry], tools: Toolbox) -> tuple[Outcome | None, str]:
        """Take the files, make a commit, and let the checker decide. Returns an outcome, or text for another try."""
        self.tries += 1
        self.show("checking")
        try:
            found = gitsafe.read_box(b, entries, tools.edited)
            # Always from the base: a second try may also have taken something back.
            self.commit = gitsafe.commit(
                self.repo, self.base, self.branch, found.files,
                "agent: " + mask(self.contract.what, self.secrets)[:72],
                parent=self.continue_from, secrets=self.secrets, replace=self.commit,
            )
        except gitsafe.Refused as e:
            self.record("result.refused", reason=str(e))
            if self.tries >= self.limits.tries:
                raise Stopped(f"the result cannot be taken: {e}") from e
            return None, f"the result cannot be taken: {e}"
        self.record("result.committed", branch=self.branch, commit=self.commit, not_taken=found.not_taken)
        approved = self._approved(tools, found.files)
        c = self.checker = Checker(
            self.repo, self.checked_from, self.commit, self.contract._replace(approved=approved), self.image,
            self.cancel,
        )
        verdict = c.check()
        self.record("check.ended", verdict=verdict, steps=[s._asdict() for s in c.steps])
        if verdict == "FAILED" and self.tries < self.limits.tries:
            # Enough to mend a real mistake; nothing about canaries.
            said = [f"{s.name}: {s.text}" for s in c.steps if s.state == FAIL]
            return None, "the independent check failed. " + "; ".join(said)[:3000]
        r = receipt.build(c, refusals=tuple(self.refused))
        r["tries"] = self.tries
        return Outcome(verdict, "", self.branch, self.commit, r), ""

    def _approved(self, tools: Toolbox, files: Sequence[gitsafe.File]) -> dict[str, str]:
        """Files whose whole change, from the base on, the human has seen and allowed."""
        ids = {e.name: e.oid for e in gitsafe._tree(self.repo, self.commit)}
        seen = {}
        for f in files:
            now = hashlib.sha256(f.data).hexdigest() if f.mode else ""
            if tools.approved.get(f.path) == now:
                seen[f.path] = ids.get(f.path, "")
        return seen
