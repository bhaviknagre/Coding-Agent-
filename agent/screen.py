"""agent run "REQUEST": one start screen, three lines while it works, and a result with the next steps."""
import argparse
import hashlib
import json
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from agent import approve, box, key
from agent.approve import Answer, ask_human
from agent.checker import Contract, test_tool
from agent.log import Log
from agent.loop import Outcome, Task
from agent.model import FakeModel, Model, Replay
from agent.policy import DEPENDENCIES
from agent.provider import GEMINI_MODEL, OpenAIFormat
from agent.verify import Stop, data_folder, repository, visible

SETUP = ("Dockerfile", "devcontainer.json")  # files that decide what the image is


class Screen:
    """Three lines: doing now, needs you, result. In a terminal the first line is redrawn in place; otherwise, or
    with --plain, every change is a line of its own. Text from the model or the project is made safe first."""

    def __init__(self, plain: bool = False, details: bool = False) -> None:
        self.live = sys.stdout.isatty() and not plain and not os.environ.get("NO_COLOR")
        self.details, self.last = details, ""

    def show(self, doing: str) -> None:
        text = visible(doing)[:60]
        if self.details or not self.live:
            if text != self.last:
                print(f"Doing now  {text}", flush=True)
        else:
            print(f"\r\x1b[2KDoing now  {text}", end="", flush=True)
        self.last = text

    def needs_you(self) -> None:
        if self.live and not self.details:
            print("\r\x1b[2K", end="")
        print("Needs you  the question below", flush=True)

    def result(self, outcome: Outcome, receipt_path: Path | None, base_branch: str) -> None:
        if self.live and not self.details:
            print("\r\x1b[2K", end="")
        print(f"Result     {outcome.state}" + (f"  ({outcome.receipt['level']})" if outcome.receipt else ""))
        if outcome.reason:
            print(f"Why        {visible(outcome.reason)}")
        if outcome.receipt:
            for s in outcome.receipt["steps"]:
                if s["state"] != "PASS":
                    print(f"           {s['state']}: {visible(s['name'])}: {visible(s['text'])[:200]}")
        if outcome.branch:
            b = shlex.quote(outcome.branch)
            print(f"Branch     {b}   (your files and your branch were not changed)")
            print(f"Look       git diff {shlex.quote(base_branch)}...{b}")
            print(f"Merge      agent merge {b}        Discard  agent discard {b}")
        if receipt_path:
            print(f"Receipt    {receipt_path}")


def fingerprint(repo: Path, image: str, command: Sequence[str], model: str) -> str:
    """Changes when the image, the test command, the model service, or a file that sets them up changes."""
    h = hashlib.sha256(json.dumps([image, list(command), model]).encode())
    for e in sorted(box.list_commit(repo, "HEAD")[0]):
        if e.name.split("/")[-1] in SETUP or e.name.startswith(".devcontainer/") or DEPENDENCIES.search(e.name):
            h.update(f"{e.name}\0{e.oid}\0".encode())
    return h.hexdigest()


def remembered(repo: Path) -> Path:
    return data_folder() / "repositories" / (hashlib.sha256(str(repo).encode()).hexdigest()[:16] + ".json")


def provider_notice(model: Model) -> str:
    if model.name in ("fake", "recorded"):
        return "none: a script or a recording answers; no code leaves this computer"
    return (f"{model.name}: your request and the code the agent reads are sent to Google. On the free tier Google may "
            "use it to improve its products, and people may read it: do not use it for private code")


def start_screen(contract: Contract, image: str, model: Model, same_as_before: bool, width: int) -> str:
    rows = [""]
    add = rows.extend
    add(approve.rows(contract.what, "Request   ", width))
    add(approve.rows(", ".join(contract.where), "Files     ", width))
    proof = ", ".join(contract.proof) or "none agreed: then the result cannot be VERIFIED"
    add(approve.rows(proof, "Proof     ", width))
    if same_as_before:
        add(approve.rows("image, test command and model service as last time", "Setup     ", width))
    else:
        add(approve.rows(f"{image}  (no network; your folder is not mounted)", "Image     ", width))
        add(approve.rows(" ".join(contract.command), "Tests     ", width))
        add(approve.rows(provider_notice(model), "Model     ", width))
    add(approve.rows("tests, settings, dependencies, other files and every command ask you first", "Asks      ", width))
    return "\n".join(rows)


def choose_command(repo: Path) -> tuple[str, ...]:
    names = {e.name for e in box.list_commit(repo, "HEAD")[0]}
    uses_pytest = bool({"pytest.ini", "conftest.py"} & names) or any(n.endswith("/conftest.py") for n in names)
    return ("python", "-m", "pytest") if uses_pytest else ("python", "-m", "unittest")


def choose_model(name: str) -> tuple[Model, tuple[str, ...]]:
    """The model, and the secrets to mask: a script or recording for tests and demos, or Gemini with your key."""
    kind, _, rest = name.partition(":")
    if kind == "script" and rest:
        return FakeModel(json.loads(Path(rest).read_text())), ()
    if kind == "recorded" and rest:
        return Replay(Path(rest)), ()
    if kind == "gemini":
        try:
            secret = key.load("gemini") or (key.ask("gemini") if sys.platform != "darwin" else "")
        except (key.KeyError_, EOFError, KeyboardInterrupt) as e:
            raise Stop(f"No key: {e}") from None
        if not secret:
            raise Stop("No Gemini key is stored. Next: agent key set gemini")
        return OpenAIFormat(secret, rest or GEMINI_MODEL), (secret,)
    raise Stop("Choose a model: --model gemini, or --model recorded:FILE. Next: agent key set gemini")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent run", description="Let the agent change this repository, then check it.")
    p.add_argument("request")
    p.add_argument("--proof", action="append", default=[], help="a test that must pass at the end; repeatable")
    p.add_argument("--files", action="append", default=[], help="files or folders/ it may change; default: all")
    p.add_argument("--command", default="", help="the full test command (default: found from the project)")
    p.add_argument("--image", default=box.HELPER_IMAGE, help="a Docker image that is already on this computer")
    p.add_argument("--model", default="gemini", help="gemini (default), gemini:MODEL, recorded:FILE or script:FILE")
    p.add_argument("--plain", action="store_true", help="no redrawn lines")
    p.add_argument("--details", action="store_true", help="a line for every step")
    return p


def run(argv: Sequence[str]) -> int:
    a = parser().parse_args(argv)
    repo = repository()
    if box.git_bytes(repo, "status", "--porcelain", "--untracked-files=no"):
        print("Note       uncommitted changes in your files are not included: the agent starts from your last commit")
    command = tuple(shlex.split(a.command)) if a.command else choose_command(repo)
    if test_tool(command)[0] == "command" and len(a.proof) > 1:
        raise Stop("For this command, give one --proof: a command that fails before and works after.")
    proof = tuple(a.proof) if test_tool(command)[0] != "command" else tuple(shlex.split(" ".join(a.proof)))
    contract = Contract(" ".join(a.request.split()), tuple(a.files) or ("*",), command, proof)
    model, secrets = choose_model(a.model)
    box.image_id(a.image)  # an unknown image stops here, before any question
    key = fingerprint(repo, a.image, command, model.name)
    memory = remembered(repo)
    try:
        same = json.loads(memory.read_text()).get("setup") == key
    except (OSError, ValueError, AttributeError):
        same = False
    screen = Screen(a.plain, a.details)
    print(start_screen(contract, a.image, model, same, approve.screen_width()))
    if not ask_human("Start?").yes:
        print("Nothing was started.")
        return 2
    memory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    memory.write_text(json.dumps({"setup": key}))

    def human(question: str) -> Answer:
        screen.needs_you()
        return ask_human(question)

    log = Log(data_folder() / "events.db", secrets)
    try:
        base_branch = box.git_bytes(repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
        outcome = Task(repo, contract, a.image, model, human, log, show=screen.show, secrets=secrets).run()
        path = None
        if outcome.receipt:
            from agent import receipt

            path = receipt.save(outcome.receipt, data_folder() / "receipts" / outcome.branch.split("/")[-1], secrets)
    finally:
        log.close()
    screen.result(outcome, path, base_branch)
    return {"VERIFIED": 0, "FAILED": 1}.get(outcome.state, 2)
