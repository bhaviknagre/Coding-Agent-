# 09 · Interview and YC prep

Every claim below comes from this repo, its git history (28 commits, 24–27 Nov 2026) or the developer's local event
log (447 events). Numbers that are not in any of these are marked **[FILL IN REAL NUMBER]**. Missing features are
marked **[GAP]**.

**Real numbers you can quote** (from the event log on the developer's computer, 25–27 Nov 2026, read-only):

| Number | Value |
|---|---|
| `agent verify` runs | 57 (35 VERIFIED, 17 FAILED, 5 older without a saved verdict) |
| Average check time | 26 s (6–57 s) |
| Live Gemini tasks | 8 started: 2 VERIFIED, 1 FAILED, 3 BLOCKED by the service, 2 cancelled |
| Tokens per VERIFIED live task | ~9–10.5 k, 8–9 model calls, ~3 min (limiter-bound) |
| Tool-call decisions | 61 calls: 58 ALLOW, 3 ASK |
| Cheats in the demo | honest → VERIFIED; weakened test, broken test → FAILED; forged report → UNVERIFIABLE |
| Our own experiment | a plain exit-code check accepted **7 of 7** cheats |
| Tests | 292, run on every push (CI: ~40 min on GitHub's Linux) |
| Human decisions for an ordinary fix | 2 (start, merge), counted by a test |

---

## Part A · Pitch

### One-sentence YC hook (under 20 words)
> **A coding agent that can't mark its own homework: every change is re-tested from scratch before you see "done".**

Alternative: *"Your AI coding agent says 'fixed'. We prove whether it's true, for any branch, with no AI involved."*

### 2-minute technical walkthrough
> "You type a request in your terminal: 'fix the add function'. Before anything runs, one screen shows which Docker
> image, which test command, and which model company will see your code. You say yes once.
>
> We don't mount your folder. We copy the files of your last commit straight out of Git into a Docker volume. The
> agent itself runs in a container with no network, no API key and a read-only view. It can only *ask* our launcher,
> one JSON line at a time, for a model reply or a tool.
>
> Every tool call goes through fixed rules: deny, then ask, then allow. Reading and editing inside the agreed files
> just happens. Running any command, touching a test, a dependency, CI or hooks stops and asks you. The question is
> drawn by us from the exact action, so invisible characters can't fake it, and your yes is bound to a hash of
> exactly what you saw.
>
> When the agent calls `finish`, we read the changed files back as plain bytes, refuse secrets and escaping links,
> and make the commit ourselves with Git plumbing. No hooks, no checkout, your branch untouched.
>
> Then the part no one else does: an independent checker, with no model in it, copies base and result into fresh
> containers. It puts your *original* tests back, runs everything three times, and adds random 'canary' tests that
> must fail, which catches forged test reports. It proves the named test fails without the change and passes with it,
> then removes the change again to see the proof fail. A fixed table turns that into VERIFIED, FAILED, FLAKY or
> UNVERIFIABLE, with a receipt that also lists what it did *not* prove.
>
> You get a branch and a receipt. Only you merge. And `agent verify` gives the same proof for a branch made by
> Claude Code, Cursor, or a person, including over MCP."

---

## Part B · Deep-dive technical defense

### B1. Core architecture trade-offs
- **A local monolith plus throwaway containers** (`agent/loop.py` owns state; `agent/box.py` runs work). Why: the value is trust. One trusted process that performs every effect is easy to audit, and a hosted service would add auth, tenancy and a sandbox fleet before proving the idea. Isolation comes from `HARDENED` Docker flags, not from a service boundary.
- **Synchronous and pull-based.** The worker *pulls* the next step by writing `{"want": ...}` and the launcher answers. There is no async event bus. Why: cancel and cleanup must be exact (`box.uninterrupted` defers Ctrl+C during Docker calls). Concurrency would make "nothing left behind" hard to prove. Cost: checker runs are sequential (~26 s average).
- **The agent is in a container but has no power** (AM5). An early cross-check found the agent container could write files directly. Now it has `write=False`, no network and no key. `Task._safe` even refuses assistant messages the model never gave, so a compromised worker cannot inject a secret into the conversation.
- **No Git in containers** (AM10). Results come back as base64 JSON from a fixed `READER`. `gitsafe.commit` uses `commit-tree` and a compare-and-swap `update-ref`.
- **Deliberately not built:** web UI, held-back examples, Windows, crash-resume of a running step, service containers, OpenTelemetry, releases (README "Limits"). And **never** push, merge or deploy: `policy.TOOLS` has no such tool.

### B2. Failure modes and circuit breaking
- **The model fails:** `OpenAIFormat._call` makes 4 tries on 429/5xx with `max(Retry-After, 5·2ⁿ+jitter) ≤ 120 s`, then the Gemini fallback chain (`3.8-flash → 3.5-flash-lite → 3.1-flash-lite`), switched for the rest of the session. 401/403 fail fast with "Next: agent key set". A reply must arrive in 180 s and be ≤ 5 MB (slow-drip safe). A busy model is remembered for 10 minutes in `cooldown.json`, so the next run skips it.
- **Malformed or oversized data:** `check_reply` enforces depth ≤ 30, ≤ 1 MB and ≤ 20 tool calls. Policy enforces exact arg types and size caps. Helper answers have 30 s and 10 MB per line, and after one bad answer the session is closed, because later answers could no longer be matched to their questions.
- **Infinite loops:** `Limits` (100 model calls, 1000 tool calls, 2 M tokens, 45 active minutes, 50 MB log), `Repeats` (same call and result 4× in 12, same error 3×, A/B alternation over 12, one warning then stop), 3 malformed replies, 10 empty replies, 2 cut replies. A test model that never finishes still ends (`test_path.Ends`).
- **Cascades:** none can happen across users (single-user). Per container: 600 s, memory = swap, 512 pids, a 3 GB disk watched every 2 s, `LOW_SPACE` if Docker's disk is low. Any exception from untrusted input becomes `BLOCKED: internal error (TypeName)`. Every task ends with an outcome.

### B3. Data modeling and query efficiency
- **State:** one append-only SQLite table `event(seq, at, task, kind, body JSON, prev, hash)`. `body` is masked JSON with sorted keys. The hash chain detects edits, and `log_end` in each receipt detects a cut-off tail.
- **Write path:** `BEGIN IMMEDIATE` → read the end → insert → commit, `synchronous=full`. This is one writer at a time across processes; we tested two programs writing at once and 20 kills.
- **N+1 / indexes:** there are no ORMs and no joins on the hot path. The log is write-mostly. Reads are rare (`--continue` scans `chat.saved`; the dashboard uses views over a *copy*). `task` and `kind` are indexed. A daily backup, rotation past 200 MB into a whole archive and 365-day retention keep the file bounded without ever breaking the chain. Lock contention never touches the live file, because the dashboard reads a backup copy every 5 s.
- **Memory bloat:** command output is kept as the first 5 MB + last 5 MB (`Kept`). The worker shortens old tool results past 200 k chars. Tool results are capped at 30 k chars. Files are streamed from `git cat-file --batch` into tar, never loaded whole.

### B4. Consistency, idempotency and rollbacks
- **Multi-step workflow:** a request becomes the commit (CAS on the ref), then the check, then the verdict. A commit that was not checked is moved back on Ctrl+C (`gitsafe.move_back`). A second try *replaces* our previous commit from the base (`replace=`), so tries never stack.
- **Optimistic concurrency on files:** `edit` and `delete` carry the sha256 from the last `read`, and the helper refuses a mismatch. Writes go to a temp file, then rename or link.
- **Approvals as transactions:** a yes is bound to `(action hash, hash of every file the command would run)` and re-checked after the answer. If anything changed, the result is REJECTED.
- **Retries:** the only non-idempotent external call is the model POST. It is safe to retry because each reply is used once. Image builds are idempotent by tree-hash tag.
- **Rollback for users:** `agent discard` prints `git branch NAME <old>` to undo. `agent merge` prints `git revert -m 1`.
- **Resume:** `chat.saved` is written only after a verdict, so `--continue` never replays a cut-off step.

### B5. Security and abuse prevention
- **AuthN/AuthZ:** single-user by design. The only authority is a person at a real TTY. `ask_human` flushes typed-ahead keys and accepts only `y` + Enter. Without a TTY, every question becomes no. **[GAP]** There are no multi-user roles.
- **Policy:** DENY → ASK → ALLOW, and nothing matching means ASK. A DENY cannot be approved. Rules check both the original name and its NFKC/casefold spelling (`Makeﬁle`).
- **Secrets:** the Keychain via stdin (never argv). The key goes only in an HTTPS `Authorization` header with no redirects. Everything is masked before it is stored, shown or sent. A commit adding a secret is refused. Error text containing any 8 characters of the key is withheld. A test with a fake marker key checks every sink.
- **Untrusted input at the boundary:** model replies (shape), file names (`<U+XXXX>` escaping, row marks), test reports (nonce, canaries, exit-code agreement, random JUnit file name, XML without DOCTYPE/ENTITY), MCP args (cannot start with `-`), requirements lines (no URLs or options).
- **Rate limiting:** outbound per service (`Limiter`); inbound, MCP `verify` is limited to 20 calls an hour. The dashboard needs a sign-in shown only in the terminal.

### B6. Testing and regression confidence
- **Correctness:** 292 tests, mostly integration against real Docker and real Git. Fakes are used only for the model (`FakeModel`, `Replay`) and the Keychain. `tests/run.py` fails a job if the number of tests run differs from the number discovered.
- **Adversarial:** 7 cheat cases plus every finding from the separate AI reviews, kept as regression tests (`test_review_findings_false_verified`). The two known misses are *asserted* to pass (`test_known_limit_two_cheats_pass`), so the published limits stay true.
- **Gate:** `./check` = ruff + mypy strict + secret scan + all tests + 0 leftover containers. It is the pre-commit hook and CI on every push.
- **Proving a refactor:** `./check` green, `agent demo` gives the exact expected verdict row (exit 1 otherwise), `Replay.different == 0` after re-recording. A Python, Docker or base-image change alters the fingerprint, so no stale pass is reused.
- **Latency and coverage:** measured from the log (`verify.done − verify.started`, avg 26 s), plus `tests/bench.py` (11.3 s median for one check on the dev Mac; fails above 1.5× the saved median). CI reports coverage of `agent/` on every push. **[GAP]** There is no coverage threshold yet.

### B7. Scale and latency at 100× (10,000 concurrent users)
First, what "users" means here: each user runs their own copy on their own machine, so 10,000 users means 10,000
*independent* installs. Nothing central breaks, except:
1. **Model rate limits break first.** The free tier allows 5 calls a minute per key. Each user has their own key, so it scales per user, but BLOCKED was already 3 of 8 live tasks. Fix: paid tiers, provider routing across services, a persistent cool-down, prompt caching.
2. **Local Docker throughput** for one user: the checker runs 6–8 containers in sequence. Fix: run the 3 repeats in parallel volumes and reuse one copied base volume via snapshots.

If this became a **hosted** service for 10,000 concurrent tasks, here is what breaks, in order:

| # | Bottleneck | Why | Re-architecture |
|---|---|---|---|
| 1 | Sandbox capacity and cold starts | ~10 containers per task, ~1 min each → ~100k container starts per wave | Firecracker/gVisor microVM pool with pre-warmed images per language; copy-on-write project snapshots |
| 2 | Provider rate limits and cost | ~10k tokens per task, central keys | provider router with per-tenant quotas, fallback across vendors, prompt caching, a budget in `Limits` |
| 3 | SQLite single-writer log | one file, `BEGIN IMMEDIATE` | per-tenant append-only stores (Postgres partitioned by tenant, or object-storage segments) keeping the hash chain per task |
| 4 | Approval UX | needs a person at a TTY | signed approval requests (web or IDE) bound to the same action hash |
| 5 | Sync loop | one task per process | a queue of checker jobs (the checker is already stateless given repo, base, result and contract), with workers that scale horizontally |

The checker is the easiest part to scale out, because its inputs are immutable commit ids and its output is a pure
function of the test runs.

---

## Part C · YC rapid-fire answers (each under 30 seconds)

**What are you building?**
A coding agent for your terminal that never says "done" on its own word. Every change is re-tested from scratch by
an independent checker with no AI in it, and you get VERIFIED, FAILED, FLAKY or UNVERIFIABLE plus a receipt.

**What is the hardest technical part?**
Making a test result trustworthy when the code under test can lie. The project's code runs in the same process as
the test runner, so we put the original tests back, add random tests that *must* fail, and require the proof to fail
again once the fix is removed. A forged report trips the canaries.

**Who are your competitors, and what's broken about them?**
Claude Code, Cursor, Copilot's agent, Devin, Aider. They show logs, screenshots or a review, but none of their public
pages describe independent, cheat-tested proof. In our own test, an exit-code check accepted 7 of 7 cheats. We're
also complementary: `agent verify` checks *their* branches over MCP.

**What are your unit economics?**
Today: $0 infrastructure, because everything runs on the user's machine, and $0 model cost on the Gemini free tier.
About 10k tokens per verified task. Pricing: [FILL IN REAL NUMBER]. On a paid model it's ~10k × [FILL IN REAL NUMBER:
$/1M tokens] per task; the checker itself costs only local CPU.

**What's your biggest weakness, and how are you fixing it?**
Two, stated in the README. VERIFIED can't catch answers hard-coded for the tests, so next is held-back examples. And
we've done no user study yet, so next week is five developers using it on their own repos.

**What did you ship most recently?**
On 27 Nov: an MCP server so Claude Code, Cursor and Copilot can verify their own branches, JavaScript and Go at
test-level, `agent --continue`, images from `requirements.txt`, and CI that pulls the language images for 292 tests.

Extra answers to have ready:
- *Why now?* Developers use AI code more than they trust it (Stack Overflow 2025, cited in CASE-STUDY: 46% distrust the accuracy, 33% trust it).
- *Users?* None external yet ([FILL IN REAL NUMBER] after the user study). Built in a 7-day solo hackathon.
- *Moat?* The cheat corpus and the checker's rules, each from a real false-VERIFIED found in review, plus being agent-neutral.

---

## Part D · Live demo and "gotcha" question bank

### 3-minute demo script

| Time | Do | Say |
|---|---|---|
| 0:00 | `agent doctor` | "Everything runs locally: Python, Git, Docker. No key needed for what you'll see first." |
| 0:15 | `agent demo` (starts, ~1 min) | "A recorded agent fixes a negative-price bug. The model replies come from a recording, but every container, tool and check runs for real." |
| 0:40 | while it runs, point at the "doing now" lines | "Each step: read, edit, then the independent check, in fresh containers with no network." |
| 1:15 | the cheat table appears | "Four branches. Honest: VERIFIED. Weakened test: FAILED, because we restore the original test. Broken other test: FAILED as a regression. Forged report: UNVERIFIABLE, because the canaries that must fail came back passing." |
| 1:45 | `open …/demo.html` | "Same result as a report." |
| 2:00 | prepared practice repo: `agent verify agent/<id> --proof test_calc.T.test_add` | "And this works on any branch, from Claude Code or a person. No model at all." |
| 2:40 | `cat …/receipt.txt` → "Not proven by this receipt" | "We also say what we did *not* prove. VERIFIED is not 'correct'." |

**Backup plans.**
- *The model service is slow or down:* the demo needs no model. Never do a live Gemini run on stage. If asked, show the event log query with a past live task (doc 05 §2.4).
- *Docker is slow:* run `docker pull python:3.12-slim` and one `agent demo` beforehand (warm cache). Keep a screen recording of a full demo and the generated `demo.html` open in a tab.
- *No Docker at all:* show `demo.html`, a saved `receipt.txt`, and walk through `checker.verdict()` (it is 10 lines).
- *`agent demo --keep`* lets the interviewer `docker exec` into the container and see that `touch x` is refused.

### 10 hardest follow-up questions

1. **"The project code runs in the same process as your test runner. Can't it just print a fake report?"**
   Yes, and the demo's "forges the test report" branch does exactly that: it finds the nonce with `gc.get_objects()`. We don't rely on the nonce being secret. A forged "all ok" also marks our random canary tests as ok, which `canary()` treats as unsafe, so the result is UNVERIFIABLE. A cheat that edits *only* specific test results without touching the canaries is a listed limit in `receipt.NOT_PROVEN`.

2. **"So what *can* still get VERIFIED wrongly?"**
   Hard-coded answers for the tests, objects whose `__eq__` is always true, rare flakes (we run 3 times), helper files outside test folders that don't import unittest/pytest, and anything no agreed test covers. These are in the README and in every receipt, and two of them are asserted by a test so the list stays honest.

3. **"Docker isn't a security boundary."**
   Agreed, and we say so in the README and every receipt ("shared kernel"). We reduce what the code can do: no network, all capabilities dropped, read-only root, non-root user, pids/memory/disk/time limits, `--pull never`, no socket, and the mount list is checked. The next step is gVisor or Docker Sandboxes behind the same `box.run` interface.

4. **"Why trust *your* checker more than the agent's tests?"**
   Because of who controls the inputs. The checker takes the base and result from your Git object store, not from the container. It restores protected files from the base unless you approved the exact content, and a fixed 10-line table decides the verdict. No model output is an input to the verdict.

5. **"Isn't asking the human for every command just approval fatigue?"**
   For an ordinary fix there are 2 decisions, start and merge, and a test counts them. Reads and in-scope edits are ALLOW. Commands, tests, dependencies, CI and hooks ask, because those are where cheats and damage live. In the dev log, 3 of 61 tool calls asked.

6. **"What if the human says yes to something malicious?"**
   We make the question honest: it is drawn from the exact action, invisible and bidi characters are shown as codes, content can't start a row, scripts the command would run are shown in full, and the yes is bound to a hash. If anything changes while waiting, the answer is REJECTED. A person can still approve without reading; the threat model says so.

7. **"Your model calls are only to Gemini's free tier. What about privacy?"**
   The start screen names the company that receives the code, with Google's free-tier note that people may read it. Secret-looking files are never copied in. Demo and model-test send only made-up code. For private code: choose a paid provider with a data agreement (`agent key add`). Retention at the provider is outside our control.

8. **"SQLite as an audit log? Someone can just edit it."**
   It is tamper-*evident*, not tamper-proof. Each row hashes the previous one, and each receipt stores the end (seq, hash), so an edited row or a cut-off tail is detected (the dashboard shows CHANGED). Someone who rewrites the log *and* every receipt can forge history, and the docstring says exactly that.

9. **"Why Python standard library only? Isn't that reinventing wheels?"**
   On the trusted side, every dependency is attack surface and something to explain. We hand-wrote a small HTTP client (no redirects, a total-time cap), a line-based JSON-RPC server, and the terminal drawing. That came to about 5,000 lines of plain Python, with mypy strict and no runtime dependency at all. We first considered Go and chose Python, because the developer must be able to explain every line.

10. **"How do you know your 292 tests test the right things?"**
    Each risky task had a separate AI review that tried to break it. Every real finding became a regression test (the CASE-STUDY lists 9, three of which were false VERIFIEDs). We also mutated the code by hand to see the tests notice. CI now reports coverage on every push. The remaining gap is that there was no independent human review, and we don't claim one.
