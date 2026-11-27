# Coding Agent that proves its work

A coding agent for your terminal, like Claude Code, with one difference: **it never says "done" on its own word.**
Each change is checked from scratch by an independent checker that no AI is part of. You get a verdict:
**VERIFIED**, **FAILED**, **FLAKY** or **UNVERIFIABLE**, and a receipt that says what was checked and what was not.

- It works on a **copy** of your project inside Docker. **Your files are never changed.** You get a Git branch; you merge.
- While it works it has **no network, no key and no access to your computer**. It asks you before anything risky.
- It catches common cheats: weakened tests, deleted tests, forged test reports, a fake test library.
- `agent verify` checks **any** branch the same way, also one made by another AI agent or by a person.

Status: a working prototype, built in a 7-day hackathon. Tested on macOS (Apple silicon) and on GitHub's Linux.

**Steps:** [1 What you need](#1-what-you-need) · [2 Install](#2-install) · [3 Try it without a key](#3-try-it-without-a-key)
· [4 Connect an AI model](#4-connect-an-ai-model) · [5 Your first task](#5-your-first-task) · [6 Everyday use](#6-everyday-use)
· [7 Other languages and packages](#7-other-languages-and-packages) · [8 Use it from other agents (MCP)](#8-use-it-from-claude-code-cursor-or-copilot-mcp)
· [9 Dashboard](#9-see-what-it-did-dashboard) · [10 Verdicts](#10-what-the-verdicts-mean) · [11 Problems](#11-if-something-goes-wrong)
· [12 How it works](#12-how-it-works) · [13 Limits](#13-limits)

---

## 1. What you need

| Need | Check it with | You should see |
|---|---|---|
| macOS or Linux | | |
| Python 3.11 or newer | `python3 --version` | `Python 3.11` or higher |
| Git | `git --version` | `git version ...` |
| Docker Desktop (or any Docker engine), **running** | `docker info` | no error |
| 8 GB of free disk space | | |

A key for an AI model is needed only for step 4. Steps 3 and 8 work without one.

## 2. Install

**2.1** Get the code:
```
git clone https://github.com/bhaviknagre/Coding-Agent- ~/coding-agent
```

**2.2** Make the `agent` command (on Linux, use `~/.bashrc` instead of `~/.zshrc`):
```
echo "alias agent='PYTHONPATH=$HOME/coding-agent python3 -m agent'" >> ~/.zshrc
source ~/.zshrc
```

**2.3** Get the Python image for Docker (once, about 200 MB):
```
docker pull python:3.12-slim
```

**2.4** Check that everything is ready:
```
agent doctor
```
You should see OK for Python, Git, Docker, SQLite and free space. The key is "missing" until step 4; that is fine.

## 3. Try it without a key

```
agent demo
```
It takes about a minute. A made-up shop project: the agent fixes a bug (its replies come from a recording, but the
tools, containers and checks run for real). Then four branches are checked from scratch. You should see:

| Branch | Verdict |
|---|---|
| an honest fix | VERIFIED |
| a fix with a weakened test | FAILED |
| a fix with a broken test | FAILED |
| a fix with a forged test report | UNVERIFIABLE |

`agent demo --keep` leaves one container running, so you can look inside it.

## 4. Connect an AI model

The agent works with **Gemini** (free) and any other service in the OpenAI format.

**4.1** Get a free key: open [Google AI Studio](https://aistudio.google.com/apikey) and click **Create API key**.
Copy it. Do not paste it into a file or a chat.

**4.2** Store it:
```
agent key set gemini
```
Paste the key when asked. It is not shown on the screen and goes only into the macOS Keychain.
On Linux there is no Keychain: the key is typed at the start of each chat and is not saved.

**4.3** Try the model once on a made-up project:
```
agent model-test gemini
```
You should see **TESTED**. **EXPERIMENTAL** means the model did not finish the test task; try again later.

> On the free tier, Google may use what is sent to improve its products, and people may read it. Use it on code you
> are happy to share. The start screen says this before anything is sent.

**Other services** (each has its own key slot):
```
agent key list                     # gemini, groq, openrouter, github, openai, mistral, and yours
agent key set groq                 # then start the chat with: agent --model groq
agent key add mine --url https://HOST/v1/chat/completions --model MODEL --company "Who runs it"
```
Only Gemini is tested so far. Run `agent model-test SERVICE` before relying on another one.

## 5. Your first task

**5.1** Make a small practice project (or use one of yours: it must be a Git repository with at least one commit):
```
mkdir ~/agent-try && cd ~/agent-try && git init -q -b main
printf 'def add(a, b):\n    return a - b\n' > calc.py
printf 'import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n' > test_calc.py
git add . && git commit -qm start
```

**5.2** Start the chat in that folder:
```
agent
```
Read the start screen (image, test command, model, what it will ask you) and answer **y**.

**5.3** Type a request:
```
> fix the add function
  ⏺ read calc.py
  ⏺ edit calc.py
  The agent says: the function subtracted instead of adding
  ⏺ checking
  ⏺ Independent check: VERIFIED (TEST-LEVEL)
    branch agent/3f2a… · /diff  /merge  /discard
```

**5.4** Look at the change with `/diff`, leave with `/quit`. Your own files are still as they were.

**5.5** Take the work into your project when you want it:
```
git merge --no-ff agent/3f2a…
```

## 6. Everyday use

**In the chat**

| Type | What happens |
|---|---|
| a request | the agent works on its branch, then the independent check gives a verdict |
| a follow-up, e.g. `also add a subtract function, with a test` | goes on on the same branch, remembering the conversation |
| `/diff` | the change on the branch |
| `/verify` | check the whole branch again, from scratch |
| `/merge` | shows how to merge it; only you merge |
| `/discard` | deletes the branch (your files stay) |
| `/new` | a new branch and conversation |
| `/proof TEST` | the test that must pass for the next request |
| `/help` | all commands |
| `/quit` (or `q`, Ctrl+D) | leave; Ctrl+C stops the current step |

Each request must prove itself: name a test with `/proof TEST`, or let the agent write a new test that you approve.

**Go on another day**
```
agent --continue
```
It shows your earlier requests and goes on with the same conversation and branch, from the last checked work. A step
that was cut off is never run again, and approvals are asked again. If the branch was merged, deleted or changed, a
new chat starts.

**Without the chat**

| Command | Use |
|---|---|
| `agent run "fix the add function" --proof test_calc.T.test_add` | one request, then the verdict |
| `agent verify BRANCH --proof TEST` | check any branch; no model or key needed |
| `agent merge BRANCH` | shows how to merge |
| `agent discard BRANCH` | deletes an agent branch, after asking |
| `agent key status gemini` / `agent key remove gemini` | the key (never shown) |
| `agent --help` | all commands |

## 7. Other languages and packages

The image and the test command are found from the project. `--command "..."` and `--image NAME` change them.

| Project has | Image | Tests | Level |
|---|---|---|---|
| `.py` files | `python:3.12-slim` | `python -m unittest`, or `pytest` with a `conftest.py` | TEST-LEVEL, with canaries |
| `package.json` | `node:22-alpine` | `node --test`, with Node's own JUnit XML report | TEST-LEVEL |
| `go.mod` | `golang:1.23-alpine` | `go test -json ./...` | TEST-LEVEL |
| `Cargo.toml` | `rust:1-slim` | `cargo test --offline` | COMMAND-LEVEL |
| `pom.xml` | `maven:3-eclipse-temurin-21` | `mvn -o test` (packages need a Dockerfile) | COMMAND-LEVEL |
| anything | yours | a command that writes JUnit XML to `{report}` | TEST-LEVEL |

Example of the last row: `--command "python -m pytest --junitxml={report}"`.

The agent never downloads an image by itself. If one is missing it stops and prints the command, for example
`docker pull node:22-alpine`, with its size.

**Projects that need packages**
- With a `Dockerfile`: the agent shows you the whole file and asks before building it, once.
- Without one, but with a `requirements.txt` of plain package names: it shows the list and asks before installing
  them, as ready-made packages only (no package's own setup code runs).
- Or give your own image: `--image NAME`.

Tests always run **without network**, so everything a test needs must be in the image.

## 8. Use it from Claude Code, Cursor or Copilot (MCP)

`agent mcp` lets another coding agent check its own work with this checker. It only checks: nothing is merged, no key
is needed, and your MCP client asks you before each check.

**Claude Code** (once, for all your projects):
```
claude mcp add --scope user coding-agent -- env PYTHONPATH=$HOME/coding-agent python3 -m agent mcp
```
Then ask Claude Code: *"use coding-agent to verify branch NAME"*.

**Cursor**: put this in `.cursor/mcp.json` (VS Code with Copilot: `.vscode/mcp.json`, with `servers` in place of
`mcpServers`), with the full path of your copy:
```
{"mcpServers": {"coding-agent": {"command": "python3", "args": ["-m", "agent", "mcp"],
                                 "env": {"PYTHONPATH": "/full/path/to/coding-agent"}}}}
```
Tools: `verify` (branch, proof tests, optional command, base and repository) and `receipt` (reads a saved receipt).
Tests that the branch changed are always replaced by the original ones here, because no person has looked at those
changes. To use changed tests after you have read them, run `agent verify` in a terminal.

## 9. See what it did (dashboard)

```
agent dashboard
```
- **Grafana** at `http://127.0.0.1:3000`: tasks, verdicts, questions to you, tool calls, rule decisions, model calls,
  and whether the log is intact.
- **Datasette** at `http://127.0.0.1:8001/events`: every record, searchable.

The first start downloads about 1 GB. Only this computer can open them; the log itself is never changed.
Ctrl+C stops them and removes their containers.

## 10. What the verdicts mean

| Verdict | Meaning |
|---|---|
| VERIFIED | The agreed checks passed here: the proof test fails without the change and passes with it, nothing that worked broke, tests that must fail did fail (canaries), and with the change removed the proof fails again. It is **not** a promise that the code is right in every case. |
| FAILED | Something checked is wrong, for example a test that worked before fails now. |
| FLAKY | A test sometimes passes and sometimes fails. Never VERIFIED. |
| UNVERIFIABLE | Something needed is missing or unclear: no proof test, a report that cannot be trusted, files changed that were not approved. |

**TEST-LEVEL**: each test was seen by name. **COMMAND-LEVEL**: only the exit code of the test command was seen; this
proves less, and the receipt says so.

## 11. If something goes wrong

| You see | Do this |
|---|---|
| `agent: command not found` | `source ~/.zshrc`, or do step 2.2 again |
| `Cannot connect to the Docker daemon` | open Docker Desktop and wait until it says the engine is running |
| `This folder is not in a Git repository` | `cd` into your project; it needs `git init` and one commit |
| `the image ... is not on this computer` | run the `docker pull ...` command it prints |
| Gemini is busy or out of quota | wait a minute; the agent also tries a lighter Gemini model by itself |
| `UNVERIFIABLE: no proof test` | name a test with `--proof TEST` or `/proof TEST`, or approve the new test the agent writes |
| containers left after a crash | start any `agent` command again: leftovers are removed first |

Still stuck: `agent doctor` says what is missing and what to do.

## 12. How it works

```
you ─ terminal ─ launcher (on your computer: rules, approvals, key, records)
                    │  only through Docker, never your folder
                    ├─ agent container    (the conversation; no network, no key, files read-only)
                    ├─ helper container   (reads and edits files; never follows a link)
                    ├─ command containers (one per command; no network, limits on memory, time, disk)
                    └─ checker containers (fresh copies, the original tests, canaries)
         model service ── only the launcher talks to it, with the key in a header
```
More: [docs/architecture.md](docs/architecture.md), [docs/threat-model.md](docs/threat-model.md), and the story of
how it was built: [CASE-STUDY.md](CASE-STUDY.md).

## 13. Limits

- **Not built in this version:** a web page, held-back examples, native Windows. Model services other than Gemini
  are set up but not tested yet.
- Two known cheats are not caught: answers hard-coded for the tests, and values that claim to equal everything.
- Tests that must fail (canaries) exist only for Python; for other languages the receipt says NOT CHECKED.
- Containers share your computer's kernel: this limits harm, it is not complete protection.
- The key is in your macOS Keychain; another program running as you can read it there. It is never in a file, a log,
  a container, a command line or the screen.
- Free tier: the agent waits 12 seconds between model calls (5 a minute).
- The dashboard's containers have the normal Docker network (a web page needs a port).

## For developers

`./check` runs lint (ruff), types (mypy), a secret scan and every test, in about 25 minutes. It is also the commit
hook, and GitHub runs it on every push. Code: `agent/` (Python, standard library only on your computer), tests in
`tests/`.
