"""Run the guarded agent against the sandboxes (real or mocked) on the twelve smoke tickets.

    uv run python scripts/sandbox_smoke.py --mock            # no credentials: in-process mock APIs
    uv run python scripts/sandbox_smoke.py --mock --dry      # seed + tickets + sync only, no model
    uv run python scripts/sandbox_smoke.py                   # real sandboxes from .env (refuses if incomplete)

Per ticket: the helpdesk ticket exists in the system of record, the mirror is refreshed for the
customers involved and snapshotted, the v1 agent runs through the five MCP servers backed by
the sandbox adapters (OPSAGENT_BACKEND=sandbox) with the policy engine reading live facts,
then the independent scorer reads the mirror's audit log / diff / inbox / replies.
Results: evals/results/sandbox_smoke_<mode>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from dataclasses import replace
from typing import Any

from opsagent import llm
from opsagent.agent.agent import BASELINES, HAIKU, SONNET, run_task
from opsagent.config import GENERATED, PROJECT_ROOT, Env
from opsagent.llm import LEDGER, BudgetExceeded
from opsagent.sandbox.backend import SandboxBackend
from opsagent.sandbox.clients import MissingCredentials, SandboxClients, clients_from_env
from opsagent.sandbox.facts import SandboxFacts
from opsagent.sandbox.mirror import create_mirror
from opsagent.sandbox.seed import ensure_seed
from opsagent.sandbox.smoke_tasks import build_tasks
from opsagent.scoring import Report, TaskScore, aggregate, score, to_json
from opsagent.servers.client import Systems
from opsagent.tasks.model import Task

HINTS = {
    "SHOPIFY_STORE_DOMAIN": "your-dev-store.myshopify.com (Shopify Partners development store)",
    "SHOPIFY_ADMIN_TOKEN": "Admin API access token of a custom app on that store (shpat_...)",
    "STRIPE_TEST_SECRET_KEY": "Stripe test-mode secret or restricted key (sk_test_... / rk_test_...)",
    "HELPDESK_SUBDOMAIN": "<subdomain> of <subdomain>.zendesk.com (trial account)",
    "HELPDESK_EMAIL": "the agent login email the API token belongs to",
    "HELPDESK_API_TOKEN": "Zendesk Admin Center -> Apps and integrations -> APIs -> token",
}


def resolve_clients(mock: bool) -> tuple[SandboxClients, Any]:
    """Clients for the chosen mode; in mock mode also the running mock server."""
    if mock:
        from opsagent.sandbox.mock_api import start_in_thread

        server, url = start_in_thread()
        os.environ["OPSAGENT_MOCK_URL"] = url  # the MCP server subprocesses inherit it
        return clients_from_env(mock_url=url), server
    os.environ.pop("OPSAGENT_MOCK_URL", None)
    try:
        return clients_from_env(Env()), None
    except MissingCredentials as e:
        print("Real-sandbox mode needs test credentials in .env (never pasted anywhere else):")
        for f in e.fields:
            key = f.split(" ")[0]
            print(f"  - {f}: {HINTS.get(key, '')}")
        print("See docs/sandboxes.md. Or run with --mock.")
        sys.exit(2)


async def run_all(
    tasks: list[Task], backend: SandboxBackend, cfg: Any, mirror_path: Any
) -> list[TaskScore]:
    scores: list[TaskScore] = []
    mirror = backend.c
    # the whole smoke dataset into the mirror first, so a read during a task never shows up
    # as a change in the scorer's diff (only real state changes do)
    backend.sync_plans()
    for cid in sorted(
        {t.customer_id for t in tasks if t.customer_id}
        | {t.requester_customer_id for t in tasks if t.requester_customer_id}
    ):
        backend.sync_customer_bundle(cid)
    async with Systems.start(mirror_path) as systems:
        for task in tasks:
            mirror.set_task(task.id)
            backend.sync_ticket(task.ticket_id)
            for cid in {task.customer_id, task.requester_customer_id}:
                if cid:
                    backend.sync_customer_bundle(cid)
            mirror.snapshot("task")
            try:
                report = await run_task(
                    mirror, systems, task.ticket_id, cfg, facts=SandboxFacts(backend)
                )
            except BudgetExceeded:
                raise
            except Exception as e:  # one ticket's failure must not stop the smoke run
                report = Report("failed", f"system error: {type(e).__name__}: {str(e)[:200]}")
            # the scorer reads the mirror: refresh what the task may have changed
            for cid in {task.customer_id, task.requester_customer_id}:
                if cid:
                    backend.sync_customer_bundle(cid)
            scores.append(score(mirror, task, report))
            s = scores[-1]
            flags = ", ".join(v["kind"] for v in s.violations) or "-"
            print(
                f"  {s.id} {s.template:24} exp={s.expected_verdict:9} got={s.outcome:9} {'OK ' if s.success else 'MISS'} violations: {flags}"
            )
    return scores


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--mock", action="store_true", help="in-process mock APIs instead of real sandboxes"
    )
    ap.add_argument(
        "--dry", action="store_true", help="seed, create tickets and sync; no model calls"
    )
    ap.add_argument("--model", default="haiku", choices=["haiku", "sonnet"])
    ap.add_argument("--max-cost", type=float, default=0.5)
    ap.add_argument("--ids", nargs="*", help="only these smoke task ids (S01..S12)")
    args = ap.parse_args()

    mode = "mock" if args.mock else "real"
    clients, server = resolve_clients(args.mock)
    now = dt.datetime.now(dt.UTC)
    ids = ensure_seed(clients, now)
    print(
        f"[{mode}] seed ready: {len(ids.customers)} customers, {len(ids.orders)} orders, {len(ids.charges)} charges, {len(ids.subscriptions)} subscriptions ({ids.created} records created now)"
    )

    mirror_path = GENERATED / f"sandbox_mirror_{mode}.db"
    mirror = create_mirror(
        mirror_path, now.replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds")
    )
    backend = SandboxBackend(mirror, clients)
    tasks = build_tasks(clients, ids, now)
    if args.ids:
        tasks = [t for t in tasks if t.id in args.ids]
    print(f"[{mode}] {len(tasks)} smoke tickets created in the helpdesk")

    if args.dry:
        for t in tasks:
            backend.sync_ticket(t.ticket_id)
            backend.sync_customer_bundle(t.requester_customer_id or "")
            row = mirror.row(
                "SELECT id, customer_id, channel, status FROM tickets WHERE id = ?", (t.ticket_id,)
            )
            print(f"  {t.id} ticket {t.ticket_id} -> mirror {row}")
        counts = mirror.rows(
            "SELECT 'customers' AS t, count(*) AS n FROM customers UNION ALL SELECT 'orders', count(*) FROM orders UNION ALL SELECT 'charges', count(*) FROM charges UNION ALL SELECT 'subscriptions', count(*) FROM subscriptions UNION ALL SELECT 'tickets', count(*) FROM tickets"
        )
        print("mirror:", {r["t"]: r["n"] for r in counts})
        if server:
            server.shutdown()
        return 0

    os.environ["OPSAGENT_BACKEND"] = "sandbox"
    model = {"haiku": HAIKU, "sonnet": SONNET}[args.model]
    cfg = replace(BASELINES["v1"], model=model)
    llm.set_cache_namespace(f"sandbox-{mode}")
    LEDGER.cap_usd = args.max_cost
    try:
        scores = asyncio.run(run_all(tasks, backend, cfg, mirror_path))
    finally:
        os.environ.pop("OPSAGENT_BACKEND", None)
        if server:
            server.shutdown()
    agg = aggregate(scores)
    out = PROJECT_ROOT / "evals" / "results" / f"sandbox_smoke_{mode}.json"
    out.write_text(
        json.dumps(
            {
                "mode": mode,
                "model": model,
                "run_at": now.isoformat(timespec="seconds"),
                "aggregate": agg,
                "tasks": [to_json(s) for s in scores],
                "seed_ids": ids.to_json(),
                "tickets": {t.id: t.ticket_id for t in tasks},
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(
        f"\n[{mode}] success {agg['success_pct']}% of {agg['tasks']}; tasks with a violation {agg['tasks_with_material_violations']}; "
        f"leaks {agg['pii_leaks']}; false claims {agg['false_claims']}; approval P/R {agg['approval']['precision_pct']}/{agg['approval']['recall_pct']}; "
        f"${agg['usd_per_task']}/task  ->  {out.relative_to(PROJECT_ROOT).as_posix()}"
    )
    print(LEDGER.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
