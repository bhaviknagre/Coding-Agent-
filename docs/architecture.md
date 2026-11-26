# Architecture

## Parts
| Part | File | Trust |
|---|---|---|
| Launcher: commands, chat, start screen | `agent/__main__.py`, `chat.py`, `screen.py` | trusted, on your computer |
| Rules: deny, ask, allow | `agent/policy.py` | trusted |
| Approvals drawn from the real action | `agent/approve.py` | trusted |
| Tools, and the file helper program | `agent/tools.py` | trusted program, untrusted files |
| The loop: limits, model calls, checks | `agent/loop.py` | trusted |
| The agent's conversation | `agent/worker.py` | **untrusted**: runs in its own container |
| Model services | `agent/model.py`, `provider.py` | replies are untrusted data |
| Containers, copies, limits, cleanup | `agent/box.py` | trusted; project code inside is not |
| Commits from plain files | `agent/gitsafe.py` | trusted; file contents are not |
| Independent checker and verdict | `agent/checker.py`, `receipt.py` | trusted; no model is asked |
| Records | `agent/log.py` (SQLite, hash chain), `mask.py`, `scan.py` | trusted |
| Key | `agent/key.py` (macOS Keychain) | trusted |
| Dashboard: Grafana and Datasette on 127.0.0.1 | `agent/dashboard.py` | reads a copy of the log only |

## One request, step by step
1. You type a request. The launcher shows the start screen once per chat: image, test command, files, proof, model.
2. The launcher copies the tracked files of your last commit into a Docker volume. Secret-looking files are left out.
3. The agent container (no network, no key, files read-only) keeps the conversation and asks the launcher for a model
   reply or a tool, one line at a time.
4. The launcher checks each tool call: deny, then ask you, then allow. Edits go through the file helper, which never
   follows a link. Each command runs in a new container without network.
5. When the agent calls `finish`, the launcher reads the changed files, refuses unsafe names, links and secrets, and
   makes one commit on `agent/…` itself, with Git's low-level commands: no checkout, no hooks.
6. The checker copies the base and the result into fresh volumes, puts the original tests back, runs everything three
   times with canaries, and the counter-check. A fixed table in code gives the verdict.
7. Follow-ups continue the same conversation and branch, and are checked against the last verdict.
8. Every step is written by the launcher to a hash-chained SQLite log; the receipt holds its end hash.
