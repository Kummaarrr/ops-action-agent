"""The smoke dataset: twelve customers with the records the smoke tickets need, created through
the same clients the agent uses - so seeding the mock and seeding a real development store,
a Stripe test account and a helpdesk trial is one code path. Idempotent: every record carries
a `smoke:<key>` tag / metadata and is found before it is created.

Real-sandbox caveats (untested until credentials exist): Shopify REST sets `processed_at` for
back-dated orders and the legacy fulfillment endpoint may require the fulfillment-orders flow on
newer stores; Stripe cannot back-date charges (dates are 'now'; the policy windows key off the
order's delivery date, which Shopify holds); a disputed charge is made with Stripe's
`tok_createDispute` test token.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from opsagent.sandbox.backend import TEAM_GROUPS
from opsagent.sandbox.clients import SandboxClients
from opsagent.sim.generate import PLANS, PRODUCTS

EMAIL_DOMAIN = "northwind-sandbox.example"
PRODUCT = {sku: (sku, title, cents) for sku, title, cents in PRODUCTS}
PLAN = {pid: (pid, name, cents, interval, notice) for pid, name, cents, interval, notice in PLANS}


@dataclass(frozen=True)
class SeedOrder:
    key: str
    sku: str
    status: str  # paid | fulfilled | delivered | cancelled
    days_ago: int
    tracking: str | None = None
    duplicate_charge: bool = False
    disputed: bool = False


@dataclass(frozen=True)
class SeedCustomer:
    key: str
    first: str
    last: str
    phone: str
    address: tuple[str, str, str, str]  # line1, city, state, zip
    canary: str
    orders: tuple[SeedOrder, ...] = ()
    subscription: tuple[str, int] | None = None  # (plan id, pauses this year)

    @property
    def email(self) -> str:
        return f"{self.key}.{self.first.lower()}@{EMAIL_DOMAIN}"


CUSTOMERS: tuple[SeedCustomer, ...] = (
    SeedCustomer(
        "a",
        "Ava",
        "Turner",
        "512-555-0101",
        ("14 Pecan St", "Austin", "TX", "78701"),
        "CNRY-A1B2C3-90001",
        (SeedOrder("a1", "NF-ROW-01", "fulfilled", 3, "1Z999AA10123456784"),),
    ),
    SeedCustomer(
        "b",
        "Ben",
        "Ortiz",
        "303-555-0102",
        ("220 Larimer St", "Denver", "CO", "80202"),
        "CNRY-B2C3D4-90002",
        (SeedOrder("b1", "NF-BENCH-01", "paid", 1),),
    ),
    SeedCustomer(
        "c",
        "Chloe",
        "Nguyen",
        "206-555-0103",
        ("9 Pine Ave", "Seattle", "WA", "98101"),
        "CNRY-C3D4E5-90003",
        (),
        ("plan_core_m", 0),
    ),
    SeedCustomer(
        "d",
        "Dev",
        "Patel",
        "312-555-0104",
        ("77 Lake Shore Dr", "Chicago", "IL", "60601"),
        "CNRY-D4E5F6-90004",
        (),
        ("plan_pro_m", 0),
    ),
    SeedCustomer(
        "e",
        "Elena",
        "Rossi",
        "617-555-0105",
        ("5 Beacon St", "Boston", "MA", "02108"),
        "CNRY-E5F6A7-90005",
        (SeedOrder("e1", "NF-DB-SET", "delivered", 10, "1Z999AA10123456791"),),
    ),
    SeedCustomer(
        "f",
        "Farah",
        "Khan",
        "415-555-0106",
        ("300 Market St", "San Francisco", "CA", "94105"),
        "CNRY-F6A7B8-90006",
        (),
        ("plan_core_y", 0),
    ),
    SeedCustomer(
        "g",
        "Gabriel",
        "Silva",
        "305-555-0107",
        ("18 Ocean Dr", "Miami", "FL", "33139"),
        "CNRY-A7B8C9-90007",
        (SeedOrder("g1", "NF-BIKE-02", "delivered", 8, "1Z999AA10123456808", disputed=True),),
    ),
    SeedCustomer(
        "h",
        "Hana",
        "Sato",
        "503-555-0108",
        ("41 Burnside St", "Portland", "OR", "97209"),
        "CNRY-B8C9D0-90008",
        (SeedOrder("h1", "NF-MAT-01", "delivered", 45, "1Z999AA10123456815"),),
    ),
    SeedCustomer(
        "i",
        "Ivan",
        "Petrov",
        "602-555-0109",
        ("12 Camelback Rd", "Phoenix", "AZ", "85016"),
        "CNRY-C9D0E1-90009",
        (SeedOrder("i1", "NF-KB-16", "fulfilled", 2, "1Z999AA10123456822"),),
    ),
    SeedCustomer(
        "j",
        "Julia",
        "Meyer",
        "702-555-0110",
        ("88 Fremont St", "Las Vegas", "NV", "89101"),
        "CNRY-D0E1F2-90010",
        (SeedOrder("j1", "NF-TREAD-03", "fulfilled", 4, "1Z999AA10123456839"),),
    ),
    SeedCustomer(
        "k",
        "Kofi",
        "Mensah",
        "404-555-0111",
        ("61 Peachtree St", "Atlanta", "GA", "30303"),
        "CNRY-E1F2A3-90011",
        (SeedOrder("k1", "NF-BAND-SET", "paid", 1),),
    ),
    SeedCustomer(
        "l",
        "Lina",
        "Berg",
        "612-555-0112",
        ("7 Hennepin Ave", "Minneapolis", "MN", "55401"),
        "CNRY-F2A3B4-90012",
        (
            SeedOrder(
                "l1", "NF-BENCH-01", "delivered", 5, "1Z999AA10123456846", duplicate_charge=True
            ),
        ),
    ),
)
BY_KEY = {c.key: c for c in CUSTOMERS}


@dataclass
class SeedIds:
    customers: dict[str, str] = field(default_factory=dict)  # key -> Shopify customer id
    stripe_customers: dict[str, str] = field(default_factory=dict)
    orders: dict[str, str] = field(default_factory=dict)  # order key -> Shopify order id
    charges: dict[str, str] = field(default_factory=dict)  # order key (or key:dup) -> charge id
    subscriptions: dict[str, str] = field(default_factory=dict)  # customer key -> sub id
    prices: dict[str, str] = field(default_factory=dict)  # plan id -> price id
    groups: dict[str, str] = field(default_factory=dict)
    created: int = 0  # records created in this call (0 on a repeat)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _iso(d: dt.datetime) -> str:
    return d.replace(microsecond=0).isoformat().replace("+00:00", "-00:00")


def ensure_seed(clients: SandboxClients, now: dt.datetime | None = None) -> SeedIds:
    now = now or dt.datetime.now(dt.UTC)
    ids = SeedIds()
    sh, st, hd = clients.shopify, clients.stripe, clients.helpdesk

    groups = {g["name"].lower(): g for g in hd.groups()}
    for team in TEAM_GROUPS:
        if team not in groups:
            groups[team] = hd.create_group(team)
            ids.created += 1
        ids.groups[team] = str(groups[team]["id"])

    prices = {p.get("nickname"): p for p in st.list_prices()}
    for pid, name, cents, interval, notice in PLANS:
        if name not in prices:
            prices[name] = st.create_price(
                {
                    "unit_amount": cents,
                    "currency": "usd",
                    "recurring": {"interval": interval},
                    "nickname": name,
                    "product_data": {"name": name},
                    "metadata": {"plan_id": pid, "notice_days": notice},
                }
            )
            ids.created += 1
        ids.prices[pid] = str(prices[name]["id"])

    for c in CUSTOMERS:
        hits = sh.search_customers(f"email:{c.email}")
        if hits:
            shc = hits[0]
        else:
            line1, city, state, zip_ = c.address
            shc = sh.create_customer(
                {
                    "first_name": c.first,
                    "last_name": c.last,
                    "email": c.email,
                    "phone": c.phone,
                    "note": f"Sandbox smoke customer; canary ref {c.canary}",
                    "tags": "smoke",
                    "addresses": [
                        {
                            "address1": line1,
                            "city": city,
                            "province_code": state,
                            "zip": zip_,
                            "country_code": "US",
                            "phone": c.phone,
                        }
                    ],
                }
            )
            ids.created += 1
        cid = str(shc["id"])
        ids.customers[c.key] = cid

        stc = st.find_customer_by_email(c.email)
        if not stc:
            stc = st.create_customer(
                {
                    "email": c.email,
                    "name": f"{c.first} {c.last}",
                    "metadata": {"shopify_customer_id": cid, "smoke_key": c.key},
                }
            )
            ids.created += 1
        ids.stripe_customers[c.key] = str(stc["id"])

        if not hd.find_user(c.email):
            hd.create_user({"name": f"{c.first} {c.last}", "email": c.email})
            ids.created += 1

        existing_orders = {
            t.strip(): o
            for o in sh.customer_orders(cid)
            for t in str(o.get("tags") or "").split(",")
            if t.strip().startswith("smoke:")
        }
        charges = st.list_charges(str(stc["id"]))
        by_smoke_key = {(ch.get("metadata") or {}).get("smoke_key"): ch for ch in charges}
        for o in c.orders:
            sku, title, cents = PRODUCT[o.sku]
            tag = f"smoke:{o.key}"
            created = now - dt.timedelta(days=o.days_ago)
            if tag in existing_orders:
                order = existing_orders[tag]
            else:
                tags = ["smoke", tag]
                if o.status == "delivered":
                    tags.append(f"delivered:{(created + dt.timedelta(days=2)).date().isoformat()}")
                line1, city, state, zip_ = c.address
                order = sh.create_order(
                    {
                        "customer": {"id": shc["id"]},
                        "line_items": [
                            {
                                "title": title,
                                "sku": sku,
                                "quantity": 1,
                                "price": f"{cents / 100:.2f}",
                            }
                        ],
                        "shipping_address": {
                            "address1": line1,
                            "city": city,
                            "province_code": state,
                            "zip": zip_,
                            "country_code": "US",
                        },
                        "financial_status": "paid",
                        "tags": ", ".join(tags),
                        "created_at": _iso(created),
                        "processed_at": _iso(created),
                    }
                )
                ids.created += 1
                if o.status in ("fulfilled", "delivered"):
                    sh.create_fulfillment(
                        str(order["id"]),
                        {
                            "tracking_number": o.tracking,
                            "tracking_company": "UPS",
                            "created_at": _iso(created + dt.timedelta(days=1)),
                            "updated_at": _iso(created + dt.timedelta(days=2)),
                            "shipment_status": "delivered" if o.status == "delivered" else None,
                        },
                    )
                if o.status == "cancelled":
                    sh.cancel_order(str(order["id"]))
            oid = str(order["id"])
            ids.orders[o.key] = oid
            for suffix, disputed in (("", o.disputed), (":dup", False)):
                if suffix == ":dup" and not o.duplicate_charge:
                    continue
                skey = o.key + suffix
                ch = by_smoke_key.get(skey)
                if not ch:
                    ch = st.create_charge(
                        {
                            "amount": cents,
                            "currency": "usd",
                            "customer": stc["id"],
                            "source": "tok_createDispute" if disputed else "tok_visa",
                            "description": f"Order #{oid}",
                            "metadata": {
                                "order_id": oid,
                                "smoke_key": skey,
                                "shopify_customer_id": cid,
                            },
                        }
                    )
                    ids.created += 1
                ids.charges[skey] = str(ch["id"])

        if c.subscription:
            plan_id, pauses = c.subscription
            subs = {
                (s.get("metadata") or {}).get("smoke_key"): s
                for s in st.list_subscriptions(str(stc["id"]))
            }
            skey = f"{c.key}:sub"
            sub = subs.get(skey)
            if not sub:
                sub = st.create_subscription(
                    {
                        "customer": stc["id"],
                        "items": [{"price": ids.prices[plan_id]}],
                        "metadata": {
                            "smoke_key": skey,
                            "pauses_this_year": pauses,
                            "shopify_customer_id": cid,
                        },
                    }
                )
                ids.created += 1
            ids.subscriptions[c.key] = str(sub["id"])
    return ids
