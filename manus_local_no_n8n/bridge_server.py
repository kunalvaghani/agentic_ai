
from __future__ import annotations

import contextlib
import io
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ.setdefault("WORKSPACE", str(ROOT))

from agent.main import run_agent  # noqa: E402
from agent.config import SETTINGS  # noqa: E402
from agent.storage_manager import list_recent_storage, store_text_artifact, storage_root  # noqa: E402
from agent.memory_os import consolidate_memories, init_db, ingest_document, run_query, format_answer_bundle  # noqa: E402

APP_TITLE = "Manus Local Open WebUI Bridge"
DEFAULT_MODEL = os.getenv("BRIDGE_AGENT_MODEL", os.getenv("OLLAMA_CHAT_MODEL", SETTINGS.chat_model))
DEFAULT_MAX_STEPS = int(os.getenv("BRIDGE_MAX_STEPS", str(SETTINGS.max_steps)))
LOG_DIR = ROOT / "logs"
ARTIFACT_DIR = ROOT / "artifacts"
STORAGE_DIR = storage_root()
LOG_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title=APP_TITLE,
    version="0.2.0",
    description=(
        "A local bridge that lets Open WebUI trigger the Manus desktop/browser/workspace agent on this PC. "
        "Run it on the host machine, then connect from Open WebUI through the included Pipe function."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://host.docker.internal:3000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_AGENT_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "busy": False,
    "last_run": None,
    "current_task": None,
    "started_at": None,
    "run_id": None,
}


class RunTaskRequest(BaseModel):
    task: str = Field(..., min_length=1)
    model: str = Field(default=DEFAULT_MODEL)
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=50)


class ArtifactItem(BaseModel):
    path: str
    modified_unix: float
    size_bytes: int


class RunTaskResponse(BaseModel):
    ok: bool
    task: str
    model: str
    max_steps: int
    duration_seconds: float
    final_output: str
    log_path: str
    recent_artifacts: list[ArtifactItem]


class DesktopAgentStatusResponse(BaseModel):
    ok: bool
    busy: bool
    workspace: str
    default_model: str
    recent_artifacts: list[ArtifactItem]
    last_run: dict[str, Any] | None
    current_task: str | None = None
    started_at: float | None = None
    run_id: str | None = None


class ListArtifactsRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class ReadWorkspaceTextRequest(BaseModel):
    path: str = Field(...)
    max_chars: int = Field(default=12000, ge=1, le=120000)


class ReadWorkspaceTextResponse(BaseModel):
    path: str
    content: str


class MemoryInitResponse(BaseModel):
    ok: bool
    db: str
    stats: dict[str, int]


class MemoryIngestRequest(BaseModel):
    source_path: str
    structure_path: str | None = None
    doc_id: str | None = None


class MemoryQueryRequest(BaseModel):
    query: str
    doc_id: str | None = None
    model: str = Field(default=DEFAULT_MODEL)


def _resolve_workspace_path(path_str: str) -> Path:
    target = (ROOT / path_str).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise HTTPException(status_code=400, detail="Path escapes the project folder.")
    return target


def _artifact_snapshot() -> dict[str, tuple[float, int]]:
    snapshot: dict[str, tuple[float, int]] = {}
    scan_roots = [ARTIFACT_DIR, STORAGE_DIR]
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for file in scan_root.rglob("*"):
            if file.is_file():
                rel = str(file.relative_to(ROOT)).replace("\\", "/")
                stat = file.stat()
                snapshot[rel] = (stat.st_mtime, stat.st_size)
    return snapshot


def _recent_artifacts(limit: int = 20) -> list[ArtifactItem]:
    items: list[ArtifactItem] = []
    for scan_root in (STORAGE_DIR, ARTIFACT_DIR):
        if scan_root.exists():
            for file in scan_root.rglob("*"):
                if file.is_file():
                    stat = file.stat()
                    items.append(
                        ArtifactItem(
                            path=str(file.relative_to(ROOT)).replace("\\", "/"),
                            modified_unix=stat.st_mtime,
                            size_bytes=stat.st_size,
                        )
                    )
    items.sort(key=lambda item: item.modified_unix, reverse=True)
    return items[:limit]


def _changed_artifacts(before: dict[str, tuple[float, int]]) -> list[ArtifactItem]:
    changed: list[ArtifactItem] = []
    after = _artifact_snapshot()
    for rel, (mtime, size) in after.items():
        if rel not in before or before[rel] != (mtime, size):
            changed.append(ArtifactItem(path=rel, modified_unix=mtime, size_bytes=size))
    changed.sort(key=lambda item: item.modified_unix, reverse=True)
    return changed[:20]


def _write_log(run_id: str, text: str) -> str:
    log_path = LOG_DIR / f"bridge-run-{run_id}.log"
    log_path.write_text(text, encoding="utf-8", errors="replace")
    return str(log_path.relative_to(ROOT)).replace("\\", "/")


def _available_ollama_models() -> set[str]:
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return set()
        models: set[str] = set()
        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            models.add(line.split()[0])
        return models
    except Exception:
        return set()


def _sanitize_model_name(model_name: str | None) -> str:
    candidate = (model_name or "").strip()
    if not candidate:
        return DEFAULT_MODEL
    blocked = {
        "manus local bridge",
        "manus-local-bridge",
        APP_TITLE.lower(),
    }
    if candidate.lower() in blocked:
        return DEFAULT_MODEL
    available = _available_ollama_models()
    if available and candidate not in available:
        return DEFAULT_MODEL
    return candidate


def _is_openwebui_meta_task(task: str) -> bool:
    t = (task or "").lower()
    patterns = [
        "suggest 3-5 relevant follow-up questions",
        '"follow_ups"',
        "### chat history:",
        "generate a concise title",
        "suggest a short title",
    ]
    return any(p in t for p in patterns)


def _mark_run_started(task: str, run_id: str) -> None:
    _STATE["busy"] = True
    _STATE["current_task"] = task
    _STATE["started_at"] = time.time()
    _STATE["run_id"] = run_id


def _mark_run_finished() -> None:
    _STATE["busy"] = False
    _STATE["current_task"] = None
    _STATE["started_at"] = None
    _STATE["run_id"] = None


def _break_stale_run_if_needed() -> None:
    started_at = _STATE.get("started_at")
    if _STATE.get("busy") and started_at:
        age = time.time() - float(started_at)
        if age > SETTINGS.bridge_stale_run_seconds:
            _mark_run_finished()
            if _AGENT_LOCK.locked():
                with contextlib.suppress(RuntimeError):
                    _AGENT_LOCK.release()


@app.get("/health", response_model=DesktopAgentStatusResponse, include_in_schema=False)
def health() -> DesktopAgentStatusResponse:
    return DesktopAgentStatusResponse(
        ok=True,
        busy=bool(_STATE.get("busy")),
        workspace=str(ROOT),
        default_model=DEFAULT_MODEL,
        recent_artifacts=_recent_artifacts(limit=10),
        last_run=_STATE.get("last_run"),
        current_task=_STATE.get("current_task"),
        started_at=_STATE.get("started_at"),
        run_id=_STATE.get("run_id"),
    )


@app.post("/reset", include_in_schema=False)
def reset_bridge() -> dict[str, Any]:
    _mark_run_finished()
    if _AGENT_LOCK.locked():
        with contextlib.suppress(RuntimeError):
            _AGENT_LOCK.release()
    return {"ok": True, "message": "Bridge state reset."}


@app.post("/memory/init", response_model=MemoryInitResponse, include_in_schema=False)
def memory_init() -> MemoryInitResponse:
    result = init_db(SETTINGS.memory_db)
    return MemoryInitResponse(ok=True, db=result["db"], stats=result["stats"])


@app.post("/memory/ingest", include_in_schema=False)
def memory_ingest(req: MemoryIngestRequest) -> dict[str, Any]:
    source = _resolve_workspace_path(req.source_path)
    structure = _resolve_workspace_path(req.structure_path) if req.structure_path else None
    result = ingest_document(
        db_path=SETTINGS.memory_db,
        source_path=source,
        structure_path=structure,
        embed_model=SETTINGS.embed_model,
        host=SETTINGS.ollama_host,
        timeout=SETTINGS.memory_timeout,
        doc_id=req.doc_id,
    )
    return {"ok": True, **result}


@app.post("/memory/query", include_in_schema=False)
def memory_query(req: MemoryQueryRequest) -> dict[str, Any]:
    bundle = run_query(
        db_path=SETTINGS.memory_db,
        query=req.query,
        model=_sanitize_model_name(req.model),
        embed_model=SETTINGS.embed_model,
        host=SETTINGS.ollama_host,
        timeout=SETTINGS.memory_timeout,
        seed=SETTINGS.memory_seed,
        doc_id=req.doc_id,
        keep_alive=SETTINGS.ollama_keep_alive,
        think=False,
        num_ctx=SETTINGS.ollama_num_ctx,
    )
    return {
        "ok": True,
        "answer": format_answer_bundle(bundle),
        "decision": bundle.decision,
        "reason": bundle.reason,
        "used_pages": bundle.used_pages,
        "used_node_ids": bundle.used_node_ids,
        "used_memory_ids": bundle.used_memory_ids,
    }


@app.post("/memory/consolidate", include_in_schema=False)
def memory_consolidate() -> dict[str, Any]:
    result = consolidate_memories(
        db_path=SETTINGS.memory_db,
        model=DEFAULT_MODEL,
        embed_model=SETTINGS.embed_model,
        host=SETTINGS.ollama_host,
        timeout=SETTINGS.memory_timeout,
        seed=SETTINGS.memory_seed,
        keep_alive=SETTINGS.ollama_keep_alive,
        think=False,
        num_ctx=SETTINGS.ollama_num_ctx,
    )
    return {"ok": True, **result}


@app.post("/list-artifacts", include_in_schema=False)
def list_artifacts(req: ListArtifactsRequest) -> dict[str, Any]:
    return {"ok": True, "items": [item.model_dump() for item in _recent_artifacts(req.limit)]}


@app.post("/read-workspace-text", response_model=ReadWorkspaceTextResponse, include_in_schema=False)
def read_workspace_text(req: ReadWorkspaceTextRequest) -> ReadWorkspaceTextResponse:
    target = _resolve_workspace_path(req.path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    content = target.read_text(encoding="utf-8", errors="replace")
    return ReadWorkspaceTextResponse(path=str(target.relative_to(ROOT)).replace("\\", "/"), content=content[:req.max_chars])


@app.post(
    "/run-local-desktop-agent",
    response_model=RunTaskResponse,
    operation_id="run_local_desktop_agent",
    summary="Run the local desktop/browser/workspace agent on this PC",
    tags=["agent"],
)
def run_local_desktop_agent(req: RunTaskRequest) -> RunTaskResponse:
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task is empty.")

    if _is_openwebui_meta_task(req.task):
        return RunTaskResponse(
            ok=True,
            task=req.task,
            model=_sanitize_model_name(req.model),
            max_steps=req.max_steps,
            duration_seconds=0.0,
            final_output='{"follow_ups":[]}',
            log_path="",
            recent_artifacts=[],
        )

    _break_stale_run_if_needed()

    if not _AGENT_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "The local agent is already running another task.",
                "current_task": _STATE.get("current_task"),
                "started_at": _STATE.get("started_at"),
                "run_id": _STATE.get("run_id"),
            },
        )

    started_at = time.time()
    run_id = time.strftime("%Y%m%d-%H%M%S")
    workspace = ROOT
    before = _artifact_snapshot()
    _mark_run_started(req.task, run_id)

    try:
        output_buffer = io.StringIO()
        final_output = ""
        error_detail: str | None = None
        effective_model = _sanitize_model_name(req.model)
        output_buffer.write(f"Requested model: {req.model}\n")
        output_buffer.write(f"Effective model: {effective_model}\n")

        try:
            with contextlib.redirect_stdout(output_buffer):
                final_output = run_agent(
                    task=req.task,
                    workspace=workspace,
                    model=effective_model,
                    max_steps=req.max_steps,
                )
        except Exception as exc:  # noqa: BLE001
            error_detail = str(exc)
            output_buffer.write("\n=== exception ===\n")
            output_buffer.write(traceback.format_exc())

        duration = round(time.time() - started_at, 2)
        log_path = _write_log(run_id, output_buffer.getvalue())
        if final_output:
            try:
                store_text_artifact(
                    final_output,
                    purpose="agent run summary",
                    title=req.task[:80],
                    suggested_name=f"agent-run-{run_id}",
                    extension=".md",
                    kind="agent_run_summary",
                    category="documents",
                )
            except Exception:
                pass

        changed = _changed_artifacts(before)
        _STATE["last_run"] = {
            "run_id": run_id,
            "task": req.task,
            "model": effective_model,
            "duration_seconds": duration,
            "ok": error_detail is None,
            "error": error_detail,
            "log_path": log_path,
            "changed_artifacts": [item.model_dump() for item in changed],
            "started_at": started_at,
        }

        if error_detail:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": error_detail,
                    "ok": False,
                    "task": req.task,
                    "model": effective_model,
                    "max_steps": req.max_steps,
                    "duration_seconds": duration,
                    "final_output": final_output or "Task failed before producing a final answer.",
                    "log_path": log_path,
                    "recent_artifacts": [item.model_dump() for item in changed],
                },
            )

        return RunTaskResponse(
            ok=True,
            task=req.task,
            model=effective_model,
            max_steps=req.max_steps,
            duration_seconds=duration,
            final_output=final_output,
            log_path=log_path,
            recent_artifacts=changed,
        )
    finally:
        _mark_run_finished()
        if _AGENT_LOCK.locked():
            with contextlib.suppress(RuntimeError):
                _AGENT_LOCK.release()


@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    return {
        "ok": True,
        "app": APP_TITLE,
        "workspace": str(ROOT),
        "default_model": DEFAULT_MODEL,
        "busy": bool(_STATE.get("busy")),
        "last_run": _STATE.get("last_run"),
        "recent_storage": list_recent_storage(limit=10),
    }
