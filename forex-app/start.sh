#!/usr/bin/env bash
# Start the FX app on this computer (Mac or Linux). First run: installs what it needs and asks a few questions.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10 or newer is needed: https://www.python.org/downloads/"; exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is needed (you have $("$PY" --version))."; exit 1; }
if [ ! -d .venv ]; then
  echo "First start: setting up (one minute)..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
[ -f .env ] || python -m forexapp.setup
exec python -m forexapp.run all --open
