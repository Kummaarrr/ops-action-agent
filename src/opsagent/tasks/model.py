"""A task = one ticket on a known company state, with the right outcome written down.

The expected outcome is what the scorer checks, independently of the agent's own guard:
- `verdict`: done | approval | refuse | ask | escalate - what a correct handling ends in;
- `end_state`: assertions on the database after the task (rows that must exist with given
  values, counts that must hold), and `allowed_tables`: the only business tables that may
  change (the helpdesk tables, verifications and approvals are always allowed);
- `approvals`: approval requests that must exist (action + a subset of params);
- `forbidden_tools`: tools whose successful call is a violation for this task;
- `reply_must_include` / `reply_must_not_include`: substrings checked (case-insensitive) in
  the customer-facing reply; the reply's quality is judged separately by a calibrated judge.
Identity facts the requester gives are part of the ticket text; `requester_customer_id` is
the account the facts belong to (None when the requester cannot be matched or is an impostor).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

ALWAYS_ALLOWED_TABLES = {"ticket_notes", "ticket_replies", "verifications", "approvals", "tickets"}


@dataclass
class Assertion:
    table: str
    where: dict[str, Any]  # column -> value (exact match); value None means IS NULL
    count: int | None = None  # expected number of matching rows (default: at least one)
    fields: dict[str, Any] = field(default_factory=dict)  # expected values on the matching row
    note: str = ""


@dataclass
class ExpectedApproval:
    action: str  # qualified tool, e.g. billing.create_refund
    params: dict[str, Any] = field(default_factory=dict)  # subset that must match


@dataclass
class Expected:
    verdict: str
    end_state: list[Assertion] = field(default_factory=list)
    allowed_tables: list[str] = field(default_factory=list)
    approvals: list[ExpectedApproval] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    # each item: a substring, or a list of alternative substrings (any one satisfies)
    reply_must_include: list[Any] = field(default_factory=list)
    reply_must_not_include: list[str] = field(default_factory=list)
    escalate_to: str = ""  # team name when verdict == escalate
    acceptable_verdicts: list[str] = field(
        default_factory=list
    )  # other verdicts also counted right
    note_must_include: list[str] = field(default_factory=list)  # for long-horizon evidence notes


@dataclass
class Task:
    id: str
    template: str
    # plain, policy_edge, approval, refuse, ambiguous, adversarial, tool_trouble, long_horizon
    category: str
    tier: str  # standard | long
    customer_id: str | None  # the account the request concerns (as the truth knows it)
    requester_customer_id: str | None  # who the requester really is (None = impostor / unknown)
    ticket_id: str
    subject: str
    body: str
    requester_email: str
    channel: str
    setup_sql: list[list[Any]]  # [sql, params] applied after the base restore
    expected: Expected
    split: str = "dev"
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Task:
        e = d["expected"]
        expected = Expected(
            verdict=e["verdict"],
            end_state=[Assertion(**a) for a in e.get("end_state", [])],
            allowed_tables=e.get("allowed_tables", []),
            approvals=[ExpectedApproval(**a) for a in e.get("approvals", [])],
            forbidden_tools=e.get("forbidden_tools", []),
            reply_must_include=e.get("reply_must_include", []),
            reply_must_not_include=e.get("reply_must_not_include", []),
            escalate_to=e.get("escalate_to", ""),
            note_must_include=e.get("note_must_include", []),
            acceptable_verdicts=e.get("acceptable_verdicts", []),
        )
        plain = {k: v for k, v in d.items() if k != "expected"}
        return cls(**plain, expected=expected)
