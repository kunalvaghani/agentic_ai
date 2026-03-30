# Local Manus-style setup with browser + desktop automation

This is a practical local stack:
- **Ollama** for local models
- **Open WebUI** for desktop-style chat UI
- **Continue** for editor-integrated coding/agent mode
- **A Python agent** for tool-calling loops over your local workspace, browser, and Windows desktop

## 1) Install the base pieces

### Ollama
Install Ollama, then pull the models:

```powershell
./scripts/setup_models.ps1
```

or

```bash
./scripts/setup_models.sh
```

### Open WebUI
Run Open WebUI with Docker:

```bash
docker compose up -d
```

Then open `http://localhost:3000`.
Inside Open WebUI, connect Ollama at:

```text
http://host.docker.internal:11434
```

### Continue
Copy `continue-config.yaml` into your local Continue config:
- Windows: `%USERPROFILE%\.continue\config.yaml`
- macOS/Linux: `~/.continue/config.yaml`

Then restart VS Code / JetBrains and choose the config.

## 2) Install Python dependencies and browser/desktop runtime

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # Windows
# or: cp .env.example .env
playwright install chromium
```

Helper scripts:

```powershell
./scripts/setup_browser.ps1
./scripts/setup_desktop.ps1
```

or

```bash
./scripts/setup_browser.sh
./scripts/setup_desktop.sh
```

## 3) Run the local agent

From your project folder:

```bash
python -m agent.main --workspace . --task "Open Notepad, type a short message, save a screenshot to artifacts/notepad.png, and tell me what happened."
```

Browser task example:

```bash
python -m agent.main --workspace . --task "Open https://example.com in a browser, inspect the page, and summarize the main call to action."
```

Repo task example:

```bash
python -m agent.main --workspace . --task "Inspect this repo, summarize the architecture, then suggest the smallest safe improvement."
```

## 4) Browser tools included in Level 2

The agent can:
- start a Chromium browser session
- open URLs
- inspect the page and list interactive elements with selector hints
- click buttons and links by selector or visible text
- fill inputs by selector or label text
- press keys such as Enter, Tab, and Escape
- read visible page text
- wait for text to appear
- save screenshots into the workspace

## 5) Desktop tools included in Level 3

The agent can now also:
- list top-level windows and detect the active window
- focus a window by title
- open apps and URLs
- move the mouse and click screen coordinates
- press hotkeys such as `ctrl+l`, `alt+tab`, and `ctrl+shift+n`
- type text into the focused app
- read and write the clipboard
- capture desktop screenshots into the workspace
- locate a template image on the screen and click it
- list running processes

## 6) Safe workflow I recommend

1. For websites, prefer browser tools over raw desktop clicks.
2. For desktop apps, inspect state first with `desktop_get_active_window`, `desktop_list_windows`, or `desktop_screenshot`.
3. Focus the target window before typing or using hotkeys.
4. Save screenshots before and after risky UI actions.
5. Keep file outputs inside the workspace, usually under `artifacts/`.
6. Avoid prompts that require credentials, purchases, or destructive system changes.

## 7) Example prompts

### Browser tasks
- "Open https://news.ycombinator.com, inspect the page, and summarize the top 5 headlines."
- "Open GitHub, navigate to the login page, and tell me which fields and buttons are present without submitting anything."
- "Open a docs page, take a screenshot, and save it to artifacts/docs-home.png."

### Desktop tasks
- "Open Notepad, type 'hello from the local agent', take a screenshot to artifacts/notepad.png, and report the active window title."
- "Focus the Chrome window, press ctrl+l, type https://example.com, press Enter, and save a desktop screenshot."
- "List open windows, switch to the one containing 'Visual Studio Code', and tell me the active window size."

### Repo tasks
- "Index this workspace, then find where auth is implemented and explain the login flow."
- "Inspect this repo, identify the entrypoint, and run the test suite."

## 8) Notes

- `run_command` blocks obviously destructive commands like `rm -rf`, `format`, and `shutdown`.
- The semantic index is stored in `.agent_index.json` inside the workspace.
- Screenshots are saved inside the workspace, usually under `artifacts/`.
- Browser navigation is not workspace-bound, but file outputs are.
- Desktop automation is **Windows-oriented** in this Level 3 build and relies on the target app being visible and interactable.
- Coordinate clicks are fragile across DPI scaling, layout changes, and multiple monitors. Prefer browser tools or window focus + hotkeys when possible.

## 9) Suggested local models

- General chat / planning: `qwen3:8b`
- Coding: `qwen2.5-coder:7b`
- Embeddings: `embeddinggemma`

If your GPU is stronger, swap in a larger coding model in both Ollama and Continue.


## Open WebUI bridge

This repo now includes a real bridge between Open WebUI chat and the local agent. See `OPENWEBUI_BRIDGE_SETUP.md`.

Quick version:

1. Run `upgrade_openwebui_bridge.bat` once.
2. Run `start_manus_openwebui_bridge.bat`.
3. Import `openwebui/manus_local_bridge_pipe.py` into Open WebUI as a Pipe Function.
4. Set the Pipe valve `BRIDGE_URL` to `http://host.docker.internal:8787`.
5. Select **Manus Local Bridge** as the model in Open WebUI.

This route gives you direct chat-driven desktop/browser/workspace control without typing BAT commands in the chat box.


## Smart storage

This build adds a `storage/` folder inside the project root. New screenshots, saved page captures, organized files, and agent run summaries are automatically categorized and named under that folder. Use the `storage_list_recent`, `storage_save_text`, and `storage_organize_file` tools when you want the agent to save deliverables cleanly.
