# Coding Agent that proves its work

A coding agent for your terminal that never says "done" on its own word. Each change is checked from scratch by an
independent checker that no AI is part of, and you get **VERIFIED**, **FAILED**, **FLAKY** or **UNVERIFIABLE** with
a receipt of what was checked and what was not.

These pages are the engineering handover: how it is built, why, how to run and debug it, and how to talk about it.
To install and use the agent, start with the [README on GitHub](https://github.com/bhaviknagre/Coding-Agent-#readme).

| Page | Read it for |
|---|---|
| [Parts at a glance](architecture.md) | every part, its file, and whether it is trusted |
| [Threat model](threat-model.md) | what is protected from whom, and what remains |
| [01 System architecture](01-system-architecture.md) | diagrams, the request flow, design decisions, every interface |
| [02 Core logic and data models](02-core-logic-and-data-models.md) | storage, the event log, receipts, policy, the checker |
| [03 Permissions and state safety](03-permissions-and-state-safety.md) | who may do what; integrity, undo, cleanup |
| [04 Security and compliance](04-security-and-compliance.md) | guardrails, isolation, secrets, privacy, licence |
| [05 FinOps and resource routing](05-finops-and-resource-routing.md) | limits, retries, model services, measured cost |
| [06 Testing and CI](06-testing-and-ci-regression.md) | test suites, fakes, `./check`, coverage, the benchmark |
| [07 Operations and runbooks](07-operations-and-runbooks.md) | debugging a run, incidents, upgrades |
| [08 Product and handover](08-product-and-handover.md) | capabilities, known issues, roadmap, ownership |
| [09 Interview and YC prep](09-interview-and-yc-prep.md) | pitch, hard questions, demo script |

The story of how it was built is in the
[case study](https://github.com/bhaviknagre/Coding-Agent-/blob/main/CASE-STUDY.md). The code is MIT-licensed.
