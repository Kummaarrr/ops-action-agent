"""The tool catalogue: every tool the agent can see, by server, with the facts the guard and
the scorer need - is it a read or a write, does it touch a customer's account (so identity
must be verified first), and which record it acts on. The MCP servers expose the tools; this
file is the single place that says what they are.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    server: str
    name: str
    kind: str  # read | write | approval
    account_scoped: bool  # touches one customer's data (identity verification applies)
    resource: str = ""  # the id parameter naming the record acted on, e.g. order_id

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"


CATALOG: list[ToolSpec] = [
    # store (customers, orders, appointments) - Shopify-like
    ToolSpec("store", "find_customer", "read", False),
    ToolSpec("store", "get_customer", "read", True, "customer_id"),
    ToolSpec("store", "verify_identity", "read", False, "customer_id"),
    ToolSpec("store", "list_orders", "read", True, "customer_id"),
    ToolSpec("store", "get_order", "read", True, "order_id"),
    ToolSpec("store", "update_shipping_address", "write", True, "order_id"),
    ToolSpec("store", "cancel_order", "write", True, "order_id"),
    ToolSpec("store", "create_return", "write", True, "order_id"),
    ToolSpec("store", "list_appointments", "read", True, "customer_id"),
    ToolSpec("store", "list_slots", "read", False),
    ToolSpec("store", "reschedule_appointment", "write", True, "appointment_id"),
    ToolSpec("store", "cancel_appointment", "write", True, "appointment_id"),
    # billing (charges, refunds, subscriptions) - Stripe-like
    ToolSpec("billing", "list_charges", "read", True, "customer_id"),
    ToolSpec("billing", "get_charge", "read", True, "charge_id"),
    ToolSpec("billing", "create_refund", "write", True, "charge_id"),
    ToolSpec("billing", "list_plans", "read", False),
    ToolSpec("billing", "list_subscriptions", "read", True, "customer_id"),
    ToolSpec("billing", "get_subscription", "read", True, "subscription_id"),
    ToolSpec("billing", "preview_plan_change", "read", True, "subscription_id"),
    ToolSpec("billing", "pause_subscription", "write", True, "subscription_id"),
    ToolSpec("billing", "resume_subscription", "write", True, "subscription_id"),
    ToolSpec("billing", "cancel_subscription", "write", True, "subscription_id"),
    ToolSpec("billing", "change_plan", "write", True, "subscription_id"),
    # helpdesk (tickets) - Zendesk-like
    ToolSpec("helpdesk", "get_ticket", "read", False, "ticket_id"),
    ToolSpec("helpdesk", "list_tickets", "read", True, "customer_id"),
    ToolSpec("helpdesk", "list_notes", "read", False, "ticket_id"),
    ToolSpec("helpdesk", "add_internal_note", "write", False, "ticket_id"),
    ToolSpec("helpdesk", "reply_to_customer", "write", False, "ticket_id"),
    ToolSpec("helpdesk", "set_status", "write", False, "ticket_id"),
    ToolSpec("helpdesk", "escalate", "write", False, "ticket_id"),
    # kb (help centre + policy)
    ToolSpec("kb", "search_articles", "read", False),
    ToolSpec("kb", "get_article", "read", False),
    ToolSpec("kb", "get_policy", "read", False),
    # approvals (the supervisor inbox)
    ToolSpec("approvals", "request_approval", "approval", False, "ticket_id"),
    ToolSpec("approvals", "get_approval", "read", False),
]
BY_NAME: dict[str, ToolSpec] = {t.qualified: t for t in CATALOG}
# tools the guard uses on the model's behalf; the model never sees them
GUARD_ONLY = {"approvals.request_approval"}
# helpdesk writes may only act on the ticket being worked; helpdesk reads may also cover
# other tickets of the same verified customer (prior correspondence is evidence)
TICKET_PINNED = {
    "helpdesk.add_internal_note",
    "helpdesk.reply_to_customer",
    "helpdesk.set_status",
    "helpdesk.escalate",
}
TICKET_READS = {"helpdesk.get_ticket", "helpdesk.list_notes"}
# verification level needed (P1.2 tiers): 1 = sender email matches, 2 = email + one fact
LEVEL_REQUIRED = {
    "store.get_customer": 1,
    "store.list_orders": 1,
    "store.get_order": 1,
    "store.list_appointments": 1,
    "billing.list_charges": 1,
    "billing.get_charge": 1,
    "billing.list_subscriptions": 1,
    "billing.get_subscription": 1,
    "billing.preview_plan_change": 1,
    "helpdesk.list_tickets": 1,
    "billing.pause_subscription": 1,
    "billing.resume_subscription": 1,
    "billing.cancel_subscription": 1,
    "store.update_shipping_address": 2,
    "store.cancel_order": 2,
    "store.create_return": 2,
    "store.reschedule_appointment": 2,
    "store.cancel_appointment": 2,
    "billing.create_refund": 2,
    "billing.change_plan": 2,
}
SERVERS = ("store", "billing", "helpdesk", "kb", "approvals")
