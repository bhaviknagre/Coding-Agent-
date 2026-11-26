# Case study: building a coding agent that cannot grade its own homework

## The problem
Coding agents say "fixed" and the developer has to find out whether that is true. Exit codes, logs and tests written by
the same agent can be wrong, or forged. In a small experiment of ours, a plain exit-code check accepted **7 of 7**
simple cheats. Developers trust AI output less than they use it (Stack Overflow 2025: 46% distrust the accuracy, 33% trust
it), and the competitors we read about show logs, screenshots or a review, but their pages do not describe independent,
cheat-tested proof.

**Our position:** do one thing better than everyone else, proof that does not come from the agent that wrote the change,
and keep everything else simple.

## How we worked
- **Research first, then decide, then build.** Twelve planning stages (problem, autonomy, interface, loop, context, models,
  tools, execution, security, state, verification, operations), each with open questions, options A/B/C, a
  recommendation, a decision by the developer, and a written record.
- **Specification and plan under Genesis**, a tool that keeps requirements, tasks and proof together and blocks a task
  until its checks pass on the current files and, for risky tasks, a human has reviewed it.
- **Every risky task had a separate AI review** that tried to break it, then fixes, then deliberate breaks of the code
  ("mutation tests") to see whether the tests notice.
- **Every push is checked on GitHub's Linux,** which found bugs a Mac never showed.

## Decisions we changed, and why
Real disagreements, in the order they happened. None are invented.

| What changed | From | To | Why |
|---|---|---|---|
| Where the agent runs | on the computer | in Docker, with a small launcher (AM1) | the developer asked for everything on Docker; it also gives isolation |
| Project files | mount the folder (team proposal) | copy the commit into a volume | a mounted folder exposes `.env` and lets bugs change your files |
| Remote MCP servers | Claude: not now | allowed through the launcher | developer: "open to adopting the latest technology" |
| Launcher language | Go (Claude's recommendation) | Python (AM9) | the developer knows Python and must explain the code |
| Moving results out | Git bundle | plain files, commit made by the launcher (AM10) | less to trust; no Git in containers |
| Security rule | "the launcher runs every effect" | made true (AM5) | our own cross-check found the agent container could write files directly |
| Interface | one request per run | a chat like Claude Code, with memory (AM11, AM12) | the developer's main point |
| Counter-check | expected to catch cheats | kept, but described honestly | in our experiment it caught none of the 7 cheats; its job is proofs that pass anyway |

## What the reviews found
The separate reviews found real problems in every risky task, also after our own tests passed. Examples:

| Task | A finding | Effect before the fix |
|---|---|---|
| Containers | an image could escape the disk limit; a hostile image could make cleanup delete your own container | disk filled; your container gone |
| Checker | approving a changed test file erased the regression baseline | **false VERIFIED on honest work** |
| Checker | old tests in an approved file counted as proof | weakened tests passed as proof |
| verify | an accented file name was approved with an empty diff | content approved unseen |
| Rules | "yes, and again" allowed a later run of changed code | approval reused for something else |
| Approvals | padded text could forge the approval screen | you approve something else |
| Agent loop | delete a test and recreate a weaker one: VERIFIED | **false VERIFIED** |
| Key | 17 characters of the key could appear in an error message | partial key on screen |
| Chat | a follow-up with no new work was VERIFIED | **false VERIFIED** |

And GitHub's Linux found what a Mac did not: a container that Docker cannot remove while our side still reads it, a
"killed by memory" mark that arrives late, a key typed microseconds before a question.

## Results (measured)
| | |
|---|---|
| Tests | about 240, run in parallel; 15–25 minutes on the developer's Mac, about 5 minutes on GitHub |
| Cheat demo | honest fix VERIFIED; weakened test, broken test FAILED; forged report UNVERIFIABLE |
| An ordinary fix | 1 question to start, 1 merge by you |
| Your files | byte-identical before and after every task (tested) |
| Cancel | 120 interruptions at random moments, nothing left behind |
| Live model | see the README: `agent model-test gemini` (result recorded by the developer) |

## What is not proven, and what we would do next
- VERIFIED is not "correct": hard-coded answers and always-equal values still pass. Next: held-back examples.
- No user study: we did not find developers to try it in time.
- Containers share a kernel. Next: Docker Sandboxes or gVisor behind the same interface.
- Next features: MCP, resuming stopped tasks, building a project's Dockerfile, more languages' test reports, Windows.
