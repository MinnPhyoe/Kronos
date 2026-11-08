"""First-time setup: four questions, then a private .env file next to the app.

    python -m forexapp.setup

1. Where prices come from (Yahoo needs nothing; OANDA needs a free practice account).
2. Which pairs.
3. A password (or none: then only this computer can open the app).
4. Whether to install the Kronos AI model (about 1 GB; runs on a normal CPU, faster with a GPU).

The .env file stays on this computer. It is in .gitignore, so it is never uploaded.
"""
from __future__ import annotations

import getpass
import platform
import subprocess
import sys

from .config import APP_DIR

ENV = APP_DIR / ".env"
DEFAULT_PAIRS = "EURUSD,GBPUSD,USDJPY,XAUUSD"


def ask(question: str, default: str = "") -> str:
    hint = " [%s]" % default if default else ""
    try:
        v = input("%s%s: " % (question, hint)).strip()
    except EOFError:
        v = ""
    return v or default


def choose(question: str, options: list, default: int = 1) -> int:
    print(question)
    for i, (label, note) in enumerate(options, 1):
        print("  %d) %s%s" % (i, label, ("  - " + note) if note else ""))
    while True:
        v = ask("Choose 1-%d" % len(options), str(default))
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v)
        print("  Please type a number from 1 to %d." % len(options))


def yes(question: str, default: bool = True) -> bool:
    v = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not v else v.startswith("y")


def install_kronos() -> bool:
    """Install torch (CPU build unless a GPU build is wanted) and Kronos' small extras."""
    print("\nInstalling the Kronos model's software (about 1 GB, a few minutes)...")
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if platform.system() in ("Windows", "Linux"):
        torch = pip + ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"]
    else:                                   # macOS: the normal wheel includes Apple-chip acceleration
        torch = pip + ["torch"]
    extras = pip + ["einops==0.8.1", "huggingface_hub==0.33.1", "safetensors==0.6.2", "tqdm"]
    for cmd in (torch, extras):
        if subprocess.call(cmd) != 0:
            print("Installing failed. The app will start with the simple stand-in model; run setup again to retry.")
            return False
    return True


def main(argv=None):
    print("\nFX app setup. Answers are saved in %s (on this computer only).\n" % ENV)
    if ENV.exists() and not yes("Settings already exist. Replace them?", default=False):
        print("Kept your settings.")
        return
    lines = ["# Written by `python -m forexapp.setup`. Private: never share or upload this file."]

    src = choose("Where should prices come from?", [
        ("Yahoo Finance", "free, nothing to sign up for; can lag a few seconds; gold is futures"),
        ("OANDA practice account", "free account, real broker bid/ask prices streamed live"),
        ("EODHD", "paid API key"),
        ("TraderMade", "API key"),
    ])
    if src == 1:
        lines.append("FOREX_PROVIDER=yahoo")
    elif src == 2:
        print("\nOANDA: sign up for a free practice (demo) account, then in the account area choose")
        print("'Manage API Access' to create a token. Your account ID looks like 101-001-1234567-001.")
        lines += ["FOREX_PROVIDER=oanda", "OANDA_ENV=practice",
                  "OANDA_TOKEN=" + getpass.getpass("OANDA token (hidden): ").strip(),
                  "OANDA_ACCOUNT=" + ask("OANDA account ID")]
    elif src == 3:
        lines += ["FOREX_PROVIDER=eodhd", "EODHD_API_KEY=" + getpass.getpass("EODHD API key (hidden): ").strip()]
    else:
        lines += ["FOREX_PROVIDER=tradermade", "TRADERMADE_API_KEY=" + getpass.getpass("TraderMade API key (hidden): ").strip()]

    pairs = ask("\nPairs, separated by commas", DEFAULT_PAIRS).upper().replace(" ", "")
    lines.append("FOREX_PAIRS=" + pairs)

    print("\nA password lets you open the app from your phone (same Wi-Fi, or anywhere with Tailscale).")
    print("With no password, only this computer can open it.")
    while True:
        pw = getpass.getpass("Password (leave empty for this-computer-only): ").strip()
        if not pw:
            break
        if len(pw) < 8:
            print("  Use at least 8 characters.")
            continue
        if getpass.getpass("Type it again: ").strip() == pw:
            lines.append("FOREX_PASSWORD=" + pw)
            break
        print("  The two passwords were different. Try again.")

    try:
        import torch  # noqa: F401
        have_torch = True
    except Exception:
        have_torch = False
    print()
    if have_torch or yes("Install the Kronos AI forecast model? (about 1 GB download; without it a simple "
                         "statistical stand-in is used and the app says so)"):
        ok = have_torch or install_kronos()
        lines.append("FOREX_MODEL_BACKEND=" + ("kronos" if ok else "bootstrap"))
        if ok:
            lines.append("KRONOS_MODEL=NeoQuasar/Kronos-small")
    else:
        lines.append("FOREX_MODEL_BACKEND=bootstrap")

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass
    print("\nSaved. Start the app with start.bat (Windows) or ./start.sh (Mac, Linux).")
    print("The first start downloads about 30 days of history, which takes a minute or two.\n")


if __name__ == "__main__":
    main()
