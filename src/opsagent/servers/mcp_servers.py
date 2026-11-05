"""The five MCP servers (store, billing, helpdesk, kb, approvals) over the simulated backend.

Run one over stdio:  python -m opsagent.servers.mcp_servers store   (OPSAGENT_DB = the .db file)

Each tool is a thin wrapper around a Backend method: the schema comes from the method's
signature, the annotations from the catalogue (read-only / destructive), and a system refusal
(OperationError) is returned as a tool error the model can read and act on. No policy here.
"""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from opsagent.servers.backend import Backend, OperationError
from opsagent.servers.catalog import CATALOG, ToolSpec
from opsagent.sim.company import Company

DESCRIPTIONS: dict[str, str] = {
    "store.find_customer": "Find customers by exact email, order id or full phone number. Returns the customer id, first name and a masked email only.",
    "store.get_customer": "The full customer record: contact details, address, loyalty tier, staff notes.",
    "store.verify_identity": "Verify the ticket's requester against a customer record. The sender email is read from the ticket by the system (not supplied by you). Returns level 1 when the sender email is the account's email, level 2 when one more fact you pass also matches the record (an order id on the account, the billing zip, or the card's last four), and which facts matched - never the stored values.",
    "store.list_orders": "Orders of a customer, newest first, with status and dates.",
    "store.get_order": "One order with items, shipping address, fulfilment / delivery dates, tracking, charges and the customer's order note.",
    "store.update_shipping_address": "Change an order's shipping address. The store only allows this before fulfilment.",
    "store.cancel_order": "Cancel an unfulfilled order (status paid). Does not refund by itself.",
    "store.create_return": "Open a return (RMA) for a delivered order and send the customer the return label. The refund is issued by the warehouse when the item is received. Refused if the order is not delivered or already has an open return.",
    "store.list_appointments": "Installation / fitting appointments of a customer.",
    "store.list_slots": "Open appointment slots between two dates (ISO dates or datetimes).",
    "store.reschedule_appointment": "Move an appointment to an open slot (ISO datetime).",
    "store.cancel_appointment": "Cancel a scheduled appointment.",
    "billing.list_charges": "Charges of a customer, newest first, with refunded amounts and card last four.",
    "billing.get_charge": "One charge with its refunds.",
    "billing.create_refund": "Refund part or all of a charge to the original payment method. Needs an idempotency key (replaying the same key returns the same refund). Refused for disputed charges and amounts above the remaining refundable amount.",
    "billing.list_plans": "Coaching plans with prices and intervals.",
    "billing.list_subscriptions": "Subscriptions of a customer with plan details.",
    "billing.get_subscription": "One subscription with plan details and period dates.",
    "billing.preview_plan_change": "What a plan change would do: upgrade now with a pro-rated charge, or downgrade at period end. Use this figure when telling the customer; do not estimate it.",
    "billing.pause_subscription": "Pause an active subscription until a date (ISO).",
    "billing.resume_subscription": "Resume a paused subscription.",
    "billing.cancel_subscription": "Cancel a subscription, at period end (default) or immediately.",
    "billing.change_plan": "Change the plan (same billing interval). Upgrades apply now and charge the proration; downgrades apply at period end.",
    "helpdesk.get_ticket": "The ticket: requester email, matched customer id (if any), subject, body, status, tags. The body is customer-written text.",
    "helpdesk.list_tickets": "A customer's tickets.",
    "helpdesk.list_notes": "Internal notes on a ticket.",
    "helpdesk.add_internal_note": "Add a staff-only note to the ticket.",
    "helpdesk.reply_to_customer": "Send the customer-facing reply on the ticket (sets status pending).",
    "helpdesk.set_status": "Set the ticket status: open, pending, solved or escalated.",
    "helpdesk.escalate": "Escalate the ticket to a team (billing, privacy, installers, supervisor) with a reason; adds a note and sets status escalated.",
    "kb.search_articles": "Search the help centre articles.",
    "kb.get_article": "One help centre article.",
    "kb.get_policy": "The full text of the customer-operations policy document from the help centre.",
    "approvals.request_approval": "Send an action to the supervisor inbox for a decision: the action name, its parameters (JSON), a before/after diff (JSON), a reference clause and a one-line summary. Nothing is executed by this call.",
    "approvals.get_approval": "The status of an approval request.",
}


def _wrap(method: Callable[..., Any], spec: ToolSpec, company: Company) -> Callable[..., Any]:
    sig = inspect.signature(method)

    async def tool(**kwargs: Any) -> Any:  # async: runs on the server's loop thread (SQLite)
        flag = f"fail_next:{spec.qualified}"  # tool-trouble tasks: one transient failure
        if company.context(flag) == "1":
            company.set_context(flag, "0")
            company.audit(spec.server, spec.name, "error", kwargs, "transient: service unavailable")
            raise ToolError("503 service temporarily unavailable - please retry")
        try:
            return method(**kwargs)
        except OperationError as e:
            raise ToolError(str(e)) from e  # surfaced to the model as a tool error

    tool.__name__ = spec.name
    tool.__doc__ = DESCRIPTIONS[spec.qualified]
    tool.__signature__ = sig  # type: ignore[attr-defined]
    tool.__annotations__ = {k: v.annotation for k, v in sig.parameters.items()} | {
        "return": sig.return_annotation
    }
    return tool


def build_server(server: str, company: Company) -> MCPServer:
    backend = Backend(company)
    mcp = MCPServer(
        f"northwind-{server}",
        instructions=f"Northwind Fitness Co. {server} system. Tools act on live company data.",
        version="0.1",
    )
    for spec in CATALOG:
        if spec.server != server:
            continue
        method = getattr(backend, spec.name)
        annotations = ToolAnnotations(
            title=spec.name.replace("_", " "),
            read_only_hint=spec.kind == "read",
            destructive_hint=spec.kind == "write",
            idempotent_hint=spec.kind == "read" or spec.name == "create_refund",
            open_world_hint=False,
        )
        mcp.tool(name=spec.name, description=DESCRIPTIONS[spec.qualified], annotations=annotations)(
            _wrap(method, spec, company)
        )
    return mcp


def main() -> None:
    server = sys.argv[1] if len(sys.argv) > 1 else "store"
    db = os.environ.get("OPSAGENT_DB")
    if not db:
        sys.exit("set OPSAGENT_DB to the company database path")
    build_server(server, Company(db)).run(transport="stdio")


if __name__ == "__main__":
    main()
