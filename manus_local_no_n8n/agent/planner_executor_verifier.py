from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ollama import Client

from .config import SETTINGS
from .tools import TOOL_SCHEMAS, execute_tool


@dataclass
class PlanDecision:
    phase: str
    reasoning: str
    tool_name: str
    tool_arguments: dict[str, Any]
    expected_observation: str
    done_when: str
    final_response: str


@dataclass
class VerifyDecision:
    status: str
    reasoning: str
    verified: bool
    next_hint: str
    user_summary: str


PLANNER_SYSTEM_PROMPT = """
You are the PLANNER for a local desktop/browser/workspace agent.

Your only job is to decide the next single step.
Do not pretend to execute tools. Do not describe hypothetical actions as completed.
Choose exactly one of these phases:
- act: choose one tool and its arguments
- finish: no more tool calls are needed

Rules:
- Prefer the minimum number of reversible steps.
- Prefer browser tools for websites, desktop tools for native apps, and workspace tools for files/code.
- For desktop typing or clicks, first ensure the correct app/window is open or focused.
- Use screenshots and window-state checks to verify risky UI actions.
- Never output anything except a single JSON object.

JSON schema:
{
  "phase": "act" | "finish",
  "reasoning": "brief",
  "tool_name": "tool name or empty",
  "tool_arguments": {"arg": "value"},
  "expected_observation": "what should be true after the tool runs",
  "done_when": "what condition means the overall task is complete",
  "final_response": "only when phase=finish"
}
""".strip()

VERIFIER_SYSTEM_PROMPT = """
You are the VERIFIER for a local desktop/browser/workspace agent.

Your only job is to judge the result of one executed step.
Do not invent success. Be strict.
Use the user task, the planner expectation, and the tool output.

Choose exactly one status:
- continue: the step helped, but more steps are needed
- done: the overall task is complete
- retry: the step did not satisfy the expectation; planner should adjust
- blocked: execution cannot safely continue without help or a different strategy

Never output anything except a single JSON object.

JSON schema:
{
  "status": "continue" | "done" | "retry" | "blocked",
  "reasoning": "brief",
  "verified": true | false,
  "next_hint": "one short hint for the next plan",
  "user_summary": "one short sentence about what happened"
}
""".strip()


JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_dict(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except Exception:
        pass
    match = JSON_RE.search(text)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _call_json_model(
    client: Client,
    model: str,
    system_prompt: str,
    user_prompt: str,
    fallback: dict[str, Any],
    temperature: float = 0.1,
) -> dict[str, Any]:
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        stream=False,
        options={"temperature": temperature},
    )
    content = (response.get("message") or {}).get("content") or ""
    parsed = _extract_json_dict(content)
    if parsed is None:
        return fallback
    return parsed


def _tool_catalog() -> str:
    lines: list[str] = []
    for item in TOOL_SCHEMAS:
        fn = item.get("function", {})
        name = fn.get("name", "")
        description = fn.get("description", "")
        properties = fn.get("parameters", {}).get("properties", {})
        arg_names = ", ".join(properties.keys())
        lines.append(f"- {name}: {description} Args: [{arg_names}]")
    return "\n".join(lines)


def _compact_text(value: str, limit: int = 3000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def _summarize_history(step_records: list[dict[str, Any]], max_steps: int = 8) -> str:
    if not step_records:
        return "No steps yet."
    chosen = step_records[-max_steps:]
    lines = []
    for item in chosen:
        lines.append(
            f"Step {item['step']}: plan={item['plan_phase']} tool={item['tool_name']} "
            f"verify={item['verify_status']} verified={item['verified']} note={item['note']}"
        )
    return "\n".join(lines)


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def plan_next_step(
    client: Client,
    *,
    task: str,
    planner_model: str,
    step_records: list[dict[str, Any]],
) -> PlanDecision:
    fallback = {
        "phase": "finish",
        "reasoning": "Planner did not produce valid JSON.",
        "tool_name": "",
        "tool_arguments": {},
        "expected_observation": "",
        "done_when": "",
        "final_response": "The planner failed to produce a valid next step.",
    }
    prompt = f"""
User task:
{task}

Recent execution history:
{_summarize_history(step_records)}

Available tools:
{_tool_catalog()}

Pick the single best next step now.
""".strip()
    raw = _call_json_model(client, planner_model, PLANNER_SYSTEM_PROMPT, prompt, fallback)
    return PlanDecision(
        phase=str(raw.get("phase", "finish")).strip().lower(),
        reasoning=str(raw.get("reasoning", "")).strip(),
        tool_name=str(raw.get("tool_name", "")).strip(),
        tool_arguments=_safe_dict(raw.get("tool_arguments", {})),
        expected_observation=str(raw.get("expected_observation", "")).strip(),
        done_when=str(raw.get("done_when", "")).strip(),
        final_response=str(raw.get("final_response", "")).strip(),
    )


def verify_step(
    client: Client,
    *,
    task: str,
    verifier_model: str,
    plan: PlanDecision,
    tool_output: str,
) -> VerifyDecision:
    fallback = {
        "status": "retry",
        "reasoning": "Verifier did not produce valid JSON.",
        "verified": False,
        "next_hint": "Re-check the tool result and use a safer confirmation step.",
        "user_summary": "The verification step failed to parse.",
    }
    prompt = f"""
User task:
{task}

Planner decision:
phase={plan.phase}
tool_name={plan.tool_name}
tool_arguments={json.dumps(plan.tool_arguments, ensure_ascii=False)}
expected_observation={plan.expected_observation}
done_when={plan.done_when}

Tool output:
{_compact_text(tool_output)}

Judge whether the executed step helped and whether the overall task is complete.
""".strip()
    raw = _call_json_model(client, verifier_model, VERIFIER_SYSTEM_PROMPT, prompt, fallback)
    return VerifyDecision(
        status=str(raw.get("status", "retry")).strip().lower(),
        reasoning=str(raw.get("reasoning", "")).strip(),
        verified=bool(raw.get("verified", False)),
        next_hint=str(raw.get("next_hint", "")).strip(),
        user_summary=str(raw.get("user_summary", "")).strip(),
    )


def run_planner_executor_verifier_agent(
    *,
    task: str,
    workspace,
    model: str,
    max_steps: int,
) -> str:
    client = Client(host=SETTINGS.ollama_host)
    planner_model = SETTINGS.planner_model or model
    verifier_model = SETTINGS.verifier_model or model

    step_records: list[dict[str, Any]] = []
    final_lines = [f"Task: {task}"]

    for step in range(1, max_steps + 1):
        plan = plan_next_step(
            client,
            task=task,
            planner_model=planner_model,
            step_records=step_records,
        )
        print(f"\n--- planner step {step} ---")
        print(json.dumps(plan.__dict__, ensure_ascii=False, indent=2))

        if plan.phase == "finish":
            summary = plan.final_response or "The planner decided the task is complete."
            if step_records:
                summary += "\n\nVerification trail:\n" + "\n".join(
                    f"- Step {item['step']}: {item['note']}" for item in step_records[-6:]
                )
            return summary

        if not plan.tool_name:
            return "Planner error: no tool was selected."

        tool_output = execute_tool(plan.tool_name, plan.tool_arguments)
        print(f"[executor] {plan.tool_name}({plan.tool_arguments})")
        print(_compact_text(tool_output, limit=1800))

        verdict = verify_step(
            client,
            task=task,
            verifier_model=verifier_model,
            plan=plan,
            tool_output=tool_output,
        )
        print(f"--- verifier step {step} ---")
        print(json.dumps(verdict.__dict__, ensure_ascii=False, indent=2))

        step_records.append(
            {
                "step": step,
                "plan_phase": plan.phase,
                "tool_name": plan.tool_name,
                "tool_arguments": plan.tool_arguments,
                "expected_observation": plan.expected_observation,
                "verify_status": verdict.status,
                "verified": verdict.verified,
                "note": verdict.user_summary or verdict.reasoning or "No note.",
            }
        )

        if verdict.status == "done":
            final_lines.append("Result: success")
            final_lines.extend(f"- Step {item['step']}: {item['note']}" for item in step_records[-6:])
            if verdict.user_summary:
                final_lines.append(f"Final verification: {verdict.user_summary}")
            return "\n".join(final_lines)

        if verdict.status == "blocked":
            final_lines.append("Result: blocked")
            final_lines.append(verdict.user_summary or verdict.reasoning or "The verifier blocked further progress.")
            return "\n".join(final_lines)

    final_lines.append("Result: step limit reached")
    final_lines.extend(f"- Step {item['step']}: {item['note']}" for item in step_records[-6:])
    return "\n".join(final_lines)
