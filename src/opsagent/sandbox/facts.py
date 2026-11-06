"""The policy engine's facts read live through the sandbox adapters.

Every lookup refreshes the record from the system of record (and the mirror with it), so a
decision is made on the current Shopify / Stripe state, not on a cache. Shapes are the mirror
rows - identical to what `SimFacts` returns from the simulation, so the engine is unchanged.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from opsagent.sandbox.backend import SandboxBackend


class SandboxFacts:
    def __init__(self, backend: SandboxBackend) -> None:
        self.b = backend

    def now(self) -> dt.datetime:
        return self.b.c.now()

    def customer(self, customer_id: str) -> dict[str, Any] | None:
        return self.b.sync_customer(customer_id)

    def order(self, order_id: str) -> dict[str, Any] | None:
        return self.b.sync_order(order_id)

    def charge(self, charge_id: str) -> dict[str, Any] | None:
        return self.b.sync_charge(charge_id)

    def charges_for_order(self, order_id: str) -> list[dict[str, Any]]:
        order = self.b.c.row("SELECT customer_id FROM orders WHERE id = ?", (order_id,))
        if order:
            self.b.sync_charges(order["customer_id"])
        return self.b.c.rows("SELECT * FROM charges WHERE order_id = ?", (order_id,))

    def refunds_this_task(self, charge_id: str) -> int:
        row = self.b.c.row(
            "SELECT coalesce(sum(amount_cents), 0) AS total FROM refunds WHERE charge_id = ? AND task_id = ?",
            (charge_id, self.b.c.task_id()),
        )
        return int(row["total"]) if row else 0

    def subscription(self, subscription_id: str) -> dict[str, Any] | None:
        return self.b.sync_subscription(subscription_id)

    def appointment(self, appointment_id: str) -> dict[str, Any] | None:
        return self.b.c.row("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
