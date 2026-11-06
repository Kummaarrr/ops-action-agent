"""`SandboxBackend`: the Backend method surface over the real systems.

Reads go to the system of record and refresh the mirror; writes go to the system of record
first (which enforces its own rules - a fulfilled order cannot be re-addressed, a refund cannot
exceed the charge, a disputed charge cannot be refunded, an idempotency key replays) and then
the mirror. What has no counterpart in the real systems stays local and is documented as such:
installation appointments and slots, returns (Shopify's returns API is GraphQL-only; we tag the
order and record the RMA locally), the knowledge base and the policy text, the approval inbox,
and the verification ledger. The audit log is the mirror's, in the simulation's shape, so the
scorer reads it unchanged. No company policy here - that is the guard's job.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from opsagent.sandbox import mirror
from opsagent.sandbox.clients import ApiError, SandboxClients, clients_from_env
from opsagent.servers.backend import Backend, OperationError, _mask_email
from opsagent.sim.company import Company

TEAM_GROUPS = ("billing", "privacy", "installers", "supervisor", "logistics")


def _epoch(iso_text: str) -> int:
    d = dt.datetime.fromisoformat(iso_text)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())


class SandboxBackend:
    def __init__(self, company: Company, clients: SandboxClients) -> None:
        self.c = company  # the mirror
        self.clients = clients
        self.local = Backend(company)  # local-only systems and the shared plumbing

    @classmethod
    def from_env(cls, company: Company) -> SandboxBackend:
        return cls(company, clients_from_env())

    # ------------------------------------------------------------------ plumbing
    def _log(self, server: str, tool: str, kind: str, params: dict[str, Any], summary: str) -> None:
        self.c.audit(server, tool, kind, params, summary)

    def _refused(
        self, server: str, tool: str, params: dict[str, Any], e: ApiError
    ) -> OperationError:
        self._log(server, tool, "error", params, f"refused by {server}: {e.message[:200]}")
        return OperationError(e.message)

    # ------------------------------------------------------------------ id mapping
    def _stripe_customer(self, customer_id: str) -> str | None:
        key = f"stripe_customer:{customer_id}"
        cached = self.c.context(key)
        if cached:
            return cached
        cust = self.c.row(
            "SELECT email FROM customers WHERE id = ?", (customer_id,)
        ) or self.sync_customer(customer_id)
        if not cust:
            return None
        found = self.clients.stripe.find_customer_by_email(cust["email"])
        if not found:
            return None
        self.c.set_context(key, found["id"])
        self.c.set_context(f"shopify_customer:{found['id']}", customer_id)
        return str(found["id"])

    def _shopify_customer_for_stripe(self, stripe_customer: str | None) -> str | None:
        if not stripe_customer:
            return None
        cached = self.c.context(f"shopify_customer:{stripe_customer}")
        if cached:
            return cached
        try:
            sc = self.clients.stripe._get(f"/customers/{stripe_customer}")
        except ApiError:
            return None
        cid = (sc.get("metadata") or {}).get("shopify_customer_id")
        if not cid and sc.get("email"):
            hits = self.clients.shopify.search_customers(f"email:{sc['email']}")
            cid = str(hits[0]["id"]) if hits else None
        if cid:
            self.c.set_context(f"shopify_customer:{stripe_customer}", str(cid))
            self.c.set_context(f"stripe_customer:{cid}", stripe_customer)
            if not self.c.row("SELECT 1 FROM customers WHERE id = ?", (str(cid),)):
                self.sync_customer(str(cid))
        return str(cid) if cid else None

    # ------------------------------------------------------------------ sync (system -> mirror)
    def sync_customer(self, customer_id: str) -> dict[str, Any] | None:
        try:
            c = self.clients.shopify.get_customer(customer_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        row = mirror.customer_row(c)
        mirror.upsert(self.c, "customers", row)
        return self.c.row("SELECT * FROM customers WHERE id = ?", (row["id"],))

    def sync_orders(self, customer_id: str) -> list[dict[str, Any]]:
        if not self.c.row("SELECT 1 FROM customers WHERE id = ?", (customer_id,)):
            self.sync_customer(customer_id)
        for o in self.clients.shopify.customer_orders(customer_id):
            mirror.write_order(self.c, o)
        return self.c.rows(
            "SELECT id, created_at, status, total_cents, currency, fulfilled_at, delivered_at, tracking "
            "FROM orders WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )

    def sync_order(self, order_id: str) -> dict[str, Any] | None:
        try:
            o = self.clients.shopify.get_order(order_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        cid = str((o.get("customer") or {}).get("id"))
        if not self.c.row("SELECT 1 FROM customers WHERE id = ?", (cid,)):
            self.sync_customer(cid)
        row = mirror.write_order(self.c, o)
        return self.c.row("SELECT * FROM orders WHERE id = ?", (row["id"],))

    def sync_plans(self) -> list[dict[str, Any]]:
        for p in self.clients.stripe.list_prices():
            mirror.upsert(self.c, "plans", mirror.plan_row(p))
        return self.c.rows("SELECT * FROM plans ORDER BY price_cents")

    def sync_subscriptions(self, customer_id: str) -> list[dict[str, Any]]:
        sc = self._stripe_customer(customer_id)
        if sc:
            self.sync_plans()
            for s in self.clients.stripe.list_subscriptions(sc):
                mirror.upsert(self.c, "subscriptions", mirror.subscription_row(s, customer_id))
        return self.c.rows(
            "SELECT s.*, p.name AS plan_name, p.price_cents, p.interval, p.notice_days FROM subscriptions s "
            "JOIN plans p ON p.id = s.plan_id WHERE s.customer_id = ? ORDER BY s.started_at DESC",
            (customer_id,),
        )

    def sync_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        try:
            s = self.clients.stripe.get_subscription(subscription_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        cid = self._shopify_customer_for_stripe(s.get("customer"))
        if not cid:
            return None
        self.sync_plans()
        mirror.upsert(self.c, "subscriptions", mirror.subscription_row(s, cid))
        return self.c.row(
            "SELECT s.*, p.name AS plan_name, p.price_cents, p.interval, p.notice_days FROM subscriptions s "
            "JOIN plans p ON p.id = s.plan_id WHERE s.id = ?",
            (subscription_id,),
        )

    def _write_charge(self, ch: dict[str, Any], customer_id: str) -> dict[str, Any]:
        row = mirror.charge_row(ch, customer_id)
        oid, sid = row["order_id"], row["subscription_id"]
        if (
            oid
            and not self.c.row("SELECT 1 FROM orders WHERE id = ?", (oid,))
            and self.sync_order(oid) is None
        ):
            row["order_id"] = None  # the charge points at an order the store does not know
        if (
            sid
            and not self.c.row("SELECT 1 FROM subscriptions WHERE id = ?", (sid,))
            and self.sync_subscription(sid) is None
        ):
            row["subscription_id"] = None
        with self.c.transaction():
            mirror.upsert(self.c, "charges", row)
            for r in mirror.refund_rows(ch):
                existing = self.c.row(
                    "SELECT task_id, idempotency_key FROM refunds WHERE id = ?", (r["id"],)
                )
                if existing:  # keep what this process recorded (task id, idempotency key)
                    r["task_id"] = r["task_id"] or existing["task_id"]
                    r["idempotency_key"] = r["idempotency_key"] or existing["idempotency_key"]
                mirror.upsert(self.c, "refunds", r)
        return row

    def sync_charges(self, customer_id: str) -> list[dict[str, Any]]:
        sc = self._stripe_customer(customer_id)
        if sc:
            for ch in self.clients.stripe.list_charges(sc):
                self._write_charge(ch, customer_id)
        return self.c.rows(
            "SELECT id, order_id, subscription_id, amount_cents, created_at, status, refunded_cents, last4 "
            "FROM charges WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )

    def sync_charge(self, charge_id: str) -> dict[str, Any] | None:
        try:
            ch = self.clients.stripe.get_charge(charge_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        cid = self._shopify_customer_for_stripe(ch.get("customer"))
        if not cid:
            return None
        self._write_charge(ch, cid)
        return self.c.row("SELECT * FROM charges WHERE id = ?", (charge_id,))

    def sync_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        try:
            t = self.clients.helpdesk.get_ticket(ticket_id)
            user = self.clients.helpdesk.get_user(str(t["requester_id"]))
            comments = self.clients.helpdesk.comments(ticket_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        email = str(user.get("email") or "")
        cust = self.c.row("SELECT id FROM customers WHERE lower(email) = lower(?)", (email,))
        cid = cust["id"] if cust else None
        if cid is None and email:
            hits = self.clients.shopify.search_customers(f"email:{email}")
            if hits:
                synced = self.sync_customer(str(hits[0]["id"]))
                cid = synced["id"] if synced else None
        with self.c.transaction():
            mirror.upsert(self.c, "tickets", mirror.ticket_row(t, email, cid, comments))
            mirror.write_notes(self.c, str(t["id"]), comments)
        return self.c.row("SELECT * FROM tickets WHERE id = ?", (str(t["id"]),))

    def sync_customer_bundle(self, customer_id: str) -> None:
        """Everything the policy engine and the guard may need about one customer."""
        self.sync_customer(customer_id)
        self.sync_orders(customer_id)
        self.sync_subscriptions(customer_id)
        self.sync_charges(customer_id)

    # ================================================================== store
    def find_customer(
        self, email: str | None = None, order_id: str | None = None, phone: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"email": email, "order_id": order_id, "phone": phone}
        found: list[dict[str, Any]] = []
        if email:
            found = self.clients.shopify.search_customers(f"email:{email.strip()}")
        elif order_id:
            o = self.sync_order(order_id.strip())
            if o:
                c = self.sync_customer(o["customer_id"])
                found = (
                    [{"id": c["id"], "first_name": c["first_name"], "email": c["email"]}]
                    if c
                    else []
                )
        elif phone:
            digits = "".join(ch for ch in phone if ch.isdigit())
            if len(digits) >= 10:
                found = self.clients.shopify.search_customers(f"phone:{digits[-10:]}")
        out = []
        for c in found:
            if "default_address" in c:  # a Shopify record: refresh the mirror
                mirror.upsert(self.c, "customers", mirror.customer_row(c))
            out.append(
                {
                    "id": str(c["id"]),
                    "first_name": c.get("first_name", ""),
                    "email_masked": _mask_email(c["email"]),
                }
            )
        self._log("store", "find_customer", "read", params, f"{len(out)} match(es)")
        return out

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        row = self.sync_customer(customer_id)
        if row is None:
            raise OperationError(f"customer {customer_id} not found")
        self._log("store", "get_customer", "read", {"customer_id": customer_id}, row["email"])
        return row

    def verify_identity(
        self,
        ticket_id: str,
        customer_id: str,
        order_id: str | None = None,
        billing_zip: str | None = None,
        card_last4: str | None = None,
    ) -> dict[str, Any]:
        # the facts come from the systems of record; the comparison is the shared local logic
        if self.sync_customer(customer_id) is None:
            raise OperationError(f"customer {customer_id} not found")
        if not self.c.row("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)):
            self.sync_ticket(ticket_id)
        self.sync_orders(customer_id)
        self.sync_charges(customer_id)
        return self.local.verify_identity(ticket_id, customer_id, order_id, billing_zip, card_last4)

    def list_orders(self, customer_id: str) -> list[dict[str, Any]]:
        if self.sync_customer(customer_id) is None:
            raise OperationError(f"customer {customer_id} not found")
        rows = self.sync_orders(customer_id)
        self._log(
            "store", "list_orders", "read", {"customer_id": customer_id}, f"{len(rows)} orders"
        )
        return rows

    def get_order(self, order_id: str) -> dict[str, Any]:
        row = self.sync_order(order_id)
        if row is None:
            raise OperationError(f"order {order_id} not found")
        row["items"] = self.c.rows(
            "SELECT sku, title, qty, unit_cents FROM order_items WHERE order_id = ?", (order_id,)
        )
        self.sync_charges(row["customer_id"])
        row["charges"] = self.c.rows(
            "SELECT id, amount_cents, created_at, status, refunded_cents FROM charges WHERE order_id = ?",
            (order_id,),
        )
        self._log(
            "store",
            "get_order",
            "read",
            {"order_id": order_id},
            f"{row['status']} {row['customer_id']}",
        )
        return row

    def update_shipping_address(
        self, order_id: str, line1: str, city: str, state: str, zip: str
    ) -> dict[str, Any]:
        params = {"order_id": order_id, "line1": line1, "city": city, "state": state, "zip": zip}
        if self.sync_order(order_id) is None:
            raise OperationError(f"order {order_id} not found")
        try:
            o = self.clients.shopify.update_order(
                order_id,
                {
                    "shipping_address": {
                        "address1": line1,
                        "city": city,
                        "province_code": state,
                        "zip": zip,
                    }
                },
            )
        except ApiError as e:
            raise self._refused("store", "update_shipping_address", params, e) from e
        mirror.write_order(self.c, o)
        self._log("store", "update_shipping_address", "write", params, "updated")
        return {
            "order_id": order_id,
            "ship_line1": line1,
            "ship_city": city,
            "ship_state": state,
            "ship_zip": zip,
        }

    def cancel_order(self, order_id: str, reason: str) -> dict[str, Any]:
        params = {"order_id": order_id, "reason": reason}
        if self.sync_order(order_id) is None:
            raise OperationError(f"order {order_id} not found")
        try:
            o = self.clients.shopify.cancel_order(order_id, "customer")
        except ApiError as e:
            raise self._refused("store", "cancel_order", params, e) from e
        mirror.write_order(self.c, o)
        self._log("store", "cancel_order", "write", params, "cancelled")
        return {"order_id": order_id, "status": "cancelled"}

    def create_return(self, order_id: str, reason: str) -> dict[str, Any]:
        """Returns have no REST endpoint on Shopify: the order is tagged and the RMA is local."""
        params = {"order_id": order_id, "reason": reason}
        order = self.sync_order(order_id)
        if order is None:
            raise OperationError(f"order {order_id} not found")
        if order["status"] != "delivered":
            self._log(
                "store", "create_return", "error", params, f"refused: order is {order['status']}"
            )
            raise OperationError(
                f"order {order_id} is {order['status']}: returns are for delivered orders"
            )
        if self.c.row(
            "SELECT 1 FROM returns WHERE order_id = ? AND status != 'closed'", (order_id,)
        ):
            self._log("store", "create_return", "error", params, "refused: open return exists")
            raise OperationError(f"order {order_id} already has an open return")
        current = self.clients.shopify.get_order(order_id)
        tags = [t.strip() for t in str(current.get("tags") or "").split(",") if t.strip()]
        try:
            self.clients.shopify.update_order(
                order_id, {"tags": ", ".join([*tags, "return-requested"])}
            )
        except ApiError as e:
            raise self._refused("store", "create_return", params, e) from e
        rid = f"rma_{self.local._next(7)}"
        self.c.execute(
            "INSERT INTO returns(id, order_id, reason, status, created_at, task_id) VALUES (?,?,?,?,?,?)",
            (rid, order_id, reason, "label_sent", mirror.utc_now_iso(), self.c.task_id()),
        )
        self._log("store", "create_return", "write", params, f"{rid} label_sent")
        return {
            "return_id": rid,
            "order_id": order_id,
            "status": "label_sent",
            "refund": "on receipt at the warehouse",
        }

    # appointments and slots are not a store concept: local tables, same rules as the simulation
    def list_appointments(self, customer_id: str) -> list[dict[str, Any]]:
        return self.local.list_appointments(customer_id)

    def list_slots(self, from_date: str, to_date: str) -> list[dict[str, Any]]:
        return self.local.list_slots(from_date, to_date)

    def reschedule_appointment(self, appointment_id: str, slot_at: str) -> dict[str, Any]:
        return self.local.reschedule_appointment(appointment_id, slot_at)

    def cancel_appointment(self, appointment_id: str) -> dict[str, Any]:
        return self.local.cancel_appointment(appointment_id)

    # ================================================================== billing
    def list_charges(self, customer_id: str) -> list[dict[str, Any]]:
        if self.sync_customer(customer_id) is None:
            raise OperationError(f"customer {customer_id} not found")
        rows = self.sync_charges(customer_id)
        self._log(
            "billing", "list_charges", "read", {"customer_id": customer_id}, f"{len(rows)} charges"
        )
        return rows

    def get_charge(self, charge_id: str) -> dict[str, Any]:
        row = self.sync_charge(charge_id)
        if row is None:
            raise OperationError(f"charge {charge_id} not found")
        row["refunds"] = self.c.rows(
            "SELECT id, amount_cents, reason, created_at FROM refunds WHERE charge_id = ?",
            (charge_id,),
        )
        self._log(
            "billing",
            "get_charge",
            "read",
            {"charge_id": charge_id},
            f"{row['status']} {row['amount_cents']}",
        )
        return row

    def create_refund(
        self, charge_id: str, amount_cents: int, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        params = {
            "charge_id": charge_id,
            "amount_cents": amount_cents,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        replay = self.c.row("SELECT id FROM refunds WHERE idempotency_key = ?", (idempotency_key,))
        if self.sync_charge(charge_id) is None:
            raise OperationError(f"charge {charge_id} not found")
        try:
            r = self.clients.stripe.create_refund(
                charge_id,
                amount_cents,
                idempotency_key,
                {
                    "reason": reason[:450],
                    "idempotency_key": idempotency_key,
                    "task_id": self.c.task_id(),
                },
            )
        except ApiError as e:
            raise self._refused("billing", "create_refund", params, e) from e
        charge = self.sync_charge(charge_id) or {}
        self.c.execute(
            "UPDATE refunds SET idempotency_key = ?, task_id = coalesce(task_id, ?) WHERE id = ?",
            (idempotency_key, self.c.task_id(), str(r["id"])),
        )
        if replay:
            self._log(
                "billing", "create_refund", "write", params, f"idempotent replay of {r['id']}"
            )
            return {
                **(self.c.row("SELECT * FROM refunds WHERE id = ?", (str(r["id"]),)) or {}),
                "replayed": True,
            }
        self._log("billing", "create_refund", "write", params, f"{r['id']} {amount_cents}")
        return {
            "id": str(r["id"]),
            "charge_id": charge_id,
            "amount_cents": int(r["amount"]),
            "status": str(r.get("status", "succeeded")),
            "charge_status": charge.get("status", "partially_refunded"),
        }

    def list_plans(self) -> list[dict[str, Any]]:
        rows = self.sync_plans()
        self._log("billing", "list_plans", "read", {}, f"{len(rows)} plans")
        return rows

    def list_subscriptions(self, customer_id: str) -> list[dict[str, Any]]:
        if self.sync_customer(customer_id) is None:
            raise OperationError(f"customer {customer_id} not found")
        rows = self.sync_subscriptions(customer_id)
        self._log(
            "billing",
            "list_subscriptions",
            "read",
            {"customer_id": customer_id},
            f"{len(rows)} subscriptions",
        )
        return rows

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        row = self.sync_subscription(subscription_id)
        if row is None:
            raise OperationError(f"subscription {subscription_id} not found")
        self._log(
            "billing",
            "get_subscription",
            "read",
            {"subscription_id": subscription_id},
            f"{row['status']} {row['plan_id']}",
        )
        return row

    def preview_plan_change(self, subscription_id: str, plan_id: str) -> dict[str, Any]:
        if self.sync_subscription(subscription_id) is None:
            raise OperationError(f"subscription {subscription_id} not found")
        return self.local.preview_plan_change(subscription_id, plan_id)

    def pause_subscription(self, subscription_id: str, resume_at: str) -> dict[str, Any]:
        params = {"subscription_id": subscription_id, "resume_at": resume_at}
        sub = self.sync_subscription(subscription_id)
        if sub is None:
            raise OperationError(f"subscription {subscription_id} not found")
        if sub["status"] != "active":
            self._log("billing", "pause_subscription", "error", params, f"refused: {sub['status']}")
            raise OperationError(
                f"subscription {subscription_id} is {sub['status']}; only active subscriptions can be paused"
            )
        try:
            resumes = _epoch(resume_at if "T" in resume_at else f"{resume_at}T00:00:00")
        except ValueError as e:
            raise OperationError("resume_at must be an ISO date") from e
        try:
            self.clients.stripe.update_subscription(
                subscription_id,
                {
                    "pause_collection": {"behavior": "void", "resumes_at": resumes},
                    "metadata": {"pauses_this_year": int(sub["pauses_this_year"]) + 1},
                },
            )
        except ApiError as e:
            raise self._refused("billing", "pause_subscription", params, e) from e
        self.sync_subscription(subscription_id)
        self._log("billing", "pause_subscription", "write", params, "paused")
        return {"subscription_id": subscription_id, "status": "paused", "paused_until": resume_at}

    def resume_subscription(self, subscription_id: str) -> dict[str, Any]:
        sub = self.sync_subscription(subscription_id)
        if sub is None:
            raise OperationError(f"subscription {subscription_id} not found")
        if sub["status"] != "paused":
            raise OperationError(f"subscription {subscription_id} is {sub['status']}")
        try:
            self.clients.stripe.update_subscription(subscription_id, {"pause_collection": ""})
        except ApiError as e:
            raise self._refused(
                "billing", "resume_subscription", {"subscription_id": subscription_id}, e
            ) from e
        self.sync_subscription(subscription_id)
        self._log(
            "billing",
            "resume_subscription",
            "write",
            {"subscription_id": subscription_id},
            "resumed",
        )
        return {"subscription_id": subscription_id, "status": "active"}

    def cancel_subscription(
        self, subscription_id: str, at_period_end: bool = True
    ) -> dict[str, Any]:
        params = {"subscription_id": subscription_id, "at_period_end": at_period_end}
        sub = self.sync_subscription(subscription_id)
        if sub is None:
            raise OperationError(f"subscription {subscription_id} not found")
        if sub["status"] == "cancelled":
            self._log(
                "billing", "cancel_subscription", "error", params, "refused: already cancelled"
            )
            raise OperationError(f"subscription {subscription_id} is already cancelled")
        try:
            if at_period_end:
                self.clients.stripe.update_subscription(
                    subscription_id, {"cancel_at_period_end": True}
                )
            else:
                self.clients.stripe.cancel_subscription(subscription_id)
        except ApiError as e:
            raise self._refused("billing", "cancel_subscription", params, e) from e
        row = self.sync_subscription(subscription_id) or {}
        self._log(
            "billing", "cancel_subscription", "write", params, f"cancel_at {row.get('cancel_at')}"
        )
        return {
            "subscription_id": subscription_id,
            "status": "cancelled",
            "cancel_at": row.get("cancel_at"),
        }

    def change_plan(self, subscription_id: str, plan_id: str) -> dict[str, Any]:
        params = {"subscription_id": subscription_id, "plan_id": plan_id}
        sub = self.sync_subscription(subscription_id)
        if sub is None:
            raise OperationError(f"subscription {subscription_id} not found")
        plan = self.c.row("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if plan is None:
            raise OperationError(f"plan {plan_id} not found")
        if sub["status"] != "active":
            raise OperationError(f"subscription {subscription_id} is {sub['status']}")
        if plan["interval"] != sub["interval"]:
            raise OperationError("plan changes must stay within the same billing interval")
        pro = self.local._proration(sub, plan)
        if pro["kind"] == "upgrade":
            live = self.clients.stripe.get_subscription(subscription_id)
            item_id = live["items"]["data"][0]["id"]
            try:
                self.clients.stripe.update_subscription(
                    subscription_id,
                    {
                        "items": [{"id": item_id, "price": plan_id}],
                        "proration_behavior": "always_invoice",
                    },
                )
            except ApiError as e:
                raise self._refused("billing", "change_plan", params, e) from e
            self.sync_subscription(subscription_id)
        else:
            try:
                self.clients.stripe.update_subscription(
                    subscription_id, {"metadata": {"pending_plan": plan_id}}
                )
            except ApiError as e:
                raise self._refused("billing", "change_plan", params, e) from e
            self.c.set_context(f"pending_plan:{subscription_id}", plan_id)
        self._log(
            "billing",
            "change_plan",
            "write",
            params,
            f"{pro['kind']} charge {pro['prorated_charge_cents']}",
        )
        return {
            "subscription_id": subscription_id,
            "plan_id": plan_id if pro["kind"] == "upgrade" else sub["plan_id"],
            **pro,
        }

    # ================================================================== helpdesk
    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        row = self.sync_ticket(ticket_id)
        if row is None:
            raise OperationError(f"ticket {ticket_id} not found")
        self._log("helpdesk", "get_ticket", "read", {"ticket_id": ticket_id}, row["subject"])
        return row

    def list_tickets(self, customer_id: str) -> list[dict[str, Any]]:
        cust = self.sync_customer(customer_id)
        if cust is None:
            raise OperationError(f"customer {customer_id} not found")
        for t in self.clients.helpdesk.search_tickets(f"type:ticket requester:{cust['email']}"):
            self.sync_ticket(str(t["id"]))
        rows = self.c.rows(
            "SELECT id, subject, status, created_at, tags FROM tickets WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        self._log(
            "helpdesk", "list_tickets", "read", {"customer_id": customer_id}, f"{len(rows)} tickets"
        )
        return rows

    def list_notes(self, ticket_id: str) -> list[dict[str, Any]]:
        if self.sync_ticket(ticket_id) is None:
            raise OperationError(f"ticket {ticket_id} not found")
        rows = self.c.rows(
            "SELECT author, body, created_at FROM ticket_notes WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
        self._log("helpdesk", "list_notes", "read", {"ticket_id": ticket_id}, f"{len(rows)} notes")
        return rows

    def _ticket_exists(self, ticket_id: str) -> None:
        if (
            not self.c.row("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,))
            and self.sync_ticket(ticket_id) is None
        ):
            raise OperationError(f"ticket {ticket_id} not found")

    def add_internal_note(self, ticket_id: str, body: str) -> dict[str, Any]:
        self._ticket_exists(ticket_id)
        try:
            self.clients.helpdesk.update_ticket(
                ticket_id, {"comment": {"body": body, "public": False}}
            )
        except ApiError as e:
            raise self._refused("helpdesk", "add_internal_note", {"ticket_id": ticket_id}, e) from e
        self.c.execute(
            "INSERT INTO ticket_notes(ticket_id, author, body, created_at, task_id) VALUES (?,?,?,?,?)",
            (ticket_id, "agent", body, mirror.utc_now_iso(), self.c.task_id()),
        )
        self._log(
            "helpdesk",
            "add_internal_note",
            "write",
            {"ticket_id": ticket_id, "body": body},
            "note added",
        )
        return {"ticket_id": ticket_id, "note": "added"}

    def reply_to_customer(self, ticket_id: str, body: str) -> dict[str, Any]:
        self._ticket_exists(ticket_id)
        try:
            self.clients.helpdesk.update_ticket(
                ticket_id, {"comment": {"body": body, "public": True}, "status": "pending"}
            )
        except ApiError as e:
            raise self._refused("helpdesk", "reply_to_customer", {"ticket_id": ticket_id}, e) from e
        with self.c.transaction():
            self.c.execute(
                "INSERT INTO ticket_replies(ticket_id, body, created_at, task_id) VALUES (?,?,?,?)",
                (ticket_id, body, mirror.utc_now_iso(), self.c.task_id()),
            )
            self.c.execute(
                "UPDATE tickets SET status = 'pending' WHERE id = ? AND status = 'open'",
                (ticket_id,),
            )
        self._log(
            "helpdesk",
            "reply_to_customer",
            "write",
            {"ticket_id": ticket_id, "body": body},
            "reply sent",
        )
        return {"ticket_id": ticket_id, "reply": "sent"}

    def set_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        if status not in ("open", "pending", "solved", "escalated"):
            raise OperationError(f"unknown status {status}")
        self._ticket_exists(ticket_id)
        fields: dict[str, Any] = (
            {"status": "open", "additional_tags": ["escalated"]}
            if status == "escalated"
            else {"status": status}
        )
        try:
            self.clients.helpdesk.update_ticket(ticket_id, fields)
        except ApiError as e:
            raise self._refused(
                "helpdesk", "set_status", {"ticket_id": ticket_id, "status": status}, e
            ) from e
        self.c.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
        self._log(
            "helpdesk", "set_status", "write", {"ticket_id": ticket_id, "status": status}, status
        )
        return {"ticket_id": ticket_id, "status": status}

    def escalate(self, ticket_id: str, team: str, reason: str) -> dict[str, Any]:
        params = {"ticket_id": ticket_id, "team": team, "reason": reason}
        self._ticket_exists(ticket_id)
        group = next(
            (g for g in self.clients.helpdesk.groups() if g["name"].lower() == team.lower()), None
        )
        fields: dict[str, Any] = {
            "comment": {"body": f"Escalated to {team}: {reason}", "public": False},
            "additional_tags": [f"escalated:{team}"],
            "status": "open",
        }
        if group:
            fields["group_id"] = group["id"]
        try:
            self.clients.helpdesk.update_ticket(ticket_id, fields)
        except ApiError as e:
            raise self._refused("helpdesk", "escalate", params, e) from e
        with self.c.transaction():
            self.c.execute(
                "UPDATE tickets SET status = 'escalated', tags = tags || ',escalated:' || ? WHERE id = ?",
                (team, ticket_id),
            )
            self.c.execute(
                "INSERT INTO ticket_notes(ticket_id, author, body, created_at, task_id) VALUES (?,?,?,?,?)",
                (
                    ticket_id,
                    "agent",
                    f"Escalated to {team}: {reason}",
                    mirror.utc_now_iso(),
                    self.c.task_id(),
                ),
            )
        self._log(
            "helpdesk",
            "escalate",
            "write",
            params,
            f"escalated to {team}" + ("" if group else " (no such group; tagged)"),
        )
        return {"ticket_id": ticket_id, "status": "escalated", "team": team}

    # ================================================================== kb and approvals: local
    def search_articles(self, query: str) -> list[dict[str, Any]]:
        return self.local.search_articles(query)

    def get_article(self, article_id: str) -> dict[str, Any]:
        return self.local.get_article(article_id)

    def get_policy(self) -> str:
        return self.local.get_policy()

    def request_approval(
        self,
        ticket_id: str,
        action: str,
        params_json: str,
        diff_json: str,
        clause: str,
        summary: str,
    ) -> dict[str, Any]:
        json.loads(params_json)  # same validation as the simulation (raises for the model)
        return self.local.request_approval(
            ticket_id, action, params_json, diff_json, clause, summary
        )

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        return self.local.get_approval(approval_id)
