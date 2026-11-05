# Ops Action Agent

A headless agent that works a support queue end to end through the company's systems - store,
billing, helpdesk - via **MCP tools**, under a written **policy**, with human approval exactly
where the policy says. Every write passes a guard that evaluates the policy on the real records
first; risky actions become approval requests instead of actions; replies are screened for other
people's data. Measured on 415 pre-registered tasks with known correct end states, including
adversarial ones, against the baselines a template integration gives you.

**Write-up:** [docs/REPORT.md](docs/REPORT.md) · **Architecture:** [docs/architecture.md](docs/architecture.md) · **Live demo:** _(Streamlit deployment pending)_

## The story

1. **A tool-calling agent with no guard does the work - and the damage.** Given the same tools and
   no policy, a plain tool-calling agent completes 81% (78-83%) of the held-out tasks and breaks policy
   on 23-32 of them: refunds outside the window, account changes without verifying who is
   asking, other customers' data in replies.
2. **Pasting the policy into the prompt helps and is not enough.** With the policy in context the
   same agent reaches 86% (83-87%) success and still violates policy on 13-19 tasks: the
   model knows the rule and skips it when the ticket is persuasive, the arithmetic is off, or the
   text it reads tells it to.
3. **The guarded agent removes the violations by construction, not by better judgement.** Identity gating, a policy
   engine on the actual records, an approval gate that files the request itself, provenance tags
   on untrusted text, an output screen and a checker: 98% (97-98%) success, **0-1 tasks
   with a policy violation, 0 data leaks, 0 false claims**, approval
   precision / recall 99% (97-100%) / 96% (94-97%), at $0.0028 per ticket. The guard was mutation-tested:
   disabling five of its seven policy rules produced the violations they exist to prevent; the
   other two were redundant with the model or the systems on this set, and the report says so.

## Results

**Held-out test split:** 190 of 415 tasks never used while building, on a simulated
subscription e-commerce company (store, billing, helpdesk; 2,000 customers), across plain
requests, policy edge cases, approval-gated actions, requests to refuse, ambiguous requests,
adversarial tickets (injection, impersonation, social engineering), tool failures and
long-horizon cases. Model systems run 3 times with fresh model calls; mean (range); 95% Wilson
intervals in the full table.

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

**In one line:** On 190 held-out tasks the guarded agent succeeds on 98% (97-98% over three runs) with 0-1 tasks with a policy violation per run, no data leak and no false claim of completion; the same model with the policy pasted into its prompt reaches 86% and breaks policy on 13-19 tasks, and with no policy 81% and 23-32 tasks - at the same cost per ticket.

Ablation on the dev split: [evals/results/ABLATION_dev.md](evals/results/ABLATION_dev.md) ·
All runs: [evals/results/FINAL_test.md](evals/results/FINAL_test.md) · Guard mutation test:
[evals/results/GUARD_MUTATION.md](evals/results/GUARD_MUTATION.md) · Real-sandbox smoke test,
review round, effort model and failure analysis: [docs/REPORT.md](docs/REPORT.md)

## How it works

```
ticket -> agent (Claude, tool use) -> guard -> MCP servers (store | billing | helpdesk | kb | approvals)
            ^                           |
            |   identity gating: no account tools until verify_identity succeeds (P1)
            |   ownership: every call targets the verified customer's records only
            |   policy engine: windows, limits, states, notice periods - on the real rows
            |   approval gate: "needs a supervisor" -> the guard files the request with the diff
            |   untrusted data: provenance tags; instruction-like text flagged as a security event
            |   output guard: no other customer's data, no staff notes, no verification facts
            v
      finish(outcome) -> consistency check -> checker model -> audit log + approval inbox
```

- **Systems are real-shaped.** The MCP servers behave like Shopify, Stripe and Zendesk do (a
  fulfilled order's address cannot change; a refund cannot exceed the charge; a disputed charge
  cannot be refunded) and nothing more. The policy lives in the agent - the part a client buys.
- **The scorer is independent of the guard.** It reads the audit log, the database diff, the
  approval inbox and the replies against each task's pre-written expectations and planted PII
  canaries.
- **Same agent, real sandboxes.** The same MCP interface runs against a Shopify development
  store, Stripe test mode and a helpdesk trial for a smoke test and the recorded demo.

## Repository

```
src/opsagent/
  sim/             the simulated company: schema, seeded data with canaries, snapshots, audit log
  policy/          policy.md (P1-P9) and engine.py (the clauses as code)
  servers/         catalog, system-rules backend, the five MCP servers, the MCP client
  agent/           the loop, the guard, prompts, the checker
  tasks/           51 templates -> data/tasks.json (415 tasks, 190 held out)
  scoring.py       success, violations, leaks, honesty, precision / recall
scripts/           build_tasks, run_eval, guard_mutation, final_table
app/               Streamlit demo: the queue, a task's trace and diff, the approval inbox; live ticket on your key
```

## Run it

```bash
uv sync --group dev --extra app
cp .env.example .env                                    # ANTHROPIC_API_KEY
uv run python scripts/build_tasks.py                    # company + frozen task set
uv run python scripts/run_eval.py --system v1 --split dev
uv run python scripts/guard_mutation.py
uv run streamlit run app/streamlit_app.py
```

The demo holds no keys. "Run a live ticket" uses the visitor's own Anthropic key, passed per call
and never stored, against the simulated company. In a deployment the agent runs inside the
client's environment: tickets, tool results and the policy go to the model API (or a private
endpoint); the approval inbox and the audit log stay in the client's systems.

## Notes

Synthetic company and policy, author's own design; no client code or employer material. The
real-sandbox smoke test uses development / test credentials only. The model is Claude Haiku 5.5
(Sonnet 5.5 / Opus 5.5 as references).

## License

MIT
