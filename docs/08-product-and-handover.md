# 08 · Product and handover

## 1. User guide and capability matrix

### 1.1 End-to-end examples

**A. See it work without a key (about 1 minute)**
```bash
agent doctor
agent demo                 # recorded task + 4 branches: VERIFIED, FAILED, FAILED, UNVERIFIABLE
agent demo --keep          # also leaves a read-only container to look into with docker exec
```

**B. A chat on your own repository**
```bash
agent key set gemini       # once, macOS Keychain
cd your-repo               # a Git repo with at least one commit
agent                      # read the start screen, answer y
> fix the add function
> /diff
> also add a subtract function, with a test      # follow-up, same branch
> /quit
git merge --no-ff agent/<id>                      # only you merge
agent --continue                                  # another day: same conversation and branch
```

**C. One request in a script-like way**
```bash
agent run "fix the add function" --proof test_calc.T.test_add --files calc.py
echo $?                    # 0 VERIFIED, 1 FAILED, 2 anything else
```

**D. Check a branch someone else made (no key, no model)**
```bash
agent verify feature/x --proof tests.test_calc.T.test_add
agent verify feature/x --command "python -m pytest --junitxml={report}" --proof tests/test_calc.py::test_add
agent verify feature/x --original-tests          # ignore the branch's test changes
```

**E. Let Claude Code / Cursor / Copilot check their own work (MCP)**
```bash
claude mcp add --scope user coding-agent -- env PYTHONPATH=$HOME/coding-agent python3 -m agent mcp
# then: "use coding-agent to verify branch NAME"
```

**F. Other languages**
```bash
docker pull node:22-alpine          # the agent never pulls by itself
agent verify my-branch              # package.json → node --test with JUnit XML → TEST-LEVEL
```

**G. See history**
```bash
agent dashboard            # Grafana :3000, Datasette :8001, on 127.0.0.1 only; Ctrl+C removes them
```

### 1.2 Capability matrix

| Capability | Status | Notes |
|---|---|---|
| Terminal chat with follow-ups and memory | ✅ | `agent` |
| Resume an earlier chat | ✅ | `agent --continue`; only checked work is resumed |
| One-shot request | ✅ | `agent run` |
| Independent check of any branch | ✅ | `agent verify`; no model, no key |
| MCP server (verify, receipt) | ✅ | original tests only |
| Python unittest / pytest | ✅ TEST-LEVEL + canaries | |
| JavaScript (`node --test`) | ✅ TEST-LEVEL, no canaries | needs `node:22-alpine` |
| Go (`go test -json`) | ✅ TEST-LEVEL, no canaries | needs `golang:1.23-alpine` |
| Any tool that writes JUnit XML to `{report}` | ✅ TEST-LEVEL | |
| Rust, Java | ⚠️ COMMAND-LEVEL | `cargo test --offline`, `mvn -o test` |
| Project Dockerfile / `requirements.txt` images | ✅ after approval | wheels only for requirements |
| Gemini (free) | ✅ tested | |
| Groq, OpenRouter, GitHub Models, OpenAI, Mistral, custom | ⚠️ configured, not tested | run `agent model-test` first |
| macOS (Apple silicon) | ✅ tested | Keychain |
| Linux | ✅ CI | key typed each session |
| Windows (native) | ❌ | `termios` is used; WSL not tested |
| Web UI | ❌ | not built |
| Services in tests (databases, network) | ❌ | tests run without network |
| Push, merge, deploy | ❌ never | by design |

### 1.3 Operational boundaries and manual review

The human **must** review:
1. The start screen (image, test command, model company), once per chat.
2. Every command the agent wants to run, every change to tests, dependencies, CI or hooks, and every change outside `--files`.
3. A project Dockerfile or `requirements.txt` list, before it is built.
4. Changed tests in `agent verify`, before they are used.
5. **The diff before merging.** VERIFIED means "the agreed checks passed here", not "the code is right" (`receipt.NOT_PROVEN`). The merge screen lists links, new executables and files that can run something later.

---

## 2. Known issues and prioritized roadmap

### 2.1 Known issues and technical debt (from the code, README "Limits" and CASE-STUDY)

| # | Issue | Source | Impact |
|---|---|---|---|
| K1 | Hard-coded answers and always-equal objects get VERIFIED | README Limits; `test_known_limit_two_cheats_pass` | false VERIFIED possible |
| K2 | Canaries only for Python | `checker.by_tests` (`NAMED`) | weaker report-forgery detection for JS/Go |
| K3 | Containers share the host kernel | `box.py` docstring | an escape is not contained |
| K4 | Only Gemini tested; 3 of 8 live tasks on the dev machine were BLOCKED by the service | `services.KNOWN`; `events.db` | reliability depends on free-tier capacity |
| K5 | PID-only owner check for leftovers | `box.py:158` | rare stuck leftovers |
| ~~K6~~ | ✅ fixed: Gemini model names now live only in `provider.py`; `services.KNOWN` uses them | | |
| ~~K7~~ | ✅ fixed: `doctor` uses `key.SERVICE`; the unused `KEY_ACCOUNT` is gone | | |
| ~~K8~~ | ✅ fixed: indexes, daily backups, rotation and archive retention (`Log.upkeep`) | | |
| ~~K9~~ | ✅ fixed: MIT `LICENSE` | | |
| K10 | The checker runs sequentially (6–8 containers) | `checker.py` | ~26 s average per check |
| K11 | The test suite takes 25–40 min | CI commit message, README | slow feedback |
| K12 | Linux: key not stored | `key.store` | typed every session |
| K13 | `receipt.environment.same_as_ci` is always NOT CHECKED | `receipt.build` | a verdict is local only |
| ~~K14~~ | ✅ fixed: input and output tokens recorded (`prompt`, `completion`) | | |
| K15 | The chat never reuses the remembered setup (`same_as_before=False`) | `chat.Chat.run` | one more screen than `agent run` |
| ~~K16~~ | ✅ fixed: at most 20 MCP checks an hour | | |
| ~~K17~~ | ✅ fixed: the dashboard was open to anyone on this computer; now it needs a sign-in | | |
| ~~K18~~ | ✅ fixed: a second Ctrl+C could stop the dashboard's cleanup and leave its containers running | `dashboard.remove` | |
| ~~K19~~ | ✅ fixed: no coverage or speed numbers; now CI reports both (`tests/bench.py`) | | |

### 2.2 Roadmap (prioritized)

| P | Item | Why | Rough size |
|---|---|---|---|
| P0 | User study with 5 developers (CASE-STUDY: "No user study") | product evidence | 1 week |
| P1 | Held-back examples (property or extra hidden tests) | closes K1, the biggest trust gap | 1–2 weeks |
| P1 | Canaries for JUnit/Go (inject a failing test file per language) | closes K2 | days |
| P1 | gVisor / Docker Sandboxes runtime option | closes most of K3 | days, plus testing |
| P1 | Live-test more providers | K4 | days |
| P2 | Run checker repeats in parallel (3 volumes at once) | K10: ~2× faster checks | days |
| P2 | `agent forget` (delete receipts and archives on request) | privacy | hours |
| P2 | Dollar budget in `Limits` | paid models | hours |
| P2 | A coverage threshold in CI, once the first number is known | quality | minutes |
| P2 | Linux secret store (libsecret / `secret-tool`) | K12 | days |
| P3 | Windows via WSL tested; then native | reach | weeks |
| P3 | Per-file counter-check | sharper proof | days |
| P3 | Service containers for tests (DB) | more real projects | weeks |

---

## 3. Knowledge transfer and ownership

### 3.1 Walkthrough recording outlines (each under 10 minutes)

**Recording 1: Debugging a real failed run (8 min)**
| Time | Show |
|---|---|
| 0:00 | The user's report: `Result FAILED` or `BLOCKED` text |
| 0:45 | `agent doctor`: rule out Docker, disk and key |
| 1:30 | Find the task: the `task.ended` query from [07-operations-and-runbooks.md](07-operations-and-runbooks.md) |
| 2:30 | Walk its events: `task.started` contract → `tool.asked`/`tool.done` → `result.committed` → `check.ended.steps` |
| 4:30 | Open `receipt.txt`; explain each step state and `not_proven` |
| 5:30 | Reproduce without a model: `agent verify agent/<id> --proof …` |
| 6:30 | Find the code for the failing step: `checker.py` `by_tests`/`proof`/`regressions` |
| 7:30 | Decide: user issue, project issue, or product bug; write a test in `tests/test_checker.py` |

**Recording 2: Adding a feature, for example a new agent tool or CLI command (9 min)**
| Time | Show |
|---|---|
| 0:00 | Where commands live: `__main__.USAGE` + `commands` table |
| 1:00 | For a tool: add it to `policy.TOOLS` (exact arg types), `model.tool_list` description, a rule in `Policy._ask`/`_allow` |
| 3:00 | Implement it in `tools.Toolbox._do`; if it touches files, extend the `FILES` helper (never follow links) |
| 4:30 | Record events through `self.record`; mask everything |
| 5:30 | Tests: `test_policy` for rules, `test_path` end-to-end with `FakeModel`, `test_path.Recording.test_every_tool_is_described` |
| 7:00 | Re-record the demo (`python3 -m agent.demo`) if the tool list changed |
| 8:00 | README: document `agent <command>`; `test_docs` will fail otherwise |

**Recording 3: Running tests and "deploying" (7 min)**
| Time | Show |
|---|---|
| 0:00 | `./check` stages; the dev image built without network |
| 1:30 | `tests/run.py`: parallel jobs, exact counts, timing file |
| 3:00 | Run one test file; read a failure |
| 4:00 | The pre-commit hook and its remembered pass; why staged ≠ working is refused |
| 5:00 | Push → GitHub Actions `check.yml`; where to read logs |
| 6:00 | Users update with `git pull` in their checkout; rollback is `git checkout <commit>` + `agent demo` |

### 3.2 Ownership and escalation matrix (fill in)

| Area | Code | Primary owner | Backup | Escalate when | Contact / channel |
|---|---|---|---|---|---|
| Checker and verdicts | `checker.py`, `junit.py`, `receipt.py` | [FILL IN] | [FILL IN] | any false VERIFIED | [FILL IN] |
| Isolation and containers | `box.py`, `image.py` | [FILL IN] | [FILL IN] | an escape, a leftover that won't clear, a Docker upgrade break | [FILL IN] |
| Policy and approvals | `policy.py`, `approve.py`, `tools.py` | [FILL IN] | [FILL IN] | an approval shows the wrong thing; a denied action ran | [FILL IN] |
| Git safety | `gitsafe.py`, `verify.py` | [FILL IN] | [FILL IN] | a user's files or branch changed | [FILL IN] |
| Secrets and privacy | `key.py`, `mask.py`, `scan.py`, `provider.py` | [FILL IN] | [FILL IN] | a key or part of one seen anywhere | [FILL IN] |
| Model providers | `provider.py`, `services.py` | [FILL IN] | [FILL IN] | the default model renamed or retired; quota changes | [FILL IN] |
| Loop and chat | `loop.py`, `worker.py`, `chat.py`, `screen.py` | [FILL IN] | [FILL IN] | a task that never ends | [FILL IN] |
| Event log and dashboard | `log.py`, `dashboard.py` | [FILL IN] | [FILL IN] | a chain shows CHANGED; lost events | [FILL IN] |
| MCP | `mcp.py` | [FILL IN] | [FILL IN] | protocol version changes | [FILL IN] |
| CI and `./check` | `check`, `tests/run.py`, `.github/workflows/check.yml` | [FILL IN] | [FILL IN] | CI red on `main` | [FILL IN] |

**Severity guide:** S1 = a false VERIFIED, a user file changed, or a key exposed (stop shipping, fix first). S2 = a
task that does not end, or leftovers after cleanup. S3 = a provider unavailable, or a slow check.
