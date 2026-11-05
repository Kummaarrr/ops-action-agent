"""Run a system over a split and score it.

    uv run python scripts/run_eval.py --system v1 --split dev
    uv run python scripts/run_eval.py --system v0 --split dev --limit 20
    uv run python scripts/run_eval.py --system v1 --split test --tag r1

Per task: restore the company to base, apply the task's setup, snapshot, run the agent against
the MCP servers, score from the audit log / diff / inbox / replies. Results go to
evals/results/<system>[_<model>]_<split>[_<tag>].json; model calls are cached (a --tag gives a
fresh namespace for an independent repeat).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from typing import Any

from tqdm import tqdm

from opsagent import llm
from opsagent.agent.agent import BASELINES, HAIKU, OPUS, SONNET, AgentConfig, run_task
from opsagent.config import DATA_DIR, GENERATED, PROJECT_ROOT, Env
from opsagent.llm import LEDGER, BudgetExceeded
from opsagent.scoring import Report, TaskScore, aggregate, score, to_json
from opsagent.servers.client import Systems
from opsagent.sim.company import Company
from opsagent.tasks.model import Task

MODELS = {"haiku": HAIKU, "sonnet": SONNET, "opus": OPUS}


def load_tasks(
    split: str,
    ids: list[str] | None,
    categories: list[str] | None,
    limit: int | None,
    per_template: int | None = None,
) -> list[Task]:
    raw = json.loads((DATA_DIR / "tasks.json").read_text(encoding="utf-8"))
    tasks = [Task.from_json(d) for d in raw]
    chosen = [t for t in tasks if (ids and t.id in ids) or (not ids and t.split == split)]
    if categories:
        chosen = [t for t in chosen if t.category in categories]
    if per_template:
        seen: dict[str, int] = {}
        picked = []
        for t in chosen:
            if seen.get(t.template, 0) < per_template:
                picked.append(t)
                seen[t.template] = seen.get(t.template, 0) + 1
        chosen = picked
    return chosen[:limit] if limit else chosen


def prepare(company: Company, task: Task) -> None:
    company.restore("base")
    for sql, params in task.setup_sql:
        company.execute(sql, params)
    company.set_task(task.id)
    company.snapshot("task")


async def run_all(tasks: list[Task], cfg: AgentConfig) -> list[TaskScore]:
    db = GENERATED / "company.db"
    company = Company(db)
    scores: list[TaskScore] = []
    async with Systems.start(db) as systems:
        for task in tqdm(tasks, desc=cfg.name):
            prepare(company, task)
            try:
                report = await run_task(company, systems, task.ticket_id, cfg)
            except BudgetExceeded:
                raise
            except Exception as e:
                report = Report("failed", f"system error: {type(e).__name__}: {str(e)[:200]}")
            scores.append(score(company, task, report))
    company.restore("base")
    return scores


def table(agg: dict[str, Any], label: str) -> str:
    rows = [
        ("Task success", f"{agg['success_pct']}%"),
        ("Verdict correct", f"{agg['verdict_correct_pct']}%"),
        ("End state correct", f"{agg['end_state_correct_pct']}%"),
        ("**Tasks with a policy violation**", str(agg["tasks_with_violations"])),
        (
            "Violations by kind",
            ", ".join(f"{k} {v}" for k, v in agg["violations_by_kind"].items() if v) or "none",
        ),
        ("PII leaks / false claims", f"{agg['pii_leaks']} / {agg['false_claims']}"),
        (
            "Approval precision / recall",
            f"{agg['approval']['precision_pct']}% / {agg['approval']['recall_pct']}%",
        ),
        ("Ask precision / recall", f"{agg['ask']['precision_pct']}% / {agg['ask']['recall_pct']}%"),
        (
            "Refuse precision / recall",
            f"{agg['refuse']['precision_pct']}% / {agg['refuse']['recall_pct']}%",
        ),
        (
            "Escalate precision / recall",
            f"{agg['escalate']['precision_pct']}% / {agg['escalate']['recall_pct']}%",
        ),
        ("Security events logged", str(agg["security_events"])),
        (
            "Tool calls / model calls per task",
            f"{agg['tool_calls_per_task']} / {agg['model_calls_per_task']}",
        ),
        ("Seconds / $ per task", f"{agg['seconds_per_task']} / ${agg['usd_per_task']}"),
    ]
    lines = [
        f"## {label}",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in rows],
        "",
    ]
    lines.append(
        "By category: "
        + ", ".join(
            f"{k} {v['success_pct']}% of {v['tasks']} ({v['tasks_with_violations']} viol.)"
            for k, v in agg["by_category"].items()
        )
    )
    conf = agg["confusion"]
    if conf:
        cols = [c for c in ("done", "approval", "refuse", "ask", "escalate", "failed")]
        lines += ["", "Expected x reached: | " + " | ".join(cols) + " |"]
        for exp_v, row in conf.items():
            lines.append(f"  {exp_v:9} | " + " | ".join(f"{row[c]:^{len(c)}}" for c in cols) + " |")
    return "\n".join(lines)


def main() -> int:
    sys.stdout.reconfigure(errors="replace")  # Windows consoles: never die on a summary glyph
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default="v1", choices=sorted(BASELINES))
    ap.add_argument("--model", default="haiku", choices=MODELS)
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--categories", nargs="*")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--per-template", type=int, help="at most N tasks per template")
    ap.add_argument("--tag", default="")
    ap.add_argument("--max-cost", type=float, default=Env().max_run_cost_usd)
    args = ap.parse_args()

    tasks = load_tasks(args.split, args.ids, args.categories, args.limit, args.per_template)
    cfg = replace(BASELINES[args.system], model=MODELS[args.model])
    llm.set_cache_namespace(args.tag)
    LEDGER.cap_usd = args.max_cost
    scores = asyncio.run(run_all(tasks, cfg))
    agg = aggregate(scores)
    name = (
        f"{args.system}_{args.model}_{args.split}"
        + (f"_{args.tag}" if args.tag else "")
        + (
            f"_{len(tasks)}"
            if args.limit or args.ids or args.categories or args.per_template
            else ""
        )
    )
    out = PROJECT_ROOT / "evals" / "results" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"aggregate": agg, "tasks": [to_json(s) for s in scores]}, indent=1),
        encoding="utf-8",
    )
    print(table(agg, f"{args.system} / {cfg.model} / {args.split} ({len(tasks)} tasks)"))
    print(f"\n{LEDGER.summary()}  ->  {out.relative_to(PROJECT_ROOT).as_posix()}")
    for s in scores:
        if not s.success:
            why = "; ".join(f"{v['kind']}" for v in s.violations) or (
                "verdict"
                if not s.verdict_ok
                else "end-state"
                if not s.end_state_ok
                else "reply/approval"
            )
            print(
                f"  {s.id} {s.template:24} exp={s.expected_verdict:9} got={s.outcome:9} {why} | {s.summary[:120]}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
