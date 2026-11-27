"""agent mcp: the independent check as an MCP server, for Claude Code, Cursor, Copilot and other coding agents.

It speaks JSON-RPC over standard input and output, one message a line. Its tools only check and read: nothing is
merged, the developer's files are not changed, and no key is needed. The MCP client asks its person before each
tool call; for that reason the check runs without a second question, and always with the original tests of the
base for any test that the branch changed, because no one here has looked at those changes.
"""
import contextlib
import io
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from agent import box, receipt
from agent.verify import Stop, data_folder, repository, verify

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MOST = 1_000_000  # a longer line is not read
NAME = re.compile(r"[^\s\-][^\s]{0,250}")  # a branch, test or base: no option, no space
RECEIPT = re.compile(r"[0-9a-f]{12}")

TOOLS = [
    {
        "name": "verify",
        "description": (
            "Independent check of a Git branch, without any AI: runs the tests from scratch in hardened Docker "
            "containers without network, three times, with canaries and a counter-check. Answers VERIFIED, FAILED, "
            "FLAKY or UNVERIFIABLE, with a receipt of what was and was not checked. Tests that the branch changed "
            "are replaced by the original ones. Takes about a minute. Nothing is merged or changed."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "the branch to check, for example agent/1a2b3c"},
                "proof": {"type": "array", "items": {"type": "string"},
                          "description": "tests that must fail before the change and pass after it, "
                                         "for example test_calc.T.test_add"},
                "command": {"type": "string", "description": "the test command; default: found from the project"},
                "base": {"type": "string", "description": "where the branch started; default: where it left HEAD"},
                "repository": {"type": "string", "description": "path of the Git repository; default: here"},
            },
            "required": ["branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "receipt",
        "description": "Read a saved receipt by its id: the 12 letters and digits after receipts/ in a result.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "pattern": "^[0-9a-f]{12}$"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


class Bad(Exception):
    """Wrong arguments: told to the client as a tool error."""


def _name(args: dict[str, Any], field: str, required: bool = False) -> str:
    value = args.get(field, "")
    if not isinstance(value, str) or (value and not NAME.fullmatch(value)) or (required and not value):
        raise Bad(f"{field}: a name without spaces that does not start with -")
    return value


def run_verify(args: dict[str, Any], here: Path) -> str:
    extra = set(args) - {"branch", "proof", "command", "base", "repository"}
    if extra:
        raise Bad(f"unknown arguments: {', '.join(sorted(extra))}")
    branch, base = _name(args, "branch", required=True), _name(args, "base")
    proof = args.get("proof", [])
    if not isinstance(proof, list) or len(proof) > 50 or not all(
            isinstance(p, str) and NAME.fullmatch(p) for p in proof):
        raise Bad("proof: a list of test names without spaces")
    command = args.get("command", "")
    if not isinstance(command, str) or len(command) > 500 or "\n" in command:
        raise Bad("command: one line, for example python -m unittest")
    where = args.get("repository", "")
    if not isinstance(where, str):
        raise Bad("repository: a path")
    repo = here
    if where:
        try:
            repo = Path(box.git_bytes(Path(where), "rev-parse", "--show-toplevel").decode().strip())
        except (box.BoxError, OSError):
            raise Bad("repository: not a Git repository") from None
    argv = [*([f"--command={command}"] if command.strip() else []), "--original-tests"]
    argv += [f"--proof={p}" for p in proof]
    argv += [f"--base={base}"] if base else []
    out = io.StringIO()
    with contextlib.redirect_stdout(out):  # standard output carries the protocol; verify's words are the answer
        try:
            verify([*argv, "--", branch], repo, yes=True)
        except SystemExit:
            raise Bad("the arguments were not understood") from None
        except (Stop, box.BoxError, ValueError, OSError) as e:
            raise Bad(str(e)) from None
    return out.getvalue().strip()


def read_receipt(args: dict[str, Any]) -> str:
    rid = args.get("id")
    if set(args) != {"id"} or not isinstance(rid, str) or not RECEIPT.fullmatch(rid):
        raise Bad("id: the 12 letters and digits after receipts/")
    path = data_folder() / "receipts" / rid / "receipt.json"
    try:
        return receipt.as_text(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError):
        raise Bad("no receipt with this id") from None


def handle(message: Any, here: Path) -> dict[str, Any] | None:
    """One request, one answer; a notification gets none."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(None, -32600, "not a JSON-RPC 2.0 request")
    method, mid, params = message["method"], message.get("id"), message.get("params") or {}
    if "id" not in message:
        return None  # notifications: initialized, cancelled
    if not isinstance(params, dict):
        return _error(mid, -32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(mid, {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "coding-agent-v2", "version": "1"},
            "instructions": "Use verify after changing code on a branch: it checks the work independently.",
        })
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(mid, -32602, "arguments must be an object")
        try:
            if name == "verify":
                text = run_verify(args, here)
            elif name == "receipt":
                text = read_receipt(args)
            else:
                return _error(mid, -32602, f"unknown tool: {str(name)[:50]}")
        except Bad as e:
            return _ok(mid, {"content": [{"type": "text", "text": f"Not checked: {e}"}], "isError": True})
        return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
    return _error(mid, -32601, f"unknown method: {method[:50]}")


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}


def serve(inp: IO[str], out: IO[str], here: Path) -> None:
    while line := inp.readline(MOST + 1):
        if len(line) > MOST:
            answer: dict[str, Any] | None = _error(None, -32600, "message too long")
            while line and not line.endswith("\n"):  # skip the rest of it
                line = inp.readline(MOST)
        elif not line.strip():
            continue
        else:
            try:
                answer = handle(json.loads(line), here)
            except (ValueError, RecursionError):
                answer = _error(None, -32700, "not JSON")
        if answer is not None:
            out.write(json.dumps(answer) + "\n")
            out.flush()


def main(argv: Sequence[str]) -> int:
    if argv:
        print("usage: agent mcp   (an MCP server on standard input and output; see the README)")
        return 2
    here = repository()
    box.clean_leftovers()
    serve(sys.stdin, sys.stdout, here)
    return 0
