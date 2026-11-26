# Coding agent that proves its work

A coding agent for your terminal, like Claude Code, with one difference that matters: **it never says "done" on its own word.**
Every change it makes is checked from scratch by an independent checker that no AI is part of, and you get a verdict:
**VERIFIED**, **FAILED**, **FLAKY** or **UNVERIFIABLE**, with a receipt that says what was checked and what was not.

- It works on a **copy** of your repository inside Docker. **Your files are never changed.** You get a Git branch; you merge.
- It has **no network, no keys and no access to your computer** while it works. It asks you before anything risky.
- It catches common cheats: changed tests, deleted tests, forged test reports, a fake test library (see the demo).
- `agent verify BRANCH` gives the same proof for **any** branch, also one made by another AI agent or a person.

Status: a working prototype (R1), built in a 7-day hackathon. Tested on macOS (Apple silicon) and on GitHub's Linux.
What is not built yet is listed at the end, plainly.

## What you need
- macOS or Linux, Python 3.11 or newer, Git, and Docker Desktop (or any Docker engine), running.
- About 1 GB of free disk space for the images, and 8 GB free in total.
- For the live AI model: a free **Google AI Studio** key for Gemini. The demo and `verify` need no key.

## Install
```
git clone https://github.com/bhaviknagre/Coding-Agent- ~/coding-agent
echo "alias agent='PYTHONPATH=~/coding-agent python3 -m agent'" >> ~/.zshrc && source ~/.zshrc
agent doctor
```
`agent doctor` checks Python, Git, Docker, SQLite, free space and the key, and says what to do about anything missing.
The first run downloads `python:3.12-slim` (about 200 MB) if Docker does not have it yet.

## Try it in one minute, without a key
```
agent demo
```
A made-up shop project. The agent fixes a bug (its replies come from a recording; the tools, containers and checks run for
real). Then four branches are checked from scratch: an honest fix is VERIFIED, a weakened test and a broken test FAILED,
and a forged test report is UNVERIFIABLE. `agent demo --keep` leaves one container open so you can look inside it.

## Use it with Gemini
```
agent key set gemini        # paste the key; it is not shown, and it goes only into the macOS Keychain
agent model-test gemini     # one task on a made-up project; marks the model TESTED or EXPERIMENTAL
cd ~/your-project
agent                       # the chat
```
On the free tier, Google may use what is sent to improve its products and people may read it, so use it on code you are
happy to share. The start screen says this before anything is sent.

### The chat
```
> make prices below zero become zero
  · thinking
  ⏺ read shop.py
  ⏺ edit shop.py
  The agent says: a discount larger than the price gave a negative price
  ⏺ checking
  ⏺ Independent check: VERIFIED (TEST-LEVEL)
    branch agent/3f2a… · /diff  /merge  /discard
> also round prices to two decimals, with a test
  ...
```
Follow-ups remember the conversation and continue on the same branch. Each one must prove itself: name a test with
`/proof TEST`, or let the agent write a new test that you approve; that test becomes the proof.

| Command | What it does |
|---|---|
| `/diff` | the change on the branch |
| `/verify` | check the whole branch again, from scratch |
| `/merge` | shows how to merge it; only you merge |
| `/discard` | deletes the branch (your files stay) |
| `/new` | a new branch and conversation |
| `/proof TEST` | the test that must pass, for the next request |
| `/help`, `/quit` | help, leave. Ctrl+C stops the current step |

### Other commands
| Command | |
|---|---|
| `agent run "REQUEST" --proof TEST` | one request without the chat |
| `agent verify BRANCH --proof TEST` | check any branch, no model or key needed |
| `agent merge BRANCH`, `agent discard BRANCH` | merge help, delete an agent branch |
| `agent key status|remove gemini` | the key (never shown) |
| `agent dashboard` | what the agent did, in your browser (below) |

### The dashboard
```
agent dashboard
```
Opens **Grafana** at `http://127.0.0.1:3000`: tasks, verdicts, questions to you, tool calls, rule decisions (allow, ask,
deny), model calls per hour, recent tasks and the raw log, and whether the log's hash chain is intact. **Datasette** at
`http://127.0.0.1:8001/events` shows every table, searchable. Both are free and open source and run in Docker; the first
start downloads about 1 GB. They read a copy of the log made every 5 seconds, so the log itself is never changed, and
only this computer can open them. Ctrl+C stops them and removes their containers.

Not used, on purpose: Prometheus (it collects from programs that run all the time; this one starts and stops),
Redis (one user, no queue) and LangChain or LangSmith (a framework, and a cloud service that would receive your code).

## What the verdicts mean
| Verdict | Meaning |
|---|---|
| VERIFIED | The agreed checks passed here: the proof test fails without the change and passes with it, nothing that worked broke, tests that must fail did fail (canaries), and with the change removed the proof fails again. It is **not** a promise that the code is right in every case. |
| FAILED | Something checked is wrong, for example a test that worked before fails now. |
| FLAKY | A test sometimes passes and sometimes fails. Never VERIFIED. |
| UNVERIFIABLE | Something needed is missing or unclear: no proof test, a report that cannot be trusted, files changed that were not approved. |

**TEST-LEVEL** means each test was seen by name (Python `unittest`, `pytest`). **COMMAND-LEVEL** means only the exit code of
a test command was seen, for other languages; it proves less, and the receipt says so.

## How it works
```
you ─ terminal ─ launcher (on your computer: rules, approvals, key, records)
                    │  only through Docker, never your folder
                    ├─ agent container   (the conversation; no network, no key, files read-only)
                    ├─ helper container  (reads and edits files; never follows a link)
                    ├─ command containers (one per command; no network, limits on memory, time, disk)
                    └─ checker containers (fresh copies, the original tests, canaries)
         model service (Gemini) ── only the launcher talks to it, with the key in a header
```
More: [docs/architecture.md](docs/architecture.md), [docs/threat-model.md](docs/threat-model.md),
and the story of how it was built: [CASE-STUDY.md](CASE-STUDY.md).

## Development
`./check` runs lint (ruff), types (mypy), a secret scan and every test, about 15–25 minutes; it is also the commit hook,
which reuses a pass for exactly the same files. GitHub runs it on every push.

## Known limits
- **Not built in this version:** MCP, a web page, resuming a stopped task, installing packages without a
  Dockerfile, memory between separate chats, held-back examples, other languages' test reports,
  native Windows, any model other than Gemini (the code reaches any OpenAI-format service, untested).
- The dashboard's containers have the normal Docker network (a page needs a port); Grafana's update checks and news
  are switched off.
- Projects that need extra packages: if the project has a `Dockerfile`, the agent shows it in full and asks before
  building it once (from the committed files; secret-looking files are left out). Or give an image: `--image NAME`.
- Two known cheats are not caught: answers hard-coded for the tests, and values that claim to equal everything.
- Containers share your computer's kernel: this limits harm, it is not complete protection.
- The key is in your macOS Keychain; another program running as you can read it there. It is never in a file, a log,
  a container, a command line or the screen.
- Free-tier limits: the agent waits 12 seconds between model calls (5 a minute) until the limits are known.
