
# Manus Live TV Patch

This patch adds a live monitor page to your bridge.

## What it adds
- `/monitor` : TV-style live page
- `/monitor-data` : polling JSON for current task, phase, current line, tail transcript, final answer, artifacts
- `/reset` : clears stuck bridge state
- live transcript streaming from the same `print(...)` calls your current agent already emits

## Why you only saw a dot before
Your current `bridge_server.py` redirects `stdout` into a `StringIO` buffer and only writes a log after the run finishes, so Open WebUI has nothing live to show while the task is running. The agent itself already prints planner, executor, verifier, and tool output lines, but they were only visible after completion.

## How to install
1. Back up your current `bridge_server.py`
2. Replace it with the patched `bridge_server.py`
3. Restart the bridge:
   `& ".\.venv\Scripts\python.exe" -m uvicorn bridge_server:app --host 0.0.0.0 --port 8787`
4. Open:
   `http://127.0.0.1:8787/monitor`

## What you will see
- current task
- current phase: planning / executing / verifying / web / coding
- current line
- live backend transcript
- final answer
- recent artifacts

## Extra
This patch also ignores Open WebUI hidden follow-up-generation tasks so they stop colliding with the bridge lock.
