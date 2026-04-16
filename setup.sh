#!/usr/bin/env bash

# ---------------------------------------------------------------------------
# setup.sh — first-time setup for the Patreon downloader post-processor
# Creates a virtual environment, installs dependencies, and writes .env
# ---------------------------------------------------------------------------

# If not running inside a terminal (e.g. double-clicked in a file manager),
# relaunch this script inside the first terminal emulator we can find.
if [ ! -t 0 ]; then
    SELF="$(realpath "${BASH_SOURCE[0]}")"
    for term in gnome-terminal konsole xfce4-terminal lxterminal mate-terminal xterm; do
        if command -v "$term" &>/dev/null; then
            case "$term" in
                gnome-terminal) exec gnome-terminal -- bash "$SELF" ;;
                konsole)        exec konsole -e bash "$SELF" ;;
                *)              exec "$term" -e bash "$SELF" ;;
            esac
        fi
    done
    echo "No terminal emulator found. Please run this script from a terminal." >&2
    exit 1
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Virtual environment ----------------------------------------------------

if [ -d ".venv" ]; then
    echo "Virtual environment already exists, skipping creation."
else
    echo "Creating virtual environment..."
    python3 -m venv .venv
    echo "Virtual environment created."
fi

echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "Updating yt-dlp to latest version..."
.venv/bin/pip install --quiet --upgrade yt-dlp
echo "Dependencies installed."

# --- Settings and credentials ------------------------------------------------

.venv/bin/python setup_config.py

echo "========================================"
echo "  Setup complete!"
echo "  Run ./run.sh to start the downloader."
echo "========================================"
echo ""
read -rp "Press Enter to close..."
