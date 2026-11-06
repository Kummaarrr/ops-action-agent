"""An in-process HTTP mock of the few Shopify, Stripe and Zendesk endpoints the adapters use.

    python -m opsagent.sandbox.mock_api --port 8899     # or start_in_thread() from code

Payload shapes follow the real APIs (Shopify: JSON, numeric ids, prices as decimal strings,
ISO timestamps; Stripe: form-encoded requests, `ch_` / `re_` / `sub_` / `price_` ids, epoch
seconds, `{"error": {...}}` bodies, Idempotency-Key replay; Zendesk: `{"ticket": {...}}`
envelopes, public / private comments, groups). The system rules the real APIs enforce are here
too: a fulfilled order's shipping address cannot change and the order cannot be cancelled; a
refund cannot exceed the remaining amount; a disputed charge cannot be refunded; the same
idempotency key returns the same refund. Nothing about the company's policy lives here.

The mock starts empty; the smoke dataset is created through the same client calls that would
seed a real sandbox (see seed.py). `POST /_mock/reset` clears everything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "-00:00")


def _money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _unflatten(qs: dict[str, list[str]]) -> dict[str, Any]:
    """metadata[order_id]=x, items[0][price]=p, expand[]=refunds -> nested dict / lists."""
    out: dict[str, Any] = {}
    for key, values in qs.items():
        parts = [p for p in re.split(r"\[|\]", key) if p != ""] or [key]
        if key.endswith("[]"):
            parts = [key[:-2]]
            out.setdefault(parts[0], []).extend(values)
            continue
        node: Any = out
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                node[part] = values[-1]
            else:
                node = node.setdefault(part, {})
    return _lists(out)


def _lists(node: Any) -> Any:
    """{"0": a, "1": b} -> [a, b] (Stripe's indexed arrays)."""
    if isinstance(node, dict):
        node = {k: _lists(v) for k, v in node.items()}
        if node and all(k.isdigit() for k in node):
            return [node[k] for k in sorted(node, key=int)]
    return node


class MockError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


def _stripe_error(status: int, code: str, message: str) -> MockError:
    return MockError(
        status, {"error": {"type": "invalid_request_error", "code": code, "message": message}}
    )


class MockState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.n = 1000
        self.sh_customers: dict[str, dict[str, Any]] = {}
        self.sh_orders: dict[str, dict[str, Any]] = {}
        self.st_customers: dict[str, dict[str, Any]] = {}
        self.st_charges: dict[str, dict[str, Any]] = {}
        self.st_refunds: dict[str, dict[str, Any]] = {}
        self.st_prices: dict[str, dict[str, Any]] = {}
        self.st_subs: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[str, tuple[int, dict[str, Any]]] = {}
        self.zd_users: dict[str, dict[str, Any]] = {}
        self.zd_groups: dict[str, dict[str, Any]] = {}
        self.zd_tickets: dict[str, dict[str, Any]] = {}
        self.zd_comments: dict[str, list[dict[str, Any]]] = {}

    def nid(self) -> int:
        self.n += 1
        return self.n

    def counts(self) -> dict[str, int]:
        return {
            "shopify_customers": len(self.sh_customers),
            "shopify_orders": len(self.sh_orders),
            "stripe_customers": len(self.st_customers),
            "stripe_charges": len(self.st_charges),
            "stripe_refunds": len(self.st_refunds),
            "stripe_prices": len(self.st_prices),
            "stripe_subscriptions": len(self.st_subs),
            "helpdesk_users": len(self.zd_users),
            "helpdesk_tickets": len(self.zd_tickets),
            "helpdesk_groups": len(self.zd_groups),
        }

    # ================================================================== Shopify
    def shopify(
        self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        path = re.sub(r"^/admin/api/[^/]+", "", path)
        if m := re.fullmatch(r"/customers/search\.json", path):
            q = (query.get("query") or [""])[0]
            field, _, value = q.partition(":")
            value = value.strip().lower() or q.strip().lower()
            found = [
                c
                for c in self.sh_customers.values()
                if (field == "email" and c["email"].lower() == value)
                or (
                    field == "phone"
                    and re.sub(r"\D", "", c.get("phone") or "")[-10:]
                    == re.sub(r"\D", "", value)[-10:]
                )
                or (
                    field not in ("email", "phone")
                    and value in (c["email"].lower(), c.get("phone", ""))
                )
            ]
            return 200, {"customers": found}
        if method == "POST" and path == "/customers.json":
            c = body["customer"]
            cid = self.nid()
            addr = (c.get("addresses") or [{}])[0]
            row = {
                "id": cid,
                "first_name": c.get("first_name", ""),
                "last_name": c.get("last_name", ""),
                "email": c["email"],
                "phone": c.get("phone"),
                "note": c.get("note"),
                "tags": c.get("tags", ""),
                "created_at": _now_iso(),
                "default_address": {**addr, "id": self.nid(), "customer_id": cid} if addr else None,
                "orders_count": 0,
            }
            self.sh_customers[str(cid)] = row
            return 201, {"customer": row}
        if m := re.fullmatch(r"/customers/(\d+)\.json", path):
            c = self.sh_customers.get(m.group(1))
            if not c:
                raise MockError(404, {"errors": "Not Found"})
            return 200, {"customer": c}
        if m := re.fullmatch(r"/customers/(\d+)/orders\.json", path):
            if m.group(1) not in self.sh_customers:
                raise MockError(404, {"errors": "Not Found"})
            orders = [o for o in self.sh_orders.values() if str(o["customer"]["id"]) == m.group(1)]
            return 200, {"orders": sorted(orders, key=lambda o: o["created_at"], reverse=True)}
        if method == "POST" and path == "/orders.json":
            o = body["order"]
            cust = self.sh_customers.get(str((o.get("customer") or {}).get("id")))
            if not cust:
                raise MockError(422, {"errors": {"customer": ["not found"]}})
            oid = self.nid()
            items = [
                {
                    "id": self.nid(),
                    "title": li["title"],
                    "sku": li.get("sku", ""),
                    "quantity": int(li.get("quantity", 1)),
                    "price": str(li["price"]),
                }
                for li in o.get("line_items", [])
            ]
            total = sum(round(float(li["price"]) * 100) * li["quantity"] for li in items)
            row = {
                "id": oid,
                "name": f"#{oid}",
                "customer": {"id": cust["id"], "email": cust["email"]},
                "email": cust["email"],
                "created_at": o.get("created_at") or _now_iso(),
                "updated_at": _now_iso(),
                "financial_status": o.get("financial_status", "paid"),
                "fulfillment_status": None,
                "cancelled_at": None,
                "cancel_reason": None,
                "currency": o.get("currency", "USD"),
                "total_price": _money(total),
                "line_items": items,
                "shipping_address": o.get("shipping_address") or {},
                "fulfillments": [],
                "note": o.get("note"),
                "note_attributes": o.get("note_attributes", []),
                "tags": o.get("tags", ""),
            }
            self.sh_orders[str(oid)] = row
            cust["orders_count"] = cust.get("orders_count", 0) + 1
            return 201, {"order": row}
        if m := re.fullmatch(r"/orders/(\d+)\.json", path):
            o = self.sh_orders.get(m.group(1))
            if not o:
                raise MockError(404, {"errors": "Not Found"})
            if method == "PUT":
                fields = body["order"]
                if "shipping_address" in fields and o["fulfillment_status"] == "fulfilled":
                    raise MockError(
                        422,
                        {
                            "errors": {
                                "shipping_address": [
                                    "cannot be changed after the order has been fulfilled"
                                ]
                            }
                        },
                    )
                if "shipping_address" in fields and o["cancelled_at"]:
                    raise MockError(
                        422,
                        {
                            "errors": {
                                "shipping_address": ["cannot be changed on a cancelled order"]
                            }
                        },
                    )
                for k in ("shipping_address", "tags", "note", "note_attributes"):
                    if k in fields:
                        o[k] = (
                            fields[k]
                            if k != "shipping_address"
                            else {**o["shipping_address"], **fields[k]}
                        )
                o["updated_at"] = _now_iso()
            return 200, {"order": o}
        if m := re.fullmatch(r"/orders/(\d+)/cancel\.json", path):
            o = self.sh_orders.get(m.group(1))
            if not o:
                raise MockError(404, {"errors": "Not Found"})
            if o["cancelled_at"]:
                raise MockError(422, {"errors": ["Order has already been cancelled"]})
            if o["fulfillment_status"] == "fulfilled":
                raise MockError(
                    422, {"errors": ["Order cannot be cancelled after it has been fulfilled"]}
                )
            o["cancelled_at"] = _now_iso()
            o["cancel_reason"] = body.get("reason", "customer")
            o["updated_at"] = _now_iso()
            return 200, {"order": o}
        if m := re.fullmatch(r"/orders/(\d+)/fulfillments\.json", path):
            o = self.sh_orders.get(m.group(1))
            if not o:
                raise MockError(404, {"errors": "Not Found"})
            if o["cancelled_at"]:
                raise MockError(422, {"errors": ["Cancelled orders cannot be fulfilled"]})
            f = body.get("fulfillment", {})
            ful = {
                "id": self.nid(),
                "order_id": o["id"],
                "status": "success",
                "created_at": f.get("created_at") or _now_iso(),
                "updated_at": f.get("updated_at") or _now_iso(),
                "tracking_company": f.get("tracking_company", "UPS"),
                "tracking_number": f.get("tracking_number"),
                "shipment_status": f.get("shipment_status"),
            }
            o["fulfillments"].append(ful)
            o["fulfillment_status"] = "fulfilled"
            o["updated_at"] = _now_iso()
            return 201, {"fulfillment": ful}
        raise MockError(404, {"errors": f"no route {method} {path}"})

    # ================================================================== Stripe
    def stripe(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        form: dict[str, Any],
        idem_key: str | None,
    ) -> tuple[int, dict[str, Any]]:
        path = re.sub(r"^/v1", "", path)
        now = int(time.time())
        if path == "/customers" and method == "GET":
            email = (query.get("email") or [""])[0].lower()
            data = (
                [c for c in self.st_customers.values() if c["email"].lower() == email]
                if email
                else list(self.st_customers.values())
            )
            return 200, {"object": "list", "data": data}
        if path == "/customers" and method == "POST":
            cid = f"cus_mock{self.nid()}"
            row = {
                "id": cid,
                "object": "customer",
                "email": form.get("email"),
                "name": form.get("name"),
                "created": now,
                "metadata": form.get("metadata", {}),
            }
            self.st_customers[cid] = row
            return 200, row
        if m := re.fullmatch(r"/customers/(cus_\w+)", path):
            c = self.st_customers.get(m.group(1))
            if not c:
                raise _stripe_error(404, "resource_missing", f"No such customer: '{m.group(1)}'")
            return 200, c
        if path == "/charges" and method == "GET":
            cust = (query.get("customer") or [""])[0]
            data = [c for c in self.st_charges.values() if not cust or c["customer"] == cust]
            return 200, {
                "object": "list",
                "data": sorted(data, key=lambda c: c["created"], reverse=True),
            }
        if path == "/charges" and method == "POST":
            cust_id = form.get("customer")
            if cust_id and cust_id not in self.st_customers:
                raise _stripe_error(400, "resource_missing", f"No such customer: '{cust_id}'")
            source = form.get("source", "tok_visa")
            chid = f"ch_mock{self.nid()}"
            last4 = {
                "tok_visa": "4242",
                "tok_mastercard": "5555",
                "tok_createDispute": "0259",
                "tok_amex": "8431",
            }.get(source, source[-4:])
            row = {
                "id": chid,
                "object": "charge",
                "amount": int(form["amount"]),
                "amount_refunded": 0,
                "currency": form.get("currency", "usd"),
                "customer": cust_id,
                "created": int(form["metadata"].pop("created_override"))
                if isinstance(form.get("metadata"), dict) and "created_override" in form["metadata"]
                else now,
                "description": form.get("description"),
                "disputed": source == "tok_createDispute",
                "refunded": False,
                "status": "succeeded",
                "paid": True,
                "payment_method_details": {
                    "type": "card",
                    "card": {"brand": "visa", "last4": last4},
                },
                "metadata": form.get("metadata", {}),
                "refunds": {"object": "list", "data": []},
            }
            self.st_charges[chid] = row
            return 200, row
        if m := re.fullmatch(r"/charges/(ch_\w+)", path):
            c = self.st_charges.get(m.group(1))
            if not c:
                raise _stripe_error(404, "resource_missing", f"No such charge: '{m.group(1)}'")
            return 200, c
        if path == "/refunds" and method == "POST":
            if idem_key and idem_key in self.idempotency:
                return self.idempotency[idem_key]
            ch = self.st_charges.get(str(form.get("charge")))
            if not ch:
                raise _stripe_error(
                    404, "resource_missing", f"No such charge: '{form.get('charge')}'"
                )
            if ch["disputed"]:
                raise _stripe_error(
                    400,
                    "charge_disputed",
                    f"Charge {ch['id']} has been charged back; refunds are not possible while a dispute is open.",
                )
            remaining = ch["amount"] - ch["amount_refunded"]
            amount = int(form.get("amount", remaining))
            if remaining <= 0:
                raise _stripe_error(
                    400, "charge_already_refunded", f"Charge {ch['id']} has already been refunded."
                )
            if amount <= 0 or amount > remaining:
                raise _stripe_error(
                    400,
                    "amount_too_large",
                    f"Refund amount (${amount / 100:.2f}) is greater than unrefunded amount on charge (${remaining / 100:.2f})",
                )
            rid = f"re_mock{self.nid()}"
            refund = {
                "id": rid,
                "object": "refund",
                "amount": amount,
                "charge": ch["id"],
                "created": now,
                "currency": ch["currency"],
                "reason": form.get("reason", "requested_by_customer"),
                "status": "succeeded",
                "metadata": form.get("metadata", {}),
            }
            self.st_refunds[rid] = refund
            ch["amount_refunded"] += amount
            ch["refunded"] = ch["amount_refunded"] >= ch["amount"]
            ch["refunds"]["data"].append(refund)
            resp = (200, refund)
            if idem_key:
                self.idempotency[idem_key] = resp
            return resp
        if m := re.fullmatch(r"/refunds/(re_\w+)", path):
            r = self.st_refunds.get(m.group(1))
            if not r:
                raise _stripe_error(404, "resource_missing", f"No such refund: '{m.group(1)}'")
            return 200, r
        if path == "/prices" and method == "GET":
            return 200, {
                "object": "list",
                "data": [p for p in self.st_prices.values() if p["active"]],
            }
        if path == "/prices" and method == "POST":
            pid = f"price_mock{self.nid()}"
            row = {
                "id": pid,
                "object": "price",
                "active": True,
                "currency": form.get("currency", "usd"),
                "unit_amount": int(form["unit_amount"]),
                "nickname": form.get("nickname"),
                "recurring": {
                    "interval": form.get("recurring", {}).get("interval", "month"),
                    "interval_count": 1,
                },
                "product": f"prod_mock{self.nid()}",
                "metadata": form.get("metadata", {}),
            }
            self.st_prices[pid] = row
            return 200, row
        if path == "/subscriptions" and method == "GET":
            cust = (query.get("customer") or [""])[0]
            data = [s for s in self.st_subs.values() if not cust or s["customer"] == cust]
            return 200, {"object": "list", "data": data}
        if path == "/subscriptions" and method == "POST":
            cust_id = form.get("customer")
            if cust_id not in self.st_customers:
                raise _stripe_error(400, "resource_missing", f"No such customer: '{cust_id}'")
            items = form.get("items") or []
            price = self.st_prices.get(str(items[0].get("price"))) if items else None
            if not price:
                raise _stripe_error(400, "resource_missing", "No such price")
            start = int(form.get("backdate_start_date", now))
            period_start, period_end = self._period(price["recurring"]["interval"], now)
            sid = f"sub_mock{self.nid()}"
            row = {
                "id": sid,
                "object": "subscription",
                "customer": cust_id,
                "status": "active",
                "created": start,
                "start_date": start,
                "current_period_start": period_start,
                "current_period_end": period_end,
                "cancel_at_period_end": False,
                "cancel_at": None,
                "canceled_at": None,
                "pause_collection": None,
                "items": {
                    "object": "list",
                    "data": [
                        {
                            "id": f"si_mock{self.nid()}",
                            "object": "subscription_item",
                            "price": price,
                        }
                    ],
                },
                "metadata": form.get("metadata", {}),
            }
            self.st_subs[sid] = row
            return 200, row
        if m := re.fullmatch(r"/subscriptions/(sub_\w+)", path):
            s = self.st_subs.get(m.group(1))
            if not s:
                raise _stripe_error(
                    404, "resource_missing", f"No such subscription: '{m.group(1)}'"
                )
            if method == "DELETE":
                if s["status"] == "canceled":
                    raise _stripe_error(
                        400,
                        "resource_missing",
                        f"No such subscription: '{s['id']}' (it is canceled)",
                    )
                s["status"] = "canceled"
                s["canceled_at"] = now
                s["cancel_at"] = now
                return 200, s
            if method == "POST":
                if s["status"] == "canceled":
                    raise _stripe_error(
                        400,
                        "resource_missing",
                        f"Subscription {s['id']} is canceled and cannot be updated.",
                    )
                if "pause_collection" in form:
                    pc = form["pause_collection"]
                    if pc in ("", None):
                        s["pause_collection"] = None
                    else:
                        s["pause_collection"] = {
                            "behavior": pc.get("behavior", "void"),
                            "resumes_at": int(pc["resumes_at"]) if pc.get("resumes_at") else None,
                        }
                if "cancel_at_period_end" in form:
                    flag = str(form["cancel_at_period_end"]).lower() == "true"
                    s["cancel_at_period_end"] = flag
                    s["cancel_at"] = s["current_period_end"] if flag else None
                if "items" in form:
                    for item in form["items"]:
                        price = self.st_prices.get(str(item.get("price")))
                        if not price:
                            raise _stripe_error(400, "resource_missing", "No such price")
                        current = s["items"]["data"][0]
                        if (
                            price["recurring"]["interval"]
                            != current["price"]["recurring"]["interval"]
                        ):
                            raise _stripe_error(
                                400,
                                "invalid_request_error",
                                "Cannot change the billing interval of an active subscription item",
                            )
                        current["price"] = price
                if "metadata" in form:
                    s["metadata"] = {**s["metadata"], **form["metadata"]}
                return 200, s
            return 200, s
        raise _stripe_error(404, "resource_missing", f"Unrecognized request URL ({method}: {path})")

    @staticmethod
    def _period(interval: str, now: int) -> tuple[int, int]:
        start = dt.datetime.fromtimestamp(now, dt.UTC)
        if interval == "year":
            begin = start.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = begin.replace(year=begin.year + 1)
        else:
            begin = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = (begin + dt.timedelta(days=32)).replace(day=1)
        return int(begin.timestamp()), int(end.timestamp())

    # ================================================================== Zendesk
    def helpdesk(
        self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        path = re.sub(r"^/api/v2", "", path)
        if path == "/users/search.json":
            q = (query.get("query") or [""])[0].replace("email:", "").strip().lower()
            return 200, {"users": [u for u in self.zd_users.values() if u["email"].lower() == q]}
        if path == "/users.json" and method == "POST":
            u = body["user"]
            uid = self.nid()
            row = {
                "id": uid,
                "name": u.get("name", ""),
                "email": u["email"],
                "role": u.get("role", "end-user"),
                "created_at": _now_iso(),
            }
            self.zd_users[str(uid)] = row
            return 201, {"user": row}
        if m := re.fullmatch(r"/users/(\d+)\.json", path):
            u = self.zd_users.get(m.group(1))
            if not u:
                raise MockError(404, {"error": "RecordNotFound", "description": "Not found"})
            return 200, {"user": u}
        if path == "/groups.json":
            if method == "POST":
                gid = self.nid()
                row = {"id": gid, "name": body["group"]["name"], "created_at": _now_iso()}
                self.zd_groups[str(gid)] = row
                return 201, {"group": row}
            return 200, {"groups": list(self.zd_groups.values())}
        if path == "/tickets.json" and method == "POST":
            t = body["ticket"]
            req = t.get("requester") or {}
            user = None
            if t.get("requester_id"):
                user = self.zd_users.get(str(t["requester_id"]))
            elif req.get("email"):
                user = next(
                    (
                        u
                        for u in self.zd_users.values()
                        if u["email"].lower() == req["email"].lower()
                    ),
                    None,
                )
                if not user:
                    _, created = self.helpdesk(
                        "POST",
                        "/users.json",
                        {},
                        {"user": {"name": req.get("name", req["email"]), "email": req["email"]}},
                    )
                    user = created["user"]
            if not user:
                raise MockError(
                    422,
                    {"error": "RecordInvalid", "description": "Requester: requester is required"},
                )
            tid = self.nid()
            comment = t.get("comment") or {}
            tags = t.get("tags") or []
            row = {
                "id": tid,
                "subject": t.get("subject", ""),
                "description": comment.get("body", ""),
                "status": "open",
                "requester_id": user["id"],
                "group_id": t.get("group_id"),
                "tags": tags,
                "via": {"channel": (t.get("via") or {}).get("channel", "email")},
                "created_at": t.get("created_at") or _now_iso(),
                "updated_at": _now_iso(),
            }
            self.zd_tickets[str(tid)] = row
            self.zd_comments[str(tid)] = [
                {
                    "id": self.nid(),
                    "author_id": user["id"],
                    "body": comment.get("body", ""),
                    "public": True,
                    "created_at": row["created_at"],
                    "attachments": [{"file_name": n} for n in comment.get("uploads", [])],
                }
            ]
            return 201, {"ticket": row}
        if m := re.fullmatch(r"/tickets/(\d+)\.json", path):
            t = self.zd_tickets.get(m.group(1))
            if not t:
                raise MockError(404, {"error": "RecordNotFound", "description": "Not found"})
            if method == "PUT":
                fields = body["ticket"]
                if "status" in fields and fields["status"] not in (
                    "new",
                    "open",
                    "pending",
                    "hold",
                    "solved",
                    "closed",
                ):
                    raise MockError(
                        422,
                        {
                            "error": "RecordInvalid",
                            "description": f"Status: {fields['status']} is not a valid status",
                        },
                    )
                comment = fields.get("comment")
                if comment and comment.get("body"):
                    self.zd_comments[t_id := str(t["id"])].append(
                        {
                            "id": self.nid(),
                            "author_id": 1,
                            "body": comment["body"],
                            "public": bool(comment.get("public", True)),
                            "created_at": _now_iso(),
                            "attachments": [],
                        }
                    )
                    del t_id
                if "status" in fields:
                    t["status"] = fields["status"]
                if "tags" in fields:
                    t["tags"] = list(fields["tags"])
                if "additional_tags" in fields:
                    t["tags"] = list(dict.fromkeys([*t["tags"], *fields["additional_tags"]]))
                if "group_id" in fields:
                    if str(fields["group_id"]) not in self.zd_groups:
                        raise MockError(
                            422, {"error": "RecordInvalid", "description": "Group: unknown group"}
                        )
                    t["group_id"] = fields["group_id"]
                t["updated_at"] = _now_iso()
            return 200, {"ticket": t}
        if m := re.fullmatch(r"/tickets/(\d+)/comments\.json", path):
            if m.group(1) not in self.zd_tickets:
                raise MockError(404, {"error": "RecordNotFound", "description": "Not found"})
            return 200, {"comments": self.zd_comments.get(m.group(1), [])}
        if path == "/search.json":
            q = (query.get("query") or [""])[0]
            email = ""
            if mm := re.search(r"requester:(\S+)", q):
                email = mm.group(1).lower()
            user = next((u for u in self.zd_users.values() if u["email"].lower() == email), None)
            results = [
                {**t, "result_type": "ticket"}
                for t in self.zd_tickets.values()
                if user and t["requester_id"] == user["id"]
            ]
            return 200, {"results": results, "count": len(results)}
        raise MockError(
            404, {"error": "RecordNotFound", "description": f"no route {method} {path}"}
        )


class MockHandler(BaseHTTPRequestHandler):
    state: MockState

    def log_message(self, format: str, *args: Any) -> None:  # BaseHTTPRequestHandler's name
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _dispatch(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        raw = self._body()
        method = self.command
        try:
            with self.state.lock:
                if parts.path.startswith("/_mock/"):
                    status, payload = self._control(parts.path)
                elif parts.path.startswith("/shopify"):
                    body = json.loads(raw) if raw else {}
                    status, payload = self.state.shopify(
                        method, parts.path[len("/shopify") :], query, body
                    )
                elif parts.path.startswith("/stripe"):
                    form = _unflatten(parse_qs(raw.decode(), keep_blank_values=True)) if raw else {}
                    status, payload = self.state.stripe(
                        method,
                        parts.path[len("/stripe") :],
                        query,
                        form,
                        self.headers.get("Idempotency-Key"),
                    )
                elif parts.path.startswith("/helpdesk"):
                    body = json.loads(raw) if raw else {}
                    status, payload = self.state.helpdesk(
                        method, parts.path[len("/helpdesk") :], query, body
                    )
                else:
                    status, payload = 404, {"error": f"unknown system in {parts.path}"}
        except MockError as e:
            status, payload = e.status, e.body
        except (KeyError, ValueError, TypeError) as e:
            status, payload = 400, {"error": f"bad request: {type(e).__name__}: {e}"}
        data = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _control(self, path: str) -> tuple[int, dict[str, Any]]:
        if path == "/_mock/reset":
            self.state.reset()
            return 200, {"reset": True}
        if path == "/_mock/health":
            return 200, {"ok": True}
        if path == "/_mock/state":
            return 200, self.state.counts()
        return 404, {"error": "unknown control"}

    do_GET = do_POST = do_PUT = do_DELETE = _dispatch


def start_in_thread(
    port: int = 0, state: MockState | None = None
) -> tuple[ThreadingHTTPServer, str]:
    """Serve the mock on 127.0.0.1 in a daemon thread; returns (server, base url)."""
    st = state or MockState()
    handler = type("BoundMockHandler", (MockHandler,), {"state": st})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="mock-api").start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    server, url = start_in_thread(args.port)
    print(f"mock sandbox APIs at {url}  (shopify | stripe | helpdesk; POST /_mock/reset)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
