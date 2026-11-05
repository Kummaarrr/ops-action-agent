"""The simulated systems behind the MCP servers: what a Shopify-like store, a Stripe-like
billing system and a Zendesk-like helpdesk actually do and refuse.

Only *system* rules live here (a fulfilled order's address cannot be edited; a refund cannot
exceed the remaining amount; a disputed charge cannot be refunded; a full slot cannot be booked).
The company *policy* (windows, approvals, identity) is the agent's job - deliberately, so the
baselines can violate it the way a careless integration would. Every call is written to the
audit log with the current task id.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from opsagent.config import POLICY_DIR
from opsagent.sim.company import Company


class OperationError(Exception):
    """What the system says when it refuses or cannot find something (shown to the model)."""


def _iso(d: dt.datetime) -> str:
    return d.isoformat(timespec="seconds")


def _parse(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


class Backend:
    def __init__(self, company: Company) -> None:
        self.c = company

    # ------------------------------------------------------------------ plumbing
    def _next(self, slot: int) -> int:
        seq = [int(x) for x in self.c.context("seq").split(",")]
        value = seq[slot]
        seq[slot] = value + 1
        self.c.set_context("seq", ",".join(map(str, seq)))
        return value

    def _log(self, server: str, tool: str, kind: str, params: dict[str, Any], summary: str) -> None:
        self.c.audit(server, tool, kind, params, summary)

    def _need(self, row: dict[str, Any] | None, what: str, id_: str) -> dict[str, Any]:
        if row is None:
            raise OperationError(f"{what} {id_} not found")
        return row

    # ================================================================== store
    def find_customer(
        self, email: str | None = None, order_id: str | None = None, phone: str | None = None
    ) -> list[dict[str, Any]]:
        """Matches by exact email, order id or full phone number. Returns the customer id and a
        masked email only: verification, not this lookup, unlocks the account."""
        params = {"email": email, "order_id": order_id, "phone": phone}
        rows: list[dict[str, Any]] = []
        if email:
            rows = self.c.rows(
                "SELECT id, first_name, email FROM customers WHERE lower(email) = lower(?)",
                (email.strip(),),
            )
        elif order_id:
            rows = self.c.rows(
                "SELECT c.id, c.first_name, c.email FROM customers c "
                "JOIN orders o ON o.customer_id = c.id WHERE o.id = ?",
                (order_id.strip(),),
            )
        elif phone:
            digits = "".join(ch for ch in phone if ch.isdigit())
            if len(digits) >= 10:
                rows = self.c.rows(
                    "SELECT id, first_name, email FROM customers WHERE replace(phone, '-', '') = ?",
                    (digits[-10:],),
                )
        out = [
            {"id": r["id"], "first_name": r["first_name"], "email_masked": _mask_email(r["email"])}
            for r in rows
        ]
        self._log("store", "find_customer", "read", params, f"{len(out)} match(es)")
        return out

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        row = self._need(
            self.c.row("SELECT * FROM customers WHERE id = ?", (customer_id,)),
            "customer",
            customer_id,
        )
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
        """Policy P1.2. The requester's email is the ticket's sender address (read by the
        system, never supplied by the caller). Level 1 = the sender is the account's email;
        level 2 = level 1 plus one more fact (an order id on the account, the billing zip, the
        card's last four). Returns the level and which facts matched; never the stored values."""
        cust = self._need(
            self.c.row("SELECT * FROM customers WHERE id = ?", (customer_id,)),
            "customer",
            customer_id,
        )
        ticket = self._need(
            self.c.row("SELECT requester_email, channel FROM tickets WHERE id = ?", (ticket_id,)),
            "ticket",
            ticket_id,
        )
        email_ok = ticket["requester_email"].strip().lower() == cust["email"].lower()
        matched: list[str] = ["email"] if email_ok else []
        if email_ok:  # extra facts only count on top of a matching sender
            if order_id and self.c.row(
                "SELECT 1 FROM orders WHERE id = ? AND customer_id = ?",
                (order_id.strip(), customer_id),
            ):
                matched.append("order_id")
            if billing_zip and billing_zip.strip() == cust["billing_zip"]:
                matched.append("billing_zip")
            if card_last4 and self.c.row(
                "SELECT 1 FROM charges WHERE customer_id = ? AND last4 = ?",
                (customer_id, card_last4.strip()),
            ):
                matched.append("card_last4")
        level = 2 if email_ok and len(matched) >= 2 else 1 if email_ok else 0
        self.c.execute(
            "INSERT INTO verifications(ticket_id, customer_id, method, verified, created_at, task_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                ticket_id,
                customer_id,
                "+".join(matched) or "none",
                level,
                _iso(self.c.now()),
                self.c.task_id(),
            ),
        )
        self._log(
            "store",
            "verify_identity",
            "read",
            {
                "ticket_id": ticket_id,
                "customer_id": customer_id,
                "facts": sorted(
                    k
                    for k, v in {
                        "order_id": order_id,
                        "billing_zip": billing_zip,
                        "card_last4": card_last4,
                    }.items()
                    if v
                ),
            },
            f"level={level} matched={matched}",
        )
        return {"level": level, "verified": level >= 1, "matched": matched}

    def list_orders(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self.c.rows(
            "SELECT id, created_at, status, total_cents, currency, fulfilled_at, delivered_at, tracking "
            "FROM orders WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        self._log(
            "store", "list_orders", "read", {"customer_id": customer_id}, f"{len(rows)} orders"
        )
        return rows

    def get_order(self, order_id: str) -> dict[str, Any]:
        row = self._need(
            self.c.row("SELECT * FROM orders WHERE id = ?", (order_id,)), "order", order_id
        )
        row["items"] = self.c.rows(
            "SELECT sku, title, qty, unit_cents FROM order_items WHERE order_id = ?", (order_id,)
        )
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
        order = self._need(
            self.c.row("SELECT * FROM orders WHERE id = ?", (order_id,)), "order", order_id
        )
        if order["status"] != "paid":
            self._log(
                "store",
                "update_shipping_address",
                "error",
                params,
                f"refused: order is {order['status']}",
            )
            raise OperationError(
                f"order {order_id} is {order['status']}: the shipping address can only be edited "
                "before fulfilment"
            )
        self.c.execute(
            "UPDATE orders SET ship_line1 = ?, ship_city = ?, ship_state = ?, ship_zip = ? WHERE id = ?",
            (line1, city, state, zip, order_id),
        )
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
        order = self._need(
            self.c.row("SELECT * FROM orders WHERE id = ?", (order_id,)), "order", order_id
        )
        if order["status"] != "paid":
            self._log(
                "store", "cancel_order", "error", params, f"refused: order is {order['status']}"
            )
            raise OperationError(
                f"order {order_id} is {order['status']}: only unfulfilled (paid) orders can be cancelled"
            )
        self.c.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
        self._log("store", "cancel_order", "write", params, "cancelled")
        return {"order_id": order_id, "status": "cancelled"}

    def create_return(self, order_id: str, reason: str) -> dict[str, Any]:
        """Open a return (RMA) for a delivered order and email the label; the refund follows when
        the warehouse receives the item (P3.7)."""
        params = {"order_id": order_id, "reason": reason}
        order = self._need(
            self.c.row("SELECT * FROM orders WHERE id = ?", (order_id,)), "order", order_id
        )
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
        rid = f"rma_{self._next(7)}"
        self.c.execute(
            "INSERT INTO returns(id, order_id, reason, status, created_at, task_id) VALUES (?,?,?,?,?,?)",
            (rid, order_id, reason, "label_sent", _iso(self.c.now()), self.c.task_id()),
        )
        self._log("store", "create_return", "write", params, f"{rid} label_sent")
        return {
            "return_id": rid,
            "order_id": order_id,
            "status": "label_sent",
            "refund": "on receipt at the warehouse",
        }

    def list_appointments(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self.c.rows(
            "SELECT * FROM appointments WHERE customer_id = ? ORDER BY slot_at", (customer_id,)
        )
        self._log(
            "store",
            "list_appointments",
            "read",
            {"customer_id": customer_id},
            f"{len(rows)} appointments",
        )
        return rows

    def list_slots(self, from_date: str, to_date: str) -> list[dict[str, Any]]:
        rows = self.c.rows(
            "SELECT slot_at, capacity - booked AS open FROM slots WHERE slot_at >= ? AND slot_at <= ? "
            "AND capacity > booked ORDER BY slot_at",
            (from_date, to_date + "T23:59:59" if len(to_date) == 10 else to_date),
        )
        self._log(
            "store",
            "list_slots",
            "read",
            {"from_date": from_date, "to_date": to_date},
            f"{len(rows)} open slots",
        )
        return rows

    def reschedule_appointment(self, appointment_id: str, slot_at: str) -> dict[str, Any]:
        params = {"appointment_id": appointment_id, "slot_at": slot_at}
        appt = self._need(
            self.c.row("SELECT * FROM appointments WHERE id = ?", (appointment_id,)),
            "appointment",
            appointment_id,
        )
        if appt["status"] != "scheduled":
            self._log(
                "store", "reschedule_appointment", "error", params, f"refused: {appt['status']}"
            )
            raise OperationError(f"appointment {appointment_id} is {appt['status']}")
        slot = self.c.row("SELECT * FROM slots WHERE slot_at = ?", (slot_at,))
        if slot is None or slot["booked"] >= slot["capacity"]:
            self._log(
                "store", "reschedule_appointment", "error", params, "refused: slot unavailable"
            )
            raise OperationError(f"slot {slot_at} is not available")
        with self.c.transaction():
            self.c.execute(
                "UPDATE slots SET booked = booked - 1 WHERE slot_at = ? AND booked > 0",
                (appt["slot_at"],),
            )
            self.c.execute("UPDATE slots SET booked = booked + 1 WHERE slot_at = ?", (slot_at,))
            self.c.execute(
                "UPDATE appointments SET slot_at = ? WHERE id = ?", (slot_at, appointment_id)
            )
        self._log(
            "store", "reschedule_appointment", "write", params, f"moved from {appt['slot_at']}"
        )
        return {
            "appointment_id": appointment_id,
            "slot_at": slot_at,
            "previous_slot_at": appt["slot_at"],
        }

    def cancel_appointment(self, appointment_id: str) -> dict[str, Any]:
        params = {"appointment_id": appointment_id}
        appt = self._need(
            self.c.row("SELECT * FROM appointments WHERE id = ?", (appointment_id,)),
            "appointment",
            appointment_id,
        )
        if appt["status"] != "scheduled":
            raise OperationError(f"appointment {appointment_id} is {appt['status']}")
        with self.c.transaction():
            self.c.execute(
                "UPDATE slots SET booked = booked - 1 WHERE slot_at = ? AND booked > 0",
                (appt["slot_at"],),
            )
            self.c.execute(
                "UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,)
            )
        self._log("store", "cancel_appointment", "write", params, "cancelled")
        return {"appointment_id": appointment_id, "status": "cancelled"}

    # ================================================================== billing
    def list_charges(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self.c.rows(
            "SELECT id, order_id, subscription_id, amount_cents, created_at, status, refunded_cents, last4 "
            "FROM charges WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        self._log(
            "billing", "list_charges", "read", {"customer_id": customer_id}, f"{len(rows)} charges"
        )
        return rows

    def get_charge(self, charge_id: str) -> dict[str, Any]:
        row = self._need(
            self.c.row("SELECT * FROM charges WHERE id = ?", (charge_id,)), "charge", charge_id
        )
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
        existing = self.c.row("SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,))
        if existing:
            self._log(
                "billing",
                "create_refund",
                "write",
                params,
                f"idempotent replay of {existing['id']}",
            )
            return {**existing, "replayed": True}
        charge = self._need(
            self.c.row("SELECT * FROM charges WHERE id = ?", (charge_id,)), "charge", charge_id
        )
        remaining = charge["amount_cents"] - charge["refunded_cents"]
        if charge["status"] == "disputed":
            self._log("billing", "create_refund", "error", params, "refused: charge is disputed")
            raise OperationError(f"charge {charge_id} is under dispute and cannot be refunded")
        if amount_cents <= 0 or amount_cents > remaining:
            self._log(
                "billing", "create_refund", "error", params, f"refused: remaining {remaining}"
            )
            raise OperationError(
                f"refund amount {amount_cents} exceeds the remaining refundable amount {remaining} on {charge_id}"
            )
        rid = f"re_{self._next(3)}"
        new_refunded = charge["refunded_cents"] + amount_cents
        status = "refunded" if new_refunded == charge["amount_cents"] else "partially_refunded"
        with self.c.transaction():
            self.c.execute(
                "INSERT INTO refunds(id, charge_id, amount_cents, reason, idempotency_key, created_at, task_id) VALUES (?,?,?,?,?,?,?)",
                (
                    rid,
                    charge_id,
                    amount_cents,
                    reason,
                    idempotency_key,
                    _iso(self.c.now()),
                    self.c.task_id(),
                ),
            )
            self.c.execute(
                "UPDATE charges SET refunded_cents = ?, status = ? WHERE id = ?",
                (new_refunded, status, charge_id),
            )
        self._log("billing", "create_refund", "write", params, f"{rid} {amount_cents}")
        return {
            "id": rid,
            "charge_id": charge_id,
            "amount_cents": amount_cents,
            "status": "succeeded",
            "charge_status": status,
        }

    def list_plans(self) -> list[dict[str, Any]]:
        rows = self.c.rows("SELECT * FROM plans ORDER BY price_cents")
        self._log("billing", "list_plans", "read", {}, f"{len(rows)} plans")
        return rows

    def list_subscriptions(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self.c.rows(
            "SELECT s.*, p.name AS plan_name, p.price_cents, p.interval, p.notice_days FROM subscriptions s "
            "JOIN plans p ON p.id = s.plan_id WHERE s.customer_id = ? ORDER BY s.started_at DESC",
            (customer_id,),
        )
        self._log(
            "billing",
            "list_subscriptions",
            "read",
            {"customer_id": customer_id},
            f"{len(rows)} subscriptions",
        )
        return rows

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        row = self._need(
            self.c.row(
                "SELECT s.*, p.name AS plan_name, p.price_cents, p.interval, p.notice_days FROM subscriptions s "
                "JOIN plans p ON p.id = s.plan_id WHERE s.id = ?",
                (subscription_id,),
            ),
            "subscription",
            subscription_id,
        )
        self._log(
            "billing",
            "get_subscription",
            "read",
            {"subscription_id": subscription_id},
            f"{row['status']} {row['plan_id']}",
        )
        return row

    def _proration(self, sub: dict[str, Any], new_plan: dict[str, Any]) -> dict[str, Any]:
        old_price = int(sub["price_cents"])
        new_price = int(new_plan["price_cents"])
        start, end = _parse(sub["current_period_start"]), _parse(sub["current_period_end"])
        total_days = max(1, (end - start).days)
        remaining = max(0, (end - self.c.now()).days)
        if new_price > old_price:
            charge = round((new_price - old_price) * remaining / total_days)
            return {
                "kind": "upgrade",
                "effective": "now",
                "prorated_charge_cents": charge,
                "remaining_days": remaining,
            }
        return {
            "kind": "downgrade",
            "effective": sub["current_period_end"],
            "prorated_charge_cents": 0,
            "remaining_days": remaining,
        }

    def preview_plan_change(self, subscription_id: str, plan_id: str) -> dict[str, Any]:
        sub = self._sub(subscription_id)
        plan = self._need(
            self.c.row("SELECT * FROM plans WHERE id = ?", (plan_id,)), "plan", plan_id
        )
        if plan["interval"] != sub["interval"]:
            raise OperationError(
                "plan changes must stay within the same billing interval (monthly <-> monthly, annual <-> annual)"
            )
        out = {
            "subscription_id": subscription_id,
            "from_plan": sub["plan_id"],
            "to_plan": plan_id,
            **self._proration(sub, plan),
        }
        self._log(
            "billing",
            "preview_plan_change",
            "read",
            {"subscription_id": subscription_id, "plan_id": plan_id},
            f"{out['kind']} {out['prorated_charge_cents']}",
        )
        return out

    def _sub(self, subscription_id: str) -> dict[str, Any]:
        return self._need(
            self.c.row(
                "SELECT s.*, p.name AS plan_name, p.price_cents, p.interval, p.notice_days FROM subscriptions s "
                "JOIN plans p ON p.id = s.plan_id WHERE s.id = ?",
                (subscription_id,),
            ),
            "subscription",
            subscription_id,
        )

    def pause_subscription(self, subscription_id: str, resume_at: str) -> dict[str, Any]:
        params = {"subscription_id": subscription_id, "resume_at": resume_at}
        sub = self._sub(subscription_id)
        if sub["status"] != "active":
            self._log("billing", "pause_subscription", "error", params, f"refused: {sub['status']}")
            raise OperationError(
                f"subscription {subscription_id} is {sub['status']}; only active subscriptions can be paused"
            )
        with self.c.transaction():
            self.c.execute(
                "UPDATE subscriptions SET status = 'paused', paused_until = ?, pauses_this_year = pauses_this_year + 1 WHERE id = ?",
                (resume_at, subscription_id),
            )
        self._log("billing", "pause_subscription", "write", params, "paused")
        return {"subscription_id": subscription_id, "status": "paused", "paused_until": resume_at}

    def resume_subscription(self, subscription_id: str) -> dict[str, Any]:
        sub = self._sub(subscription_id)
        if sub["status"] != "paused":
            raise OperationError(f"subscription {subscription_id} is {sub['status']}")
        self.c.execute(
            "UPDATE subscriptions SET status = 'active', paused_until = NULL WHERE id = ?",
            (subscription_id,),
        )
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
        sub = self._sub(subscription_id)
        if sub["status"] == "cancelled":
            self._log(
                "billing", "cancel_subscription", "error", params, "refused: already cancelled"
            )
            raise OperationError(f"subscription {subscription_id} is already cancelled")
        cancel_at = sub["current_period_end"] if at_period_end else _iso(self.c.now())
        self.c.execute(
            "UPDATE subscriptions SET status = 'cancelled', cancel_at = ? WHERE id = ?",
            (cancel_at, subscription_id),
        )
        self._log("billing", "cancel_subscription", "write", params, f"cancel_at {cancel_at}")
        return {"subscription_id": subscription_id, "status": "cancelled", "cancel_at": cancel_at}

    def change_plan(self, subscription_id: str, plan_id: str) -> dict[str, Any]:
        params = {"subscription_id": subscription_id, "plan_id": plan_id}
        sub = self._sub(subscription_id)
        plan = self._need(
            self.c.row("SELECT * FROM plans WHERE id = ?", (plan_id,)), "plan", plan_id
        )
        if sub["status"] != "active":
            raise OperationError(f"subscription {subscription_id} is {sub['status']}")
        if plan["interval"] != sub["interval"]:
            raise OperationError("plan changes must stay within the same billing interval")
        pro = self._proration(sub, plan)
        with self.c.transaction():
            if pro["kind"] == "upgrade":
                self.c.execute(
                    "UPDATE subscriptions SET plan_id = ? WHERE id = ?", (plan_id, subscription_id)
                )
                if pro["prorated_charge_cents"] > 0:
                    last4 = self.c.row(
                        "SELECT last4 FROM charges WHERE subscription_id = ? ORDER BY created_at DESC",
                        (subscription_id,),
                    )
                    self.c.execute(
                        "INSERT INTO charges(id, customer_id, order_id, subscription_id, amount_cents, created_at, status, refunded_cents, last4) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            f"ch_{self._next(1)}",
                            sub["customer_id"],
                            None,
                            subscription_id,
                            pro["prorated_charge_cents"],
                            _iso(self.c.now()),
                            "succeeded",
                            0,
                            last4["last4"] if last4 else "0000",
                        ),
                    )
            else:  # downgrade takes effect at period end: recorded as the plan to switch to
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
        row = self._need(
            self.c.row("SELECT * FROM tickets WHERE id = ?", (ticket_id,)), "ticket", ticket_id
        )
        self._log("helpdesk", "get_ticket", "read", {"ticket_id": ticket_id}, row["subject"])
        return row

    def list_tickets(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self.c.rows(
            "SELECT id, subject, status, created_at, tags FROM tickets WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        self._log(
            "helpdesk", "list_tickets", "read", {"customer_id": customer_id}, f"{len(rows)} tickets"
        )
        return rows

    def list_notes(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self.c.rows(
            "SELECT author, body, created_at FROM ticket_notes WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
        self._log("helpdesk", "list_notes", "read", {"ticket_id": ticket_id}, f"{len(rows)} notes")
        return rows

    def add_internal_note(self, ticket_id: str, body: str) -> dict[str, Any]:
        self._need(
            self.c.row("SELECT id FROM tickets WHERE id = ?", (ticket_id,)), "ticket", ticket_id
        )
        self.c.execute(
            "INSERT INTO ticket_notes(ticket_id, author, body, created_at, task_id) VALUES (?,?,?,?,?)",
            (ticket_id, "agent", body, _iso(self.c.now()), self.c.task_id()),
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
        self._need(
            self.c.row("SELECT id FROM tickets WHERE id = ?", (ticket_id,)), "ticket", ticket_id
        )
        with self.c.transaction():
            self.c.execute(
                "INSERT INTO ticket_replies(ticket_id, body, created_at, task_id) VALUES (?,?,?,?)",
                (ticket_id, body, _iso(self.c.now()), self.c.task_id()),
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
        self._need(
            self.c.row("SELECT id FROM tickets WHERE id = ?", (ticket_id,)), "ticket", ticket_id
        )
        self.c.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
        self._log(
            "helpdesk", "set_status", "write", {"ticket_id": ticket_id, "status": status}, status
        )
        return {"ticket_id": ticket_id, "status": status}

    def escalate(self, ticket_id: str, team: str, reason: str) -> dict[str, Any]:
        self._need(
            self.c.row("SELECT id FROM tickets WHERE id = ?", (ticket_id,)), "ticket", ticket_id
        )
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
                    _iso(self.c.now()),
                    self.c.task_id(),
                ),
            )
        self._log(
            "helpdesk",
            "escalate",
            "write",
            {"ticket_id": ticket_id, "team": team, "reason": reason},
            f"escalated to {team}",
        )
        return {"ticket_id": ticket_id, "status": "escalated", "team": team}

    # ================================================================== kb
    def search_articles(self, query: str) -> list[dict[str, Any]]:
        words = [w for w in query.lower().split() if len(w) > 2]
        rows = self.c.rows("SELECT id, title, body FROM kb_articles")
        scored = []
        for r in rows:
            text = (r["title"] + " " + r["body"]).lower()
            score = sum(text.count(w) for w in words)
            if score:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        out = [r for _, r in scored[:3]]
        self._log("kb", "search_articles", "read", {"query": query}, f"{len(out)} articles")
        return out

    def get_article(self, article_id: str) -> dict[str, Any]:
        row = self._need(
            self.c.row("SELECT * FROM kb_articles WHERE id = ?", (article_id,)),
            "article",
            article_id,
        )
        self._log("kb", "get_article", "read", {"article_id": article_id}, row["title"])
        return row

    def get_policy(self) -> str:
        self._log("kb", "get_policy", "read", {}, "policy text")
        return (POLICY_DIR / "policy.md").read_text(encoding="utf-8")

    # ================================================================== approvals
    def request_approval(
        self,
        ticket_id: str,
        action: str,
        params_json: str,
        diff_json: str,
        clause: str,
        summary: str,
    ) -> dict[str, Any]:
        try:
            json.loads(params_json)
            json.loads(diff_json)
        except json.JSONDecodeError as e:
            raise OperationError(f"params_json and diff_json must be JSON: {e}") from e
        aid = f"apr_{self._next(6)}"
        self.c.execute(
            "INSERT INTO approvals(id, task_id, ticket_id, action, params_json, diff_json, clause, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                aid,
                self.c.task_id(),
                ticket_id,
                action,
                params_json,
                diff_json,
                clause,
                "pending",
                _iso(self.c.now()),
            ),
        )
        self._log(
            "approvals",
            "request_approval",
            "approval",
            {
                "ticket_id": ticket_id,
                "action": action,
                "params": json.loads(params_json),
                "clause": clause,
                "summary": summary,
            },
            aid,
        )
        return {"approval_id": aid, "status": "pending", "expected_decision_by": "one business day"}

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        row = self._need(
            self.c.row(
                "SELECT * FROM approvals WHERE id = ? AND task_id = ?",
                (approval_id, self.c.task_id()),
            ),
            "approval",
            approval_id,
        )
        self._log("approvals", "get_approval", "read", {"approval_id": approval_id}, row["status"])
        return row
