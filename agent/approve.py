"""Ask the human. The launcher draws the question from the exact action; the agent only adds a marked reason."""
import json
import re
import shutil
import sys
import termios
import unicodedata
from collections.abc import Callable
from typing import NamedTuple

from agent.mask import mask
from agent.policy import Action, Decision

MAX_LINES = 400
# Printable to Python and not a mark, but drawn as nothing.
NOTHING = re.compile("[\u115f\u1160\u2800\u3164\uffa0\U0001d159]")


class Answer(NamedTuple):
    yes: bool
    reason: str = ""


NO = Answer(False)
Ask = Callable[[str], Answer]


def exact(text: str) -> str:
    """Text in which different content always looks different: no invisible, combining or right-to-left characters,
    and no second way to write a code."""
    out = []
    for ch in text.replace("<U+", "<U+003C>U+"):
        odd = (
            not ch.isprintable() or unicodedata.category(ch)[0] == "M"
            or unicodedata.bidirectional(ch) in ("R", "AL", "AN") or NOTHING.match(ch) or ch == "\u21e5"
        )
        out.append("\u21e5" if ch == "\t" else f"<U+{ord(ch):04X}>" if odd else ch)
    return "".join(out)


def rows(text: str, mark: str, width: int, words: bool = False) -> list[str]:
    """Content with our mark on every row of the screen. The terminal never wraps, so no row starts with content.
    words: break after a space where one is near, for plain sentences; nothing is left out either way."""
    room, out = width - len(mark) - 1, []

    def size(ch: str) -> int:
        return 1 if " " <= ch <= "~" or ch == "\u21e5" else 2

    for line in text.split("\n"):
        row, used = "", 0
        for ch in exact(line):
            if used + size(ch) > room:
                cut = row.rfind(" ") + 1 if words else 0
                if cut < len(row) // 2:
                    cut = len(row)
                out.append(mark + row[:cut])
                row = row[cut:]
                used = sum(size(c) for c in row)
            row, used = row + ch, used + size(ch)
        out.append(mark + row)
    return out


def screen_width() -> int:
    return max(40, min(shutil.get_terminal_size().columns, 120))


def draw(
    action: Action, decision: Decision, reason: str, shown_files: dict[str, str | None], secrets: tuple[str, ...] = (),
    width: int = 0,
) -> str | None:
    """The whole question, or None if it cannot be shown completely. `shown_files`: content, or None if unreadable."""
    a = action.args
    width = width or screen_width()
    bar = "=" * (width - 1)
    title = "THE AGENT ASKS TO " + {
        "edit": "CHANGE A FILE", "create": "MAKE A FILE", "delete": "DELETE A FILE", "run_command": "RUN A COMMAND",
    }.get(action.tool, action.tool.upper())
    facts = [title]
    if "path" in a:
        facts += rows(a["path"], "File      ", width)
    if action.tool == "run_command":
        # Each part in quotes, so it is clear where an argument ends. There is no shell.
        facts += rows("  ".join(json.dumps(x, ensure_ascii=False) for x in a["program"]), "Command   ", width)
        facts += rows("in a container: no network, no keys, not your folder. It can change the agent's copy, and run "
                      "any file of the project, also files that are not shown here.", "Runs      ", width, words=True)
    facts += rows(decision.rule, "Asks because: ", width)
    content: list[str] = []
    if action.tool == "edit":
        content += rows(a["old"], "  - ", width) + rows(a["new"], "  + ", width)
    elif action.tool == "create":
        content += rows(a["text"], "  + ", width)
    used: list[str] = []
    for name, text in shown_files.items():
        used += rows(name, "It uses   ", width)
        part = None if text is None or mask(text, secrets) != text else rows(text, "  | ", width)
        if part is None or len(part) + len(used) > MAX_LINES // 2:
            part = ["  NOT INSPECTABLE: not text, too long, or it seems to hold a secret"]
        used += part
    # The agent's words come after the facts and before the content, and cannot start a row.
    claim = rows(" ".join(reason.split())[:300] or "nothing", "The agent says (its claim): ", width, words=True)
    out = ["", bar, *facts, *claim, bar, *content, *used]
    if content or used:
        out += [bar, *facts]  # again, because the first part may have scrolled away
    out.append(bar)
    # An approval must show everything, and a secret must not be shown; if both cannot hold, there is no question.
    said = [reason, *(v for v in a.values() if isinstance(v, str)), *(a.get("program") or [])]
    if len(out) > MAX_LINES or any(mask(x, secrets) != x for x in said):
        return None
    return "\n".join(out)


def ask_open(question: str) -> Answer:
    """An open question from the agent: any text is the answer."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return NO
    print(question)
    print("Type your answer, then Enter (nothing = no answer): ", end="", flush=True)
    termios.tcdrain(sys.stdout.fileno())
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    answer = sys.stdin.readline()
    if not answer:
        return NO
    return Answer(bool(answer.strip()), reason=answer.strip()[:2000])


def ask_human(question: str) -> Answer:
    """Only a person at a terminal can answer. Keys typed before the question do not count; silence waits."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return NO
    print(question)
    print("Type y = yes, anything else = no (you may say why), then Enter: ", end="", flush=True)
    # First the whole question on the screen, then forget every key: none typed before it can answer it.
    termios.tcdrain(sys.stdout.fileno())
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    try:
        answer = sys.stdin.readline()
        if not answer:
            raise EOFError
        answer = answer.strip()
    except EOFError:
        return NO
    return Answer(True) if answer in ("y", "Y") else Answer(False, reason=answer[:500])
