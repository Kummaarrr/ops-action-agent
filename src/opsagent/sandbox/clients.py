"""HTTP clients for the real systems: Shopify Admin REST, Stripe (test mode), a Zendesk-shaped
helpdesk. Synchronous httpx, one call per method, errors surfaced as `ApiError(status, message)`
so the backend can turn a system refusal into the model-visible `OperationError`.

The same classes talk to the mock (`mock_api.py`) when OPSAGENT_MOCK_URL is set: only the base
URL and the credentials differ, so the mock exercises exactly the code the real run uses.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from opsagent.config import Env

logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request otherwise

SHOPIFY_API_VERSION = "2024-10"
TIMEOUT = 20.0


class ApiError(Exception):
    """A non-2xx answer from a system (the message is what the system said)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class MissingCredentials(Exception):
    def __init__(self, fields: list[str]) -> None:
        super().__init__("missing credentials: " + ", ".join(fields))
        self.fields = fields


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        for key in ("errors", "error", "description", "message"):
            if body.get(key):
                v = body[key]
                if isinstance(v, dict):
                    return "; ".join(
                        f"{k}: {' '.join(map(str, vs)) if isinstance(vs, list) else vs}"
                        for k, vs in v.items()
                    )
                if isinstance(v, list):
                    return "; ".join(map(str, v))
                return str(v)
    return resp.text[:300]


def _check(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, _message(resp))
    if not resp.content:
        return {}
    return resp.json()


def _form(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Stripe's bracketed form encoding: {"metadata": {"a": 1}, "items": [{"price": p}]} ->
    metadata[a]=1, items[0][price]=p."""
    out: dict[str, str] = {}
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_form(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.update(_form(item, f"{key}[{i}]"))
                else:
                    out[f"{key}[{i}]"] = str(item)
        elif isinstance(v, bool):
            out[key] = "true" if v else "false"
        elif v is None:
            out[key] = ""
        else:
            out[key] = str(v)
    return out


# ====================================================================== Shopify
class ShopifyClient:
    """Shopify Admin REST API (a custom app's admin token on a development store)."""

    def __init__(self, base_url: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/admin/api/{SHOPIFY_API_VERSION}",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _check(self.http.get(path, params=params))

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return _check(self.http.post(path, json=payload))

    def _put(self, path: str, payload: dict[str, Any]) -> Any:
        return _check(self.http.put(path, json=payload))

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        return self._get(f"/customers/{customer_id}.json")["customer"]

    def search_customers(self, query: str) -> list[dict[str, Any]]:
        return list(self._get("/customers/search.json", {"query": query})["customers"])

    def customer_orders(self, customer_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/customers/{customer_id}/orders.json", {"status": "any"})["orders"])

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._get(f"/orders/{order_id}.json")["order"]

    def update_order(self, order_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        ident: Any = int(order_id) if str(order_id).isdigit() else order_id
        return self._put(f"/orders/{order_id}.json", {"order": {"id": ident, **fields}})["order"]

    def cancel_order(self, order_id: str, reason: str = "customer") -> dict[str, Any]:
        return self._post(f"/orders/{order_id}/cancel.json", {"reason": reason})["order"]

    # seeding
    def create_customer(self, customer: dict[str, Any]) -> dict[str, Any]:
        return self._post("/customers.json", {"customer": customer})["customer"]

    def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return self._post("/orders.json", {"order": order})["order"]

    def create_fulfillment(self, order_id: str, fulfillment: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/orders/{order_id}/fulfillments.json", {"fulfillment": fulfillment})[
            "fulfillment"
        ]


# ====================================================================== Stripe
class StripeClient:
    """Stripe API in test mode (a restricted test key is enough: charges, refunds, customers,
    subscriptions, prices read/write)."""

    def __init__(self, base_url: str, secret_key: str) -> None:
        self.http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/v1", auth=(secret_key, ""), timeout=TIMEOUT
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _check(self.http.get(path, params=params))

    def _post(self, path: str, data: dict[str, Any], idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return _check(self.http.post(path, data=_form(data), headers=headers))

    def _delete(self, path: str) -> Any:
        return _check(self.http.delete(path))

    def find_customer_by_email(self, email: str) -> dict[str, Any] | None:
        data = self._get("/customers", {"email": email, "limit": 1})["data"]
        return dict(data[0]) if data else None

    def list_charges(self, customer: str) -> list[dict[str, Any]]:
        return list(
            self._get("/charges", {"customer": customer, "limit": 100, "expand[]": "data.refunds"})[
                "data"
            ]
        )

    def get_charge(self, charge_id: str) -> dict[str, Any]:
        return self._get(f"/charges/{charge_id}", {"expand[]": "refunds"})

    def create_refund(
        self, charge: str, amount: int, idempotency_key: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        return self._post(
            "/refunds",
            {
                "charge": charge,
                "amount": amount,
                "reason": "requested_by_customer",
                "metadata": metadata,
            },
            idempotency_key=idempotency_key,
        )

    def list_prices(self) -> list[dict[str, Any]]:
        return list(self._get("/prices", {"active": "true", "limit": 100})["data"])

    def list_subscriptions(self, customer: str) -> list[dict[str, Any]]:
        return list(
            self._get("/subscriptions", {"customer": customer, "status": "all", "limit": 100})[
                "data"
            ]
        )

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._get(f"/subscriptions/{subscription_id}")

    def update_subscription(self, subscription_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/subscriptions/{subscription_id}", fields)

    def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._delete(f"/subscriptions/{subscription_id}")

    # seeding
    def create_customer(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._post("/customers", fields)

    def create_charge(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._post("/charges", fields)

    def create_price(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._post("/prices", fields)

    def create_subscription(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._post("/subscriptions", fields)


# ====================================================================== helpdesk
class HelpdeskClient:
    """Zendesk-shaped ticket API (API token auth: `email/token`)."""

    def __init__(self, base_url: str, email: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/api/v2",
            auth=(f"{email}/token", token),
            timeout=TIMEOUT,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _check(self.http.get(path, params=params))

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return _check(self.http.post(path, json=payload))

    def _put(self, path: str, payload: dict[str, Any]) -> Any:
        return _check(self.http.put(path, json=payload))

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return self._get(f"/tickets/{ticket_id}.json")["ticket"]

    def comments(self, ticket_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/tickets/{ticket_id}/comments.json")["comments"])

    def update_ticket(self, ticket_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._put(f"/tickets/{ticket_id}.json", {"ticket": fields})["ticket"]

    def search_tickets(self, query: str) -> list[dict[str, Any]]:
        return list(self._get("/search.json", {"query": query})["results"])

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._get(f"/users/{user_id}.json")["user"]

    def groups(self) -> list[dict[str, Any]]:
        return list(self._get("/groups.json")["groups"])

    # seeding
    def find_user(self, email: str) -> dict[str, Any] | None:
        users = self._get("/users/search.json", {"query": email})["users"]
        return dict(users[0]) if users else None

    def create_user(self, user: dict[str, Any]) -> dict[str, Any]:
        return self._post("/users.json", {"user": user})["user"]

    def create_group(self, name: str) -> dict[str, Any]:
        return self._post("/groups.json", {"group": {"name": name}})["group"]

    def create_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return self._post("/tickets.json", {"ticket": ticket})["ticket"]


# ====================================================================== wiring
@dataclass
class SandboxClients:
    shopify: ShopifyClient
    stripe: StripeClient
    helpdesk: HelpdeskClient
    mode: str  # mock | real


CREDENTIAL_FIELDS = (
    "shopify_store_domain",
    "shopify_admin_token",
    "stripe_test_secret_key",
    "helpdesk_subdomain",
    "helpdesk_email",
    "helpdesk_api_token",
)


def clients_from_env(env: Env | None = None, mock_url: str | None = None) -> SandboxClients:
    """Mock when OPSAGENT_MOCK_URL (or `mock_url`) is set; otherwise the real sandboxes from
    .env, refusing to start (MissingCredentials) when any field is empty."""
    mock = mock_url or os.environ.get("OPSAGENT_MOCK_URL", "")
    if mock:
        base = mock.rstrip("/")
        return SandboxClients(
            ShopifyClient(f"{base}/shopify", "mock-token"),
            StripeClient(f"{base}/stripe", "sk_test_mock"),
            HelpdeskClient(f"{base}/helpdesk", "agent@mock.example", "mock-token"),
            "mock",
        )
    env = env or Env()
    missing = [f.upper() for f in CREDENTIAL_FIELDS if not getattr(env, f)]
    if missing:
        raise MissingCredentials(missing)
    if not env.stripe_test_secret_key.startswith(("sk_test_", "rk_test_")):
        raise MissingCredentials(["STRIPE_TEST_SECRET_KEY (must be a test-mode key)"])
    domain = env.shopify_store_domain
    shop = domain if domain.startswith("http") else f"https://{domain}"
    return SandboxClients(
        ShopifyClient(shop, env.shopify_admin_token),
        StripeClient("https://api.stripe.com", env.stripe_test_secret_key),
        HelpdeskClient(
            f"https://{env.helpdesk_subdomain}.zendesk.com",
            env.helpdesk_email,
            env.helpdesk_api_token,
        ),
        "real",
    )
