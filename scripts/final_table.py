"""Final tables from saved results (no model calls).

uv run python scripts/final_table.py   # -> evals/results/FINAL_test.md, ABLATION_dev.md
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from opsagent.config import PROJECT_ROOT

RESULTS = PROJECT_ROOT / "evals" / "results"
SYSTEMS = [
    ("v0_haiku", "v0 - tools, no policy (Haiku 5.5)"),
    ("v0+_haiku", "v0+ - policy in the prompt (Haiku 5.5)"),
    ("v1_haiku", "**v1 - guarded agent (Haiku 5.5)**"),
    ("v1_sonnet", "v1 - guarded agent (Sonnet 5.5)"),
    ("v0+_opus", "v0+ - policy in the prompt (Opus 5.5)"),
]
ROWS = [
    ("success_pct", "**Task success**", "%"),
    ("verdict_correct_pct", "Right outcome (done / approval / refuse / ask / escalate)", "%"),
    ("end_state_correct_pct", "Right end state", "%"),
    (
        "tasks_with_material_violations",
        "**Tasks with a policy violation** (money / data / irreversible / honesty)",
        "n",
    ),
    ("tasks_with_violations", "...any violation, paperwork included", "n"),
    ("pii_leaks", "...data leaks", "n"),
    ("false_claims", "...false claims (reported done, state disagrees)", "n"),
    ("approval", "Approval precision / recall", "pr"),
    ("refuse", "Refusal precision / recall", "pr"),
    ("ask", "Clarification precision / recall", "pr"),
    ("escalate", "Escalation precision / recall", "pr"),
    ("security_events", "Security events logged", "n"),
    ("tool_calls_per_task", "Tool calls / task", "x"),
    ("model_calls_per_task", "Model calls / task", "x"),
    ("seconds_per_task", "Seconds / task", "s"),
    ("usd_per_task", "$ / task", "$"),
]


def wilson(p_pct: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 100.0)
    p = p_pct / 100
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, 100 * (centre - half)), min(100.0, 100 * (centre + half)))


def _load(name: str) -> dict[str, Any] | None:
    p = RESULTS / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _fmt(v: float, unit: str) -> str:
    return {
        "%": f"{v:.0f}%",
        "n": f"{v:.1f}" if v != int(v) else f"{int(v)}",
        "x": f"{v:.1f}",
        "s": f"{v:.0f}",
        "$": f"${v:.4f}",
    }[unit]


def _cell(values: list[float], unit: str) -> str:
    if not values:
        return "-"
    m = statistics.mean(values)
    if max(values) == min(values):
        return _fmt(m, unit)
    return f"{_fmt(m, unit)} ({_fmt(min(values), unit)}-{_fmt(max(values), unit)})"


def _pr_cell(runs: list[dict[str, Any]], key: str) -> str:
    ps = [
        r["aggregate"][key]["precision_pct"]
        for r in runs
        if r["aggregate"][key]["precision_pct"] is not None
    ]
    rs = [
        r["aggregate"][key]["recall_pct"]
        for r in runs
        if r["aggregate"][key]["recall_pct"] is not None
    ]
    return f"{_cell(ps, '%') if ps else '-'} / {_cell(rs, '%') if rs else '-'}"


def final() -> str:
    runs = {k: [r for t in ("r1", "r2", "r3") if (r := _load(f"{k}_test_{t}"))] for k, _ in SYSTEMS}
    for k, _ in SYSTEMS:
        if not runs[k] and (r := _load(f"{k}_test")):
            runs[k] = [r]
    present = [(k, label) for k, label in SYSTEMS if runs[k]]
    n = runs[present[0][0]][0]["aggregate"]["tasks"] if present else 0
    lines = [
        "# Final results - test split",
        "",
        f"{n} held-out tasks never used while building; model systems run 3 times independently; "
        "mean (range); [95% Wilson interval] on the headline rates. Simulated company (store, "
        "billing, helpdesk as SQLite behind MCP servers); the same agent's real-sandbox smoke test "
        "is reported separately in the report.",
        "",
        "| Metric | " + " | ".join(label for _, label in present) + " |",
        "|---|" + "---|" * len(present),
    ]
    for key, label, unit in ROWS:
        cells = []
        for k, _ in present:
            if unit == "pr":
                cells.append(_pr_cell(runs[k], key))
                continue
            vals = [r["aggregate"][key] for r in runs[k] if r["aggregate"].get(key) is not None]
            cell = _cell(vals, unit)
            if unit == "%" and vals and key == "success_pct":
                lo, hi = wilson(statistics.mean(vals), runs[k][0]["aggregate"]["tasks"])
                cell += f" [{lo:.0f}-{hi:.0f}]"
            cells.append(cell)
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    for group, title in (("by_category", "category"), ("by_tier", "tier")):
        keys = sorted(
            {key for k, _ in present for r in runs[k] for key in r["aggregate"].get(group, {})}
        )
        lines += [
            "",
            f"By {title}: success % (tasks with a violation):",
            "",
            f"| {title} | " + " | ".join(label for _, label in present) + " |",
            "|---|" + "---|" * len(present),
        ]
        for key in keys:
            cells = []
            n_obj = ""
            for k, _ in present:
                groups = [r["aggregate"].get(group, {}).get(key) for r in runs[k]]
                groups = [g for g in groups if g]
                if not groups:
                    cells.append("-")
                    continue
                n_obj = groups[0]["tasks"]
                cells.append(
                    f"{_cell([g['success_pct'] or 0 for g in groups], '%')} ({_cell([g['tasks_with_violations'] for g in groups], 'n')})"
                )
            lines.append(f"| {key} (n={n_obj}) | " + " | ".join(cells) + " |")
    lines += ["", "Runs available: " + ", ".join(f"{k} x{len(runs[k])}" for k, _ in present)]
    return "\n".join(lines) + "\n"


def _p(v: float | None) -> str:
    return "-" if v is None else f"{v}%"


def ablation() -> str:
    variants = [
        ("v1_haiku_dev", "**v1 (all layers)**"),
        (
            "v1-noengine_haiku_dev",
            "without the policy engine and approval gate (model judgement only)",
        ),
        ("v1-nogating_haiku_dev", "without identity gating of account tools"),
        (
            "v1-noapproval_haiku_dev",
            "without the approval gate (the engine's 'needs approval' is not enforced; the model acts on its own judgement)",
        ),
        ("v1-nountrusted_haiku_dev", "without provenance tags / injection flags"),
        ("v1-nochecker_haiku_dev", "without the checker model"),
        ("v1-noverify_haiku_dev", "without the consistency check and the checker"),
        ("v0+_haiku_dev", "v0+ policy in the prompt (reference)"),
        ("v0_haiku_dev", "v0 no policy (reference)"),
    ]
    lines = [
        "# Ablation - dev split (one run each)",
        "",
        "| Variant | Success | Tasks with a violation (material / any) | Leaks | False claims | Approval P / R | Security events | $ / task |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, label in variants:
        r = _load(name)
        if r:
            a = r["aggregate"]
            ap = a["approval"]
            lines.append(
                f"| {label} | {a['success_pct']}% | {a['tasks_with_material_violations']} / {a['tasks_with_violations']} | {a['pii_leaks']} | {a['false_claims']} | "
                f"{_p(ap['precision_pct'])} / {_p(ap['recall_pct'])} | {a['security_events']} | ${a['usd_per_task']:.4f} |"
            )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    (RESULTS / "FINAL_test.md").write_text(final(), encoding="utf-8")
    (RESULTS / "ABLATION_dev.md").write_text(ablation(), encoding="utf-8")
    print((RESULTS / "ABLATION_dev.md").read_text(encoding="utf-8"))
    print((RESULTS / "FINAL_test.md").read_text(encoding="utf-8"))
