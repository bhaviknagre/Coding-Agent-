# 03 · Permissions and state safety

## 1. Authentication and authorization

### 1.1 Who the actors are

There are **no user accounts, sessions, tokens, roles or MFA** in this product. It is a single-user local tool.
Authority comes from two facts that the code checks:

1. **Being the OS user** who runs `agent` (file modes 0600/0700, the Keychain item, ownership of the Git repo).
2. **Being a person at a terminal.** `approve.ask_human` and `verify.confirm` return *no* unless both stdin and
   stdout are TTYs. They flush any keys typed before the question (`termios.tcflush(..., TCIFLUSH)`), and only `y`
   or `Y` followed by Enter is a yes. Silence waits, EOF is no, and anything else is no with a reason.

| Actor | How it is identified | Trust |
|---|---|---|
| **Developer (TTY)** | process owner + a real terminal | the only authority that can approve |
| **Scripted caller (no TTY)** | `isatty()` is false | every ASK becomes NO; `agent` (chat) refuses to start |
| **Model / agent worker** | everything that comes over the worker pipe | untrusted; can only request |
| **Project code** | runs in command or checker containers | untrusted; its output is a claim |
| **MCP client** | JSON-RPC on stdin/stdout of `agent mcp` | its host prompts its user; the server limits what it can do |
| **Other local processes as the same user** | — | out of scope. They can read the Keychain item and the log (see [threat-model.md](threat-model.md)) |

### 1.2 Authorization matrix: agent tool calls

"Approval-required" means the human must answer `y` to a question the launcher draws from the exact action
(`tools.Toolbox._approve`). There is no MFA. The table follows `agent/policy.py` `Policy.decide`.

| Action (by the model) | Allowed | Approval-required | Denied | Enforcement |
|---|---|---|---|---|
| `list`, `search`, `read` a path inside the copy | ✅ | | path unsafe or secret-like | `_deny_path`; the helper never follows links (`O_NOFOLLOW`) |
| `edit` / `create` inside `--files` (default `*`) | ✅ | | | `_allow` |
| `edit` / `create` outside `--files` | | ✅ "outside the agreed files" | | `_ask`, `checker.allowed` |
| change a test or test setting (`PROTECTED`) | | ✅ | | `_ask`; the checker still restores it unless the exact content was approved |
| change dependencies, lock files, migrations, `*.sql` (`DEPENDENCIES`) | | ✅ | | `_ask` |
| change CI, hooks, Dockerfile, Makefile, `.claude/`, `.cursor/`, `AGENTS.md`, `CLAUDE.md`, `setup.py`, … (`RUNS_LATER`) | | ✅ | | `_ask` |
| `delete` a file the agent created in this task | ✅ | | | `Policy.created` |
| `delete` any other file | | ✅ | | `_ask` |
| `create` over a file the project had at the start | | (rule text gets "it REPLACES a file…") | | `_approve` |
| `run_command` (any program) | | ✅ **every time** | | no ALLOW rule → `ASK: no rule allows this` |
| `run_checks` (the confirmed test command) | ✅ | | | `_allow` |
| `ask` (open question to the human) | ✅ | | | answered with free text via `ask_open` |
| `finish` | ✅ | | | starts the checker |
| a path with `..`, `.git`, `\`, a non-printable character, > 1000 chars | | | ❌ | `box.unsafe_path`, `_deny_path` |
| a file named like a secret (`.env*`, `id_rsa*`, `*.pem`, `credentials.json`, `.npmrc`, `.kube/config`, …) | | | ❌ | `scan.BLOCKED_NAMES`, checked on both spellings |
| an unknown tool, missing or extra args, wrong types, text > 200 k, invalid UTF-8 | | | ❌ | `_deny` |
| push, merge, deploy, network, reading keys | | | ❌ (no such tool exists) | `policy.TOOLS` |
| a question that cannot be shown in full (> 400 lines, or contains a secret) | | | ❌ `REJECTED` | `approve.draw` returns `None` |
| an action or a used file that changed while the question waited | | | ❌ `REJECTED` | hash check after the answer |

A DENY cannot be approved away. It is decided before ASK (`policy.py` module docstring). The model sees the rule text
and must find another way.

### 1.3 Authorization matrix: commands run by the developer

| Command | Allowed without a question | Needs `y` at a TTY | Refused | Code |
|---|---|---|---|---|
| `agent` (chat) | | start screen | without a TTY (`Stop: The chat needs a terminal`) | `chat.main`, `Chat.run` |
| `agent run` | | start screen | without a TTY (ask → NO → "Nothing was started") | `screen.run` |
| `agent verify` | | "Run these checks?"; changed tests are shown in full first | branch has no change, or no common base; changed tests that are not text, hold a secret, or exceed 5000 lines | `verify.verify`, `show_protected` |
| `agent merge` | ✅ prints commands only | | | `verify.merge` |
| `agent discard` | | ✅ | branch not `agent/…`, a symbolic ref, a near-duplicate name, or checked out | `gitsafe.Git.check_branch` |
| `agent key set` | | key typed at a TTY (`getpass`) | not macOS (store), no TTY, wrong shape | `key.store`, `key.ask` |
| `agent key add` | ✅ | | not https, user/password in the URL, `?` or `#`, reserved or known name | `services.add`, `check_address` |
| Build a project image | | whole Dockerfile or package list shown, then `y` | file > 64 KB; a requirements line that is not a plain package | `image.choose` |
| `agent dashboard` | ✅ | | port outside 1024–65535 | `dashboard.main` |
| `agent mcp` `verify` | ✅ (the MCP host asks its user) | | arguments starting with `-` or containing spaces; > 50 proofs; multi-line command | `mcp.run_verify` |

MCP runs `verify(..., yes=True)`. This is allowed **only** because `--original-tests` is forced: no test content
that no person has read is ever used (`verify.py:148`).

### 1.4 Limits on other callers, and the remaining gap

| Gap | Why it matters | Template |
|---|---|---|
| No record of *which* person approved | Fine for single-user. Needed for teams or audit | Add `os.getlogin()` and `os.getuid()` to `human.answered` events |
| ✅ Built: at most 20 MCP checks an hour | A client in a loop could keep the computer busy (each check runs many containers) | `mcp.CHECKS_PER_HOUR`, a sliding window per server; the 21st call is answered with `isError: true` and the minutes to wait (`test_mcp.test_a_client_in_a_loop_is_stopped`) |
| ✅ Built: the dashboard needs a sign-in | Before, anyone using this computer, or a web page in its browser, could read the masked log | Grafana: no anonymous access, an admin password made for each start and passed through an owner-only env file that is deleted at once (never in a command line); another host name is redirected to 127.0.0.1. Datasette: `--root` and `{"allow": {"id": "root"}}`, signed in by a link that works once. Tested live: anonymous 401/403, a reused link 403 |

---

## 2. Data integrity, idempotency and rollback

### 2.1 Mechanisms

| Concern | Mechanism | Code | Tested by |
|---|---|---|---|
| Developer's files never change | Copy the commit into a volume; never mount; Git plumbing only; never checkout | `box.Box.copy_entries`, `gitsafe.commit` | `test_box.CopyIn.test_only_the_commit_is_copied_and_the_folder_is_untouched`, `test_verify.Commit.test_the_folder_index_and_branch_are_untouched_and_no_hook_runs` |
| Branch update is atomic and conditional | `git update-ref --no-deref <ref> <new> <expected old>` (compare-and-swap) | `gitsafe.commit:325`, `move_back`, `discard` | `test_verify.Commit.test_more_commits_and_unexpected_moves` |
| Git silently drops a name | Compare the written tree with the intended map | `gitsafe.commit:320` | `test_verify.Commit.test_refused` (refusals in general) |
| Partial file write in the copy | temp file `.coding-agent-<rand>` → `rename` (edit) or `link` (create, fails if it exists) | `tools.FILES` `put` | `test_policy.Tools.test_create_and_delete` |
| Lost update (the file changed since it was read) | `edit`/`delete` send the sha256 from the last `read`; the helper refuses a mismatch | `tools.FILES` `do`, `Toolbox.read_hash` | `test_policy.Tools.test_edit_needs_the_text_once_and_the_file_as_it_was_read` |
| Event log write lost on a kill | `BEGIN IMMEDIATE` → insert → `COMMIT`, `synchronous=full`, `fullfsync` | `log.Log.add` | `test_log.test_twenty_kills_lose_nothing` |
| Event changed or log cut off | hash chain + `log_end` in the receipt | `log.Log.verify`, `receipt.build` | `test_log.test_every_changed_field_is_found`, `test_removed_events` |
| Two writers | one transaction per event, `seq` derived inside it | `log.Log.add` | `test_two_programs_writing_at_the_same_time` |
| An approval reused for something else | Bound to `(action.hash(), hash of files it uses)` and re-checked after the answer | `tools.Toolbox._approve` | `test_policy.Tools.test_the_approved_action_cannot_be_changed` |
| Ctrl+C during a Docker call leaves an unknown container | Signals deferred inside `uninterrupted()`; cleanup cannot be interrupted | `box.uninterrupted`, `Box._remove` | `test_box.Cleanup.test_ctrl_c_right_after_a_docker_call_started` (CASE-STUDY: 120 random interruptions) |
| Crash or SIGKILL leaves containers | Labels carry the owner PID; the next start removes items of dead owners | `box.clean_leftovers` | `test_box.Popen.test_leftovers_of_a_killed_launcher_go_and_running_ones_stay` |
| Hostile image tries to get its container cleaned up as ours | Cleanup requires the name prefix (which images cannot set) **and** the label | `box.our_items` | `test_box.Popen.test_a_hostile_image_cannot_mark_the_developers_container_as_ours` |
| Unchecked commit left on a branch after Ctrl+C in the chat | `gitsafe.move_back(repo, branch, last checked commit or "")` | `chat.Chat.task` | `test_chat.test_ctrl_c_during_the_check_takes_the_unchecked_commit_off_the_branch` |
| A second try must not stack on a bad first try | The commit always starts from `parent or base` and replaces our earlier commit (`replace=`) | `loop.Task._check`, `gitsafe.commit` | `test_path.Path_.test_second_try_after_failed_and_taking_something_back`, `OneCommit.test_replace_needs_our_own_commit` |
| A repeated command after a resume | Only checked work is continued; tool results that were not kept come back as `UNKNOWN` | `loop.Task._save` | `test_chat.test_continue_goes_on_with_the_last_chat_on_its_branch` |

### 2.2 Idempotency
- **There are no idempotency keys.** None are needed because there is no remote write API. The one external call that costs something, the model call, is **not** idempotent and is retried on 429/5xx (`provider.TRIES = 4`). A retried POST can be billed twice on paid services. Each retry is a fresh completion, and the reply is used only once.
- **Image builds** are content-addressed: the tag is `coding-agent-v2/project:<sha256(tree)[:16]>`, so the same commit is never built twice (`image.tag`).
- **Setup confirmation** is remembered by fingerprint: image, command, model, and the Dockerfile, devcontainer and dependency file blob ids (`screen.fingerprint`). This applies to `agent run` only. The chat always shows the full start screen.
- **Cleanup** is idempotent: `docker rm -f`, `volume rm -f`, retried with pauses of 0, 0.5, 1 and 2 s (`Box._remove_all`).

### 2.3 Rollback and undo

| Situation | How to undo | Source |
|---|---|---|
| Do not want the agent's work | `/discard` or `agent discard BRANCH`. It prints `git branch BRANCH <old commit>` to bring it back | `gitsafe.discard` returns the old id |
| Merged and regretted | `git revert -m 1 <the merge commit>` (printed by `agent merge`) | `verify.merge` |
| A follow-up made things worse | The branch has one commit per checked request. Reset the branch yourself: `git branch -f agent/<id> <earlier commit>` | `gitsafe.commit` (parent chain) |
| A task stopped mid-way | Nothing to undo. The volume is deleted, and the branch either does not exist or still points at the last checked commit | `Box.__exit__`, `move_back` |
| A wrong key is stored | `agent key remove SERVICE` | `key.remove` |
| A bad service was added | Edit `<data>/services.json` and delete the entry. **[GAP]** there is no `agent key forget` command | `services.added` |

### 2.4 Soft deletes and backups
- **Nothing is soft-deleted.** Events are never edited. They leave the live log only by rotation, and then they are kept whole in an archive.
- **Backups are automatic** (`log.Log.upkeep`, run when a log is opened). Once a day, a consistent copy is made with SQLite's online backup to `<data>/backups/events-YYYY-MM-DD.db` (mode 0600), and the last 14 are kept. A failed backup never stops the work; the next start tries again.
- **Rotation.** A log over 200 MB (`ROTATE_BYTES`) is copied whole to `backups/events-until-<time>.db`. Its rows are then removed in one transaction, but only if nothing was added since the copy. The live log starts a new chain at `seq 1`. A receipt made earlier is checked against that archive (`test_log.test_a_large_log_is_archived_whole_and_starts_a_new_chain`).
- **Restore:** stop `agent`, then `cp <data>/backups/events-YYYY-MM-DD.db <data>/events.db`.

### 2.5 Known integrity limits
- `box._alive` uses the PID only. If the PID of a crashed launcher is reused by another program, its leftovers stay until that program ends (the `ponytail:` comment in `agent/box.py:158`). Fix: store the process start time in the label and compare.
- Someone who can rewrite both the log **and** every receipt can forge history (`agent/log.py` docstring).
- `receipt.environment.same_as_ci` is always `NOT CHECKED`. A VERIFIED result here is not a claim about CI.
