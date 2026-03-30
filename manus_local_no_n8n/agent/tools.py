from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from ollama import Client

from .browser_tools import BROWSER_TOOL_REGISTRY, BROWSER_TOOL_SCHEMAS, browser_execute_tool
from .desktop_tools import DESKTOP_TOOL_REGISTRY, DESKTOP_TOOL_SCHEMAS, desktop_execute_tool
from .config import SETTINGS
from .vector_store import VectorStore, chunk_text, iter_text_files
from .storage_manager import list_recent_storage, store_existing_file, store_text_artifact


DISALLOWED_COMMAND_PATTERNS = [
    r"(^|\s)rm\s+-rf\b",
    r"(^|\s)del\s+/[sq]\b",
    r"(^|\s)format\b",
    r"(^|\s)mkfs\b",
    r"(^|\s)shutdown\b",
    r"(^|\s)reboot\b",
    r":\(\)\{\s*:.*\}",
]


def resolve_in_workspace(path_str: str) -> Path:
    workspace = SETTINGS.workspace
    path = Path(path_str)
    candidate = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(f"Path escapes workspace: {path_str}")
    return candidate


def list_files(path: str = ".", max_entries: int = 200) -> dict[str, Any]:
    target = resolve_in_workspace(path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    items = []
    for child in sorted(target.iterdir())[:max_entries]:
        items.append(
            {
                "name": child.name,
                "path": str(child.relative_to(SETTINGS.workspace)),
                "type": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
    return {"cwd": str(target), "items": items}


def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
    target = resolve_in_workspace(path)
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    start_idx = max(0, start_line - 1)
    end_idx = end_line if end_line is not None else len(lines)
    selected = lines[start_idx:end_idx]
    numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=start_idx + 1)]
    return {"path": str(target.relative_to(SETTINGS.workspace)), "content": "\n".join(numbered)}


def write_file(path: str, content: str) -> dict[str, Any]:
    target = resolve_in_workspace(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target.relative_to(SETTINGS.workspace)), "bytes_written": len(content.encode("utf-8"))}


def search_files(query: str, path: str = ".", max_hits: int = 50) -> dict[str, Any]:
    target = resolve_in_workspace(path)
    hits = []
    lower_query = query.lower()
    files = iter_text_files(target) if target.is_dir() else [target]
    for file in files:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(lines, start=1):
            if lower_query in line.lower():
                hits.append(
                    {
                        "path": str(file.relative_to(SETTINGS.workspace)),
                        "line": i,
                        "text": line.strip(),
                    }
                )
                if len(hits) >= max_hits:
                    return {"query": query, "hits": hits}
    return {"query": query, "hits": hits}


def run_command(command: str, cwd: str = ".", timeout: int | None = None) -> dict[str, Any]:
    for pattern in DISALLOWED_COMMAND_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            raise ValueError(f"Blocked potentially destructive command: {command}")
    target_cwd = resolve_in_workspace(cwd)
    timeout = timeout or SETTINGS.command_timeout
    completed = subprocess.run(
        command,
        cwd=target_cwd,
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    return {
        "cwd": str(target_cwd.relative_to(SETTINGS.workspace)),
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-12_000:],
        "stderr": completed.stderr[-12_000:],
    }




def storage_list_recent(limit: int = 20) -> dict[str, Any]:
    return {"items": list_recent_storage(limit=limit)}


def storage_save_text(
    text: str,
    purpose: str = "text output",
    title: str = "",
    extension: str = ".md",
) -> dict[str, Any]:
    return store_text_artifact(
        text=text,
        purpose=purpose,
        title=title,
        extension=extension,
        kind="generated_text",
    )


def storage_organize_file(
    path: str,
    purpose: str = "saved file",
    title: str = "",
    move: bool = False,
) -> dict[str, Any]:
    return store_existing_file(
        path=path,
        purpose=purpose,
        title=title,
        move=move,
        kind="organized_file",
    )

def ingest_docs(path: str = ".") -> dict[str, Any]:
    target = resolve_in_workspace(path)
    files = iter_text_files(target)
    client = Client(host=SETTINGS.ollama_host)
    store = VectorStore(SETTINGS.workspace / SETTINGS.index_file)
    store.clear()

    chunk_count = 0
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for chunk in chunk_text(text):
            response = client.embed(model=SETTINGS.embed_model, input=chunk)
            embedding = response["embeddings"][0]
            store.add(str(file.relative_to(SETTINGS.workspace)), chunk, embedding)
            chunk_count += 1
    store.save()
    return {"indexed_files": len(files), "indexed_chunks": chunk_count, "index": SETTINGS.index_file}


def semantic_search(query: str, top_k: int = 5) -> dict[str, Any]:
    store = VectorStore(SETTINGS.workspace / SETTINGS.index_file)
    if not store.data["items"]:
        return {"query": query, "results": [], "message": "Index is empty. Run ingest_docs first."}
    client = Client(host=SETTINGS.ollama_host)
    response = client.embed(model=SETTINGS.embed_model, input=query)
    embedding = response["embeddings"][0]
    results = store.search(embedding, top_k=top_k)
    simplified = [
        {
            "source": item["source"],
            "score": round(item["score"], 4),
            "chunk": item["chunk"],
        }
        for item in results
    ]
    return {"query": query, "results": simplified}


ToolFunc = Callable[..., dict[str, Any]]

TOOL_REGISTRY: dict[str, ToolFunc] = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "search_files": search_files,
    "run_command": run_command,
    "ingest_docs": ingest_docs,
    "semantic_search": semantic_search,
    "storage_list_recent": storage_list_recent,
    "storage_save_text": storage_save_text,
    "storage_organize_file": storage_organize_file,
    **BROWSER_TOOL_REGISTRY,
    **DESKTOP_TOOL_REGISTRY,
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders inside the workspace or a subfolder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside the workspace."},
                    "max_entries": {"type": "integer", "description": "Maximum entries to return."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file with line numbers.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative file path inside the workspace."},
                    "start_line": {"type": "integer", "description": "1-based start line."},
                    "end_line": {"type": "integer", "description": "1-based end line."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a UTF-8 text file inside the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "Relative file path inside the workspace."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a text string in workspace files.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Case-insensitive text to search for."},
                    "path": {"type": "string", "description": "Relative folder or file path."},
                    "max_hits": {"type": "integer", "description": "Maximum matches to return."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a non-destructive shell command in the workspace. Use for tests, git status, build commands, and code generation scripts.",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "cwd": {"type": "string", "description": "Relative working directory inside the workspace."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "storage_list_recent",
            "description": "List recently saved files from the smart storage folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum items to return."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "storage_save_text",
            "description": "Save text into the storage folder with an automatic file name and folder.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Text content to save."},
                    "purpose": {"type": "string", "description": "Purpose for naming the file."},
                    "title": {"type": "string", "description": "Short title for naming the file."},
                    "extension": {"type": "string", "description": "File extension such as .md or .txt."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "storage_organize_file",
            "description": "Copy or move an existing workspace file into the smart storage folder using automatic categorization and naming.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Existing file path inside the workspace."},
                    "purpose": {"type": "string", "description": "Purpose for naming the organized file."},
                    "title": {"type": "string", "description": "Short title for naming the organized file."},
                    "move": {"type": "boolean", "description": "Move instead of copying."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_docs",
            "description": "Build a local semantic index for the workspace using Ollama embeddings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative folder to index."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": "Search the local semantic index for relevant code or documentation chunks.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "What you want to find."},
                    "top_k": {"type": "integer", "description": "How many results to return."},
                },
            },
        },
    },
    *BROWSER_TOOL_SCHEMAS,
    *DESKTOP_TOOL_SCHEMAS,
]


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if name in BROWSER_TOOL_REGISTRY:
        return browser_execute_tool(name, arguments)
    if name in DESKTOP_TOOL_REGISTRY:
        return desktop_execute_tool(name, arguments)
    if name not in TOOL_REGISTRY:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = TOOL_REGISTRY[name](**arguments)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
