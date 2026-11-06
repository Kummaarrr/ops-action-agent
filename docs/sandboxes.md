# Phase 4: the same agent on real sandboxes

The simulated company is one SQLite file behind five MCP servers. Phase 4 keeps the servers,
the guard, the policy engine, the agent and the scorer, and swaps the backend for adapters over
three real systems in their free test tiers: a **Shopify development store**, **Stripe test
mode** and a **Zendesk-shaped helpdesk trial**. Until the accounts exist, everything runs
against an in-process mock of the same endpoints (`opsagent.sandbox.mock_api`), through the
same client code.

```
uv sync --extra sandbox
uv run pytest -q tests/test_sandbox.py                 # adapters + mirror + engine over the mock
uv run python scripts/sandbox_smoke.py --mock --dry    # seed, tickets, mirror; no model calls
uv run python scripts/sandbox_smoke.py --mock          # the v1 agent on 12 smoke tickets (Haiku, cap $0.50)
uv run python scripts/sandbox_smoke.py                 # real sandboxes from .env; refuses if a credential is missing
```

## What runs where

| Tool family | System of record | Notes |
|---|---|---|
| customers, orders, address change, cancel, fulfilment / tracking | Shopify Admin REST (`/customers`, `/orders`, `/orders/{id}/cancel`, fulfillments) | the store refuses address edits and cancellations after fulfilment |
| returns (RMA) | local + a `return-requested` tag on the Shopify order | Shopify's returns API is GraphQL-only; the RMA row and its rules stay in the mirror |
| appointments, slots | local | scheduling is not a store concept; same rules as the simulation |
| charges, refunds (idempotency key), subscriptions (pause via `pause_collection`, cancel at period end, plan change via items), disputes | Stripe test mode | Stripe refuses refunds over the remaining amount and on disputed charges, and replays an idempotency key |
| plans | Stripe prices | one price per plan, `nickname` = plan name |
| tickets, public replies, internal notes, status, tags, escalation | Zendesk-shaped API (`/tickets`, comments with `public: false`, `group_id`) | escalation = assign to the team's group + tag; Zendesk has no "escalated" status, the mirror does |
| knowledge base, policy text | local (`policy/`, the six articles) | |
| approvals inbox, verification ledger, audit log | local (the mirror) | the client's systems in a deployment |

**The mirror** (`opsagent.sandbox.mirror`) is a SQLite file in the simulation's schema. Every
adapter call refreshes the rows it touched from the system of record and writes the audit
entry; the guard's ownership and output checks, the checker and the scorer read it unchanged.
The **policy engine reads live** (`SandboxFacts`): each fact is fetched from Shopify / Stripe at
decision time, not from the cache. Timestamps are normalised to UTC; the billing zip is the
customer's default-address zip (the store has no separate billing record); the card's last four
come from the Stripe charge.

## What the owner creates (test credentials only, pasted into `.env`, never anywhere else)

| `.env` field | Where it comes from |
|---|---|
| `SHOPIFY_STORE_DOMAIN` | the development store's `*.myshopify.com` domain (Shopify Partners -> Stores -> Add store -> development store) |
| `SHOPIFY_ADMIN_TOKEN` | the store's custom app: Settings -> Apps and sales channels -> Develop apps -> create app -> Admin API scopes `read_customers, write_customers, read_orders, write_orders, read_fulfillments, write_fulfillments` -> install -> Admin API access token |
| `STRIPE_TEST_SECRET_KEY` | Stripe Dashboard in **test mode** -> Developers -> API keys; a restricted key with write access to Customers, Charges, Refunds, Subscriptions, Prices and Products is enough (`rk_test_...`); the code refuses anything that is not a test key |
| `HELPDESK_SUBDOMAIN` | the trial's `<subdomain>.zendesk.com` |
| `HELPDESK_EMAIL` | the agent account the token belongs to |
| `HELPDESK_API_TOKEN` | Admin Center -> Apps and integrations -> Zendesk API -> enable token access -> add token |

No card is needed for any of the three; nothing leaves test mode. Start the helpdesk trial
only when the run is scheduled (14 days).

## The smoke run

`scripts/sandbox_smoke.py` seeds twelve customers (`opsagent.sandbox.seed`) through the same
clients the agent uses: orders in every state the tickets need (paid, fulfilled with tracking,
delivered 5 / 8 / 10 / 45 days ago), one disputed charge (Stripe's `tok_createDispute`), one
duplicate charge, three subscriptions (two monthly, one annual), one Zendesk user per customer
and the escalation groups. Seeding is idempotent (`smoke:<key>` tags and metadata). It then
creates twelve tickets (`opsagent.sandbox.smoke_tasks`) - plain, approval-gated, refuse,
escalate, impostor, injection, duplicate charge - runs the v1 agent on each through the MCP
servers with `OPSAGENT_BACKEND=sandbox`, and scores with the same scorer as the main
measurement. Results: `evals/results/sandbox_smoke_<mock|real>.json`.

## What the mock proves, and what it does not

The mock reproduces the request and response shapes of the endpoints used, the ids and
timestamps formats, the error envelopes, and the system rules above; `tests/test_sandbox.py`
checks the adapters, the mirror and the engine against it, and the mock smoke run exercises
the full agent path end to end. It does not prove that the real APIs accept these payloads
exactly (API versions, required fields on order creation, the fulfillment-orders flow on newer
Shopify stores, Zendesk's upload tokens for attachments) - that is what the first real run is
for, and `--dry` surfaces those differences before any model call.
