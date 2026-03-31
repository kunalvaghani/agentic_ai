
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ollama import Client

from .config import SETTINGS
from .memory_os import MemoryError, format_answer_bundle, has_memory_data, run_query
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


def _safe_chat(
    client: Client,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
) -> dict:
    options = {
        "temperature": temperature,
        "num_ctx": SETTINGS.ollama_num_ctx,
    }
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    # Recent Ollama supports these. Fall back cleanly on older clients.
    kwargs["keep_alive"] = SETTINGS.ollama_keep_alive
    kwargs["think"] = SETTINGS.ollama_think
    try:
        return client.chat(**kwargs)
    except TypeError:
        kwargs.pop("keep_alive", None)
        kwargs.pop("think", None)
        return client.chat(**kwargs)


def _call_json_model(client: Client, model: str, system_prompt: str, user_prompt: str, temperature: float) -> dict:
    response = _safe_chat(
        client,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
    )
    content = response.get("message", {}).get("content", "")
    return _extract_json_object(content)


def _looks_like_action_task(task: str) -> bool:
    t = task.lower()
    action_words = [
        "open", "click", "type", "press", "search", "go to", "visit", "browse",
        "save", "download", "take a screenshot", "focus", "run", "launch",
        "read file", "organize", "move", "scroll", "drag", "select", "close",
        "create file", "write file", "edit file", "modify file", "build app",
        "create app", "make all necessary files", "workspace", "current workspace",
    ]
    return any(word in t for word in action_words)


def _looks_like_memory_task(task: str) -> bool:
    t = task.lower()
    memory_words = [
        "according to", "from the docs", "from my docs", "from memory", "use memory",
        "search memory", "search the docs", "what does the document say", "cite",
        "ingest this", "remember this", "consolidate memory",
    ]
    return any(word in t for word in memory_words)


def _run_direct_chat(client: Client, task: str, model: str) -> str:
    response = _safe_chat(
        client,
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ],
        temperature=SETTINGS.ollama_temperature,
    )
    return response.get("message", {}).get("content", "").strip() or "No response."


def _run_memory_query(task: str, model: str) -> str:
    bundle = run_query(
        db_path=SETTINGS.memory_db,
        query=task,
        model=model,
        embed_model=SETTINGS.embed_model,
        host=SETTINGS.ollama_host,
        timeout=SETTINGS.memory_timeout,
        seed=SETTINGS.memory_seed,
        keep_alive=SETTINGS.ollama_keep_alive,
        think=False,
        num_ctx=SETTINGS.ollama_num_ctx,
    )
    return format_answer_bundle(bundle)


def run_agent(task: str, workspace: Path, model: str, max_steps: int) -> str:
    os.environ["WORKSPACE"] = str(workspace.resolve())
    client = Client(host=SETTINGS.ollama_host)

    # Fast path: normal chat should not go through the action loop.
    if not _looks_like_action_task(task):
        if SETTINGS.memory_enabled and _looks_like_memory_task(task) and has_memory_data(SETTINGS.memory_db):
            try:
                return _run_memory_query(task, model)
            except MemoryError as exc:
                return f"Memory query failed: {exc}"
        return _run_direct_chat(client, task, model)

    planner_model = os.getenv("OLLAMA_PLANNER_MODEL", model)
    verifier_model = os.getenv("OLLAMA_VERIFIER_MODEL", model)
    history_limit = SETTINGS.planner_history_items
    verifier_chars = SETTINGS.verifier_output_chars

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
{chr(10).join(history[-history_limit:]) if history else "(none)"}

Choose the next best single step.
"""

        plan = _call_json_model(
            client=client,
            model=planner_model,
            system_prompt=PLANNER_PROMPT,
            user_prompt=planner_input,
            temperature=SETTINGS.planner_temperature,
        )

        mode = str(plan.get("mode", "")).strip().lower()
        reason = str(plan.get("reason", ""))
        tool_name = str(plan.get("tool_name", ""))
        arguments = plan.get("arguments", {}) or {}
        success_check = str(plan.get("success_check", ""))
        planned_final = str(plan.get("final_answer", ""))

        print(f"[planner] mode={mode} reason={reason}")
        if tool_name:
            print(f"[planner] tool={tool_name} args={arguments}")

        if mode == "finish":
            return planned_final or reason or final_text or "Task completed."

        if mode != "tool":
            history.append(f"Planner returned invalid mode: {mode or '(empty)'}")
            continue

        if tool_name not in tools_by_name:
            history.append(f"Planner chose invalid tool: {tool_name}")
            print(f"[planner-error] invalid tool: {tool_name}")
            continue

        try:
            print(f"[executor] {tool_name}({arguments})")
            tool_output = execute_tool(tool_name, arguments)
        except Exception as exc:
            tool_output = f"Tool execution failed: {type(exc).__name__}: {exc}"

        tool_output = tool_output or ""
        print(tool_output[:1600])

        verifier_input = f"""TASK:
{task}

PLANNED STEP:
- tool_name: {tool_name}
- arguments: {arguments}
- reason: {reason}
- success_check: {success_check}

TOOL OUTPUT:
{tool_output[:verifier_chars]}

RECENT HISTORY:
{chr(10).join(history[-history_limit:]) if history else "(none)"}

Decide whether the task is done, should continue, or should retry.
"""

        verdict = _call_json_model(
            client=client,
            model=verifier_model,
            system_prompt=VERIFIER_PROMPT,
            user_prompt=verifier_input,
            temperature=SETTINGS.verifier_temperature,
        )

        status = str(verdict.get("status", "")).strip().lower()
        verify_reason = str(verdict.get("reason", ""))
        next_hint = str(verdict.get("next_hint", ""))
        verify_final = str(verdict.get("final_answer", ""))

        print(f"[verifier] status={status} reason={verify_reason}")

        history.append(
            f"step={step} tool={tool_name} args={arguments} "
            f"verdict={status} reason={verify_reason} hint={next_hint}"
        )

        final_text = verify_final or verify_reason or final_text

        if status == "done":
            return verify_final or "Task completed successfully."

        if status == "retry":
            history.append(f"Verifier requested retry for tool {tool_name}")

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
