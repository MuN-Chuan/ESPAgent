@echo off
setlocal

set "ESPAGENT_APP_ARGS="
:parse_arguments
if "%~1"=="" goto :arguments_ready
if /i "%~1"=="--console" (
  shift
  goto :parse_arguments
)
set ESPAGENT_APP_ARGS=%ESPAGENT_APP_ARGS% "%~1"
shift
goto :parse_arguments

:arguments_ready
cd /d "%~dp0"
echo [ESPAgent] Working directory: %CD%
echo [ESPAgent] Preparing desktop application environment...
set "VENV_PYTHON=%~dp0apps\demo_host\.venv\Scripts\python.exe"
set "DEMO_PROJECT=%~dp0apps\demo_host"
set "VENV_ROOT_MARKER=%~dp0apps\demo_host\.venv\.espagent_root"
set "VENV_CONFIG=%~dp0apps\demo_host\.venv\pyvenv.cfg"
set "REQUIREMENTS_FILE=%~dp0requirements.txt"
set "RECREATE_VENV=0"

if not exist "%DEMO_PROJECT%\pyproject.toml" goto :incomplete_install
if not exist "%REQUIREMENTS_FILE%" goto :incomplete_install

if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" -c "import sys" >nul 2>nul
  if errorlevel 1 set "RECREATE_VENV=1"
)

if exist "%VENV_ROOT_MARKER%" goto :read_venv_root_marker
if not exist "%VENV_CONFIG%" goto :venv_root_checked
"%VENV_PYTHON%" -c "import os,pathlib; expected=str(pathlib.Path(os.environ['DEMO_PROJECT'])/'.venv').casefold(); text=pathlib.Path(os.environ['VENV_CONFIG']).read_text(encoding='utf-8').casefold(); raise SystemExit(0 if expected in text else 1)" >nul 2>nul
if errorlevel 1 set "RECREATE_VENV=1"
goto :venv_root_checked

:read_venv_root_marker
set /p INSTALLED_VENV_ROOT=<"%VENV_ROOT_MARKER%"
if /i not "%INSTALLED_VENV_ROOT%"=="%DEMO_PROJECT%" set "RECREATE_VENV=1"

:venv_root_checked

if "%RECREATE_VENV%"=="1" (
  echo [ESPAgent] The application environment was copied from another directory.
  echo [ESPAgent] Rebuilding it for: %DEMO_PROJECT%
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv --clear "%DEMO_PROJECT%\.venv"
  ) else (
    where python >nul 2>nul
    if errorlevel 1 goto :python_missing
    python -m venv --clear "%DEMO_PROJECT%\.venv"
  )
  if errorlevel 1 goto :setup_failed
)

if not exist "%VENV_PYTHON%" (
  echo [ESPAgent] First run: creating the application environment...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv "%DEMO_PROJECT%\.venv"
  ) else (
    where python >nul 2>nul
    if errorlevel 1 goto :python_missing
    python -m venv "%DEMO_PROJECT%\.venv"
  )
  if errorlevel 1 goto :setup_failed
)

"%VENV_PYTHON%" -c "import fastapi, uvicorn, PIL, pydantic, httpx, dotenv, cv2, websocket, msgpack, sounddevice, webview, esptool, serial, dxcam" >nul 2>nul
if errorlevel 1 goto :install_dependencies

goto :environment_ready

:install_dependencies
  echo [ESPAgent] Installing or updating desktop application dependencies...
  "%VENV_PYTHON%" -m pip install -r "%REQUIREMENTS_FILE%"
  if errorlevel 1 goto :setup_failed

:environment_ready
>"%VENV_ROOT_MARKER%" echo %DEMO_PROJECT%

echo [ESPAgent] Python runtime: %VENV_PYTHON%
echo [ESPAgent] Starting desktop application...
"%VENV_PYTHON%" "%~dp0start_espagent_app.py" %ESPAGENT_APP_ARGS%
set "APP_EXIT_CODE=%errorlevel%"
echo.
echo [ESPAgent] Desktop application exited with code %APP_EXIT_CODE%.
echo [ESPAgent] Review the messages above before closing this window.
pause
exit /b %APP_EXIT_CODE%

:incomplete_install
echo [ESPAgent] Installation is incomplete.
echo [ESPAgent] Missing: %DEMO_PROJECT%\pyproject.toml
echo [ESPAgent] Copy the complete ESPAgent project directory, not only the EXE and launcher scripts.
pause
exit /b 3

:python_missing
echo [ESPAgent] Python 3.11 or newer was not found.
echo [ESPAgent] Install Python and enable the "Add Python to PATH" option.
pause
exit /b 4

:setup_failed
set "APP_EXIT_CODE=%errorlevel%"
echo [ESPAgent] Failed to prepare the desktop application environment.
echo [ESPAgent] Setup exited with code %APP_EXIT_CODE%.
pause
exit /b %APP_EXIT_CODE%
