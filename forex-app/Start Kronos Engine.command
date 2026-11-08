#!/usr/bin/env bash
# Double-click on your Mac: starts the Kronos + Day Trader engine for OBSIDIAN (this computer only).
# First run installs what it needs (about 1 GB for the Kronos model's software) and downloads Kronos-small.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is needed: https://www.python.org/downloads/"; read -r -p "Press Return to close."; exit 1; }
[ -d .venv ] || { echo "First start: setting up (a few minutes)..."; "$PY" -m venv .venv; }
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
python -c "import torch, einops, huggingface_hub, safetensors" 2>/dev/null || \
  python -m pip install --quiet --disable-pip-version-check torch einops==0.8.1 huggingface_hub==0.33.1 safetensors==0.6.2 tqdm
exec python -m forexapp.engine_server
