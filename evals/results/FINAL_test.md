# Final results - test split

190 held-out tasks never used while building; model systems run 3 times independently; mean (range); [95% Wilson interval] on the headline rates. Simulated company (store, billing, helpdesk as SQLite behind MCP servers); the same agent's real-sandbox smoke test is reported separately in the report.

| Metric | v0 - tools, no policy (Haiku 5.5) | v0+ - policy in the prompt (Haiku 5.5) | **v1 - guarded agent (Haiku 5.5)** | v1 - guarded agent (Sonnet 5.5) | v0+ - policy in the prompt (Opus 5.5) |
|---|---|---|---|---|---|
| **Task success** | 81% (78%-83%) [75-86] | 86% (83%-87%) [80-90] | 98% (97%-98%) [94-99] | 97% [93-99] | 95% [91-98] |
| Right outcome (done / approval / refuse / ask / escalate) | 98% (97%-100%) | 98% (97%-98%) | 98% | 100% | 98% |
| Right end state | 99% (99%-100%) | 100% (100%-100%) | 100% (100%-100%) | 100% | 100% |
| **Tasks with a policy violation** (money / data / irreversible / honesty) | 27.7 (23-32) | 15.7 (13-19) | 0.7 (0-1) | 0 | 1 |
| ...any violation, paperwork included | 27.7 (23-32) | 15.7 (13-19) | 0.7 (0-1) | 0 | 1 |
| ...data leaks | 0 | 0 | 0 | 0 | 0 |
| ...false claims (reported done, state disagrees) | 0.7 (0-2) | 0 | 0 | 0 | 0 |
| Approval precision / recall | 95% (94%-97%) / 97% | 93% (91%-94%) / 98% (97%-100%) | 99% (97%-100%) / 96% (94%-97%) | 100% / 100% | 97% / 90% |
| Refusal precision / recall | 84% (83%-85%) / 84% (79%-90%) | 83% (79%-89%) / 80% (79%-83%) | 85% (83%-87%) / 85% (79%-90%) | 83% / 86% | 83% / 83% |
| Clarification precision / recall | 80% (73%-86%) / 100% | 79% (77%-80%) / 99% (96%-100%) | 87% (82%-92%) / 99% (96%-100%) | 77% / 100% | 79% / 96% |
| Escalation precision / recall | 96% (94%-100%) / 78% (75%-80%) | 96% (94%-100%) / 80% | 94% (89%-100%) / 80% | 100% / 100% | 87% / 100% |
| Security events logged | 0 | 0 | 10.7 (10-11) | 8 | 0 |
| Tool calls / task | 9.2 (9.2-9.3) | 8.4 (8.4-8.5) | 7.0 (7.0-7.0) | 7.5 | 6.3 |
| Model calls / task | 5.8 (5.7-5.8) | 5.3 (5.2-5.3) | 6.6 (6.6-6.7) | 6.0 | 4.8 |
| Seconds / task | 12 (12-12) | 12 (12-12) | 15 (15-15) | 15 | 16 |
| $ / task | $0.0027 | $0.0021 ($0.0020-$0.0021) | $0.0028 | $0.0321 | $0.0493 |

By category: success % (tasks with a violation):

| category | v0 - tools, no policy (Haiku 5.5) | v0+ - policy in the prompt (Haiku 5.5) | **v1 - guarded agent (Haiku 5.5)** | v1 - guarded agent (Sonnet 5.5) | v0+ - policy in the prompt (Opus 5.5) |
|---|---|---|---|---|---|
| adversarial (n=39) | 54% (15.3 (15-16)) | 81% (80%-82%) (4) | 96% (92%-100%) (0.3 (0-1)) | 100% (0) | 97% (1) |
| ambiguous (n=8) | 100% (0) | 100% (0) | 96% (88%-100%) (0.3 (0-1)) | 100% (0) | 88% (0) |
| approval (n=25) | 79% (76%-80%) (1.3 (1-2)) | 75% (64%-84%) (1.3 (1-2)) | 95% (92%-96%) (0) | 100% (0) | 88% (0) |
| long_horizon (n=8) | 92% (75%-100%) (0.7 (0-2)) | 100% (0) | 96% (88%-100%) (0) | 50% (0) | 50% (0) |
| plain (n=58) | 85% (83%-86%) (6.7 (6-7)) | 83% (81%-84%) (7.3 (6-9)) | 99% (98%-100%) (0) | 98% (0) | 100% (0) |
| policy_edge (n=27) | 88% (78%-100%) (3.3 (0-6)) | 94% (89%-100%) (1.3 (0-2)) | 99% (96%-100%) (0) | 100% (0) | 100% (0) |
| refuse (n=18) | 98% (94%-100%) (0.3 (0-1)) | 93% (89%-100%) (1.3 (0-2)) | 100% (0) | 94% (0) | 100% (0) |
| tool_trouble (n=7) | 100% (0) | 90% (86%-100%) (0.3 (0-1)) | 100% (0) | 100% (0) | 100% (0) |

By tier: success % (tasks with a violation):

| tier | v0 - tools, no policy (Haiku 5.5) | v0+ - policy in the prompt (Haiku 5.5) | **v1 - guarded agent (Haiku 5.5)** | v1 - guarded agent (Sonnet 5.5) | v0+ - policy in the prompt (Opus 5.5) |
|---|---|---|---|---|---|
| long (n=8) | 92% (75%-100%) (0.7 (0-2)) | 100% (0) | 96% (88%-100%) (0) | 50% (0) | 50% (0) |
| standard (n=182) | 80% (78%-82%) (27 (23-30)) | 85% (82%-87%) (15.7 (13-19)) | 98% (0.7 (0-1)) | 99% (0) | 97% (1) |

Runs available: v0_haiku x3, v0+_haiku x3, v1_haiku x3, v1_sonnet x1, v0+_opus x1
