"""A local read model of the sandboxes in the simulated company's schema.

The guard (ownership, output screen), the checker and the scorer read a `Company`; keeping
that contract means the sandbox adapters refresh a local SQLite copy of every record they
touch, and local-only state (verifications, approvals, replies with task ids, appointments,
returns, the audit log) lives there too. Timestamps are normalised to naive UTC ISO seconds so
the policy engine's date arithmetic is the same as against the simulation.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from opsagent.sim.company import SCHEMA, Company
from opsagent.sim.generate import ARTICLES

CANARY = re.compile(r"CNRY-[0-9A-F]{6}-\d+")
DELIVERED_TAG = re.compile(r"delivered(?::(\d{4}-\d{2}-\d{2}))?")


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds")


def iso(value: Any) -> str | None:
    """Epoch seconds (Stripe) or an ISO 8601 string with any offset (Shopify, Zendesk) -> naive
    UTC 'YYYY-MM-DDTHH:MM:SS'; None stays None."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
        d = dt.datetime.fromtimestamp(int(value), dt.UTC)
    else:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is not None:
        d = d.astimezone(dt.UTC).replace(tzinfo=None)
    return d.replace(microsecond=0).isoformat(timespec="seconds")


def create_mirror(path: Path | str, now: str | None = None) -> Company:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    company = Company(p)
    company.con.executescript(SCHEMA)
    company.set_context("now", now or utc_now_iso())
    company.set_context("task_id", "")
    company.set_context("seq", "1,1,1,1,1,1,800001,900001")
    with company.transaction():
        for aid, title, body in ARTICLES:
            company.execute(
                "INSERT INTO kb_articles(id, title, body) VALUES (?,?,?)", (aid, title, body)
            )
    company.snapshot("base")
    return company


def upsert(company: Company, table: str, row: dict[str, Any]) -> None:
    cols = list(row)
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    company.execute(
        f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT(id) DO UPDATE SET {sets}",
        [row[c] for c in cols],
    )


# ====================================================================== Shopify -> store tables
def customer_row(c: dict[str, Any]) -> dict[str, Any]:
    addr = c.get("default_address") or {}
    note = c.get("note") or ""
    m = CANARY.search(note)
    tags = str(c.get("tags") or "")
    return {
        "id": str(c["id"]),
        "first_name": c.get("first_name") or "",
        "last_name": c.get("last_name") or "",
        "email": c["email"],
        "phone": c.get("phone") or addr.get("phone") or "",
        "created_at": iso(c.get("created_at")) or utc_now_iso(),
        "address_line1": addr.get("address1") or "",
        "city": addr.get("city") or "",
        "state": addr.get("province_code") or addr.get("province") or "",
        "zip": addr.get("zip") or "",
        "country": addr.get("country_code") or "US",
        # the billing zip is the default address zip (the store holds no separate billing record)
        "billing_zip": addr.get("zip") or "",
        "loyalty_tier": "plus"
        if "plus" in tags.lower().split(", ") or "plus" in tags.lower().split(",")
        else "standard",
        "canary": m.group(0) if m else f"CNRY-NONE-{c['id']}",
        "internal_note": note or None,
    }


def order_rows(o: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tags = [t.strip() for t in str(o.get("tags") or "").split(",") if t.strip()]
    fulfillments = o.get("fulfillments") or []
    first = fulfillments[0] if fulfillments else {}
    fulfilled_at = iso(first.get("created_at")) if first else None
    delivered_at: str | None = None
    for f in fulfillments:
        if f.get("shipment_status") == "delivered":
            delivered_at = iso(f.get("updated_at") or f.get("created_at"))
    for t in tags:
        m = DELIVERED_TAG.fullmatch(t)
        if m:
            delivered_at = (
                f"{m.group(1)}T12:00:00" if m.group(1) else (delivered_at or fulfilled_at)
            )
    if o.get("cancelled_at"):
        status = "cancelled"
    elif "returned" in tags:
        status = "returned"
    elif delivered_at:
        status = "delivered"
    elif o.get("fulfillment_status") == "fulfilled":
        status = "fulfilled"
    else:
        status = "paid"
    attrs = {
        a.get("name"): a.get("value")
        for a in (o.get("note_attributes") or [])
        if isinstance(a, dict)
    }
    eta = iso(attrs.get("carrier_eta"))
    if eta is None and fulfilled_at:
        eta = (dt.datetime.fromisoformat(fulfilled_at) + dt.timedelta(days=5)).isoformat(
            timespec="seconds"
        )
    ship = o.get("shipping_address") or {}
    row = {
        "id": str(o["id"]),
        "customer_id": str((o.get("customer") or {}).get("id")),
        "created_at": iso(o.get("processed_at") or o.get("created_at")) or utc_now_iso(),
        "status": status,
        "total_cents": round(float(o.get("total_price") or 0) * 100),
        "currency": str(o.get("currency") or "USD"),
        "ship_line1": ship.get("address1") or "",
        "ship_city": ship.get("city") or "",
        "ship_state": ship.get("province_code") or ship.get("province") or "",
        "ship_zip": ship.get("zip") or "",
        "fulfilled_at": fulfilled_at,
        "delivered_at": delivered_at,
        "tracking": first.get("tracking_number") if first else None,
        "carrier_eta": eta,
        "note": o.get("note"),
    }
    items = [
        {
            "order_id": str(o["id"]),
            "sku": li.get("sku") or "",
            "title": li.get("title") or "",
            "qty": int(li.get("quantity") or 1),
            "unit_cents": round(float(li.get("price") or 0) * 100),
        }
        for li in o.get("line_items") or []
    ]
    return row, items


def write_order(company: Company, o: dict[str, Any]) -> dict[str, Any]:
    row, items = order_rows(o)
    current = company.rows(
        "SELECT sku, title, qty, unit_cents FROM order_items WHERE order_id = ? ORDER BY id",
        (row["id"],),
    )
    with company.transaction():
        upsert(company, "orders", row)
        if current == [{k: it[k] for k in ("sku", "title", "qty", "unit_cents")} for it in items]:
            return row  # a read is not a change: keep the existing item rows (and their ids)
        company.execute("DELETE FROM order_items WHERE order_id = ?", (row["id"],))
        for it in items:
            company.execute(
                "INSERT INTO order_items(order_id, sku, title, qty, unit_cents) VALUES (?,?,?,?,?)",
                (it["order_id"], it["sku"], it["title"], it["qty"], it["unit_cents"]),
            )
    return row


# ====================================================================== Stripe -> billing tables
def plan_row(price: dict[str, Any]) -> dict[str, Any]:
    meta = price.get("metadata") or {}
    return {
        "id": str(price["id"]),
        "name": price.get("nickname") or meta.get("name") or str(price["id"]),
        "price_cents": int(price.get("unit_amount") or 0),
        "interval": (price.get("recurring") or {}).get("interval", "month"),
        "notice_days": int(meta.get("notice_days") or 0),
    }


def charge_row(ch: dict[str, Any], customer_id: str) -> dict[str, Any]:
    meta = ch.get("metadata") or {}
    if ch.get("disputed"):
        status = "disputed"
    elif ch.get("refunded"):
        status = "refunded"
    elif int(ch.get("amount_refunded") or 0) > 0:
        status = "partially_refunded"
    else:
        status = "succeeded"
    card = (ch.get("payment_method_details") or {}).get("card") or {}
    return {
        "id": str(ch["id"]),
        "customer_id": customer_id,
        "order_id": meta.get("order_id") or None,
        "subscription_id": meta.get("subscription_id") or None,
        "amount_cents": int(ch["amount"]),
        "created_at": iso(ch.get("created")) or utc_now_iso(),
        "status": status,
        "refunded_cents": int(ch.get("amount_refunded") or 0),
        "last4": str(card.get("last4") or "0000"),
    }


def refund_rows(ch: dict[str, Any]) -> list[dict[str, Any]]:
    refunds = ch.get("refunds") or {}
    data = refunds.get("data", []) if isinstance(refunds, dict) else refunds
    out = []
    for r in data:
        meta = r.get("metadata") or {}
        out.append(
            {
                "id": str(r["id"]),
                "charge_id": str(ch["id"]),
                "amount_cents": int(r["amount"]),
                "reason": meta.get("reason") or r.get("reason") or "",
                "idempotency_key": meta.get("idempotency_key") or None,
                "created_at": iso(r.get("created")) or utc_now_iso(),
                "task_id": meta.get("task_id") or None,
            }
        )
    return out


def subscription_row(s: dict[str, Any], customer_id: str) -> dict[str, Any]:
    item = ((s.get("items") or {}).get("data") or [{}])[0]
    price = item.get("price") or {}
    meta = s.get("metadata") or {}
    if s.get("status") == "canceled" or s.get("cancel_at_period_end"):
        status = "cancelled"
    elif s.get("pause_collection"):
        status = "paused"
    else:
        status = "active"
    pause = s.get("pause_collection") or {}
    cancel_at = s.get("cancel_at") or (
        s.get("current_period_end") if s.get("cancel_at_period_end") else None
    )
    return {
        "id": str(s["id"]),
        "customer_id": customer_id,
        "plan_id": str(price.get("id")),
        "status": status,
        "started_at": iso(s.get("start_date") or s.get("created")) or utc_now_iso(),
        "current_period_start": iso(s.get("current_period_start")) or utc_now_iso(),
        "current_period_end": iso(s.get("current_period_end")) or utc_now_iso(),
        "paused_until": iso(pause.get("resumes_at")) if pause else None,
        "cancel_at": iso(cancel_at),
        "pauses_this_year": int(meta.get("pauses_this_year") or 0),
    }


# ====================================================================== Zendesk -> helpdesk tables
STATUS = {
    "new": "open",
    "open": "open",
    "pending": "pending",
    "hold": "pending",
    "solved": "solved",
    "closed": "solved",
}
CHANNEL = {"email": "email", "web": "web_form", "chat": "chat", "api": "email"}


def ticket_row(
    t: dict[str, Any], requester_email: str, customer_id: str | None, comments: list[dict[str, Any]]
) -> dict[str, Any]:
    tags = [str(x) for x in t.get("tags") or []]
    channel = CHANNEL.get(str((t.get("via") or {}).get("channel", "email")), "email")
    if "internal" in tags:
        channel = "internal"
    status = (
        "escalated"
        if any(x.startswith("escalated") for x in tags)
        else STATUS.get(str(t.get("status")), "open")
    )
    first = comments[0] if comments else {}
    attachments = ",".join(a.get("file_name", "") for a in first.get("attachments") or [])
    return {
        "id": str(t["id"]),
        "customer_id": customer_id,
        "requester_email": requester_email,
        "channel": channel,
        "subject": t.get("subject") or "",
        "body": t.get("description") or first.get("body") or "",
        "status": status,
        "created_at": iso(t.get("created_at")) or utc_now_iso(),
        "tags": ",".join(tags),
        "attachments": attachments,
    }


def write_notes(company: Company, ticket_id: str, comments: list[dict[str, Any]]) -> None:
    """Private comments become ticket_notes once (rows this task added keep their task id)."""
    for c in comments:
        if c.get("public", True):
            continue
        created = iso(c.get("created_at")) or utc_now_iso()
        exists = company.row(
            "SELECT 1 FROM ticket_notes WHERE ticket_id = ? AND body = ?",
            (ticket_id, c.get("body", "")),
        )
        if not exists:
            company.execute(
                "INSERT INTO ticket_notes(ticket_id, author, body, created_at, task_id) VALUES (?,?,?,?,NULL)",
                (ticket_id, "staff", c.get("body", ""), created),
            )
