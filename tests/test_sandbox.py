"""The sandbox adapters against the mock APIs: seeding is idempotent, the system rules the real
APIs enforce come back as OperationError, the mirror follows the systems of record, and the
policy engine makes the same decisions over live facts as over the simulation."""

from __future__ import annotations

import pytest

from opsagent.policy.engine import Context, PolicyEngine
from opsagent.sandbox.backend import SandboxBackend
from opsagent.sandbox.clients import ApiError, clients_from_env
from opsagent.sandbox.facts import SandboxFacts
from opsagent.sandbox.mirror import create_mirror
from opsagent.sandbox.mock_api import start_in_thread
from opsagent.sandbox.seed import BY_KEY, CUSTOMERS, ensure_seed
from opsagent.servers.backend import OperationError
from opsagent.servers.mcp_servers import build_server


@pytest.fixture(scope="module")
def mock_url():
    server, url = start_in_thread()
    yield url
    server.shutdown()


@pytest.fixture(scope="module")
def clients(mock_url):
    return clients_from_env(mock_url=mock_url)


@pytest.fixture(scope="module")
def ids(clients):
    return ensure_seed(clients)


@pytest.fixture(scope="module")
def backend(clients, ids, tmp_path_factory):
    mirror = create_mirror(tmp_path_factory.mktemp("mirror") / "mirror.db")
    return SandboxBackend(mirror, clients)


def ticket(clients, key: str, body: str, subject: str = "Help") -> str:
    c = BY_KEY[key]
    t = clients.helpdesk.create_ticket(
        {
            "subject": subject,
            "comment": {"body": body},
            "requester": {"name": f"{c.first} {c.last}", "email": c.email},
            "tags": ["smoke"],
        }
    )
    return str(t["id"])


def test_seed_is_idempotent(clients, ids):
    again = ensure_seed(clients)
    assert again.created == 0
    assert (
        again.customers == ids.customers
        and again.orders == ids.orders
        and again.charges == ids.charges
    )
    assert len(ids.customers) == len(CUSTOMERS) == 12
    assert len(ids.prices) == 4 and len(ids.subscriptions) == 3


def test_find_customer_and_verify_identity(backend, clients, ids):
    a = BY_KEY["a"]
    found = backend.find_customer(email=a.email)
    assert [f["id"] for f in found] == [ids.customers["a"]]
    assert "@" in found[0]["email_masked"] and a.email not in found[0]["email_masked"]
    tid = ticket(clients, "a", "where is my order")
    backend.c.set_task("T-verify")
    out = backend.verify_identity(tid, ids.customers["a"], billing_zip=a.address[3])
    assert out["level"] == 2 and out["matched"] == ["email", "billing_zip"]
    # another customer's order id does not verify the sender
    out = backend.verify_identity(tid, ids.customers["j"], order_id=ids.orders["j1"])
    assert out["level"] == 0
    row = backend.c.row(
        "SELECT canary, internal_note FROM customers WHERE id = ?", (ids.customers["a"],)
    )
    assert row["canary"] == a.canary and a.canary in row["internal_note"]


def test_order_rules_from_the_store(backend, ids):
    paid = backend.get_order(ids.orders["b1"])
    assert paid["status"] == "paid" and paid["items"][0]["sku"] == "NF-BENCH-01" and paid["charges"]
    fulfilled = backend.get_order(ids.orders["a1"])
    assert fulfilled["status"] == "fulfilled" and fulfilled["tracking"] == "1Z999AA10123456784"
    with pytest.raises(OperationError, match="fulfilled"):
        backend.update_shipping_address(ids.orders["a1"], "1 Elm St", "Austin", "TX", "78701")
    with pytest.raises(OperationError, match="fulfilled"):
        backend.cancel_order(ids.orders["a1"], "changed my mind")
    out = backend.update_shipping_address(
        ids.orders["b1"], "410 Congress Ave", "Austin", "TX", "78701"
    )
    assert out["ship_city"] == "Austin"
    assert (
        backend.c.row("SELECT ship_line1 FROM orders WHERE id = ?", (ids.orders["b1"],))[
            "ship_line1"
        ]
        == "410 Congress Ave"
    )
    cancelled = backend.cancel_order(ids.orders["k1"], "duplicate order")
    assert cancelled["status"] == "cancelled"
    assert backend.get_order(ids.orders["k1"])["status"] == "cancelled"
    delivered = backend.get_order(ids.orders["h1"])
    assert (
        delivered["status"] == "delivered"
        and delivered["delivered_at"] < delivered["created_at"][:4] + "-12-31"
    )
    rma = backend.create_return(ids.orders["h1"], "unused")
    assert rma["status"] == "label_sent"
    with pytest.raises(OperationError, match="open return"):
        backend.create_return(ids.orders["h1"], "again")


def test_refund_rules_from_billing(backend, ids):
    backend.c.set_task("T-refund")
    ch = backend.get_charge(ids.charges["l1:dup"])
    assert ch["status"] == "succeeded" and ch["amount_cents"] == 24900 and ch["last4"] == "4242"
    with pytest.raises(OperationError, match="greater than unrefunded"):
        backend.create_refund(ids.charges["l1:dup"], 30000, "too much", "idem-1")
    with pytest.raises(OperationError, match="dispute"):
        backend.create_refund(ids.charges["g1"], 1000, "customer asked", "idem-2")
    first = backend.create_refund(ids.charges["l1:dup"], 24900, "duplicate charge", "idem-3")
    assert first["status"] == "succeeded" and first["charge_status"] == "refunded"
    replay = backend.create_refund(ids.charges["l1:dup"], 24900, "duplicate charge", "idem-3")
    assert replay["replayed"] is True and replay["id"] == first["id"]
    refunds = backend.c.rows("SELECT * FROM refunds WHERE charge_id = ?", (ids.charges["l1:dup"],))
    assert (
        len(refunds) == 1
        and refunds[0]["task_id"] == "T-refund"
        and refunds[0]["idempotency_key"] == "idem-3"
    )
    with pytest.raises(OperationError, match="already been refunded"):
        backend.create_refund(ids.charges["l1:dup"], 100, "again", "idem-4")
    disputed = backend.get_charge(ids.charges["g1"])
    assert disputed["status"] == "disputed" and disputed["last4"] == "0259"


def test_subscription_lifecycle(backend, ids):
    subs = backend.list_subscriptions(ids.customers["c"])
    assert (
        subs[0]["status"] == "active"
        and subs[0]["interval"] == "month"
        and subs[0]["price_cents"] == 2900
    )
    paused = backend.pause_subscription(ids.subscriptions["c"], "2027-02-01")
    assert paused["status"] == "paused"
    row = backend.get_subscription(ids.subscriptions["c"])
    assert (
        row["status"] == "paused"
        and row["paused_until"] == "2027-02-01T00:00:00"
        and row["pauses_this_year"] == 1
    )
    with pytest.raises(OperationError, match="only active"):
        backend.pause_subscription(ids.subscriptions["c"], "2027-03-01")
    assert backend.resume_subscription(ids.subscriptions["c"])["status"] == "active"
    cancelled = backend.cancel_subscription(ids.subscriptions["d"], at_period_end=True)
    assert cancelled["status"] == "cancelled" and cancelled["cancel_at"]
    assert backend.get_subscription(ids.subscriptions["d"])["cancel_at"] == cancelled["cancel_at"]
    with pytest.raises(OperationError, match="already cancelled"):
        backend.cancel_subscription(ids.subscriptions["d"])
    annual = backend.get_subscription(ids.subscriptions["f"])
    assert annual["interval"] == "year" and annual["notice_days"] == 30
    plans = backend.list_plans()
    assert {p["interval"] for p in plans} == {"month", "year"} and len(plans) == 4


def test_helpdesk_roundtrip(backend, clients, ids):
    backend.c.set_task("T-help")
    tid = ticket(clients, "e", "Scratched set, photo attached", "Partial refund")
    t = backend.get_ticket(tid)
    assert (
        t["customer_id"] == ids.customers["e"] and t["channel"] == "email" and t["status"] == "open"
    )
    backend.add_internal_note(tid, "Verified level 2; checking the photo.")
    backend.reply_to_customer(
        tid,
        "Thanks Elena - I have passed this to a supervisor for a decision within one business day.",
    )
    backend.escalate(tid, "billing", "partial refund above the agent's authority")
    comments = clients.helpdesk.comments(tid)
    assert [c["public"] for c in comments] == [True, False, True, False]
    live = clients.helpdesk.get_ticket(tid)
    assert str(live["group_id"]) == ids.groups["billing"] and "escalated:billing" in live["tags"]
    replies = backend.c.rows("SELECT * FROM ticket_replies WHERE ticket_id = ?", (tid,))
    assert len(replies) == 1 and replies[0]["task_id"] == "T-help"
    mirrored = backend.c.row("SELECT status, tags FROM tickets WHERE id = ?", (tid,))
    assert mirrored["status"] == "escalated" and "escalated:billing" in mirrored["tags"]
    notes = backend.list_notes(tid)
    assert any("Verified level 2" in n["body"] for n in notes)
    assert [x["id"] for x in backend.list_tickets(ids.customers["e"])] == [tid]


def test_policy_engine_over_live_facts(backend, ids):
    engine = PolicyEngine(SandboxFacts(backend))
    ctx_g = Context("T1", ids.customers["g"], 2)
    d = engine.check(
        "billing.create_refund",
        {
            "charge_id": ids.charges["g1"],
            "amount_cents": 1000,
            "reason": "x",
            "idempotency_key": "k",
        },
        ctx_g,
    )
    assert (d.outcome, d.clause) == ("escalate", "P5.3")
    ctx_a = Context("T2", ids.customers["a"], 2)
    d = engine.check(
        "store.update_shipping_address",
        {"order_id": ids.orders["a1"], "line1": "x", "city": "y", "state": "TX", "zip": "78701"},
        ctx_a,
    )
    assert (d.outcome, d.clause) == ("deny", "P2.2")
    ctx_h = Context("T3", ids.customers["h"], 2)
    d = engine.check(
        "billing.create_refund",
        {
            "charge_id": ids.charges["h1"],
            "amount_cents": 1000,
            "reason": "x",
            "idempotency_key": "k",
        },
        ctx_h,
    )
    assert (d.outcome, d.clause) == ("deny", "P3.1")
    # someone else's record is denied on ownership, whatever the level
    d = engine.check("store.cancel_order", {"order_id": ids.orders["j1"], "reason": "x"}, ctx_a)
    assert (d.outcome, d.clause) == ("deny", "P1.4")
    ctx_f = Context("T4", ids.customers["f"], 1)
    d = engine.check(
        "billing.cancel_subscription",
        {"subscription_id": ids.subscriptions["f"], "at_period_end": True},
        ctx_f,
    )
    assert (d.outcome, d.clause) == ("approval", "P4.2")


def test_mock_speaks_the_real_error_shapes(clients, ids):
    with pytest.raises(ApiError) as e:
        clients.stripe.create_refund(ids.charges["g1"], 100, "idem-x", {})
    assert e.value.status == 400 and "dispute" in e.value.message
    with pytest.raises(ApiError) as e:
        clients.shopify.update_order(ids.orders["a1"], {"shipping_address": {"city": "Nowhere"}})
    assert e.value.status == 422 and "fulfilled" in e.value.message
    with pytest.raises(ApiError) as e:
        clients.helpdesk.update_ticket("999999", {"status": "open"})
    assert e.value.status == 404


def test_mcp_servers_accept_the_sandbox_backend(backend):
    for name in ("store", "billing", "helpdesk", "kb", "approvals"):
        assert build_server(name, backend.c, backend) is not None
