"""Policy engine: one test per clause family, on hand-built facts (no database, no model)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from opsagent.policy.engine import Context, PolicyEngine

NOW = dt.datetime(2026, 11, 4, 10, 0, 0)


def iso(d: dt.datetime) -> str:
    return d.isoformat(timespec="seconds")


class Facts:
    def __init__(self) -> None:
        self.customers: dict[str, dict[str, Any]] = {"C1": {"id": "C1"}, "C2": {"id": "C2"}}
        self.orders: dict[str, dict[str, Any]] = {}
        self.charges: dict[str, dict[str, Any]] = {}
        self.subs: dict[str, dict[str, Any]] = {}
        self.appts: dict[str, dict[str, Any]] = {}
        self.refunded: dict[str, int] = {}  # charge -> cents refunded "this task"

    def now(self) -> dt.datetime:
        return NOW

    def customer(self, i: str) -> dict[str, Any] | None:
        return self.customers.get(i)

    def order(self, i: str) -> dict[str, Any] | None:
        return self.orders.get(i)

    def charge(self, i: str) -> dict[str, Any] | None:
        return self.charges.get(i)

    def charges_for_order(self, i: str) -> list[dict[str, Any]]:
        return [c for c in self.charges.values() if c["order_id"] == i]

    def refunds_this_task(self, charge_id: str) -> int:
        return self.refunded.get(charge_id, 0)

    def subscription(self, i: str) -> dict[str, Any] | None:
        return self.subs.get(i)

    def appointment(self, i: str) -> dict[str, Any] | None:
        return self.appts.get(i)

    # builders
    def add_order(
        self,
        oid,
        customer,
        status,
        total,
        days_ago,
        delivered_days_ago=None,
        fulfilled_days_ago=None,
        eta_days_ago=None,
    ):
        self.orders[oid] = {
            "id": oid, "customer_id": customer, "status": status, "total_cents": total,
            "created_at": iso(NOW - dt.timedelta(days=days_ago)),
            "fulfilled_at": iso(NOW - dt.timedelta(days=fulfilled_days_ago)) if fulfilled_days_ago is not None else None,
            "delivered_at": iso(NOW - dt.timedelta(days=delivered_days_ago)) if delivered_days_ago is not None else None,
            "carrier_eta": iso(NOW - dt.timedelta(days=eta_days_ago)) if eta_days_ago is not None else None,
        }  # fmt: skip

    def add_charge(
        self, chid, customer, order, amount, days_ago, status="succeeded", refunded=0, sub=None
    ):
        self.charges[chid] = {
            "id": chid, "customer_id": customer, "order_id": order, "subscription_id": sub, "amount_cents": amount,
            "created_at": iso(NOW - dt.timedelta(days=days_ago)), "status": status, "refunded_cents": refunded,
        }  # fmt: skip


L2 = Context("T1", "C1", 2)
L1 = Context("T1", "C1", 1)
NONE = Context("T1", None, 0)


def engine() -> tuple[PolicyEngine, Facts]:
    f = Facts()
    return PolicyEngine(f), f


def test_levels_ownership_and_unknowns_fail_closed():
    e, f = engine()
    f.add_order("O1", "C1", "paid", 10000, 1)
    f.add_order("O2", "C2", "paid", 10000, 1)
    assert e.check("store.get_order", {"order_id": "O1"}, NONE).clause == "P1.2"
    assert e.check("store.get_order", {"order_id": "O1"}, L1).allowed  # reads need level 1
    assert e.check("store.get_order", {"order_id": "O2"}, L1).clause == "P1.4"
    addr = {"order_id": "O1", "line1": "x", "city": "y", "state": "TX", "zip": "1"}
    assert e.check("store.update_shipping_address", addr, L1).clause == "P1.2"  # writes need 2
    assert e.check("store.update_shipping_address", addr, L2).allowed
    assert e.check("store.get_order", {"order_id": "O9"}, L2).outcome == "deny"
    assert e.check("store.get_order", {"order_id": 42}, L2).outcome == "deny"  # wrong type
    assert e.check("kb.search_articles", {"query": "x"}, NONE).allowed
    assert e.check("nonsense.tool", {}, L2).outcome == "deny"


def test_cancellation_and_returns_follow_state_and_amount():
    e, f = engine()
    f.add_order("O1", "C1", "paid", 10000, 1)
    f.add_order("O2", "C1", "fulfilled", 10000, 3, fulfilled_days_ago=2)
    f.add_order("O3", "C1", "paid", 60000, 1)
    f.add_order("O4", "C1", "delivered", 10000, 20, delivered_days_ago=15)
    f.add_order("O5", "C1", "delivered", 10000, 60, delivered_days_ago=45)
    assert e.check("store.cancel_order", {"order_id": "O1", "reason": "r"}, L2).allowed
    assert e.check("store.cancel_order", {"order_id": "O2", "reason": "r"}, L2).outcome == "deny"
    assert (
        e.check("store.cancel_order", {"order_id": "O3", "reason": "r"}, L2).outcome == "approval"
    )
    assert e.check("store.create_return", {"order_id": "O4", "reason": "r"}, L2).allowed
    assert e.check("store.create_return", {"order_id": "O2", "reason": "r"}, L2).clause == "P3.7"
    assert e.check("store.create_return", {"order_id": "O5", "reason": "r"}, L2).clause == "P3.1"


def test_refund_rules_including_cumulative_thresholds():
    e, f = engine()
    f.add_order("O1", "C1", "delivered", 15000, 20, delivered_days_ago=15)  # inside window
    f.add_charge("ch1", "C1", "O1", 15000, 20)
    f.add_order("O2", "C1", "delivered", 15000, 60, delivered_days_ago=45)  # outside window
    f.add_charge("ch2", "C1", "O2", 15000, 60)
    f.add_order("O4", "C1", "fulfilled", 9000, 30, fulfilled_days_ago=29, eta_days_ago=24)  # lost
    f.add_charge("ch4", "C1", "O4", 9000, 30)
    f.add_order("O5", "C1", "delivered", 40000, 3, delivered_days_ago=1)  # duplicate charge
    f.add_charge("ch5a", "C1", "O5", 40000, 3.0)
    f.add_charge("ch5b", "C1", "O5", 40000, 2.9)
    f.add_charge("ch6", "C1", "O1", 5000, 20, status="disputed")
    f.add_charge("ch7", "C1", "O1", 5000, 20, refunded=5000, status="refunded")
    f.add_charge("ch8", "C1", None, 29900, 5, sub="sub1")
    f.add_order("O9", "C1", "delivered", 40000, 5, delivered_days_ago=2)  # salami target
    f.add_charge("ch9", "C1", "O9", 40000, 5)

    def refund(ch: str, amount: Any):
        return e.check(
            "billing.create_refund",
            {"charge_id": ch, "amount_cents": amount, "reason": "r", "idempotency_key": "k"},
            L2,
        )

    assert refund("ch1", 15000).clause == "P3.7"  # full refund of a delivered order -> return flow
    assert refund("ch1", 3000).allowed  # partial 20% under $150, customer keeps the item
    assert refund("ch1", 5000).outcome == "approval"  # partial > 25% -> P3.2
    assert refund("ch2", 3000).clause == "P3.1"  # outside window
    assert refund("ch4", 9000).allowed and refund("ch4", 9000).clause == "P3.6"
    assert refund("ch5b", 40000).allowed and refund("ch5b", 40000).clause == "P5.1"
    assert refund("ch6", 1000).outcome == "escalate"  # disputed
    assert refund("ch7", 1000).clause == "P3.4"  # already refunded
    assert refund("ch1", 20000).clause == "P3.4"  # more than remaining
    assert refund("ch8", 29900).outcome == "approval" and refund("ch8", 29900).clause == "P4.2"
    assert refund("ch1", "49.99").outcome == "deny" and refund("ch1", -5).outcome == "deny"
    # salami: 10% is fine once; after 3 x 10% this task, the next 10% crosses 25% -> approval
    assert refund("ch9", 4000).allowed
    f.refunded["ch9"] = 12000
    assert refund("ch9", 4000).outcome == "approval"


def test_subscription_rules():
    e, f = engine()
    f.subs["s1"] = {
        "id": "s1",
        "customer_id": "C1",
        "status": "active",
        "interval": "month",
        "pauses_this_year": 0,
    }
    f.subs["s2"] = {
        "id": "s2",
        "customer_id": "C1",
        "status": "active",
        "interval": "year",
        "pauses_this_year": 2,
    }
    f.subs["s3"] = {
        "id": "s3",
        "customer_id": "C1",
        "status": "cancelled",
        "interval": "month",
        "pauses_this_year": 0,
    }
    ok_resume = iso(NOW + dt.timedelta(days=60))
    too_long = iso(NOW + dt.timedelta(days=120))
    assert e.check(
        "billing.pause_subscription", {"subscription_id": "s1", "resume_at": ok_resume}, L1
    ).allowed
    assert (
        e.check(
            "billing.pause_subscription", {"subscription_id": "s1", "resume_at": too_long}, L1
        ).outcome
        == "approval"
    )
    assert (
        e.check(
            "billing.pause_subscription", {"subscription_id": "s2", "resume_at": ok_resume}, L1
        ).outcome
        == "approval"
    )
    assert (
        e.check(
            "billing.pause_subscription", {"subscription_id": "s1", "resume_at": "yesterday"}, L1
        ).outcome
        == "deny"
    )
    assert e.check(
        "billing.cancel_subscription", {"subscription_id": "s1", "at_period_end": True}, L1
    ).allowed
    assert (
        e.check(
            "billing.cancel_subscription", {"subscription_id": "s1", "at_period_end": False}, L1
        ).outcome
        == "deny"
    )
    assert (
        e.check(
            "billing.cancel_subscription", {"subscription_id": "s1", "at_period_end": "false"}, L1
        ).outcome
        == "deny"
    )
    assert (
        e.check(
            "billing.cancel_subscription", {"subscription_id": "s2", "at_period_end": True}, L1
        ).outcome
        == "approval"
    )
    assert (
        e.check(
            "billing.cancel_subscription", {"subscription_id": "s3", "at_period_end": True}, L1
        ).outcome
        == "deny"
    )
    assert (
        e.check("billing.change_plan", {"subscription_id": "s1", "plan_id": "p"}, L1).clause
        == "P1.2"
    )
    assert e.check("billing.change_plan", {"subscription_id": "s1", "plan_id": "p"}, L2).allowed


def test_appointment_rules():
    e, f = engine()
    f.appts["a1"] = {
        "id": "a1",
        "customer_id": "C1",
        "slot_at": iso(NOW + dt.timedelta(days=5)),
        "status": "scheduled",
    }
    f.appts["a2"] = {
        "id": "a2",
        "customer_id": "C1",
        "slot_at": iso(NOW + dt.timedelta(hours=20)),
        "status": "scheduled",
    }
    far = iso(NOW + dt.timedelta(days=7))
    near = iso(NOW + dt.timedelta(hours=30))
    assert e.check(
        "store.reschedule_appointment", {"appointment_id": "a1", "slot_at": far}, L2
    ).allowed
    assert (
        e.check(
            "store.reschedule_appointment", {"appointment_id": "a1", "slot_at": near}, L2
        ).outcome
        == "deny"
    )
    assert (
        e.check(
            "store.reschedule_appointment", {"appointment_id": "a2", "slot_at": far}, L2
        ).outcome
        == "escalate"
    )
    assert e.check("store.cancel_appointment", {"appointment_id": "a1"}, L2).allowed
    assert e.check("store.cancel_appointment", {"appointment_id": "a2"}, L2).outcome == "approval"


def test_engine_exceptions_deny():
    class Broken(Facts):
        def order(self, i: str):
            raise RuntimeError("db down")

    e = PolicyEngine(Broken())
    d = e.check("store.get_order", {"order_id": "O1"}, L2)
    assert d.outcome == "deny" and "failed" in d.reason
