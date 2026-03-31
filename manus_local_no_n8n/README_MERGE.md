
# Manus merged upgrade

This package gives you a safer and faster Manus merge without changing your model.

## Included files
- `agent/main.py`
- `agent/config.py`
- `agent/memory_os.py`
- `bridge_server.py`

## What it fixes
- adds missing `json` import in `agent/main.py`
- normal chat no longer goes through the slow action loop
- Open WebUI follow-up/title meta prompts are ignored instead of causing `409 busy` errors
- adds `/reset` bridge endpoint
- adds stale-run timeout recovery
- adds memory DB endpoints:
  - `/memory/init`
  - `/memory/ingest`
  - `/memory/query`
  - `/memory/consolidate`

## Replace these files
Copy the files over your current project:
- `bridge_server.py` -> project root
- `agent/main.py`
- `agent/config.py`
- `agent/memory_os.py`

## Restart
Run:
```powershell
cd "D:\Phyhton Project\manus_local_5\manus_local_no_n8n"
Get-NetTCPConnection -LocalPort 8787 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique | Where-Object { $_ -gt 4 } | ForEach-Object { Stop-Process -Id $_ -Force }
& ".\.venv\Scripts\python.exe" -m uvicorn bridge_server:app --host 0.0.0.0 --port 8787
```

## Recommended `.env`
```env
OLLAMA_KEEP_ALIVE=-1
OLLAMA_NUM_CTX=4096
OLLAMA_THINK=false
OLLAMA_TEMPERATURE=0.2
OLLAMA_PLANNER_TEMPERATURE=0.1
OLLAMA_VERIFIER_TEMPERATURE=0.05
BRIDGE_STALE_RUN_SECONDS=300
MEMORY_ENABLED=true
MEMORY_DB_PATH=storage/memory/manus_memory.db
```

## Memory usage
Initialize memory DB:
```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/memory/init
```

Ingest a file from your project:
```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/memory/ingest -ContentType "application/json" -Body '{"source_path":"README.md"}'
```

Query memory:
```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/memory/query -ContentType "application/json" -Body '{"query":"According to the ingested docs, what does the README say?"}'
```

Reset the bridge without restarting all of Docker:
```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/reset
```

## Important
- Use Manus Local Bridge for action tasks.
- Use your normal model for ordinary chat if you want the fastest UI experience.
- This merge keeps the same inner model name; it only changes routing and runtime behavior.
