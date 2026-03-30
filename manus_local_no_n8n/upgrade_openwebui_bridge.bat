@echo off
setlocal
cd /d "%~dp0"

echo === Upgrade to Open WebUI Bridge ===
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv || goto :fail
)

.\.venv\Scripts\python.exe -m pip install --upgrade pip || goto :fail
.\.venv\Scripts\python.exe -m pip install -r requirements.txt || goto :fail
.\.venv\Scripts\python.exe -m playwright install chromium || goto :fail
if not exist ".env" copy ".env.example" ".env" >nul
if not exist ".venv\.ready" type nul > ".venv\.ready"
if not exist ".venv\.level2-browser-ready" type nul > ".venv\.level2-browser-ready"
if not exist ".venv\.level3-desktop-ready" type nul > ".venv\.level3-desktop-ready"
if not exist ".venv\.openwebui-bridge-ready" type nul > ".venv\.openwebui-bridge-ready"

echo.
echo Bridge dependencies are installed.
echo Next:
echo   1. Run start_manus_openwebui_bridge.bat
echo   2. In Open WebUI import openwebui\manus_local_bridge_pipe.py as a Pipe Function
echo   3. Enable it and set BRIDGE_URL to http://host.docker.internal:8787
echo   4. Select Manus Local Bridge from the model list
echo.
pause
exit /b 0

:fail
echo.
echo Upgrade failed.
pause
exit /b 1
