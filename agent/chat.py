"""agent: a chat in the terminal, like Claude Code. Type a request; the agent works on its own copy, you see each step,
it asks you before anything risky, and an independent check gives a verdict. Follow-ups remember the conversation and
continue on the same branch. Your folder is never changed: you merge when you want.
"""
import argparse
import shlex
import sys
from collections.abc import Sequence

from agent import approve, box, gitsafe, screen
from agent.approve import Answer, ask_human, ask_open
from agent.checker import Contract, test_tool
from agent.log import Log
from agent.loop import Outcome, Task
from agent.verify import Stop, data_folder, merge, repository, verify, visible

HELP = """  Type a request, for example: make prices below zero become zero
  /diff      show the change on the branch          /merge    how to merge it (only you merge)
  /verify    check the whole branch again, from scratch
  /discard   delete the branch and start again       /new      start a new branch and conversation
  /proof T   a test that must pass, for the next request; otherwise a new test you approve proves it
  /help      this text
  /quit      leave (Ctrl+D too). Ctrl+C stops the current step."""


class Chat:
    def __init__(self, a: argparse.Namespace) -> None:
        self.repo = repository()
        self.model, self.secrets = screen.choose_model(a.model)
        command = tuple(shlex.split(a.command)) if a.command else screen.choose_command(self.repo)
        self.contract = Contract("", tuple(a.files) or ("*",), command, tuple(a.proof))
        self.image = a.image
        box.image_id(self.image)  # an unknown image stops here
        self.branch, self.commit, self.quit = "", "", False
        self.typed_proof: tuple[str, ...] = ()  # a /proof typed at this prompt, for the next request only
        self.base_branch = box.git_bytes(self.repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()

    # ---- what the person sees

    def step(self, text: str) -> None:
        if not text.startswith("thinking"):
            print(f"  ⏺ {visible(text)}", flush=True)

    def words(self, text: str) -> None:
        for row in approve.rows(" ".join(text.split())[:600], "  The agent says: ", approve.screen_width()):
            print(row)

    def human(self, question: str) -> Answer:
        return ask_human(question)

    def result(self, o: Outcome) -> None:
        if o.branch:
            self.branch, self.commit = o.branch, o.commit
        level = f" ({o.receipt['level']})" if o.receipt else ""
        print(f"  ⏺ Independent check: {o.state}{level}")
        if o.reason:
            print(f"    {visible(o.reason)}")
        for s in (o.receipt or {}).get("steps", []):
            if s["state"] != "PASS":
                print(f"    {s['state']}: {visible(s['name'])}: {visible(s['text'])[:200]}")
        if self.branch:
            print(f"    branch {self.branch} · /diff  /merge  /discard")

    # ---- the prompt

    def ask(self) -> str | None:
        """The next request, or None to leave (or to start again, when self.quit is False)."""
        while True:
            try:
                line = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                self.quit = True
                return None
            if not line:
                continue
            if not line.startswith("/"):
                return line
            try:
                if self.command(line):
                    return None
            except (Stop, box.BoxError, gitsafe.Refused, ValueError) as e:  # a failing command keeps the chat going
                print(f"  Stopped: {visible(e)}")

    def command(self, line: str) -> bool:
        """A /command. True if the conversation should start again."""
        word, _, rest = line.partition(" ")
        if word == "/quit":
            self.quit = True
            return True
        if word == "/new":
            print("  A new branch and conversation start with your next request.")
            self.branch = self.commit = ""
            return True
        if word == "/help":
            print(HELP)
        elif word == "/proof":
            named = test_tool(self.contract.command)[0] != "command"
            proof = tuple(rest.split()) if named else tuple(shlex.split(rest))
            self.contract = self.contract._replace(proof=proof)
            self.typed_proof = proof
            print(f"  Proof for the next request: {visible(' '.join(proof)) or 'none'}")
        elif word not in ("/diff", "/merge", "/discard", "/verify"):
            print(f"  Unknown: {visible(word)}. Type /help.")
        elif not self.branch:
            print("  There is no branch yet. Type a request first.")
        elif word == "/diff":
            flags = ("--no-ext-diff", "--no-textconv", "--no-color", "--stat", "-p")
            change = box.git_bytes(self.repo, "diff", *flags, f"{self.base_branch}...{self.commit}")
            text = change.decode("utf-8", "replace")
            print("\n".join(visible(x.replace("\t", "    ")) for x in text.splitlines()[:400]))
        elif word == "/merge":
            merge([self.branch], self.repo)
        elif word == "/verify":
            proofs = [x for p in self.contract.proof for x in ("--proof", p)]
            verify([self.branch, "--command", shlex.join(self.contract.command), *proofs], self.repo)
        elif word == "/discard":
            if ask_human(f"Delete {self.branch}? Your files stay as they are.").yes:
                old = gitsafe.discard(self.repo, self.branch)
                print(f"  Deleted. To bring it back: git branch {self.branch} {old}")
                self.branch = self.commit = ""
                return True
        return False

    # ---- the whole chat

    def run(self) -> int:
        print(f"\n Coding agent · {visible(self.repo.name)} · {visible(self.model.name)} · /help for commands")
        print(screen.start_screen(self.contract._replace(what="(each request you type)"), self.image, self.model, False,
                                  approve.screen_width()))
        if not ask_human("Start the chat with this setup?").yes:
            print("Nothing was started.")
            return 2
        log = Log(data_folder() / "events.db", self.secrets)
        try:
            request = self.ask()
            while request is not None:
                request = self.task(log, request)
                if request is None and not self.quit:
                    request = self.ask()
        finally:
            log.close()
        print("  Bye. Nothing is left running." + (f" Your work: {self.branch}" if self.branch else ""))
        return 0

    def task(self, log: Log, request: str) -> str | None:
        """One conversation: the request and its follow-ups. Returns a request to begin again with, or None."""
        task: list[Task] = []

        def next_request(o: Outcome) -> str | None:
            self.result(o)
            more = self.ask()
            # A follow-up proves itself: a /proof typed now, or else a new test that the human approves.
            task[0].contract = task[0].contract._replace(proof=self.typed_proof)
            self.typed_proof = ()
            return more

        checked = self.commit
        t = Task(self.repo, self.contract._replace(what=request), self.image, self.model, self.human, log,
                 show=self.step, say=self.words, secrets=self.secrets, next_request=next_request,
                 branch=self.branch, continue_from=checked, open_question=ask_open)
        task.append(t)
        try:
            o = t.run()
        except KeyboardInterrupt:
            # A commit that was not checked must not stay on the branch.
            gitsafe.move_back(self.repo, t.branch, self.commit if self.branch == t.branch else "")
            kept = "Your next request goes on from the last checked work." if self.commit else "Nothing was kept."
            print(f"\n  Stopped. Its containers are removed. {kept}")
            return None
        if o.state in ("STOPPED", "BLOCKED"):
            self.result(o)
            print("  The conversation starts again with your next request; the branch stays.")
        return None


def main(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="agent", description="A chat with the coding agent, in this repository.")
    p.add_argument("--model", default="gemini", help="gemini (default), gemini:MODEL, recorded:FILE or script:FILE")
    p.add_argument("--files", action="append", default=[], help="files or folders/ it may change; default: all")
    p.add_argument("--proof", action="append", default=[], help="a test that must pass at the end")
    p.add_argument("--command", default="", help="the full test command (default: found from the project)")
    p.add_argument("--image", default=box.HELPER_IMAGE, help="a Docker image that is already on this computer")
    a = p.parse_args(argv)
    if not sys.stdin.isatty():
        raise Stop("The chat needs a terminal. Next: use 'agent run \"REQUEST\"' in scripts.")
    return Chat(a).run()
