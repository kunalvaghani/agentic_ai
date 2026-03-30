@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "OLLAMA_URL=http://localhost:11434/api/tags"
set "WEBUI_URL=http://127.0.0.1:3000"
set "BRIDGE_URL=http://127.0.0.1:8787/health"
set "BRIDGE_DOCS=http://127.0.0.1:8787/docs"
set "WEBUI_NAME=open-webui"
set "CHAT_MODEL=qwen3:8b"
set "CODE_MODEL=qwen2.5-coder:7b"
set "EMBED_MODEL=embeddinggemma"
set "OLLAMA_LOG=%LOCALAPPDATA%\Ollama\server.log"
set "OLLAMA_SERVE_LOG=%TEMP%\ollama-serve.log"

if not "%~1"=="" (
  echo This launcher starts the bridge service. It does not take a task argument.
  echo Use Open WebUI with the Manus Local Bridge pipe after startup.
  echo.
)

echo.
echo === Manus Open WebUI Bridge Starter ===
echo Project: %CD%
echo.

call :ensure_ollama || goto :fail
call :ensure_docker || goto :fail
call :ensure_models || goto :fail
call :ensure_webui || goto :fail
call :ensure_python_env || goto :fail
call :ensure_bridge || goto :fail
call :open_webui || goto :fail
call :open_bridge_docs || goto :fail

echo.
echo Everything is up.
echo Open WebUI: %WEBUI_URL%
echo Bridge docs: %BRIDGE_DOCS%
echo.
echo Next in Open WebUI:
echo   1. Import openwebui\manus_local_bridge_pipe.py as a Pipe Function.
echo   2. Enable it.
echo   3. Set BRIDGE_URL valve to http://host.docker.internal:8787
echo   4. Select "Manus Local Bridge" as the model.
echo.
exit /b 0

:ensure_ollama
echo [1/7] Checking Ollama...
where ollama >nul 2>nul || (
  echo Ollama is not installed or not in PATH.
  exit /b 1
)

call :ollama_ready
if !errorlevel! equ 0 (
  echo Ollama is already ready.
  exit /b 0
)

echo Ollama is not responding yet. Trying desktop app first...
call :start_ollama_app
call :wait_ollama 60 2
if !errorlevel! equ 0 exit /b 0

echo Desktop app did not become ready. Trying background server...
start "Ollama Server" /min cmd /c "ollama serve > \"%OLLAMA_SERVE_LOG%\" 2>&1"
call :wait_ollama 60 2
if !errorlevel! equ 0 exit /b 0

echo Ollama still did not become ready.
if exist "%OLLAMA_LOG%" echo Check Ollama log: %OLLAMA_LOG%
if exist "%OLLAMA_SERVE_LOG%" echo Check serve log: %OLLAMA_SERVE_LOG%
exit /b 1

:start_ollama_app
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
  start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
  exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Ollama\Ollama app.exe" (
  start "" "%LOCALAPPDATA%\Programs\Ollama\Ollama app.exe"
  exit /b 0
)
if exist "%ProgramFiles%\Ollama\ollama app.exe" (
  start "" "%ProgramFiles%\Ollama\ollama app.exe"
  exit /b 0
)
if exist "%ProgramFiles%\Ollama\Ollama app.exe" (
  start "" "%ProgramFiles%\Ollama\Ollama app.exe"
  exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
  start "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
  exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Ollama\Ollama.exe" (
  start "" "%LOCALAPPDATA%\Programs\Ollama\Ollama.exe"
  exit /b 0
)
if exist "%ProgramFiles%\Ollama\ollama.exe" (
  start "" "%ProgramFiles%\Ollama\ollama.exe"
  exit /b 0
)
if exist "%ProgramFiles%\Ollama\Ollama.exe" (
  start "" "%ProgramFiles%\Ollama\Ollama.exe"
  exit /b 0
)
exit /b 0

:ollama_ready
ollama list >nul 2>nul
if %errorlevel% equ 0 exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing 'http://localhost:11434/api/tags' -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
exit /b %errorlevel%

:wait_ollama
set "TRIES=%~1"
set "SLEEP=%~2"
for /l %%I in (1,1,!TRIES!) do (
  call :ollama_ready
  if !errorlevel! equ 0 (
    echo Ollama is ready.
    exit /b 0
  )
  timeout /t !SLEEP! /nobreak >nul
)
exit /b 1

:ensure_docker
echo [2/7] Checking Docker...
where docker >nul 2>nul || (
  echo Docker CLI was not found. Install Docker Desktop first.
  exit /b 1
)

docker info >nul 2>nul
if !errorlevel! equ 0 (
  echo Docker is already running.
  exit /b 0
)

echo Starting Docker Desktop...
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
  start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
) else if exist "%LocalAppData%\Docker\Docker Desktop.exe" (
  start "" "%LocalAppData%\Docker\Docker Desktop.exe"
) else (
  echo Docker Desktop.exe was not found.
  exit /b 1
)

for /l %%I in (1,1,120) do (
  docker info >nul 2>nul
  if !errorlevel! equ 0 (
    echo Docker is ready.
    exit /b 0
  )
  timeout /t 2 /nobreak >nul
)

echo Docker did not become ready in time.
exit /b 1

:ensure_models
echo [3/7] Checking Ollama models...
call :ensure_model "%CHAT_MODEL%" || exit /b 1
call :ensure_model "%CODE_MODEL%" || exit /b 1
call :ensure_model "%EMBED_MODEL%" || exit /b 1
exit /b 0

:ensure_model
echo   - %~1
ollama list | findstr /i /c:"%~1" >nul
if %errorlevel% equ 0 (
  echo     already installed.
  exit /b 0
)

echo     pulling %~1 ...
ollama pull %~1
exit /b %errorlevel%

:ensure_webui
echo [4/7] Checking Open WebUI...
docker ps --format "{{.Names}}" | findstr /i /x /c:"%WEBUI_NAME%" >nul
if !errorlevel! equ 0 (
  echo Open WebUI container is already running.
  call :wait_http "%WEBUI_URL%" 90 2 "Open WebUI"
  exit /b %errorlevel%
)

docker ps -a --format "{{.Names}}" | findstr /i /x /c:"%WEBUI_NAME%" >nul
if !errorlevel! equ 0 (
  echo Starting existing Open WebUI container...
  docker start %WEBUI_NAME% >nul || exit /b 1
) else (
  echo Creating Open WebUI container...
  docker run -d -p 3000:8080 -e OLLAMA_BASE_URL=http://host.docker.internal:11434 -v open-webui:/app/backend/data --name %WEBUI_NAME% --restart always ghcr.io/open-webui/open-webui:main >nul || exit /b 1
)

call :wait_http "%WEBUI_URL%" 90 2 "Open WebUI"
exit /b %errorlevel%

:ensure_python_env
echo [5/7] Checking Python environment and bridge dependencies...
where py >nul 2>nul || (
  echo Python launcher ^(py^) was not found. Install Python 3 first.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv || exit /b 1
)

set "NEEDS_BRIDGE_REQS=0"
if not exist ".venv\.ready" set "NEEDS_BRIDGE_REQS=1"
if not exist ".venv\.openwebui-bridge-ready" set "NEEDS_BRIDGE_REQS=1"
if !NEEDS_BRIDGE_REQS! equ 0 (
  .\.venv\Scripts\python.exe -c "import fastapi, uvicorn" >nul 2>nul
  if errorlevel 1 set "NEEDS_BRIDGE_REQS=1"
)

if !NEEDS_BRIDGE_REQS! equ 1 (
  echo Installing Python requirements...
  .\.venv\Scripts\python.exe -m pip install --upgrade pip || exit /b 1
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
  type nul > ".venv\.ready"
  type nul > ".venv\.openwebui-bridge-ready"
) else (
  echo Python packages already marked ready.
)

if not exist ".venv\.level2-browser-ready" (
  echo Installing Playwright Chromium...
  .\.venv\Scripts\python.exe -m playwright install chromium || exit /b 1
  type nul > ".venv\.level2-browser-ready"
) else (
  echo Playwright Chromium already marked ready.
)

if not exist ".venv\.level3-desktop-ready" (
  echo Marking desktop automation dependencies as ready...
  type nul > ".venv\.level3-desktop-ready"
) else (
  echo Desktop automation already marked ready.
)
exit /b 0

:ensure_bridge
echo [6/7] Checking local Open WebUI bridge...
call :bridge_ready
if !errorlevel! equ 0 (
  echo Bridge is already running.
  exit /b 0
)

echo Starting bridge server...
start "Manus Open WebUI Bridge" /min cmd /c "cd /d \"%~dp0\" && .\.venv\Scripts\python.exe bridge_server.py"
call :wait_http "%BRIDGE_URL%" 60 2 "Bridge server"
exit /b %errorlevel%

:bridge_ready
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8787/health' -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
exit /b %errorlevel%

:open_webui
echo [7/7] Opening Open WebUI in your browser...
start "" "%WEBUI_URL%"
exit /b 0

:open_bridge_docs
echo Opening bridge API docs...
start "" "%BRIDGE_DOCS%"
exit /b 0

:http_ready
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing '%~1' -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
exit /b %errorlevel%

:wait_http
set "URL=%~1"
set "TRIES=%~2"
set "SLEEP=%~3"
set "LABEL=%~4"
for /l %%I in (1,1,!TRIES!) do (
  call :http_ready "%URL%"
  if !errorlevel! equ 0 (
    echo !LABEL! is ready.
    exit /b 0
  )
  timeout /t !SLEEP! /nobreak >nul
)
echo !LABEL! did not become ready in time.
exit /b 1

:fail
echo.
echo Startup failed. Fix the error above and run the BAT file again.
pause
exit /b 1
