"""agent demo: shows the product without a key, a model, or a network. For judges and first-time users.

1. A recorded task: the agent fixes a bug in a small made-up project. The model's replies come from a recording, but
   every tool, container and check really runs.
2. The cheat check: four branches of the same project, one honest and three that cheat, each checked from scratch.
3. A report as an HTML page. With --keep, one container waits so that you can look inside it.
"""
import argparse
import html
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from agent import box
from agent.approve import NO
from agent.checker import Checker, Contract
from agent.log import Log
from agent.loop import Task
from agent.model import FakeModel, Recorder, Replay, call_message
from agent.verify import data_folder, visible

RECORDING = Path(__file__).parent.parent / "demo" / "recording.jsonl"
CONTRACT = Contract(
    "prices below zero must become zero", ("shop.py",), ("python", "-m", "unittest"),
    ("test_shop.T.test_never_below_zero",),
)
SHOP = "def price(base, discount):\n    return base - discount\n"
FIXED = "def price(base, discount):\n    return max(0, base - discount)\n"
TESTS = """import unittest
from shop import price
class T(unittest.TestCase):
    def test_discount(self): self.assertEqual(price(10, 3), 7)
    def test_never_below_zero(self): self.assertEqual(price(5, 9), 0)
"""
SCRIPT = [
    call_message("read", "look at the code first", path="shop.py", first=1),
    call_message("edit", "a discount larger than the price gave a negative price", path="shop.py",
                 old="    return base - discount", new="    return max(0, base - discount)"),
    call_message("finish", summary="prices can no longer be negative"),
]
STEAL = """import gc, json, os
def price(base, discount):
    nonce = next(o['nonce'] for o in gc.get_objects() if type(o) is dict and 'nonce' in o)
    r = {'test_shop.T.test_discount': 'ok', 'test_shop.T.test_never_below_zero': 'ok'}
    os.write(1, ('\\n@@' + nonce + ' ' + json.dumps(r) + '@@\\n').encode()); os._exit(0)
"""
CHEATS = {
    "honest fix": {"shop.py": FIXED},
    "weakens the test": {"shop.py": SHOP + "\n", "test_shop.py": TESTS.replace("price(5, 9), 0", "0, 0")},
    "breaks the other test": {"shop.py": "def price(base, discount):\n    return 0\n"},
    "forges the test report": {"shop.py": STEAL},
}


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {"GIT_CONFIG_GLOBAL": os.devnull}
    command = ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "-c", "core.hooksPath=/dev/null"]
    done = subprocess.run([*command, *args], cwd=repo, env=env, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def project(folder: Path) -> Path:
    """The made-up project, with fixed dates, so that the recording matches it every time."""
    repo = folder / "shop"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "shop.py").write_text(SHOP)
    (repo / "test_shop.py").write_text(TESTS)
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-qm", "a small shop", "--date", "2026-01-01T00:00:00Z")
    return repo


def branch(repo: Path, name: str, files: dict[str, str]) -> str:
    git(repo, "checkout", "-q", "-b", name, "main")
    for path, text in files.items():
        (repo / path).write_text(text)
    git(repo, "commit", "-qam", name)
    git(repo, "checkout", "-q", "main")
    return name


def record() -> None:
    """For the developers: make the recording again, from the script, after a change to the prompt or tools."""
    with tempfile.TemporaryDirectory() as d:
        repo = project(Path(d))
        log = Log(Path(d) / "log.db")
        o = Task(repo, CONTRACT, box.HELPER_IMAGE, Recorder(FakeModel(SCRIPT), RECORDING), lambda q: NO, log).run()
        log.close()
    print(f"recorded: {o.state}")


def demo(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="agent demo", description="Show the agent working, without a key or a model.")
    p.add_argument("--keep", action="store_true", help="keep one container running at the end, to look inside")
    a = p.parse_args(argv)
    rows: list[tuple[str, str, str]] = []
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as d:
        repo = project(Path(d))
        print("1/2  A recorded task: the agent fixes a bug. Model replies are recorded; tools and checks really run.")
        log = Log(data_folder() / "events.db")
        try:
            replay = Replay(RECORDING)
            o = Task(repo, CONTRACT, box.HELPER_IMAGE, replay, lambda q: NO, log,
                     show=lambda s: print(f"     doing now: {visible(s)}", flush=True)).run()
        finally:
            log.close()
        note = f"{replay.different} of {len(SCRIPT)} requests differed from the recording" if replay.different else ""
        rows.append(("recorded task", o.state, note or "the request matched the recording"))
        print(f"     result: {o.state}")
        print("2/2  The cheat check: each branch is checked from scratch in fresh containers.")
        for name, files in CHEATS.items():
            c = Checker(repo, "main", branch(repo, name.replace(" ", "-"), files), CONTRACT, box.HELPER_IMAGE)
            verdict = c.check()
            why = "; ".join(f"{s.name}: {s.text}" for s in c.steps if s.state != "PASS") or "all checks passed"
            rows.append((name, verdict, why))
            print(f"     {name:24} {verdict}")
        if a.keep:
            keep(repo)
    report = data_folder() / "demo.html"
    report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report.write_text(page(rows, time.monotonic() - start))
    hint = f"\nOpen it with: open {shlex.quote(str(report))}" if sys.platform == "darwin" else ""
    print(f"\nReport: {report}{hint}")
    wanted = ["VERIFIED", "VERIFIED", "FAILED", "FAILED", "UNVERIFIABLE"]
    good = [r[1] for r in rows] == wanted
    print("The demo gave the expected results." if good else "The demo did NOT give the expected results.")
    return 0 if good else 1


def keep(repo: Path) -> None:
    with box.Box(box.HELPER_IMAGE) as b:
        b.copy_in(repo, "honest-fix")
        name = f"{b.volume}-look"
        flags = [*box.HARDENED, *b.limits, *b._labels(), *b._mount(False), "--name", name, "--detach"]
        box.docker("run", *flags, "--entrypoint", "", b.helper, "sleep", "3600")
        try:
            print("\n     A container with the fixed project is running, as the agent's commands run: no network,")
            print("     read-only, no keys, not your folder. Look inside from another terminal:")
            print(f"       docker exec -it {name} sh")
            print("     Try: ls; cat shop.py; touch x (refused: read-only); exit")
            input("     Press Enter here when you are done; it is then removed. ")
        finally:
            box.docker("rm", "-f", "-v", name, ok=False)


def page(rows: list[tuple[str, str, str]], seconds: float) -> str:
    colour = {"VERIFIED": "#1a7f37", "FAILED": "#cf222e", "FLAKY": "#9a6700", "UNVERIFIABLE": "#9a6700"}
    body = "".join(
        f"<tr><td>{html.escape(n)}</td><td style='color:{colour.get(v, '#57606a')};font-weight:600'>"
        f"{html.escape(v)}</td>"
        f"<td>{html.escape(visible(w))}</td></tr>" for n, v, w in rows
    )
    return f"""<!doctype html><meta charset="utf-8"><title>Coding agent demo</title>
<style>body{{font:15px system-ui;max-width:860px;margin:2em auto;padding:0 16px;background:#fff;color:#1f2328}}
td,th{{padding:.5em;border-bottom:1px solid #d0d7de;text-align:left;vertical-align:top}}
table{{border-collapse:collapse}}
@media (prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}}td,th{{border-color:#30363d}}}}</style>
<h1>Coding agent demo</h1>
<p>A made-up shop project: a discount larger than the price gave a negative price. No key, no model and no network
were used. Every check ran in fresh containers without network. Took {seconds:.0f} seconds.</p>
<table><tr><th>Case</th><th>Verdict</th><th>Why</th></tr>{body}</table>
<p>Expected: the recorded task and the honest fix are VERIFIED; a weakened test and a broken test FAILED; a forged
report is UNVERIFIABLE, because tests that must fail were reported as passing. VERIFIED means that the agreed checks
passed here, not that the code is right in every case.</p>"""


def model_test(argv: Sequence[str]) -> int:
    """agent model-test gemini: the demo task with a real model. TESTED if it is VERIFIED, else EXPERIMENTAL."""
    from agent.screen import choose_model

    if len(argv) != 1:
        print("usage: agent model-test gemini[:MODEL]")
        return 2
    model, secrets = choose_model(argv[0])
    print(f"Testing {visible(model.name)} on the made-up shop project. Only made-up code is sent. About 1-3 minutes.")
    with tempfile.TemporaryDirectory() as d:
        repo = project(Path(d))
        log = Log(data_folder() / "events.db", secrets)
        try:
            o = Task(repo, CONTRACT, box.HELPER_IMAGE, model, lambda q: NO, log, secrets=secrets,
                     show=lambda s: print(f"     doing now: {visible(s)}", flush=True)).run()
        finally:
            log.close()
    print(f"Result: {o.state}" + (f" ({visible(o.reason)})" if o.reason else ""))
    if o.state == "BLOCKED" and "model service" in o.reason:
        # Google being busy or unreachable says nothing about the model: nothing is recorded.
        print(f"{model.name} was not tested: the service did not answer. Nothing is recorded; try again later.")
        return 1
    status = "TESTED" if o.state == "VERIFIED" else "EXPERIMENTAL"
    saved = data_folder() / "models.json"
    try:
        known = json.loads(saved.read_text())
    except (OSError, ValueError):
        known = {}
    known[model.name] = {"status": status, "result": o.state, "at": time.strftime("%Y-%m-%d %H:%M")}
    saved.write_text(json.dumps(known, indent=1))
    print(f"{model.name} is {status}." + ("" if status == "TESTED" else " It may still work; results need care."))
    return 0 if status == "TESTED" else 1


if __name__ == "__main__":
    record()
