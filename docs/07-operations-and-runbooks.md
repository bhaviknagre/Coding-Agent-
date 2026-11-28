# 07 · Operations and runbooks

"Production" for this product means a developer's computer. There is no fleet, pager or deploy. The runbooks below
are written for the person supporting a user, or for a developer debugging their own run.

Paths: `<data>` = `~/Library/Application Support/coding-agent-v2` (macOS) or
`${XDG_DATA_HOME:-~/.local/share}/coding-agent-v2` (Linux).

## 1. Observability and debugging

### 1.1 Signals that exist

| Signal | Where | Emitted by |
|---|---|---|
| Structured event log (the main trace) | `<data>/events.db`, table `event` | `log.Log.add` (catalogue in [02-core-logic-and-data-models.md](02-core-logic-and-data-models.md)) |
| Receipts | `<data>/receipts/<id>/receipt.{json,txt}` | `receipt.save` (`agent run`: id = branch suffix; `agent verify`: run id) |
| Terminal lines | `Doing now / Needs you / Result` (`agent run`), `⏺ step` lines (chat), `Stopped: … Next: …` | `screen.Screen`, `chat.Chat.step`, `__main__.main` |
| Health check | `agent doctor` (Python, Git, repository, Docker, SQLite WAL bug, ≥ 8 GB free, key present) | `doctor.check_all` |
| Log integrity | `Log.verify()`; dashboard "Log hash chain" panel | `log.py`, `dashboard.snapshot` |
| Dashboards | Grafana `127.0.0.1:3000` (13 panels; user `admin`, password printed at start), Datasette (sign-in link printed at start, works once) | `agent dashboard` |
| Log backups | `<data>/backups/` (daily, 14 kept; archives of rotated logs) | `log.Log.upkeep` |
| Docker items | `docker ps -a --filter label=coding-agent-v2=task`, `docker volume ls --filter label=coding-agent-v2=task` | `box._labels` |

**Not present:** metrics endpoint, OpenTelemetry traces, log shipping, crash reporting. Container logs are disabled
on purpose (`--log-driver none`). Output is captured by the launcher instead. **[GAP / RECOMMENDED]** For a team
setting, export `events.db` rows as OTLP logs (one span per task, `task` as the trace id) from a separate reader,
never from the launcher's write path.

### 1.2 Inspecting a failed request

```bash
D="$HOME/Library/Application Support/coding-agent-v2"
# 1. the last tasks and how they ended
sqlite3 "file:$D/events.db?mode=ro" \
  "select task, datetime(at,'unixepoch'), json_extract(body,'$.state'), json_extract(body,'$.reason')
   from event where kind='task.ended' order by seq desc limit 10;"
# 2. everything one task did, in order
sqlite3 "file:$D/events.db?mode=ro" \
  "select seq, kind, substr(body,1,300) from event where task='<TASK>' order by seq;"
# 3. why the checker said what it said
sqlite3 "file:$D/events.db?mode=ro" \
  "select json_extract(body,'$.steps') from event where task='<TASK>' and kind='check.ended';"
# 4. the receipt as text
cat "$D/receipts/<id>/receipt.txt"
```

On Linux, `D="${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-v2"`. If `sqlite3` is not installed, use
`python3 -c "import sqlite3; ..."` or `agent dashboard` → Datasette.

**Reading an outcome:**

| State | Meaning | First place to look |
|---|---|---|
| `VERIFIED` / `FAILED` / `FLAKY` / `UNVERIFIABLE` | the checker ran | `check.ended.steps`, then the receipt's `not_proven` |
| `STOPPED` | a limit, a repetition, the human said no, or the result could not be taken 3 times | `task.ended.reason`; `result.refused` events |
| `BLOCKED` | model service, Docker or Git failed, or an internal error (`internal error (TypeName)`) | `task.ended.reason`; `agent doctor` |
| (no `task.ended`) | the process was killed | `task.cancelled`, or nothing; the next `agent` start removes leftovers |

**Reproducing locally without a key:** take the contract from `task.started.body.contract` and run
`agent verify agent/<id> --command "<command>" --proof <test>` on the same branch. No model is involved.

### 1.3 Inspecting a stuck run
1. `docker ps --filter label=coding-agent-v2=task --format '{{.Names}} {{.Status}} {{.Label "coding-agent-v2.owner"}}'`
2. The owner label is the launcher PID: `ps -p <pid>`.
3. Ctrl+C in the terminal stops it cleanly. If the terminal is gone, `kill <pid>` (SIGTERM is handled like Ctrl+C).

---

## 2. Incident runbooks

### (a) The model service is down, rate-limited or out of quota

**Symptoms:** `Note: gemini-3.8-flash cannot answer now (503); using gemini-3.5-flash-lite from now on`; then
`BLOCKED: the model service said 429: … Next: wait a minute, or try tomorrow`; `agent model-test` prints "was not
tested: the service did not answer".

| Step | Action |
|---|---|
| 1 | Check that it is the service and not the key: a 401/403 says "the key was refused" → `agent key set gemini` |
| 2 | 429 (quota): free-tier limits are per minute and per day. Wait, or choose another model with its own quota: `agent --model gemini:gemini-3.1-flash-lite` |
| 3 | 503 (busy): the fallback chain has already been tried. Retry in a few minutes |
| 4 | Network errors (`could not be reached: URLError`): check proxy or VPN. The launcher uses the system's urllib proxy settings (containers never get them) |
| 5 | Switch provider: `agent key set groq` then `agent --model groq`. **Run `agent model-test groq` first**: only Gemini is tested |
| 6 | Keep working with no model: `agent verify` and `agent demo` need none |

Work that was already checked is safe on its branch. `agent --continue` resumes from the last verdict.

### (b) Load spike / resource exhaustion (disk, memory, containers)

| Symptom | Cause | Recovery |
|---|---|---|
| `Only N GB free. Next: Free some space; at least 8 GB is needed.` | host disk below 8 GB | free space; `docker system df`; remove old project images: `docker images --filter label=coding-agent-v2=image` |
| Result `LOW_SPACE` | Docker's disk < 2 GB or < 200k inodes while running | `docker system prune` (it removes **all** unused Docker data, so check with the user first) |
| `TOO_MUCH_DISK` | the command wrote > 3 GB into the copy | the project's command is the problem; check its caches/outputs |
| `OUT_OF_MEMORY` | over min(3 GB, half of Docker's RAM), or exit 137 | raise Docker Desktop's memory; the limit scales with `MemTotal` |
| `TIMED_OUT` | > 600 s for one command, or the check's 1800 s used up | speed up the tests or narrow them with `--proof` |
| Many leftover containers | launchers killed with SIGKILL | run any `agent` command (`clean_leftovers` runs first) |
| Leftovers that do not go away | PID reused by another process (`box.py:158`) | find the items in the table below; remove them once the user confirms no agent is running |

```bash
docker ps -a --filter label=coding-agent-v2=task --format '{{.Names}}\t{{.Label "coding-agent-v2.owner"}}'
docker volume ls --filter label=coding-agent-v2=task -q
# only after confirming no agent process is running:
docker ps -aq --filter label=coding-agent-v2=task | xargs -r docker rm -f -v
docker volume ls -q --filter label=coding-agent-v2=task | xargs -r docker volume rm -f
```

There is no queue, so there is no backlog. Parallel `agent` processes share the CPU and the event log, and the log's
`BEGIN IMMEDIATE` serializes their writes.

### (c) The event log or other local state is degraded

| Symptom | Cause | Recovery |
|---|---|---|
| `Stopped: database is locked` | another process held the write lock > 10 s | close other `agent` processes; retry |
| `the log has version N; this program knows 1` | a newer program wrote the log | update this copy (`git pull`), or move the file aside |
| Dashboard shows `CHANGED at event N` | the log was edited, or the disk is corrupted | **do not repair it in place.** Copy the file aside as evidence; start a fresh log by renaming `events.db`. Receipts keep their `log_end` for comparison |
| `doctor` warns `SQLite … has the WAL-reset bug` | old SQLite | nothing to do: the plain journal mode is used automatically |
| `services.json` unreadable | edited by hand | it is ignored (`services.added` returns `{}`); fix the JSON |
| `--continue`: "is not where the last chat left it" | the branch was merged, deleted or moved | expected; a new chat starts |

Integrity check by hand:
```bash
python3 -c "from agent.log import Log; from pathlib import Path; import sys; \
l=Log(Path(sys.argv[1])); print(l.verify() or 'intact'); l.close()" "$D/events.db"
```
(Run with `PYTHONPATH` pointing at the repo. `Log()` opens for writing and sets pragmas, so use it only on a copy
when you are investigating tampering.)

### (d) Corrupted state or a failed "deployment" (upgrade) to roll back

**There is no deployment.** A user's install is a Git checkout run through an alias (README §2). To roll back:

```bash
cd ~/coding-agent
git log --oneline -5
git checkout <last good commit>        # or: git reset --hard <commit> on a local branch
agent doctor && agent demo             # demo must print: The demo gave the expected results.
```

Corrupted agent branch or repository state:
| Situation | Recovery |
|---|---|
| An agent branch points at an unexpected commit | `gitsafe.commit` refuses to move it (`update-ref` compare-and-swap). Inspect with `git log agent/<id>`; discard and redo |
| An unchecked commit after a crash | in the chat it is moved off automatically; after `agent run` with SIGKILL it can remain. `agent verify agent/<id>` decides whether it is usable |
| The developer merged a bad branch | `git revert -m 1 <merge commit>` |
| A project image is wrong (bad Dockerfile built) | `docker rmi coding-agent-v2/project:<tag>`; the tag is per tree, so a new commit builds again after approval |
| Keychain item is wrong | `agent key remove SERVICE && agent key set SERVICE` |

---

## 3. Release and migration playbook

### 3.1 Pre-flight checklist for any change

- [ ] `./check` passes locally (lint, types, secret scan, all tests, 0 containers left).
- [ ] The GitHub Actions `check` job is green on the pushed commit.
- [ ] `agent demo` gives the expected results; if the prompt or tools changed, the recording was re-made (`python3 -m agent.demo`).
- [ ] `agent doctor` is clean on a fresh shell.
- [ ] README still names only commands in `USAGE`, every chat command, and the limit sentences (`tests/test_docs.py`).
- [ ] No new runtime dependency (`pyproject.toml` `dependencies = []`).

### 3.2 Schema migration (event log or receipt)

- [ ] Bump `log.VERSION` / the receipt `"receipt"` field.
- [ ] Add a schema-only migration (indexes, new tables or columns). **Never rewrite `body`, `prev` or `hash`.**
- [ ] Old receipts must still verify: keep `test_log.test_an_older_receipt_stays_valid` green.
- [ ] Update `dashboard.VIEWS` and `test_dashboard.test_every_panel_query_runs`.
- [ ] Test on a copy of a real `events.db` (the dashboard snapshot code shows how to copy safely).

### 3.3 Major dependency or platform upgrade

| Upgrade | Extra checks |
|---|---|
| Python | `test_doctor.test_main_file_can_be_read_by_old_python`; mypy `python_version` |
| Docker engine | `test_box` in full (OOM signalling and removal timing differ between versions; see CASE-STUDY "GitHub's Linux found") |
| `python:3.12-slim` base | the dev `Dockerfile` digest; `HELPER_IMAGE`; the `RUNNER` must still run on its Python |
| Gemini model names | `provider.GEMINI_MODEL`/`GEMINI_BUSY` (the one place; `services.KNOWN` uses them), then `agent model-test gemini` |
| ruff / mypy | pins in `Dockerfile`; the dev image tag changes automatically |
| Grafana / Datasette | `dashboard.GRAFANA_BASE`, `DATASETTE`, plugin version; `test_dashboard` |

### 3.4 Feature flags and kill switches

There is **no feature-flag system**. The controls that act as switches are:

| Switch | Effect |
|---|---|
| Ctrl+C / SIGTERM / SIGHUP | stops the current step; containers and volumes are removed |
| `agent key remove SERVICE` | no model calls are possible for that service |
| `--model recorded:FILE` / `script:FILE` | no network model at all |
| `--files PATH` | narrows what can change without a question |
| `--image NAME` | no project image build |
| `--original-tests` (verify) | the branch's test changes are ignored |
| Remove the MCP entry from the client config | other agents can no longer call `verify` |
| `NO_COLOR=1` or `--plain` | no redrawn terminal lines |

**[GAP / RECOMMENDED]** A global "safe mode" environment variable read in `__main__.main`, for example
`CODING_AGENT_ONLY_VERIFY=1`, that refuses `""`, `run` and `model-test`. It would act as an organization-level kill
switch that still allows checking.
