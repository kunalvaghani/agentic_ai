@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Manus Reset + Recreate Open WebUI

cd /d "%~dp0"
set "PROJECT_DIR=%CD%"
set "OPENWEBUI_NAME=open-webui"
set "OPENWEBUI_IMAGE=ghcr.io/open-webui/open-webui:main"
set "OLLAMA_URL=http://host.docker.internal:11434"
set "WEBUI_PORT=3000"
set "BRIDGE_PORT=8787"

echo ============================================================
echo   MANUS RESET + RECREATE OPEN WEBUI
echo ============================================================
echo Project: %PROJECT_DIR%
echo.

REM ------------------------------------------------------------
REM 0) Check basics
REM ------------------------------------------------------------
echo [0/8] Checking prerequisites...
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Missing .venv\Scripts\python.exe
  echo Make sure you run this from your project folder.
  echo.
  pause
  exit /b 1
)

set "BRIDGE_MODULE="
if exist "bridge_server.py" set "BRIDGE_MODULE=bridge_server:app"
if not defined BRIDGE_MODULE if exist "openwebui\bridge_server.py" set "BRIDGE_MODULE=openwebui.bridge_server:app"

if not defined BRIDGE_MODULE (
  echo ERROR: Could not find bridge_server.py
  echo Looked for:
  echo   %PROJECT_DIR%\bridge_server.py
  echo   %PROJECT_DIR%\openwebui\bridge_server.py
  echo.
  pause
  exit /b 1
)

echo Bridge module: %BRIDGE_MODULE%
echo.

echo [1/8] Checking Ollama...
ollama list >nul 2>&1
if errorlevel 1 (
  echo ERROR: Ollama is not ready. Start Ollama first, then run this file again.
  echo.
  pause
  exit /b 1
)
echo Ollama is ready.
echo.

echo [2/8] Checking Docker...
docker version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Docker Desktop is not ready. Start Docker Desktop first, then run this file again.
  echo.
  pause
  exit /b 1
)
echo Docker is ready.
echo.

REM ------------------------------------------------------------
REM 1) Kill bridge/agent processes and free port 8787
REM ------------------------------------------------------------
echo [3/8] Stopping anything listening on port %BRIDGE_PORT%...
set "FOUNDPORT="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%BRIDGE_PORT% .*LISTENING"') do (
  set "FOUNDPORT=1"
  echo   Killing PID %%P
  taskkill /PID %%P /F >nul 2>&1
)
if not defined FOUNDPORT echo   Nothing was listening on %BRIDGE_PORT%.
echo.

echo [4/8] Killing stale AI Python processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"$targets = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match 'python|pythonw|py') -and ( $_.CommandLine -match 'uvicorn' -or $_.CommandLine -match 'bridge_server' -or $_.CommandLine -match 'agent\.main' -or $_.CommandLine -match 'playwright' ) }; if ($targets) { $targets | ForEach-Object { Write-Host ('  Killing PID ' + $_.ProcessId + ' :: ' + $_.Name); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } else { Write-Host '  No stale AI Python processes found.' }"
echo.

REM ------------------------------------------------------------
REM 2) Hard reset Open WebUI using the exact commands requested
REM ------------------------------------------------------------
echo [5/8] Recreating Open WebUI container...
docker rm -f %OPENWEBUI_NAME% >nul 2>&1
echo   Existing open-webui container removed if it existed.

docker run -d -p %WEBUI_PORT%:8080 -e OLLAMA_BASE_URL=%OLLAMA_URL% -v open-webui:/app/backend/data --name %OPENWEBUI_NAME% --restart always %OPENWEBUI_IMAGE%
if errorlevel 1 (
  echo.
  echo ERROR: Failed to recreate open-webui container.
  echo Try:
  echo   docker ps -a
  echo   docker logs %OPENWEBUI_NAME% --tail 100
  echo.
  pause
  exit /b 1
)
echo.

echo [6/8] Waiting for Open WebUI on http://127.0.0.1:%WEBUI_PORT% ...
set "OWU_OK="
for /L %%i in (1,1,40) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $null = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:%WEBUI_PORT% -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "OWU_OK=1"
    goto :owu_ready
  )
  timeout /t 2 >nul
)
:owu_ready
if not defined OWU_OK (
  echo ERROR: Open WebUI did not come up on http://127.0.0.1:%WEBUI_PORT%
  echo Try:
  echo   docker logs %OPENWEBUI_NAME% --tail 100
  echo.
  pause
  exit /b 1
)
echo Open WebUI is up.
echo.

REM ------------------------------------------------------------
REM 3) Start bridge directly, not through old starter BAT
REM ------------------------------------------------------------
echo [7/8] Starting bridge directly with uvicorn...
start "Manus Bridge API" /min cmd /c ""%PROJECT_DIR%\.venv\Scripts\python.exe" -m uvicorn %BRIDGE_MODULE% --host 0.0.0.0 --port %BRIDGE_PORT%"

echo Waiting for bridge health on http://127.0.0.1:%BRIDGE_PORT%/health ...
set "BRIDGE_OK="
for /L %%i in (1,1,40) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $null = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:%BRIDGE_PORT%/health -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "BRIDGE_OK=1"
    goto :bridge_ready
  )
  timeout /t 2 >nul
)
:bridge_ready
if not defined BRIDGE_OK (
  echo ERROR: Bridge health did not come up on http://127.0.0.1:%BRIDGE_PORT%/health
  echo Check:
  echo   the minimized "Manus Bridge API" window
  echo   %PROJECT_DIR%\logs
  echo.
  pause
  exit /b 1
)
echo Bridge is up.
echo.

echo [8/8] Opening pages...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process 'http://localhost:%WEBUI_PORT%'"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process 'http://127.0.0.1:%BRIDGE_PORT%/health'"

echo.
echo ============================================================
echo RESET COMPLETE
echo ============================================================
echo This script:
echo   - killed ongoing bridge/agent tasks
echo   - freed port %BRIDGE_PORT%
echo   - removed and recreated open-webui
echo   - started the bridge directly with uvicorn
echo.
echo IMPORTANT:
echo   - Use Manus Local Bridge only for action tasks
echo   - Use your normal model for regular chat
echo   - Send one action task once, then wait
echo.
pause
