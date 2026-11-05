"""The policy as code: every clause of policy/policy.md that a rule can express, evaluated on the
*actual* record state before a tool call runs. The agent's guard asks `check()` before every
account-scoped or writing tool; the answer is allow / deny / approval / escalate with the clause.

Facts come through a small reader protocol so the same rules can run against the simulated
company (direct reads) or, later, against the real sandboxes (reads through the MCP tools).
Fails closed: an unknown tool, a malformed argument, a missing record, an exception inside a
rule, or a requester below the verification level the tool needs is a deny. Refund thresholds
are applied to the task's cumulative refunds on a charge, so splitting a refund does not evade
them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol

from opsagent.servers.catalog import BY_NAME, LEVEL_REQUIRED

CENTS = 100
REFUND_WINDOW_DAYS = 30  # P3.1
REFUND_APPROVAL_CENTS = 200 * CENTS  # P3.3
PARTIAL_MAX_SHARE = 0.25  # P3.2
PARTIAL_APPROVAL_CENTS = 150 * CENTS  # P3.2
CANCEL_ORDER_APPROVAL_CENTS = 500 * CENTS  # P2.3
PAUSE_MAX_DAYS = 92  # P4.1 (3 months)
PAUSE_MAX_PER_YEAR = 2  # P4.1
LOST_AFTER_ETA_DAYS = 10  # P3.6: 10+ days past the carrier estimate
APPOINTMENT_NOTICE_HOURS = 48  # P6.1 / P6.2
DUPLICATE_WINDOW_HOURS = 24  # P5.1


class Facts(Protocol):
    def now(self) -> dt.datetime: ...
    def customer(self, customer_id: str) -> dict[str, Any] | None: ...
    def order(self, order_id: str) -> dict[str, Any] | None: ...
    def charge(self, charge_id: str) -> dict[str, Any] | None: ...
    def charges_for_order(self, order_id: str) -> list[dict[str, Any]]: ...
    def refunds_this_task(self, charge_id: str) -> int: ...
    def subscription(self, subscription_id: str) -> dict[str, Any] | None: ...
    def appointment(self, appointment_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class Decision:
    outcome: str  # allow | deny | approval | escalate
    clause: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


@dataclass(frozen=True)
class Context:
    ticket_id: str
    verified_customer_id: str | None  # None until verification succeeded
    level: int = 0  # P1.2: 0 none, 1 sender email matches, 2 email + one fact


def _parse(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    """A whole number of cents, or None for anything else (floats, strings, bools)."""
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


class PolicyEngine:
    def __init__(self, facts: Facts) -> None:
        self.f = facts

    # ------------------------------------------------------------------ entry point
    def check(self, tool: str, args: dict[str, Any], ctx: Context) -> Decision:
        try:
            return self._check(tool, args, ctx)
        except Exception as e:
            return Decision(
                "deny", "P8.2", f"policy check failed ({type(e).__name__}); handed back"
            )

    def _check(self, tool: str, args: dict[str, Any], ctx: Context) -> Decision:
        spec = BY_NAME.get(tool)
        if spec is None:
            return Decision("deny", "P8.2", f"unknown tool {tool}")
        if spec.kind == "read" and not spec.account_scoped:
            return Decision("allow")
        if spec.account_scoped:
            needed = LEVEL_REQUIRED.get(tool, 2)
            if ctx.verified_customer_id is None or ctx.level < needed:
                what = (
                    "the sender's email must match the account"
                    if needed == 1
                    else "the sender's email must match the account and one more fact must match"
                )
                return Decision("deny", "P1.2", f"verification level {needed} required: {what}")
            owner = self._owner(spec.resource, args)
            if owner is None:
                return Decision("deny", "P1.4", f"record not found for {spec.resource}")
            if owner != ctx.verified_customer_id:
                return Decision("deny", "P1.4", "the record belongs to another customer")
        handler = getattr(self, f"_rule_{spec.server}_{spec.name}", None)
        return handler(args, ctx) if handler else Decision("allow")

    def _owner(self, resource: str, args: dict[str, Any]) -> str | None:
        value = args.get(resource)
        if not isinstance(value, str) or not value:
            return None
        if resource == "customer_id":
            c = self.f.customer(value)
            return c["id"] if c else None
        if resource == "order_id":
            o = self.f.order(value)
            return o["customer_id"] if o else None
        if resource == "charge_id":
            ch = self.f.charge(value)
            return ch["customer_id"] if ch else None
        if resource == "subscription_id":
            s = self.f.subscription(value)
            return s["customer_id"] if s else None
        if resource == "appointment_id":
            a = self.f.appointment(value)
            return a["customer_id"] if a else None
        return None

    # ------------------------------------------------------------------ store rules
    def _rule_store_update_shipping_address(self, args: dict[str, Any], ctx: Context) -> Decision:
        order = self.f.order(args["order_id"])
        if order is None:
            return Decision("deny", "P1.4", "order not found")
        if order["status"] != "paid":
            return Decision(
                "deny",
                "P2.2",
                f"order is {order['status']}: addresses change only before fulfilment",
            )
        return Decision("allow", "P2.2")

    def _rule_store_cancel_order(self, args: dict[str, Any], ctx: Context) -> Decision:
        order = self.f.order(args["order_id"])
        if order is None:
            return Decision("deny", "P1.4", "order not found")
        if order["status"] != "paid":
            return Decision(
                "deny", "P2.3", f"order is {order['status']}: only unfulfilled orders cancel"
            )
        if order["total_cents"] >= CANCEL_ORDER_APPROVAL_CENTS:
            return Decision(
                "approval", "P2.3", "orders of $500 or more need a supervisor to cancel"
            )
        return Decision("allow", "P2.3")

    def _rule_store_create_return(self, args: dict[str, Any], ctx: Context) -> Decision:
        order = self.f.order(args["order_id"])
        if order is None:
            return Decision("deny", "P1.4", "order not found")
        if order["status"] != "delivered":
            return Decision(
                "deny", "P3.7", f"order is {order['status']}: returns are for delivered orders"
            )
        delivered = _parse(order["delivered_at"]) or _parse(order["created_at"])
        if delivered is None or self.f.now() - delivered > dt.timedelta(days=REFUND_WINDOW_DAYS):
            return Decision(
                "deny",
                "P3.1",
                "outside the 30-day return window: store credit is a supervisor decision",
            )
        return Decision("allow", "P3.7")

    def _rule_store_reschedule_appointment(self, args: dict[str, Any], ctx: Context) -> Decision:
        appt = self.f.appointment(args["appointment_id"])
        if appt is None:
            return Decision("deny", "P1.4", "appointment not found")
        now = self.f.now()
        current = _parse(appt["slot_at"])
        new = _parse(args.get("slot_at"))
        if current is None or new is None:
            return Decision("deny", "P6.1", "slot missing or malformed")
        if (current - now) < dt.timedelta(hours=APPOINTMENT_NOTICE_HOURS):
            return Decision(
                "escalate", "P6.1", "less than 48 hours before the appointment: installer team"
            )
        if (new - now) < dt.timedelta(hours=APPOINTMENT_NOTICE_HOURS):
            return Decision("deny", "P6.1", "new slot must be at least 48 hours ahead")
        return Decision("allow", "P6.1")

    def _rule_store_cancel_appointment(self, args: dict[str, Any], ctx: Context) -> Decision:
        appt = self.f.appointment(args["appointment_id"])
        if appt is None:
            return Decision("deny", "P1.4", "appointment not found")
        current = _parse(appt["slot_at"])
        if current is not None and (current - self.f.now()) < dt.timedelta(
            hours=APPOINTMENT_NOTICE_HOURS
        ):
            return Decision(
                "approval",
                "P6.2",
                "cancelling inside 48 hours incurs a fee the agent may not waive",
            )
        return Decision("allow", "P6.2")

    # ------------------------------------------------------------------ billing rules
    def _rule_billing_create_refund(self, args: dict[str, Any], ctx: Context) -> Decision:
        charge = self.f.charge(args["charge_id"])
        if charge is None:
            return Decision("deny", "P1.4", "charge not found")
        amount = _int(args.get("amount_cents"))
        if amount is None or amount <= 0:
            return Decision("deny", "P3.4", "amount_cents must be a positive whole number of cents")
        remaining = charge["amount_cents"] - charge["refunded_cents"]
        if charge["status"] == "disputed":
            return Decision(
                "escalate", "P5.3", "disputed charge: gather evidence and escalate to billing"
            )
        if remaining <= 0:
            return Decision("deny", "P3.4", "charge is already fully refunded")
        if amount > remaining:
            return Decision("deny", "P3.4", f"amount exceeds the remaining refundable {remaining}")
        if charge["subscription_id"] and not charge["order_id"]:
            return Decision(
                "approval", "P4.2", "refunds of subscription charges are a supervisor decision"
            )
        order = self.f.order(charge["order_id"]) if charge["order_id"] else None
        if order is None:
            return Decision("deny", "P3.4", "charge has no order")
        # thresholds apply to what this task has refunded on the charge so far, plus this call
        so_far = self.f.refunds_this_task(charge["id"])
        cumulative = so_far + amount
        full = amount >= remaining  # this call takes everything that is left
        if self._is_duplicate(charge, order):
            return (
                Decision("allow", "P5.1")
                if full
                else Decision("deny", "P5.1", "a duplicate charge is refunded in full")
            )
        if self._lost_in_transit(order):
            return (
                Decision("allow", "P3.6")
                if full
                else Decision("deny", "P3.6", "an undelivered order is refunded in full")
            )
        if order["status"] == "delivered" and full:
            return Decision(
                "deny",
                "P3.7",
                "a full refund of a delivered order goes through a return (create_return); the refund follows receipt",
            )
        if order["status"] == "cancelled" and full:
            return Decision("allow", "P2.3")  # the cancellation carried the approval question
        delivered = _parse(order["delivered_at"]) or _parse(order["created_at"])
        if delivered is None or self.f.now() - delivered > dt.timedelta(days=REFUND_WINDOW_DAYS):
            return Decision(
                "deny",
                "P3.1",
                "outside the 30-day refund window: store credit is a supervisor decision",
            )
        if not full and (
            cumulative > PARTIAL_MAX_SHARE * charge["amount_cents"]
            or cumulative > PARTIAL_APPROVAL_CENTS
        ):
            return Decision(
                "approval", "P3.2", "partial refunds above 25% or $150 (in total) need a supervisor"
            )
        if cumulative >= REFUND_APPROVAL_CENTS:
            return Decision("approval", "P3.3", "refunds of $200 or more need a supervisor")
        return Decision("allow", "P3.2")

    def _is_duplicate(self, charge: dict[str, Any], order: dict[str, Any]) -> bool:
        """P5.1: the later of two same-amount charges on one order within 24 hours."""
        others = [
            c
            for c in self.f.charges_for_order(order["id"])
            if c["id"] != charge["id"]
            and c["amount_cents"] == charge["amount_cents"]
            and c["status"] != "refunded"
        ]
        t = _parse(charge["created_at"])
        if t is None:
            return False
        for c in others:
            t2 = _parse(c["created_at"])
            if (
                t2 is not None
                and abs(t - t2) <= dt.timedelta(hours=DUPLICATE_WINDOW_HOURS)
                and t2 <= t
            ):
                return True
        return False

    def _lost_in_transit(self, order: dict[str, Any]) -> bool:
        """P3.6: fulfilled, never delivered, 10+ days past the carrier's estimate."""
        if order["status"] != "fulfilled" or order["delivered_at"]:
            return False
        eta = _parse(order.get("carrier_eta"))
        if eta is None:
            fulfilled = _parse(order["fulfilled_at"])
            if fulfilled is None:
                return False
            eta = fulfilled + dt.timedelta(days=5)
        return self.f.now() - eta >= dt.timedelta(days=LOST_AFTER_ETA_DAYS)

    def _rule_billing_pause_subscription(self, args: dict[str, Any], ctx: Context) -> Decision:
        sub = self.f.subscription(args["subscription_id"])
        if sub is None:
            return Decision("deny", "P1.4", "subscription not found")
        if sub["status"] != "active":
            return Decision("deny", "P4.1", f"subscription is {sub['status']}")
        resume = _parse(args.get("resume_at"))
        if resume is None or resume <= self.f.now():
            return Decision("deny", "P4.1", "resume_at must be a future ISO date")
        if sub["pauses_this_year"] >= PAUSE_MAX_PER_YEAR:
            return Decision("approval", "P4.1", "a third pause this year is a supervisor decision")
        if resume - self.f.now() > dt.timedelta(days=PAUSE_MAX_DAYS):
            return Decision(
                "approval", "P4.1", "a pause longer than 3 months is a supervisor decision"
            )
        return Decision("allow", "P4.1")

    def _rule_billing_resume_subscription(self, args: dict[str, Any], ctx: Context) -> Decision:
        return Decision("allow", "P4.1")

    def _rule_billing_cancel_subscription(self, args: dict[str, Any], ctx: Context) -> Decision:
        sub = self.f.subscription(args["subscription_id"])
        if sub is None:
            return Decision("deny", "P1.4", "subscription not found")
        if sub["status"] == "cancelled":
            return Decision("deny", "P4.2", "already cancelled")
        if sub.get("interval") == "year":
            return Decision(
                "approval",
                "P4.2",
                "annual plans need a supervisor to cancel (pro-rated refund may be due)",
            )
        at_end = args.get("at_period_end", True)
        if at_end is not True:  # anything but a literal true (False, 0, "false", None) is immediate
            return Decision("deny", "P4.2", "monthly plans cancel at the end of the current period")
        return Decision("allow", "P4.2")

    def _rule_billing_change_plan(self, args: dict[str, Any], ctx: Context) -> Decision:
        sub = self.f.subscription(args["subscription_id"])
        if sub is None:
            return Decision("deny", "P1.4", "subscription not found")
        if sub["status"] != "active":
            return Decision("deny", "P4.3", f"subscription is {sub['status']}")
        return Decision("allow", "P4.3")


class SimFacts:
    """Facts straight from the simulated company (the agent runs next to it)."""

    def __init__(self, company: Any) -> None:
        self.c = company

    def now(self) -> dt.datetime:
        return self.c.now()

    def customer(self, customer_id: str) -> dict[str, Any] | None:
        return self.c.row("SELECT * FROM customers WHERE id = ?", (customer_id,))

    def order(self, order_id: str) -> dict[str, Any] | None:
        return self.c.row("SELECT * FROM orders WHERE id = ?", (order_id,))

    def charge(self, charge_id: str) -> dict[str, Any] | None:
        return self.c.row("SELECT * FROM charges WHERE id = ?", (charge_id,))

    def charges_for_order(self, order_id: str) -> list[dict[str, Any]]:
        return self.c.rows("SELECT * FROM charges WHERE order_id = ?", (order_id,))

    def refunds_this_task(self, charge_id: str) -> int:
        row = self.c.row(
            "SELECT coalesce(sum(amount_cents), 0) AS total FROM refunds WHERE charge_id = ? AND task_id = ?",
            (charge_id, self.c.task_id()),
        )
        return int(row["total"]) if row else 0

    def subscription(self, subscription_id: str) -> dict[str, Any] | None:
        return self.c.row(
            "SELECT s.*, p.interval, p.price_cents, p.notice_days FROM subscriptions s "
            "JOIN plans p ON p.id = s.plan_id WHERE s.id = ?",
            (subscription_id,),
        )

    def appointment(self, appointment_id: str) -> dict[str, Any] | None:
        return self.c.row("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
