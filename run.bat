@echo off
rem ======================================================================
rem  sitewatch launcher for Windows
rem
rem    run.bat                  interactive menu (just double-click this file)
rem    run.bat doctor           any sitewatch command, for example:
rem    run.bat run --dry-run
rem    run.bat auto             unattended daily run for Task Scheduler:
rem                             no prompts, log goes to logs\sitewatch.log
rem
rem  On the first start it creates .venv, installs dependencies and creates
rem  .env. The program itself speaks Russian; this launcher is ASCII-only so
rem  it does not depend on the console code page.
rem ======================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title sitewatch
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "AUTO="
if /i "%~1"=="auto" set "AUTO=1"

echo.
echo ===== sitewatch: preparing environment =====

rem --- 1. Python 3.10 or newer ---
set "PY=py -3"
%PY% -c "import sys; sys.exit(sys.version_info[:2] < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python_found
set "PY=python"
%PY% -c "import sys; sys.exit(sys.version_info[:2] < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python_found
set "PY=python3"
%PY% -c "import sys; sys.exit(sys.version_info[:2] < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python_found

echo [error] Python 3.10 or newer was not found.
echo         Install it from https://www.python.org/downloads/windows/
echo         and tick "Add python.exe to PATH" in the installer, then run this file again.
goto fail

:python_found
echo [ok] Python found, command: %PY%
%PY% --version

rem --- 2. Virtual environment ---
if exist ".venv\Scripts\python.exe" goto venv_ready
echo [setup] Creating virtual environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 goto venv_failed
:venv_ready
set "VPY=.venv\Scripts\python.exe"
echo [ok] Virtual environment: .venv

rem --- 3. Dependencies (reinstalled automatically when pyproject.toml changes) ---
fc /b pyproject.toml ".venv\pyproject.installed" >nul 2>&1
if not errorlevel 1 goto deps_ready
echo [setup] Installing dependencies, the first time takes about a minute ...
"%VPY%" -m pip install --disable-pip-version-check -e .
if errorlevel 1 goto pip_failed
copy /y pyproject.toml ".venv\pyproject.installed" >nul
:deps_ready
echo [ok] Dependencies are installed

rem --- 4. Settings file ---
if exist ".env" goto env_ready
if not exist ".env.example" goto env_ready
copy ".env.example" ".env" >nul
echo [setup] Created .env from .env.example
if defined AUTO goto env_missing_auto
echo         Notepad opens now: fill in DEEPSEEK_API_KEY and the SMTP settings,
echo         then save the file and close Notepad to continue.
pause
notepad ".env"
:env_ready
echo [ok] Settings file: .env

rem --- 5. Start ---
echo [ok] Ready. Log file: logs\sitewatch.log
echo.
if defined AUTO goto auto_run
if "%~1"=="" goto menu
"%VPY%" -m sitewatch %*
exit /b %errorlevel%

:menu
"%VPY%" -m sitewatch menu
set "RC=%errorlevel%"
if "%RC%"=="0" exit /b 0
echo.
echo [error] The menu stopped with exit code %RC%. See the messages above.
pause
exit /b %RC%

:auto_run
"%VPY%" -m sitewatch run
set "RC=%errorlevel%"
echo [auto] sitewatch finished with exit code %RC%
exit /b %RC%

:venv_failed
echo.
echo [error] Could not create the virtual environment in .venv
echo         Delete the .venv folder if it exists and run this file again.
goto fail

:pip_failed
echo.
echo [error] Could not install dependencies, see the pip output above.
echo         Check the internet connection (PyPI must be reachable) and run this file again.
goto fail

:env_missing_auto
echo [error] .env did not exist and was just created from .env.example.
echo         Fill it in and run again.
goto fail

:fail
echo.
if defined AUTO exit /b 1
pause
exit /b 1
