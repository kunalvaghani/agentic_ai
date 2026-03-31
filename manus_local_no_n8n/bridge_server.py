from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ.setdefault("WORKSPACE", str(ROOT))

from agent.config import SETTINGS  # noqa: E402
from agent.storage_manager import store_text_artifact, storage_root  # noqa: E402

APP_TITLE = "Manus Local Open WebUI Bridge"
DEFAULT_MODEL = os.getenv("BRIDGE_AGENT_MODEL", os.getenv("OLLAMA_CHAT_MODEL", SETTINGS.chat_model))
DEFAULT_MAX_STEPS = int(os.getenv("BRIDGE_MAX_STEPS", str(SETTINGS.max_steps)))
LOG_DIR = ROOT / "logs"
ARTIFACT_DIR = ROOT / "artifacts"
STORAGE_DIR = storage_root()
LOG_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

STALE_RUN_SECONDS = int(os.getenv("BRIDGE_STALE_RUN_SECONDS", "900"))
_TAIL_MAX = int(os.getenv("BRIDGE_MONITOR_TAIL_MAX", "600"))

app = FastAPI(
    title=APP_TITLE,
    version="0.3.0",
    description=(
        "A local bridge that lets Open WebUI trigger the Manus desktop/browser/workspace agent on this PC. "
        "This version supports async run start, per-run live monitor pages, and safe process reset."
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
_STATE_LOCK = threading.Lock()
_ACTIVE_THREAD: threading.Thread | None = None
_ACTIVE_PROCESS: subprocess.Popen[str] | None = None
_ACTIVE_RUN_ID: str | None = None

_STATE: dict[str, Any] = {
    "busy": False,
    "last_run": None,
    "current_task": None,
    "started_at": None,
    "run_id": None,
    "phase": "idle",
    "current_line": "",
    "error": None,
    "final_output": "",
}

_RUNS: dict[str, dict[str, Any]] = {}


class RunTaskRequest(BaseModel):
    task: str = Field(..., min_length=1)
    model: str = Field(default=DEFAULT_MODEL)
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=80)


class StartTaskResponse(BaseModel):
    ok: bool
    run_id: str
    task: str
    model: str
    max_steps: int
    monitor_url: str


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
    run_id: str


class ListArtifactsRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class ReadWorkspaceTextRequest(BaseModel):
    path: str
    max_chars: int = Field(default=12000, ge=1, le=120000)


class ReadWorkspaceTextResponse(BaseModel):
    path: str
    content: str


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
    phase: str | None = None
    current_line: str | None = None
    error: str | None = None


def _resolve_workspace_path(path_str: str) -> Path:
    target = (ROOT / path_str).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise HTTPException(status_code=400, detail="Path escapes the project folder.")
    return target


def _artifact_snapshot() -> dict[str, tuple[float, int]]:
    snapshot: dict[str, tuple[float, int]] = {}
    for scan_root in (ARTIFACT_DIR, STORAGE_DIR):
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
    return (
        "suggest 3-5 relevant follow-up questions" in t
        or '"follow_ups"' in t
        or "### chat history:" in t
    )


def _new_run_state(run_id: str, req: RunTaskRequest, effective_model: str) -> dict[str, Any]:
    now = time.time()
    return {
        "ok": True,
        "run_id": run_id,
        "task": req.task,
        "requested_model": req.model,
        "model": effective_model,
        "max_steps": req.max_steps,
        "started_at": now,
        "ended_at": None,
        "duration_seconds": 0.0,
        "phase": "starting",
        "current_line": "Starting task...",
        "busy": True,
        "error": None,
        "final_output": "",
        "log_path": "",
        "recent_artifacts": [],
        "returncode": None,
        "tail": deque(maxlen=_TAIL_MAX),
    }


def _copy_run_state(run: dict[str, Any]) -> dict[str, Any]:
    data = dict(run)
    data["tail"] = list(run.get("tail") or [])
    return data


def _mark_run_started(run_id: str, req: RunTaskRequest, effective_model: str) -> None:
    global _ACTIVE_RUN_ID
    with _STATE_LOCK:
        run = _new_run_state(run_id, req, effective_model)
        _RUNS[run_id] = run
        _ACTIVE_RUN_ID = run_id
        _STATE["busy"] = True
        _STATE["current_task"] = req.task
        _STATE["started_at"] = run["started_at"]
        _STATE["run_id"] = run_id
        _STATE["phase"] = "starting"
        _STATE["current_line"] = "Starting task..."
        _STATE["error"] = None
        _STATE["final_output"] = ""


def _mark_run_finished(run_id: str, phase: str = "done") -> None:
    global _ACTIVE_RUN_ID
    with _STATE_LOCK:
        run = _RUNS.get(run_id)
        if run:
            run["busy"] = False
            run["phase"] = phase
            if not run.get("ended_at"):
                run["ended_at"] = time.time()
                run["duration_seconds"] = round(run["ended_at"] - float(run["started_at"]), 2)
        if _ACTIVE_RUN_ID == run_id:
            _ACTIVE_RUN_ID = None
            _STATE["busy"] = False
            _STATE["current_task"] = None
            _STATE["started_at"] = None
            _STATE["run_id"] = None
            _STATE["phase"] = phase


def _update_run(run_id: str, **kwargs: Any) -> None:
    with _STATE_LOCK:
        run = _RUNS.get(run_id)
        if not run:
            return
        for key, value in kwargs.items():
            run[key] = value
        if _STATE.get("run_id") == run_id:
            if "phase" in kwargs:
                _STATE["phase"] = kwargs["phase"]
            if "current_line" in kwargs:
                _STATE["current_line"] = kwargs["current_line"]
            if "error" in kwargs:
                _STATE["error"] = kwargs["error"]
            if "final_output" in kwargs:
                _STATE["final_output"] = kwargs["final_output"]


def _infer_phase(line: str) -> str | None:
    line_lower = line.lower()
    if line.startswith("=== planner step"):
        return "planning"
    if line.startswith("[planner]"):
        return "planning"
    if line.startswith("[executor]"):
        return "executing"
    if line.startswith("[verifier]"):
        return "verifying"
    if line.startswith("=== final ==="):
        return "finishing"
    if "http://" in line_lower or "https://" in line_lower:
        return "web"
    if "write_file" in line_lower or "storage_save_text" in line_lower or "workspace_" in line_lower:
        return "coding"
    return None


def _append_tail(run_id: str, line: str) -> None:
    clean = line.rstrip("\r\n")
    if not clean:
        return
    event = {"ts": time.time(), "line": clean}
    with _STATE_LOCK:
        run = _RUNS.get(run_id)
        if not run:
            return
        tail = run.get("tail")
        if isinstance(tail, deque):
            tail.append(event)
        run["current_line"] = clean
        inferred = _infer_phase(clean)
        if inferred:
            run["phase"] = inferred
            if _STATE.get("run_id") == run_id:
                _STATE["phase"] = inferred
        if _STATE.get("run_id") == run_id:
            _STATE["current_line"] = clean


def _extract_final_output_from_log(text: str) -> str:
    marker = "=== final ==="
    if marker in text:
        tail = text.rsplit(marker, 1)[-1].strip()
        if tail:
            return tail
    stripped = text.strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    return "\n".join(lines[-12:]).strip()


def _build_agent_command(task: str, model: str, max_steps: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "agent.main",
        "--task",
        task,
        "--workspace",
        str(ROOT),
        "--model",
        model,
        "--max-steps",
        str(max_steps),
    ]


def _finish_run(run_id: str, req: RunTaskRequest, effective_model: str, full_log: str, before: dict[str, tuple[float, int]], returncode: int) -> dict[str, Any]:
    final_output = _extract_final_output_from_log(full_log)
    log_path = _write_log(run_id, full_log)
    error_detail = None
    if returncode != 0:
        error_detail = f"Agent subprocess exited with code {returncode}"

    if final_output and returncode == 0:
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
            full_log += "\n=== storage save warning ===\n" + traceback.format_exc()
            log_path = _write_log(run_id, full_log)

    changed = _changed_artifacts(before)
    duration = round(time.time() - float(_RUNS[run_id]["started_at"]), 2)
    payload = {
        "ok": error_detail is None,
        "task": req.task,
        "model": effective_model,
        "max_steps": req.max_steps,
        "duration_seconds": duration,
        "final_output": final_output or ("Task failed before producing a final answer." if error_detail else ""),
        "log_path": log_path,
        "recent_artifacts": [item.model_dump() for item in changed],
        "run_id": run_id,
        "returncode": returncode,
        "error": error_detail,
    }
    _update_run(
        run_id,
        ended_at=time.time(),
        duration_seconds=duration,
        final_output=payload["final_output"],
        log_path=log_path,
        recent_artifacts=payload["recent_artifacts"],
        returncode=returncode,
        error=error_detail,
        phase="error" if error_detail else "done",
        busy=False,
    )
    with _STATE_LOCK:
        _STATE["last_run"] = payload
    return payload


def _execute_run(run_id: str, req: RunTaskRequest) -> dict[str, Any]:
    global _ACTIVE_PROCESS
    effective_model = _sanitize_model_name(req.model)
    before = _artifact_snapshot()
    _mark_run_started(run_id, req, effective_model)

    output_buffer = io.StringIO()
    cmd = _build_agent_command(req.task, effective_model, req.max_steps)
    env = {**os.environ, "PYTHONUTF8": "1", "WORKSPACE": str(ROOT)}

    _append_tail(run_id, f"Requested model: {req.model}")
    _append_tail(run_id, f"Effective model: {effective_model}")
    _append_tail(run_id, f"[bridge] Task: {req.task}")
    _append_tail(run_id, f"[bridge] Command: {' '.join(cmd)}")

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        _ACTIVE_PROCESS = proc
        _update_run(run_id, worker_pid=proc.pid, phase="running")

        assert proc.stdout is not None
        for line in proc.stdout:
            output_buffer.write(line)
            _append_tail(run_id, line)

        returncode = proc.wait()
        full_log = output_buffer.getvalue()
        return _finish_run(run_id, req, effective_model, full_log, before, returncode)
    except Exception as exc:
        output_buffer.write("\n=== exception ===\n")
        output_buffer.write(traceback.format_exc())
        full_log = output_buffer.getvalue()
        payload = _finish_run(run_id, req, effective_model, full_log, before, returncode=1)
        payload["ok"] = False
        payload["error"] = str(exc)
        _update_run(run_id, error=str(exc), phase="error", busy=False)
        with _STATE_LOCK:
            _STATE["last_run"] = payload
        return payload
    finally:
        _ACTIVE_PROCESS = None
        phase = "error" if _RUNS.get(run_id, {}).get("error") else "done"
        _mark_run_finished(run_id, phase=phase)
        if _AGENT_LOCK.locked():
            with contextlib.suppress(RuntimeError):
                _AGENT_LOCK.release()


def _async_worker(run_id: str, req: RunTaskRequest) -> None:
    global _ACTIVE_THREAD
    try:
        _execute_run(run_id, req)
    finally:
        _ACTIVE_THREAD = None


def _break_stale_run_if_needed() -> None:
    with _STATE_LOCK:
        started_at = _STATE.get("started_at")
        busy = bool(_STATE.get("busy"))
        run_id = _STATE.get("run_id")
    if busy and started_at and run_id:
        age = time.time() - float(started_at)
        if age > STALE_RUN_SECONDS:
            _terminate_active_process(reason=f"Stale run cleared after {round(age, 1)}s", force=True)


def _terminate_active_process(reason: str, force: bool = False) -> None:
    global _ACTIVE_PROCESS
    proc = _ACTIVE_PROCESS
    run_id = _ACTIVE_RUN_ID
    if not proc or proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        if force:
            with contextlib.suppress(Exception):
                proc.kill()
    if run_id:
        _append_tail(run_id, f"[bridge] {reason}")
        _update_run(run_id, error=reason, phase="resetting")


@app.get("/health", response_model=DesktopAgentStatusResponse, include_in_schema=False)
def health() -> DesktopAgentStatusResponse:
    with _STATE_LOCK:
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
            phase=_STATE.get("phase"),
            current_line=_STATE.get("current_line"),
            error=_STATE.get("error"),
        )


@app.get("/monitor-data", include_in_schema=False)
def monitor_data() -> dict[str, Any]:
    run_id = _ACTIVE_RUN_ID or (_STATE.get("last_run") or {}).get("run_id")
    if not run_id:
        return {
            "ok": True,
            "busy": False,
            "phase": "idle",
            "task": None,
            "run_id": None,
            "started_at": None,
            "elapsed_seconds": 0,
            "current_line": "",
            "error": None,
            "final_output": "",
            "tail": [],
            "last_run": _STATE.get("last_run"),
            "recent_artifacts": [item.model_dump() for item in _recent_artifacts(limit=10)],
        }
    return monitor_data_for_run(run_id)


@app.get("/monitor-data/{run_id}", include_in_schema=False)
def monitor_data_for_run(run_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        run = _RUNS.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found.")
        started_at = run.get("started_at")
        return {
            "ok": True,
            "busy": bool(run.get("busy")),
            "phase": run.get("phase"),
            "task": run.get("task"),
            "run_id": run_id,
            "started_at": started_at,
            "elapsed_seconds": round((time.time() - started_at), 1) if started_at and run.get("busy") else run.get("duration_seconds", 0),
            "current_line": run.get("current_line"),
            "error": run.get("error"),
            "final_output": run.get("final_output"),
            "tail": list(run.get("tail") or []),
            "last_run": _STATE.get("last_run"),
            "recent_artifacts": run.get("recent_artifacts") or [],
            "log_path": run.get("log_path") or "",
            "worker_pid": run.get("worker_pid"),
            "returncode": run.get("returncode"),
        }


@app.post("/reset", include_in_schema=False)
def reset_bridge() -> dict[str, Any]:
    current_run_id = _ACTIVE_RUN_ID
    if current_run_id:
        _terminate_active_process(reason="Reset requested by user.", force=True)
        # worker thread will finalize state after process exits
        return {"ok": True, "message": f"Reset requested for active run {current_run_id}."}

    with _STATE_LOCK:
        _STATE["last_run"] = None
        _STATE["error"] = None
        _STATE["final_output"] = ""
        _STATE["current_line"] = "Bridge reset."
        _STATE["phase"] = "reset"
    return {"ok": True, "message": "Bridge state reset."}


def _render_monitor_html(run_id: str) -> str:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Manus Live TV - {run_id}</title>
<style>
:root{{--bg:#0b1020;--panel:#10182d;--muted:#94a3b8;--text:#e2e8f0;--accent:#38bdf8;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:#020617;color:var(--text)}}
.wrap{{max-width:1400px;margin:0 auto;padding:16px}}
body.embed .wrap{{max-width:none;padding:10px}}
body.embed .header small{{display:none}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}}
.badge{{padding:6px 10px;border-radius:999px;font-size:12px;font-weight:700;background:#0f172a;color:#cbd5e1;border:1px solid #1e293b}}
.badge.busy{{background:#082f49;color:#bae6fd;border-color:#0ea5e9}}
.grid{{display:grid;grid-template-columns:380px 1fr;gap:16px}}
body.embed .grid{{grid-template-columns:320px 1fr;gap:10px}}
.panel{{background:linear-gradient(180deg,#0f172a,#0b1223);border:1px solid #1f2b45;border-radius:18px;box-shadow:0 10px 30px rgba(0,0,0,.35)}}
.panel .hd{{padding:12px 14px;border-bottom:1px solid #1f2b45;color:#93c5fd;font-weight:700}}
.panel .bd{{padding:14px}}
.kv{{display:grid;grid-template-columns:110px 1fr;gap:8px 10px;font-size:14px}}
.kv div:nth-child(odd){{color:var(--muted)}}
.tv{{height:66vh;overflow:auto;background:#040814;border-radius:14px;padding:14px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px;line-height:1.45;border:1px solid #1f2b45;white-space:pre-wrap}}
body.embed .tv{{height:70vh}}
.line{{padding:2px 0;border-bottom:1px dotted rgba(148,163,184,.08)}}
.line.plan{{color:#93c5fd}}.line.exec{{color:#fcd34d}}.line.verify{{color:#86efac}}.line.err{{color:#fca5a5}}
.answer{{white-space:pre-wrap;background:#07111f;border-radius:12px;padding:12px;border:1px solid #1f2b45;max-height:220px;overflow:auto}}
.art{{font-size:13px;line-height:1.5}}
small{{color:var(--muted)}}
button{{background:#0ea5e9;color:#04111f;border:none;border-radius:10px;padding:8px 12px;font-weight:700;cursor:pointer}}
button:hover{{filter:brightness(1.1)}}
a{{color:#7dd3fc}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h2 style="margin:0 0 6px 0">📺 Manus Live TV</h2>
      <small>Run ID: {run_id}</small>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <span id="badge" class="badge">idle</span>
      <button onclick="resetBridge()">Reset Run</button>
    </div>
  </div>
  <div class="grid">
    <div class="panel">
      <div class="hd">Live status</div>
      <div class="bd">
        <div class="kv">
          <div>Task</div><div id="task">—</div>
          <div>Phase</div><div id="phase">idle</div>
          <div>Elapsed</div><div id="elapsed">0s</div>
          <div>Current</div><div id="current">—</div>
          <div>Error</div><div id="error">—</div>
          <div>Log</div><div id="log">—</div>
        </div>
        <hr style="border-color:#1f2b45;margin:14px 0">
        <div><strong>Final answer</strong></div>
        <div id="answer" class="answer">No answer yet.</div>
        <hr style="border-color:#1f2b45;margin:14px 0">
        <div><strong>Artifacts</strong></div>
        <div id="artifacts" class="art">No recent artifacts.</div>
      </div>
    </div>
    <div class="panel">
      <div class="hd">Backend transcript</div>
      <div class="bd">
        <div id="tv" class="tv"></div>
      </div>
    </div>
  </div>
</div>
<script>
const RUN_ID = {json.dumps(run_id)};
const EMBED = new URLSearchParams(window.location.search).get("embed") === "1";
if (EMBED) document.body.classList.add("embed");
let lastFingerprint = "";
let autoScroll = true;
const tv = document.getElementById("tv");
tv.addEventListener("scroll", () => {{
  autoScroll = (tv.scrollTop + tv.clientHeight) >= (tv.scrollHeight - 20);
}});
function cssClass(line){{
  const l = (line || "").toLowerCase();
  if (l.startsWith("[planner]") || l.startsWith("=== planner")) return "plan";
  if (l.startsWith("[executor]")) return "exec";
  if (l.startsWith("[verifier]")) return "verify";
  if (l.includes("error") || l.includes("exception") || l.includes("failed")) return "err";
  return "";
}}
function esc(s){{ return (s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }}
async function poll(){{
  try{{
    const r = await fetch(`/monitor-data/${{encodeURIComponent(RUN_ID)}}`, {{cache:"no-store"}});
    const data = await r.json();
    const badge = document.getElementById("badge");
    badge.textContent = data.busy ? "busy" : "idle";
    badge.className = "badge" + (data.busy ? " busy" : "");
    document.getElementById("task").textContent = data.task || "—";
    document.getElementById("phase").textContent = data.phase || "idle";
    document.getElementById("elapsed").textContent = (data.elapsed_seconds || 0) + "s";
    document.getElementById("current").textContent = data.current_line || "—";
    document.getElementById("error").textContent = data.error || "—";
    document.getElementById("log").innerHTML = data.log_path ? `<a href="/read-workspace-text-file" onclick="return false;">${{esc(data.log_path)}}</a>` : "—";
    const answerText = data.final_output || "No answer yet.";
    document.getElementById("answer").textContent = answerText;
    const arts = data.recent_artifacts || [];
    document.getElementById("artifacts").innerHTML = arts.length
      ? arts.map(a => "• " + esc(a.path) + " (" + a.size_bytes + " bytes)").join("<br>")
      : "No recent artifacts.";
    const lines = (data.tail || []).map(x => x.line);
    const fp = lines.join("\\n");
    if (fp !== lastFingerprint){{
      lastFingerprint = fp;
      tv.innerHTML = lines.map(line => `<div class="line ${{cssClass(line)}}">${{esc(line)}}</div>`).join("");
      if (autoScroll){{ tv.scrollTop = tv.scrollHeight; }}
    }}
  }}catch(e){{
    document.getElementById("error").textContent = "Monitor fetch failed: " + e;
  }}finally{{
    setTimeout(poll, 1000);
  }}
}}
async function resetBridge(){{
  await fetch("/reset", {{method:"POST"}});
}}
poll();
</script>
</body>
</html>
"""


@app.get("/monitor", response_class=HTMLResponse, include_in_schema=False)
def monitor_page_latest(embed: int = Query(default=0)) -> HTMLResponse:
    run_id = _ACTIVE_RUN_ID or (_STATE.get("last_run") or {}).get("run_id")
    if not run_id:
        return HTMLResponse("<html><body style='font-family:sans-serif;background:#020617;color:#e2e8f0'>No runs yet.</body></html>")
    suffix = "?embed=1" if embed else ""
    return HTMLResponse(f"<html><body style='margin:0'><script>location.replace('/monitor/{run_id}{suffix}')</script></body></html>")


@app.get("/monitor/{run_id}", response_class=HTMLResponse, include_in_schema=False)
def monitor_page_for_run(run_id: str, embed: int = Query(default=0)) -> HTMLResponse:
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail="Run not found.")
    html = _render_monitor_html(run_id)
    if embed:
        html = html.replace("<body>", "<body class='embed'>", 1)
    return HTMLResponse(html)


@app.post(
    "/start-local-desktop-agent",
    response_model=StartTaskResponse,
    operation_id="start_local_desktop_agent",
    summary="Start the local desktop/browser/workspace agent asynchronously and return a run ID",
    tags=["agent"],
)
def start_local_desktop_agent(req: RunTaskRequest) -> StartTaskResponse:
    global _ACTIVE_THREAD
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task is empty.")

    if _is_openwebui_meta_task(req.task):
        raise HTTPException(status_code=400, detail="Meta tasks should not be sent to async start.")

    _break_stale_run_if_needed()

    if not _AGENT_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "The local agent is already running another task.",
                    "current_task": _STATE.get("current_task"),
                    "started_at": _STATE.get("started_at"),
                    "run_id": _STATE.get("run_id"),
                },
            )

    run_id = time.strftime("%Y%m%d-%H%M%S")
    effective_model = _sanitize_model_name(req.model)
    _mark_run_started(run_id, req, effective_model)

    worker = threading.Thread(target=_async_worker, args=(run_id, req), daemon=True, name=f"manus-run-{run_id}")
    _ACTIVE_THREAD = worker
    worker.start()

    return StartTaskResponse(
        ok=True,
        run_id=run_id,
        task=req.task,
        model=effective_model,
        max_steps=req.max_steps,
        monitor_url=f"/monitor/{run_id}?embed=1",
    )


@app.post(
    "/run-local-desktop-agent",
    response_model=RunTaskResponse,
    operation_id="run_local_desktop_agent",
    summary="Run the local desktop/browser/workspace agent on this PC and wait for completion",
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
            run_id="meta-task",
        )

    _break_stale_run_if_needed()

    if not _AGENT_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "The local agent is already running another task.",
                    "current_task": _STATE.get("current_task"),
                    "started_at": _STATE.get("started_at"),
                    "run_id": _STATE.get("run_id"),
                },
            )

    run_id = time.strftime("%Y%m%d-%H%M%S")
    payload = _execute_run(run_id, req)
    if not payload["ok"]:
        raise HTTPException(status_code=500, detail=payload)
    return RunTaskResponse(**payload)


@app.post(
    "/list-recent-workspace-artifacts",
    response_model=list[ArtifactItem],
    operation_id="list_recent_workspace_artifacts",
    summary="List recent files created under the workspace artifacts folder",
    tags=["artifacts"],
)
def list_recent_workspace_artifacts(req: ListArtifactsRequest) -> list[ArtifactItem]:
    return _recent_artifacts(limit=req.limit)


@app.post(
    "/read-workspace-text-file",
    response_model=ReadWorkspaceTextResponse,
    operation_id="read_workspace_text_file",
    summary="Read a UTF-8 text file from the workspace",
    tags=["workspace"],
)
def read_workspace_text_file(req: ReadWorkspaceTextRequest) -> ReadWorkspaceTextResponse:
    target = _resolve_workspace_path(req.path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"File is not UTF-8 text: {exc}") from exc
    return ReadWorkspaceTextResponse(
        path=str(target.relative_to(ROOT)).replace("\\", "/"),
        content=content[: req.max_chars],
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
