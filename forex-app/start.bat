@echo off
rem Start the FX app on this computer (Windows). First run: installs what it needs and asks four questions.
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo Python 3.10 or newer is needed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Python 3.10 or newer is needed.
  pause
  exit /b 1
)
if not exist .venv (
  echo First start: setting up, one minute...
  %PY% -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if not exist .env python -m forexapp.setup
python -m forexapp.run all --open
pause
