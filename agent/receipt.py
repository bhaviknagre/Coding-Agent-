"""The receipt: what was checked, what was found, and what is not proven."""
import json
import os
from pathlib import Path
from typing import Any

from agent import box
from agent.checker import Checker, verdict
from agent.log import End, _masked

NOT_PROVEN = (
    "Behaviour that no agreed test covers.",
    "Answers written only for the tests, or values that claim to equal everything (no held-back examples in R1).",
    "Cheats that change single tests from inside the test process; canaries only find changes that reach them too.",
    "Test helper files outside test folders that do not import unittest or pytest.",
    "Tests that fail only rarely.",
    "Escape from the container: containers share the computer's kernel.",
)
COMMAND_ONLY = "COMMAND-LEVEL: only exit codes were seen. The program can report success without doing its work."


def build(c: Checker, refusals: tuple[str, ...] = (), log_end: End | None = None) -> dict[str, Any]:
    server, arch = box.docker("version", "-f", "{{.Server.Version}} {{.Server.Arch}}").split()
    return {
        "receipt": 1,
        "verdict": verdict(c.steps),
        "level": c.level,
        "base": c.base,
        "result": c.result,
        "contract": {**c.contract._asdict(), "where": list(c.contract.where)},
        "steps": [s._asdict() for s in c.steps],
        "tests": c.counts,
        "changes": [x._asdict() for x in c.changes],
        "original_kept": c.restored,
        "left_out": c.left_out,
        "refusals": list(refusals),
        "environment": {
            "image": c.image, "docker": server, "cpu": arch, "isolation": "hardened container, shared kernel",
            "same_as_ci": "NOT CHECKED",
        },
        "not_proven": [*([COMMAND_ONLY] if c.level == "COMMAND-LEVEL" else []), *NOT_PROVEN],
        "log_end": list(log_end) if log_end else None,
    }


def visible(text: object) -> str:
    """Show control and invisible characters as codes, so that a file name cannot draw on the screen."""
    return "".join(ch if ch.isprintable() else f"<U+{ord(ch):04X}>" for ch in str(text))


def as_text(r: dict[str, Any]) -> str:
    lines = [f"{r['verdict']}  ({r['level']})", "", f"Request   {visible(r['contract']['what'])}",
             f"Command   {visible(' '.join(r['contract']['command']))}",
             f"Proof     {visible(' '.join(r['contract']['proof']) or 'none')}",
             f"Files     {visible(' '.join(r['contract']['where']))}",
             f"Compared  {r['base'][:12]} -> {r['result'][:12]}", "", "Checks"]
    lines += [f"  {s['state']:12}{s['name']:15}{visible(s['text'])}" for s in r["steps"]]
    lines += ["", "Changed files", *(f"  {visible(x['path'])}" for x in r["changes"])]
    for title, key in (("Original version was used for", "original_kept"), ("Not copied", "left_out"),
                       ("Refused", "refusals"), ("Not proven by this receipt", "not_proven")):
        if r[key]:
            lines += ["", title, *(f"  {visible(x)}" for x in r[key])]
    e = r["environment"]
    lines += ["", f"Run in {e['image'][:19]} on Docker {e['docker']} ({e['cpu']}); {e['isolation']}"]
    return "\n".join(lines) + "\n"


def save(r: dict[str, Any], folder: Path, secrets: tuple[str, ...] = ()) -> Path:
    """Write receipt.json and receipt.txt, readable by the owner only, with secrets masked."""
    clean = _masked(r, secrets)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, text in (("receipt.json", json.dumps(clean, indent=1, sort_keys=True)), ("receipt.txt", as_text(clean))):
        with open(os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(text)
    return folder / "receipt.json"
