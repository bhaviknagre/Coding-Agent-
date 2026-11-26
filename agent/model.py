"""Where replies come from: a script, a recording, or (later) a provider. Messages are in the OpenAI format, and the
provider's own message goes back to it unchanged, because some providers refuse it otherwise."""
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from agent.log import _masked
from agent.policy import TOOLS

Message = dict[str, Any]
MAX_REPLY = 1_000_000


class ModelError(Exception):
    """The model service gave no usable reply. The message is shown to the human."""


class Reply(NamedTuple):
    message: Message
    cut: bool = False  # the reply ended early, so nothing in it may run
    tokens: int = 0


class Model(Protocol):
    name: str

    def call(self, messages: Sequence[Message], tools: Sequence[Message]) -> Reply: ...


def tool_list() -> list[Message]:
    """The tools as the model sees them. Every call may carry a reason, shown to the human as the agent's claim."""
    kinds: dict[type, Message] = {str: {"type": "string"}, int: {"type": "integer"},
                                  list: {"type": "array", "items": {"type": "string"}}}
    about = {
        "list": "List files below a folder. Empty path: the whole project.",
        "search": "Find exact text in files below a folder.",
        "read": "Read a text file from line `first` (1 is the start). Read a file before changing it.",
        "edit": "Replace `old`, which must be there exactly once, with `new`.",
        "create": "Make a new text file.", "delete": "Delete a file.",
        "run_command": "Run one program with arguments, without a shell. The human is asked.",
        "run_checks": "Run the agreed test command.", "ask": "Ask the human a question.",
        "finish": "Say that the work is done. An independent check then decides.",
    }
    out = []
    for name, args in TOOLS.items():
        fields = {k: kinds[t] for k, t in args.items()} | {"reason": {"type": "string"}}
        schema = {"type": "object", "properties": fields, "required": list(args)}
        out.append({"type": "function", "function": {"name": name, "description": about[name], "parameters": schema}})
    return out


def _depth(value: object) -> int:
    level, here = 0, [value]
    while here and level <= 100:
        level += 1
        here = [y for x in here for y in (x.values() if isinstance(x, dict) else x if isinstance(x, list) else ())]
    return level


def check_reply(message: object) -> Message:
    """A reply is data from outside. Only its shape is checked here; what it asks for is up to the policy."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ModelError("the model service sent something that is not a reply")
    if _depth(message) > 30 or len(json.dumps(message)) > MAX_REPLY:
        raise ModelError("the model service sent a reply that is far too long or too deep")
    calls = message.get("tool_calls")
    readable = isinstance(message.get("content"), str | None) and isinstance(calls, list | None)
    if not readable or len(calls or []) > 20:
        raise ModelError("the model service sent a reply that cannot be understood")
    for c in calls or []:
        f = c.get("function") if isinstance(c, dict) else None
        parts = (c.get("id"), f.get("name"), f.get("arguments")) if isinstance(f, dict) else (None,)
        if not all(isinstance(x, str) for x in parts):
            raise ModelError("the model service sent a tool call that cannot be understood")
    return message


def call_message(name: str, reason: str = "", **args: Any) -> Message:
    """One tool call as a reply, for scripts and tests."""
    f = {"name": name, "arguments": json.dumps(args | ({"reason": reason} if reason else {}))}
    number = hashlib.sha256(json.dumps(f).encode()).hexdigest()[:8]
    return {"role": "assistant", "content": None, "tool_calls": [{"id": number, "type": "function", "function": f}]}


class FakeModel:
    """Says what it was told to say. Needs no key and no network."""

    name = "fake"

    def __init__(self, script: Sequence[Message | Callable[[Sequence[Message]], Message]]) -> None:
        self.script = list(script)
        self.seen: list[list[Message]] = []

    def call(self, messages: Sequence[Message], tools: Sequence[Message]) -> Reply:
        self.seen.append(json.loads(json.dumps(list(messages))))
        if not self.script:
            raise ModelError("the script is at its end")
        step = self.script.pop(0)
        return Reply(step(messages) if callable(step) else step, tokens=1)


def fingerprint(messages: Sequence[Message]) -> str:
    return hashlib.sha256(json.dumps(list(messages), sort_keys=True).encode()).hexdigest()


class Recorder:
    """Passes calls on and writes each reply to a file, with secrets masked."""

    def __init__(self, model: Model, path: Path, secrets: tuple[str, ...] = ()) -> None:
        self.model, self.path, self.secrets, self.name = model, path, secrets, model.name
        path.write_text("")

    def call(self, messages: Sequence[Message], tools: Sequence[Message]) -> Reply:
        r = self.model.call(messages, tools)
        reply = _masked(r.message, self.secrets)
        row = {"request": fingerprint(messages), "reply": reply, "cut": r.cut, "tokens": r.tokens}
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return r


class Replay:
    """Gives the recorded replies again: no key, no network, no model. It is not exact: `different` counts the requests
    that are not the same as when it was recorded."""

    name = "recorded"

    def __init__(self, path: Path) -> None:
        self.rows = [json.loads(x) for x in path.read_text().splitlines()]
        self.different = 0

    def call(self, messages: Sequence[Message], tools: Sequence[Message]) -> Reply:
        if not self.rows:
            raise ModelError("the recording is at its end")
        row = self.rows.pop(0)
        self.different += row["request"] != fingerprint(messages)
        return Reply(row["reply"], row["cut"], row["tokens"])
