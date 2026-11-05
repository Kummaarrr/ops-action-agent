# Architecture

## The idea in one sentence

An agent that works a support queue is only worth deploying if it never does what the policy
forbids; so every action goes through a guard that evaluates the policy on the real records
before the call, risky actions become approval requests instead of actions, and the whole thing
is measured on tasks whose correct end state is known in advance.

## The pieces

```
ticket  ->  agent loop (Claude, tool use)  ->  guard  ->  MCP servers  ->  company systems
                 ^                              |              store | billing | helpdesk | kb | approvals
                 |                              v
            tool results <---- provenance-tagged, injection-flagged
                 |
            finish(outcome) -> consistency check -> checker model -> report
```

| Piece | Module | What it does |
|---|---|---|
| Simulated company | `sim/` | One SQLite file with the store, billing and helpdesk tables of a subscription e-commerce business; 2,000 seeded customers; a simulation clock; snapshot / restore / diff; the audit log. Every customer carries a unique *canary* string in a staff-only field, so a leak is detectable. |
| Policy | `policy/policy.md`, `policy/engine.py` | The written policy (clauses P1-P9) and the same clauses as code: identity (P1), orders (P2), returns and refunds (P3), subscriptions (P4), payments (P5), appointments (P6), promises (P7), escalation (P8). The engine answers allow / deny / approval / escalate with the clause, from the actual record state. Fails closed: an exception, a wrong argument type or a missing record is a deny. |
| MCP servers | `servers/` | Five servers (store, billing, helpdesk, kb, approvals) over stdio, one tool per system operation, with read / write / approval annotations. Tool descriptions say what the systems do, not what the policy wants. They enforce only what the real systems enforce (a fulfilled order's address cannot change; a refund cannot exceed the charge; a disputed charge cannot be refunded; a return needs a delivered order). Identity is verified server-side: `verify_identity` reads the sender address from the ticket itself and returns a level and which facts matched, never the stored values; `find_customer` returns a masked email. The policy is *not* in the servers - deliberately, so the baselines can break it the way a careless integration would. A `fail_next` hook simulates a transient outage. |
| Agent | `agent/agent.py` | A manual tool-use loop on the Anthropic SDK: model turn -> tool calls -> results -> ... -> `finish(outcome)`. Systems: v0 (tools, no policy), v0+ (policy in the prompt), v1 (policy + guard + checker), and one ablation per guard layer. |
| Guard | `agent/guard.py` | Between the model and the servers. **Identity gating:** account tools are offered by verification level - level 1 (the sender address is the account's) unlocks reads, a pause and a monthly cancellation; level 2 (one more fact: an order id on the account, the billing zip, the card's last four) unlocks money and account changes. Internal-channel tickets start at level 2. A second verification against a different customer is refused and logged. **Ownership:** every account-scoped call must target the verified customer's record; helpdesk writes are pinned to the ticket being worked; other tickets may be read only when they belong to the verified requester. **Argument validation** against the tool's own JSON schema before anything else. **Policy engine** on every write, with cumulative refund amounts within the task (no salami slicing). **Approval gate:** an action the engine marks "approval" is not run; the guard files the approval request itself - the model cannot call the approvals server - with the before / after diff and the clause. **Untrusted data:** customer-written fields come back inside explicit delimiters, and instruction-like text is flagged as a security event. **Output guard:** a reply may not contain another customer's data (normalised, so spacing or `[at]` tricks do not help), staff notes, the requester's own verification facts (P1.5), or a disclosure to an impostor. **Caps** on tool calls and writes. |
| Checker | `agent/agent.py::_check` | A second model call on finish, with a structured verdict: does the reported outcome match what the systems recorded; did any write break a clause; does the reply state anything the systems did not record or echo data it must not. A fail turns the outcome into a hand-back; a checker error also fails closed. |
| Tasks | `tasks/` | 51 templates over the seeded records -> 415 tasks with the ticket text, the sender address and channel, the setup, and the expected outcome: verdict, end-state assertions, allowed tables, required approvals, forbidden tools, reply constraints. 45% of each template held out: 190 test tasks. |
| Scorer | `scoring.py` | Independent of the guard. From the audit log, the diff, the inbox and the replies: success, verdict correctness, violations by kind (unverified access, cross-customer, forbidden action, approval bypassed, unexpected change, PII leak, internal leak, verification echo, false claim, closed without reply, credit promised) and by class (money out, data out, irreversible, honesty, paperwork), precision / recall per verdict kind, hand-back and verification-ask rates, cost. Verification is judged from the true requester's legitimate verifications, not from the agent's own claims. |

## Why the policy lives in the agent, not the servers

Real systems (Shopify, Stripe, Zendesk) do not know the company's refund window or approval
thresholds. If the simulated servers enforced the policy, every system under test would look
safe and the comparison would say nothing. The servers behave like the real ones; the guard is
the part a client would actually buy.

## Why the scorer does not reuse the engine

The guard uses `policy/engine.py`. If the scorer used it too, "the guard follows the engine"
would be true by construction. The scorer checks the task's pre-written expectations (which
tool calls are forbidden, which approvals must exist, which rows must change and which must not)
and the leak canaries, with its own ownership lookup. The guard mutation test closes the loop
from the other side: each engine rule is disabled in turn and the scorer has to catch the
violations that rule exists to prevent.

## What the review round changed

Two blind reviews (a support-operations lead and an application-security reviewer, both
role-played by a model from the review brief; reports in `docs/reviews/`) reshaped the design:
returns go through an RMA with the refund on receipt rather than an instant refund; verification
became two-level and server-side; approvals became guard-only; refunds are cumulative per task;
foreign ticket reads, identity switches and type confusion are blocked; the checker fails
closed; untrusted text is delimited; leak checks are normalised; results are reported per
template. The one recommendation not adopted - handling a ticket that bundles several requests
as several verdicts - is disclosed as a limitation.

## Deployment shape

The MCP interface is the seam: swap `servers/backend.py` for adapters over the Shopify Admin
API, Stripe and a helpdesk API (phase 4 runs the same tasks against real sandboxes), replace
`SimFacts` with reads through those tools, keep the guard, the policy engine and the agent as
they are. What leaves the client's environment is the ticket text, tool results and the policy,
sent to the model API; the approval inbox and the audit log stay in their systems.
