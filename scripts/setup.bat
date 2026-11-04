@echo off
REM ===========================================================================
REM  Aether setup (Windows 10/11) - launches the interactive setup wizard.
REM
REM  The wizard replaced the old typed prompts: arrow keys move the highlight,
REM  Space toggles multi-select rows, and Enter confirms. Secrets are masked.
REM  Pass --answers FILE or --yes for a non-interactive install.
REM
REM  This script only bootstraps Python and hands the arguments over; all of
REM  the questions, the branding and the generated artifacts live in
REM  src\aether\setup.
REM
REM  Usage: setup.bat [--answers FILE] [--yes] [--no-animation] [--help]
REM ===========================================================================
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  echo [stop] Python 3 is required for the setup wizard.
  echo        Install it from https://www.python.org/downloads/
  exit /b 1
)

set "PYTHONPATH=%REPO_ROOT%\src;%PYTHONPATH%"

REM Check Python, rich and prompt_toolkit, and install what is missing. A
REM system Python that refuses pip (PEP 668) gets a private environment.
%PY% -m aether.setup.requirements --mode setup
if errorlevel 1 (
  %PY% -m aether.setup.requirements --bootstrap "%USERPROFILE%\.aether-setup\venv"
  if errorlevel 1 exit /b 1
  set "PY=%USERPROFILE%\.aether-setup\venv\Scripts\python.exe"
)

%PY% -m aether.setup %*
exit /b %errorlevel%
