"""The agent loop: model turn -> tool calls through the guard -> tool results -> ... -> finish.

Systems:
- v0:  tools, no policy in the prompt, no guard (what a tool-calling template gives);
- v0+: the policy in the prompt, no guard;
- v1:  the policy in the prompt and every guard layer, plus the checker model on finish and a
       consistency check between the reported outcome and the systems' state.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from opsagent import llm
from opsagent.agent import prompts
from opsagent.agent.guard import Guard, GuardConfig
from opsagent.scoring import Report
from opsagent.servers.client import Systems
from opsagent.sim.company import Company

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


@dataclass(frozen=True)
class AgentConfig:
    name: str = "v1"
    model: str = HAIKU
    policy_in_prompt: bool = True
    guard: GuardConfig | None = field(default_factory=GuardConfig)  # None = no guard (baselines)
    checker: bool = True  # checker model on finish
    checker_model: str = HAIKU
    verify_state: bool = True  # outcome "done" needs every write to have succeeded
    max_turns: int = 16
    thinking: bool = False


BASELINES = {
    "v0": AgentConfig("v0", policy_in_prompt=False, guard=None, checker=False, verify_state=False),
    "v0+": AgentConfig("v0+", policy_in_prompt=True, guard=None, checker=False, verify_state=False),
    "v1": AgentConfig("v1"),
    "v1-noengine": AgentConfig(
        "v1-noengine", guard=GuardConfig(policy_engine=False, approval_gate=False)
    ),
    "v1-nochecker": AgentConfig("v1-nochecker", checker=False),
    "v1-nountrusted": AgentConfig("v1-nountrusted", guard=GuardConfig(untrusted_marking=False)),
    "v1-noapproval": AgentConfig("v1-noapproval", guard=GuardConfig(approval_gate=False)),
    "v1-noverify": AgentConfig("v1-noverify", verify_state=False, checker=False),
    "v1-nogating": AgentConfig("v1-nogating", guard=GuardConfig(identity_gating=False)),
}


class CheckerVerdict(BaseModel):
    verdict: str = Field(description="pass or fail")
    reasons: str = Field(description="One or two sentences citing the evidence.")


async def run_task(
    company: Company, systems: Systems, ticket_id: str, cfg: AgentConfig, api_key: str | None = None
) -> Report:
    start = time.perf_counter()
    guard = Guard(company, systems, ticket_id, cfg.guard) if cfg.guard else None
    system = prompts.system_prompt(cfg.policy_in_prompt, guarded=guard is not None)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompts.task_message(ticket_id)}]
    trace: list[dict[str, Any]] = []
    calls = 0
    cost = 0.0
    outcome, summary = "failed", "no finish call"
    write_failures = 0
    writes_ok = 0

    for _turn in range(cfg.max_turns):
        tools = (guard.tool_definitions() if guard else systems.tool_definitions()) + [
            prompts.FINISH_TOOL
        ]
        try:
            reply, info = llm.chat(
                cfg.model, system, messages, tools, thinking=cfg.thinking, api_key=api_key
            )
        except llm.BudgetExceeded:
            raise
        except Exception as e:
            summary = f"model error: {type(e).__name__}: {str(e)[:200]}"
            trace.append({"step": "error", "detail": summary})
            break
        calls += 1
        cost += info.cost_usd
        messages.append({"role": "assistant", "content": reply["content"]})
        tool_uses = [b for b in reply["content"] if b.get("type") == "tool_use"]
        if not tool_uses:
            # the model stopped talking without finishing: nudge once, then give up
            if _turn + 1 < cfg.max_turns and not any(t.get("step") == "nudge" for t in trace):
                trace.append({"step": "nudge"})
                messages.append(
                    {"role": "user", "content": "Continue: use the tools, then call `finish`."}
                )
                continue
            break
        results: list[dict[str, Any]] = []
        finished = False
        for tu in tool_uses:
            name, args = tu["name"], tu.get("input") or {}
            if name == "finish":
                outcome, summary = str(args.get("outcome", "failed")), str(args.get("summary", ""))
                finished = True
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": "ok"})
                trace.append({"step": "finish", "outcome": outcome, "summary": summary[:300]})
                continue
            if guard:
                text, is_error = await guard.call(name, args)
            else:
                server, tool = Systems.split(name)
                if server not in systems.sessions:
                    text, is_error = f"ERROR: unknown tool {name}", True
                else:
                    res = await systems.call(server, tool, args)
                    text, is_error = res.for_model(), not res.ok
            spec_kind = _kind(name)
            if spec_kind in ("write", "approval"):
                if is_error and not text.startswith("NOT EXECUTED"):
                    write_failures += 1
                elif not is_error or text.startswith("NOT EXECUTED"):
                    writes_ok += 1
            trace.append(
                {
                    "step": "tool",
                    "tool": name,
                    "args": args,
                    "error": is_error,
                    "result": text[:400],
                }
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": text,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})
        if finished:
            break

    # ---- consistency: "done" with an unresolved failed write is not done
    if cfg.verify_state and outcome == "done" and write_failures and _last_write_failed(trace):
        trace.append(
            {"step": "verify", "detail": "a write failed and was not completed; outcome -> failed"}
        )
        outcome = "failed"
    # ---- checker model: every outcome that produced a customer reply, or claims work
    if cfg.checker and outcome != "failed":
        try:
            verdict, info = _check(company, ticket_id, trace, outcome, summary, cfg, api_key)
        except llm.BudgetExceeded:
            raise
        except Exception as e:
            trace.append(
                {
                    "step": "checker",
                    "verdict": "error",
                    "reasons": f"{type(e).__name__}: {str(e)[:200]}",
                }
            )
            outcome = "failed"
            summary = f"checker unavailable ({type(e).__name__}); handed back for review"
        else:
            calls += 1
            cost += info.cost_usd
            trace.append(
                {"step": "checker", "verdict": verdict.verdict, "reasons": verdict.reasons[:300]}
            )
            if verdict.verdict.lower().startswith("fail"):
                outcome = "failed"
                summary = f"checker failed the handling: {verdict.reasons[:200]}"
                company.audit(
                    "agent", "checker", "security", {}, f"checker fail: {verdict.reasons[:200]}"
                )

    events = len(guard.state.security_events) if guard else 0
    return Report(
        outcome=outcome,
        summary=summary,
        model_calls=calls,
        cost_usd=cost,
        seconds=time.perf_counter() - start,
        security_events=events,
        trace=trace,
    )


def _kind(model_tool_name: str) -> str:
    from opsagent.servers.catalog import BY_NAME

    server, tool = Systems.split(model_tool_name)
    spec = BY_NAME.get(f"{server}.{tool}")
    return spec.kind if spec else "read"


def _last_write_failed(trace: list[dict[str, Any]]) -> bool:
    """True when the most recent attempt at each write tool ended in an error."""
    last: dict[str, bool] = {}
    for t in trace:
        if t.get("step") == "tool" and _kind(t["tool"]) in ("write", "approval"):
            last[t["tool"]] = bool(t["error"]) and not str(t["result"]).startswith("NOT EXECUTED")
    return any(last.values())


def _check(
    company: Company,
    ticket_id: str,
    trace: list[dict[str, Any]],
    outcome: str,
    summary: str,
    cfg: AgentConfig,
    api_key: str | None = None,
) -> tuple[CheckerVerdict, llm.CallInfo]:
    ticket = (
        company.row("SELECT subject, body, requester_email FROM tickets WHERE id = ?", (ticket_id,))
        or {}
    )
    replies = company.rows(
        "SELECT body FROM ticket_replies WHERE ticket_id = ? AND task_id = ? ORDER BY id",
        (ticket_id, company.task_id()),
    )
    reply_text = "\n---\n".join(r["body"] for r in replies) or "(none)"
    if ticket:
        ticket = {
            **ticket,
            "subject": f"<<UNTRUSTED-TEXT>>{ticket['subject']}<</UNTRUSTED-TEXT>>",
            "body": f"<<UNTRUSTED-TEXT>>{ticket['body']}<</UNTRUSTED-TEXT>>",
        }
    trail = "\n".join(
        f"- {t['tool']}({json.dumps(t['args'], default=str)[:200]}) -> {'ERROR ' if t['error'] else ''}{str(t['result'])[:600]}"
        for t in trace
        if t.get("step") == "tool"
    )
    prompt = prompts.CHECKER_PROMPT.format(
        policy=prompts.policy_text(),
        ticket_id=ticket_id,
        ticket=json.dumps(ticket, default=str),
        trail=trail or "(no tool calls)",
        outcome=outcome,
        summary=summary,
        reply=reply_text,
    )
    return llm.parse_full(
        cfg.checker_model,
        prompt,
        CheckerVerdict,
        system=prompts.CHECKER_SYSTEM,
        max_tokens=3000,
        api_key=api_key,
    )
