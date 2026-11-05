"""Public demo (Streamlit Community Cloud), free to host and free to run for the owner.

- The queue: every held-out task from the final test run, with the outcome the agent reached,
  the verdict the task expected, and any violation the independent scorer found.
- A task: the ticket, the agent's trace (tool calls, guard decisions, security events, approval
  requests), the customer reply, and what changed in the company's systems.
- Run a live ticket: the visitor's own Anthropic key runs the guarded agent against the
  simulated company on a ticket they write; the key is passed per call, never stored or logged.
  The demo holds no key of its own.

Run locally:  uv run streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import streamlit as st  # noqa: E402

SAMPLES = ROOT / "app" / "samples"
RESULTS = SAMPLES / "results.json"
TASKS = SAMPLES / "tasks.json"
REPO_URL = os.environ.get("DEMO_REPO_URL", "https://github.com/Kummaarrr/ops-action-agent")
CATEGORY = {
    "plain": "plain request",
    "policy_edge": "policy edge case",
    "approval": "needs a supervisor",
    "refuse": "refuse / escalate",
    "ambiguous": "ambiguous",
    "adversarial": "adversarial",
    "tool_trouble": "tool trouble",
    "long_horizon": "long-horizon",
}
CLASS_LABEL = {
    "money_out": "money out",
    "data_out": "data out",
    "irreversible": "irreversible action",
    "honesty": "false claim",
    "paperwork": "paperwork",
}


@st.cache_data
def load() -> tuple[dict, dict[str, dict]]:
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in json.loads(TASKS.read_text(encoding="utf-8"))}
    return results, tasks


def badge(s: dict) -> str:
    material = [v for v in s["violations"] if v["cls"] != "paperwork"]
    if material:
        kinds = ", ".join(sorted({CLASS_LABEL.get(v["cls"], v["cls"]) for v in material}))
        return f"❌ VIOLATION ({kinds})"
    if s["success"]:
        return f"✅ {s['outcome']}"
    if s["verdict_ok"]:
        return f"⚠️ {s['outcome']} (end state or reply check failed)"
    return f"⚠️ {s['outcome']} (expected {s['expected_verdict']})"


def render_task(task: dict, s: dict | None) -> None:
    st.subheader(f"{task['id']} · {task['subject']}")
    st.caption(
        f"{CATEGORY.get(task['category'], task['category'])} · template `{task['template']}` · "
        f"expected outcome **{task['expected']['verdict']}**"
        + (
            f" (or {', '.join(task['expected']['acceptable_verdicts'])})"
            if task["expected"].get("acceptable_verdicts")
            else ""
        )
    )
    st.markdown("**The ticket** (customer-written text)")
    st.text(f"From: {task['requester_email']}  ·  channel: {task['channel']}\n\n{task['body']}")
    if s is None:
        st.info("No run recorded for this task.")
        return
    st.markdown(f"**Agent outcome:** {badge(s)}")
    if s["summary"]:
        st.caption(s["summary"])
    if s["violations"]:
        for v in s["violations"]:
            st.error(f"{CLASS_LABEL.get(v['cls'], v['cls'])}: {v['kind']} - {v['detail']}")
    with st.expander("Trace: tool calls, guard decisions, security events", expanded=False):
        for step in s["trace"]:
            if step.get("step") == "tool":
                flag = "🛑 " if step.get("error") else ""
                st.markdown(
                    f"{flag}`{step['tool']}` `{json.dumps(step.get('args', {}), default=str)[:200]}`"
                )
                st.code(str(step.get("result", ""))[:600], language="json")
            elif step.get("step") == "checker":
                st.markdown(f"**Checker:** {step.get('verdict')} - {step.get('reasons', '')}")
            elif step.get("step") == "finish":
                st.markdown(f"**finish({step.get('outcome')})** - {step.get('summary', '')}")
            else:
                st.caption(json.dumps(step, default=str)[:300])
    if s["reply"]:
        st.markdown("**Customer reply sent**")
        st.text(s["reply"])
    if s["diff"]:
        st.markdown("**What changed in the systems** (rows added / removed / changed)")
        st.json(s["diff"])


def page_queue(results: dict, tasks: dict[str, dict]) -> None:
    agg = results["aggregate"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Task success", f"{agg['success_pct']}%")
    c2.metric("Tasks with a material violation", agg["tasks_with_material_violations"])
    c3.metric("Data leaks / false claims", f"{agg['pii_leaks']} / {agg['false_claims']}")
    c4.metric("$ per ticket", f"${agg['usd_per_task']:.4f}")
    st.caption(
        f"{agg['tasks']} held-out tasks, one run of the guarded agent (Claude Haiku 5.5). "
        "Full results, three runs and the baselines are in the report."
    )
    rows = []
    for s in results["tasks"]:
        t = tasks.get(s["id"], {})
        rows.append(
            {
                "task": s["id"],
                "category": CATEGORY.get(s["category"], s["category"]),
                "subject": t.get("subject", ""),
                "expected": s["expected_verdict"],
                "agent": s["outcome"],
                "result": badge(s),
                "tool calls": s["tool_calls"],
                "$": round(s["cost_usd"], 4),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True, height=520)
    pick = st.selectbox("Open a task", [r["task"] for r in rows], index=0)
    if pick:
        render_task(tasks[pick], next(s for s in results["tasks"] if s["id"] == pick))


def page_inbox(results: dict, tasks: dict[str, dict]) -> None:
    st.markdown(
        "Approval requests the agent filed instead of acting (a supervisor would decide here). "
        "Each carries the action, the parameters, the record before, and the policy clause."
    )
    found = 0
    for s in results["tasks"]:
        for step in s["trace"]:
            if (
                step.get("step") == "tool"
                and str(step.get("result", "")).startswith("NOT EXECUTED - policy")
                and "Approval request" in str(step.get("result", ""))
            ):
                found += 1
                st.markdown(f"**{s['id']}** · `{step['tool']}` · {tasks[s['id']]['subject']}")
                st.code(
                    json.dumps(step.get("args", {}), indent=1, default=str)[:800], language="json"
                )
                st.caption(str(step.get("result", ""))[:300])
    if not found:
        st.info("No approval requests in this run.")


def page_live() -> None:
    st.markdown(
        "Write a ticket to Northwind Fitness Co. (the simulated company) and run the guarded agent on "
        "it with **your own Anthropic API key**. The key is passed per call and never stored or logged; "
        "the company is reset before the run. A real customer's email from the seeded data is needed "
        "for identity verification to pass - use the one shown."
    )
    from opsagent.config import GENERATED
    from opsagent.sim.company import Company

    db = GENERATED / "company.db"
    if not db.exists():
        with st.spinner("Building the simulated company (first run, ~10 s)..."):
            Company.create(db)
    company = Company(db)
    sample = company.row(
        "SELECT c.id, c.email, c.billing_zip, o.id AS order_id, o.status FROM customers c JOIN orders o ON o.customer_id = c.id "
        "WHERE o.status = 'fulfilled' AND o.tracking IS NOT NULL LIMIT 1"
    )
    assert sample
    st.caption(
        f"Seeded customer for this demo: **{sample['email']}** · order **{sample['order_id']}** ({sample['status']}) · "
        f"billing zip **{sample['billing_zip']}** (give it in the ticket for level-2 actions)"
    )
    with st.form("live"):
        key = st.text_input(
            "Your Anthropic API key (used only for this run; never stored or logged)",
            type="password",
        )
        sender = st.text_input("From (sender email)", value=sample["email"])
        subject = st.text_input("Subject", value="Where is my order?")
        body = st.text_area(
            "Ticket body",
            value=f"Hi, any update on order {sample['order_id']}? Has it shipped?",
            height=140,
        )
        submitted = st.form_submit_button("Run the agent", type="primary")
    if not submitted:
        return
    if not key.strip():
        st.warning("Paste an Anthropic API key to run a live ticket.")
        return
    from opsagent.agent.agent import BASELINES, run_task
    from opsagent.servers.client import Systems

    api_key = key.strip()
    company.restore("base")
    ticket_id = "T999999"
    company.execute(
        "INSERT INTO tickets(id, customer_id, requester_email, channel, subject, body, status, created_at, tags) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            ticket_id,
            (
                company.row(
                    "SELECT id FROM customers WHERE lower(email) = lower(?)", (sender.strip(),)
                )
                or {}
            ).get("id"),
            sender.strip(),
            "email",
            subject[:200],
            body[:3000],
            "open",
            company.now().isoformat(timespec="seconds"),
            "",
        ),
    )
    company.set_task("live")
    company.snapshot("task")

    async def go():
        async with Systems.start(db) as systems:
            return await run_task(company, systems, ticket_id, BASELINES["v1"], api_key=api_key)

    try:
        with st.spinner("Working the ticket through the guard and the systems..."):
            report = asyncio.run(go())
    except Exception as e:  # never echo the key; show the error type only
        name = type(e).__name__
        st.error(
            "That API key was rejected by Anthropic."
            if "Authentication" in name
            else f"The run failed ({name})."
        )
        return
    st.markdown(f"**Outcome:** {report.outcome} - {report.summary}")
    st.caption(
        f"{report.model_calls} model calls · ${report.cost_usd:.4f} · {report.seconds:.0f} s · {report.security_events} security events"
    )
    reply = company.row(
        "SELECT body FROM ticket_replies WHERE ticket_id = ? ORDER BY id DESC LIMIT 1", (ticket_id,)
    )
    if reply:
        st.markdown("**Customer reply**")
        st.text(reply["body"])
    with st.expander("Trace"):
        for step in report.trace:
            st.caption(json.dumps(step, default=str)[:400])
    st.markdown("**What changed**")
    st.json(
        {t: {k: len(v) for k, v in ch.items()} for t, ch in company.diff("task").items()}
        or {"nothing": "no system changed"}
    )
    company.restore("base")


def main() -> None:
    st.set_page_config(page_title="Ops Action Agent", layout="wide")
    st.title("Ops Action Agent")
    st.caption(
        "A headless support agent that works tickets through store, billing and helpdesk via MCP tools "
        f"under a written policy, with a guard that removes policy violations by construction rather than by better judgement. [Repo]({REPO_URL})"
    )
    tab1, tab2, tab3 = st.tabs(
        ["The queue (held-out results)", "Approval inbox", "Run a live ticket (your key)"]
    )
    if RESULTS.exists() and TASKS.exists():
        results, tasks = load()
        with tab1:
            page_queue(results, tasks)
        with tab2:
            page_inbox(results, tasks)
    else:
        with tab1:
            st.info("No sample results yet: run scripts/build_demo_samples.py.")
    with tab3:
        page_live()


if __name__ == "__main__":
    main()
