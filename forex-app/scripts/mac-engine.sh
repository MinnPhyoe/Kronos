#!/usr/bin/env bash
# Started by the FX Desk Mac app: sets up (first run) and runs the engine on this Mac only.
# Lines starting with "FXDESK:" are progress messages the app shows.
set -uo pipefail
cd "$(dirname "$0")/.."

PY=""
for c in "${FXDESK_PYTHON:-}" /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
         /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/3.1[0-9]/bin/python3 \
         "$HOME/.pyenv/shims/python3" /usr/bin/python3; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "FXDESK:ERROR Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and press Start again."
  exit 2
fi

echo "Using $PY ($("$PY" -V 2>&1))"

if [ ! -x .venv/bin/python ]; then
  echo "FXDESK:STAGE First start: creating the engine's Python environment"
  "$PY" -m venv .venv || { echo "FXDESK:ERROR could not create .venv"; exit 3; }
fi
VPY=.venv/bin/python

STAMP=.venv/.fxdesk-requirements
if [ ! -f "$STAMP" ] || ! cmp -s requirements.txt "$STAMP"; then
  echo "FXDESK:STAGE Installing the engine's packages (first start, about a minute)"
  "$VPY" -m pip install --quiet --disable-pip-version-check -r requirements.txt || { echo "FXDESK:ERROR pip install failed"; exit 4; }
  cp requirements.txt "$STAMP"
fi

BACKEND="${FOREX_MODEL_BACKEND:-$(grep -E '^FOREX_MODEL_BACKEND=' .env 2>/dev/null | tail -1 | cut -d= -f2)}"
if [ "${BACKEND:-kronos}" != "bootstrap" ] && ! "$VPY" -c "import torch, einops, huggingface_hub, safetensors" 2>/dev/null; then
  echo "FXDESK:STAGE Installing Kronos (first start only: about 1 GB, a few minutes)"
  "$VPY" -m pip install --quiet --disable-pip-version-check torch einops==0.8.1 huggingface_hub==0.33.1 safetensors==0.6.2 tqdm \
    || { echo "FXDESK:ERROR installing Kronos failed; the app can run with the simple stand-in model (Settings)"; exit 5; }
fi

echo "FXDESK:STAGE Starting the engine"
export FOREX_HOST=127.0.0.1                 # this Mac only
export PYTORCH_ENABLE_MPS_FALLBACK=1        # on Apple silicon, any step the graphics chip can't do runs on the CPU
exec "$VPY" -m forexapp.run all --port "${FOREX_PORT:-8000}"
