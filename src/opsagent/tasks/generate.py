"""Build the task set from the templates on the seeded company and freeze it.

    uv run python scripts/build_tasks.py        # -> data/tasks.json

Each template draws `weight` tasks on distinct records; the split is stratified by template
(TEST_SHARE held out). Tickets are inserted by the harness at run time (setup), so the frozen
file is self-contained: company seed + task records reproduce every starting state.
"""

from __future__ import annotations

import random

from opsagent.sim.company import Company
from opsagent.tasks.model import Task
from opsagent.tasks.templates import TEMPLATES, Picked

TEST_SHARE = 0.45
SEED = 20261104


def _record_key(p: Picked) -> tuple:
    return (
        p.customer["id"],
        p.order["id"] if p.order else None,
        p.subscription["id"] if p.subscription else None,
        p.appointment["id"] if p.appointment else None,
        p.other["id"] if p.other else None,
    )


def generate(company: Company, seed: int = SEED) -> list[Task]:
    rng = random.Random(seed)
    tasks: list[Task] = []
    used_customers: set[str] = set()
    n = 1
    for tpl in TEMPLATES:
        seen: set[tuple] = set()
        drawn = 0
        attempts = 0
        while drawn < tpl.weight and attempts < tpl.weight * 25:
            attempts += 1
            picked = tpl.pick(company, rng)
            if picked is None:
                break
            key = _record_key(picked)
            # one task per customer across the whole set: tasks never interfere
            if key in seen or picked.customer["id"] in used_customers:
                continue
            seen.add(key)
            used_customers.add(picked.customer["id"])
            subject, body, setup, expected, meta = tpl.build(picked, rng, company)
            ticket_id = f"T9{n:05d}"
            impostor = bool(meta.get("impostor"))
            internal = bool(meta.get("internal"))
            requester_email = (
                f"{picked.customer['first_name'].lower()}.{rng.randint(100, 999)}@othermail.example"
                if impostor
                else "billing-ops@northwind.example"
                if internal
                else picked.customer["email"]
            )
            channel = "internal" if internal else rng.choice(["email", "web_form", "email"])
            setup = [
                *setup,
                [
                    "INSERT INTO tickets(id, customer_id, requester_email, channel, subject, body, status, created_at, tags, attachments) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        ticket_id,
                        None if impostor else picked.customer["id"],
                        requester_email,
                        channel,
                        subject,
                        body,
                        "open",
                        company.now().isoformat(timespec="seconds"),
                        "",
                        ",".join(meta.get("attachments", [])),
                    ],
                ],
            ]
            tasks.append(
                Task(
                    id=f"K{n:03d}",
                    template=tpl.name,
                    category=tpl.category,
                    tier=tpl.tier,
                    customer_id=picked.customer["id"],
                    requester_customer_id=None if impostor else picked.customer["id"],
                    ticket_id=ticket_id,
                    subject=subject,
                    body=body,
                    requester_email=requester_email,
                    channel=channel,
                    setup_sql=setup,
                    expected=expected,
                    notes=", ".join(f"{k}={v}" for k, v in meta.items()),
                )
            )
            n += 1
            drawn += 1
        if drawn < tpl.weight:
            print(f"  {tpl.name}: only {drawn} of {tpl.weight} tasks (not enough fitting records)")
    # split: stratified by template, seeded
    by_tpl: dict[str, list[Task]] = {}
    for t in tasks:
        by_tpl.setdefault(t.template, []).append(t)
    for group in by_tpl.values():
        rng.shuffle(group)
        n_test = round(len(group) * TEST_SHARE)
        for i, t in enumerate(group):
            t.split = "test" if i < n_test else "dev"
    tasks.sort(key=lambda t: t.id)
    return tasks
