# Threat model

| Asset | Attacker | Control | Test | What remains |
|---|---|---|---|---|
| Your files | the model, project code | work only on a copied volume; commits made by the launcher | files byte-identical before and after | merging runs your own hooks; the code runs when you run it |
| The API key | everyone | Keychain; only the launcher sends it; masked everywhere | a fake key never appears in logs, receipts, Git, containers or requests | another program running as you can read the Keychain item |
| Your computer | project code | containers: no network, no capabilities, read-only root, limits | network, socket, home folder, root all unreachable in tests | containers share the kernel |
| The verdict | the model, project code | original tests, canaries, three runs, counter-check, approvals by exact content | 7 cheat cases and the review findings as tests | hard-coded answers; cheats that rewrite one test from inside the test process |
| Approvals | the model's text, file names | drawn by the launcher from the action; invisible characters shown as codes; bound by hash | forged rows, escape codes, swapped scripts | a person can still say yes without reading |
| Your other Docker items | hostile images | name and label must both match ours | your container survives our cleanup | — |
| Records | the agent | written by the launcher only; hash chain; end hash in the receipt | a changed or shortened log is found | someone who can rewrite the log and every receipt |
| Network | prompt injection | containers have none; only the launcher reaches the model service | `curl … \| sh` from a file does not run | the model service receives the code the agent reads |

Mapped to the OWASP Top 10 for agentic applications: goal hijack and tool misuse (policy, approvals), privilege abuse
(no keys or network in containers), supply chain (no installs in R1), unexpected code execution (containers),
memory poisoning (labelled data, masking), human trust exploitation (exact approvals), rogue agent (limits, independent
checker).
