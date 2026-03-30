from __future__ import annotations

import argparse
import os
import json
from pathlib import Path

from .planner_executor_verifier import run_planner_executor_verifier_agent

from ollama import Client

from .config import SETTINGS
from .tools import TOOL_SCHEMAS, execute_tool

SYSTEM_PROMPT = """
You are a local browser-desktop-workspace agent running inside the user's machine.

Operating rules:
- Prefer small, reversible steps.
- For browser tasks, use browser tools instead of raw desktop clicks whenever possible.
- For desktop tasks, inspect state first with desktop_get_active_window, desktop_list_windows, or desktop_screenshot.
- Focus the correct window before you type or send hotkeys.
- Use desktop_screenshot before and after risky UI actions so you can verify what changed.
- Prefer browser tools for websites, desktop tools for native apps, and workspace tools for code/files.
- Use semantic_search only after ingest_docs has been run.
- After code edits, run an appropriate verification command when possible.
- Never attempt purchases, account creation, credential entry, package installs, system settings changes, file deletion outside the workspace, or deployment unless the user explicitly requested them.
- Never use destructive shell commands.
- When using coordinate clicks, explain what you are targeting and verify with a screenshot if the UI is uncertain.
- Save user-facing outputs, screenshots, notes, page captures, and organized files under the workspace storage folder whenever possible.
- Use storage_save_text for summaries/notes and storage_organize_file for files that were created elsewhere in the workspace.
- Prefer automatic storage names unless the user explicitly asks for a specific filename.
- When you finish, summarize exactly what you changed, where you saved it under storage, and what still needs manual work.
""".strip()


def _tool_index() -> dict:
    index = {}
    for schema in TOOL_SCHEMAS:
        fn = schema.get("function", {})
        name = fn.get("name")
        if name:
            index[name] = fn
    return index


def _tool_catalog_text() -> str:
    lines = []
    for schema in TOOL_SCHEMAS:
        fn = schema.get("function", {})
        name = fn.get("name", "")
        desc = fn.get("description", "")
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict:
    text = (text or "").strip()

    if text.startswith("```"):
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("{") and part.endswith("}"):
                text = part
                break
            if "\n" in part:
                maybe = part.split("\n", 1)[1].strip()
                if maybe.startswith("{") and maybe.endswith("}"):
                    text = maybe
                    break

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}

    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        return {}


def _call_json_model(client: Client, model: str, system_prompt: str, user_prompt: str) -> dict:
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        stream=False,
        options={"temperature": 0.1},
    )
    content = response.get("message", {}).get("content", "")
    return _extract_json_object(content)


PLANNER_PROMPT = """You are the PLANNER for a local desktop/browser agent.

Your job:
- decide exactly ONE next step
- prefer the smallest safe step
- prefer browser/file/native tools over fragile UI actions
- do not claim the task is done unless it is actually complete

Return STRICT JSON only:
{
  "mode": "tool" | "finish",
  "reason": "short reason",
  "tool_name": "tool name if mode=tool",
  "arguments": {},
  "success_check": "what the verifier should confirm",
  "final_answer": "only if mode=finish"
}
"""


VERIFIER_PROMPT = """You are the VERIFIER for a local desktop/browser agent.

Your job:
- judge whether the last tool action moved the task forward
- decide if the overall task is finished
- never assume success without evidence from the tool output

Return STRICT JSON only:
{
  "status": "done" | "continue" | "retry",
  "reason": "short reason",
  "next_hint": "what should happen next",
  "final_answer": "only if status=done"
}
"""

def _looks_like_action_task(task: str) -> bool:
    t = task.lower()
    action_words = [
        "open", "click", "type", "press", "search", "go to", "visit",
        "save", "download", "take a screenshot", "focus", "run",
        "read file", "summarize this page", "organize", "move", "launch"
    ]
    return any(word in t for word in action_words)


def _route_task(client: Client, model: str, task: str) -> dict:
    route_prompt = f"""
Classify this task into exactly one mode:
- chat: normal Q&A, ideas, code snippets, explanations
- codegen: create/update multiple files in the workspace
- action: browser/desktop/file operations that require tools

Return strict JSON:
{{
  "mode": "chat" | "codegen" | "action",
  "reason": "short reason"
}}

Task:
{task}
"""
    return _call_json_model(
        client=client,
        model=model,
        system_prompt="You are a task router. Return strict JSON only.",
        user_prompt=route_prompt,
    )


def _looks_like_action_task(task: str) -> bool:
    t = task.lower()
    action_words = [
        "open", "click", "type", "press", "search", "go to", "visit",
        "save", "download", "take a screenshot", "focus", "run",
        "read file", "organize", "move", "launch"
    ]
    return any(word in t for word in action_words)

def run_agent(task: str, workspace: Path, model: str, max_steps: int) -> str:
    os.environ["WORKSPACE"] = str(workspace.resolve())
    client = Client(host=SETTINGS.ollama_host)

    # Fast path for normal chat/code questions
    if not _looks_like_action_task(task):
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task},
            ],
            stream=False,
            options={"temperature": 0.2},
        )
        return response["message"].get("content", "").strip() or "No response."

    planner_model = os.getenv("OLLAMA_PLANNER_MODEL", model)
    verifier_model = os.getenv("OLLAMA_VERIFIER_MODEL", model)

    tools_by_name = _tool_index()
    tool_catalog = _tool_catalog_text()

    history: list[str] = []
    final_text = ""

    for step in range(1, max_steps + 1):
        print(f"\n=== planner step {step} ===")

        planner_input = f"""TASK:
{task}

WORKSPACE:
{workspace}

AVAILABLE TOOLS:
{tool_catalog}

RECENT HISTORY:
{chr(10).join(history[-3:]) if history else "(none)"}

Choose the next best single step.
"""

        plan = _call_json_model(
            client=client,
            model=planner_model,
            system_prompt=PLANNER_PROMPT,
            user_prompt=planner_input,
        )

        mode = plan.get("mode", "").strip().lower()
        reason = plan.get("reason", "")
        tool_name = plan.get("tool_name", "")
        arguments = plan.get("arguments", {}) or {}
        success_check = plan.get("success_check", "")
        planned_final = plan.get("final_answer", "")

        if mode == "finish":
            return planned_final or reason or final_text or "Task completed."

        if mode != "tool":
            history.append(f"Planner returned invalid mode: {mode or '(empty)'}")
            continue

        if tool_name not in tools_by_name:
            history.append(f"Planner chose invalid tool: {tool_name}")
            continue

        try:
            tool_output = execute_tool(tool_name, arguments)
        except Exception as exc:
            tool_output = f"Tool execution failed: {type(exc).__name__}: {exc}"

        verifier_input = f"""TASK:
{task}

PLANNED STEP:
- tool_name: {tool_name}
- arguments: {arguments}
- reason: {reason}
- success_check: {success_check}

TOOL OUTPUT:
{(tool_output or '')[:1200]}

RECENT HISTORY:
{chr(10).join(history[-3:]) if history else "(none)"}

Decide whether the task is done, should continue, or should retry.
"""

        verdict = _call_json_model(
            client=client,
            model=verifier_model,
            system_prompt=VERIFIER_PROMPT,
            user_prompt=verifier_input,
        )

        status = verdict.get("status", "").strip().lower()
        verify_reason = verdict.get("reason", "")
        next_hint = verdict.get("next_hint", "")
        verify_final = verdict.get("final_answer", "")

        history.append(
            f"step={step} tool={tool_name} args={arguments} "
            f"verdict={status} reason={verify_reason} hint={next_hint}"
        )

        final_text = verify_final or verify_reason or final_text

        if status == "done":
            return verify_final or "Task completed successfully."

    return final_text or "Stopped after reaching the step limit."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Manus-like agent using Ollama tool calling.")
    parser.add_argument("--task", required=True, help="Task for the agent.")
    parser.add_argument("--workspace", default=str(SETTINGS.workspace), help="Workspace folder.")
    parser.add_argument("--model", default=SETTINGS.chat_model, help="Ollama chat model.")
    parser.add_argument("--max-steps", type=int, default=SETTINGS.max_steps, help="Maximum tool loop steps.")
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    workspace = Path(args.workspace).resolve()
    result = run_agent(task=args.task, workspace=workspace, model=args.model, max_steps=args.max_steps)
    print("\n=== final ===")
    print(result)
