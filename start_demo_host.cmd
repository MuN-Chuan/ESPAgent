@echo off
setlocal

if /i "%~1"=="--console" (
  set "ESPAGENT_CONSOLE=1"
  shift
)

if /i "%~1"=="--setup-only" (
  set "ESPAGENT_SETUP_ONLY=1"
  shift
)

if not defined ESPAGENT_BACKGROUND if not defined ESPAGENT_CONSOLE if not defined ESPAGENT_SETUP_ONLY (
  wscript.exe //nologo "%~dp0start_demo_host.vbs"
  exit /b 0
)

cd /d "%~dp0"
set "VENV_PYTHON=%~dp0apps\demo_host\.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
  echo [ESPAgent] First run: creating the Demo Host environment...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv "%~dp0apps\demo_host\.venv"
  ) else (
    where python >nul 2>nul
    if errorlevel 1 (
      echo [ESPAgent] Python 3.11 or newer was not found.
      echo Install Python, then run this file again.
      if not defined ESPAGENT_NO_PAUSE pause
      exit /b 1
    )
    python -m venv "%~dp0apps\demo_host\.venv"
  )
  if errorlevel 1 goto :setup_failed
)

"%VENV_PYTHON%" -c "import fastapi, uvicorn, PIL, pydantic, httpx, dotenv, cv2, websocket, msgpack, sounddevice, webview, esptool, serial, dxcam" >nul 2>nul
if errorlevel 1 (
  echo [ESPAgent] Installing or updating Demo Host dependencies...
  "%VENV_PYTHON%" -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 goto :setup_failed
)

if defined ESPAGENT_SETUP_ONLY exit /b 0

echo [ESPAgent] Starting Demo Host...
echo [ESPAgent] Desktop UI:   http://127.0.0.1:8787/app/
echo [ESPAgent] Diagnostics:  http://127.0.0.1:8787/debug/
echo [ESPAgent] Display UI:   http://127.0.0.1:8787/screen/
echo [ESPAgent] Press Ctrl+C to stop.
echo.
"%VENV_PYTHON%" "%~dp0start_demo_host.py" %*
if errorlevel 1 (
  echo.
  echo [ESPAgent] Demo Host exited with an error.
  if not defined ESPAGENT_NO_PAUSE pause
)
exit /b %errorlevel%

:setup_failed
echo.
echo [ESPAgent] Failed to create the environment or install dependencies.
echo Check the network and Python installation, then run this file again.
if not defined ESPAGENT_NO_PAUSE pause
exit /b 1
