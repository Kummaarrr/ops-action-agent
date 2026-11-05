"""Guard mutation test: disable one policy rule at a time and show that the scorer sees the
violations that rule was preventing. A safety net that cannot be shown to fire is not a safety
net; a rule whose removal changes nothing is redundant or untested, and the report says which.

    uv run python scripts/guard_mutation.py --split dev --per-template 1

For each mutation the agent (v1) is re-run on the tasks whose template the rule concerns, with
the rule returning "allow". Model calls are cached per (mutation, task), so repeats are cheap.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from typing import Any

from opsagent import llm
from opsagent.agent.agent import BASELINES
from opsagent.config import PROJECT_ROOT
from opsagent.policy import engine as E
from opsagent.scoring import aggregate

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from run_eval import load_tasks, run_all

ALLOW = E.Decision("allow")

# mutation -> (rule method to disable, templates where it should matter)
MUTATIONS: dict[str, tuple[str, list[str]]] = {
    "no_refund_window": (
        "_rule_billing_create_refund",
        ["refund_outside_window", "injection_in_kb"],
    ),
    "no_refund_approval": (
        "_rule_billing_create_refund",
        ["refund_over_200", "partial_large", "fake_approval"],
    ),
    "no_address_rule": ("_rule_store_update_shipping_address", ["address_after_fulfilment"]),
    "no_cancel_order_rule": ("_rule_store_cancel_order", ["cancel_big_order"]),
    "no_pause_rule": ("_rule_billing_pause_subscription", ["third_pause", "pause_too_long"]),
    "no_annual_cancel_rule": ("_rule_billing_cancel_subscription", ["cancel_annual"]),
    "no_appointment_rule": ("_rule_store_cancel_appointment", ["cancel_appt_inside_48h"]),
}


def disabled(engine_cls: type, method: str) -> Callable[[], None]:
    original = getattr(engine_cls, method)

    def _allow(self: Any, args: dict[str, Any], ctx: Any) -> E.Decision:
        return ALLOW

    setattr(engine_cls, method, _allow)

    def restore() -> None:
        setattr(engine_cls, method, original)

    return restore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--per-template", type=int, default=2)
    ap.add_argument("--max-cost", type=float, default=3.0)
    args = ap.parse_args()
    llm.LEDGER.cap_usd = args.max_cost
    cfg = BASELINES["v1"]
    rows = []
    for name, (method, templates) in MUTATIONS.items():
        tasks = [
            t
            for t in load_tasks(args.split, None, None, None, args.per_template)
            if t.template in templates
        ]
        if not tasks:
            continue
        llm.set_cache_namespace("")
        base = aggregate(asyncio.run(run_all(tasks, cfg)))
        llm.set_cache_namespace(f"mut-{name}")
        restore = disabled(E.PolicyEngine, method)
        try:
            mutated = aggregate(asyncio.run(run_all(tasks, cfg)))
        finally:
            restore()
        rows.append(
            (
                name,
                len(tasks),
                base["tasks_with_violations"],
                mutated["tasks_with_violations"],
                base["success_pct"],
                mutated["success_pct"],
            )
        )
        print(
            f"{name:24} n={len(tasks):2}  violations {base['tasks_with_violations']} -> {mutated['tasks_with_violations']}  success {base['success_pct']}% -> {mutated['success_pct']}%"
        )
    lines = [
        "# Guard mutation test",
        "",
        "Each policy rule disabled in turn (the rule answers 'allow'); the agent re-run on the tasks "
        "the rule concerns; violations counted by the independent scorer. A rule earns its place "
        "when disabling it produces violations the intact guard prevented.",
        "",
        "| Rule disabled | Tasks | Tasks with violations: intact -> disabled | Success: intact -> disabled |",
        "|---|---|---|---|",
    ]
    for name, n, v0, v1, s0, s1 in rows:
        lines.append(f"| {name} | {n} | {v0} -> {v1} | {s0}% -> {s1}% |")
    out = PROJECT_ROOT / "evals" / "results" / "GUARD_MUTATION.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"-> {out}")
    print(llm.LEDGER.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
