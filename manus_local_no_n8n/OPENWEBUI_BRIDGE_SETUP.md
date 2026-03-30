# Open WebUI bridge setup

This project now includes a real bridge between Open WebUI chat and the local desktop/browser/workspace agent.

## What is included

- `bridge_server.py` — local FastAPI bridge running on `http://127.0.0.1:8787`
- `openwebui/manus_local_bridge_pipe.py` — Open WebUI Pipe Function that forwards chat messages into the bridge
- `start_manus_openwebui_bridge.bat` — starts Ollama, Docker/Open WebUI, Python env, and the bridge server
- `upgrade_openwebui_bridge.bat` — one-time dependency installer

## Recommended connection method: Pipe Function

This gives you the cleanest experience: choose **Manus Local Bridge** as a model in Open WebUI and type tasks normally.

1. Run `upgrade_openwebui_bridge.bat` once.
2. Run `start_manus_openwebui_bridge.bat`.
3. In Open WebUI, open the Functions editor and create/import a **Pipe Function**.
4. Paste the contents of `openwebui/manus_local_bridge_pipe.py`.
5. Save it, then enable it.
6. Open the Pipe's valves/settings and set `BRIDGE_URL` to `http://host.docker.internal:8787`.
7. Select **Manus Local Bridge** in the model picker.
8. Chat with it normally, for example:
   - `Open Notepad, type hello from the local desktop agent, and take a screenshot to artifacts/notepad.png`
   - `Open https://www.youtube.com and search for lofi Bollywood songs`
   - `Read README.md and summarize the architecture`

## Alternative connection method: OpenAPI Tool Server

If you prefer to keep using a normal model and let it call a tool:

1. Run `start_manus_openwebui_bridge.bat`.
2. In Open WebUI, open **Settings → Tools**.
3. Add a new tool server with URL `http://127.0.0.1:8787`.
4. Enable the `run_local_desktop_agent` tool in chat or on the model.

For small local models, Open WebUI's **Default** function-calling mode is often more reliable than **Native** mode for complex tool routing.

## Notes

- The bridge only allows one active task at a time.
- Task logs are written to `logs/bridge-run-*.log`.
- Screenshots and other generated files go into `artifacts/`.
- Browser automation is more reliable than raw desktop clicking. Prefer browser tasks when possible.
