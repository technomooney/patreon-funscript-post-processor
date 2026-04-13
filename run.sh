#!/usr/bin/env bash

# ---------------------------------------------------------------------------
# run.sh — launch the Patreon downloader post-processor
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

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    read -rp "Press Enter to close..."
    exit 1
fi

if [ ! -f ".env" ]; then
    echo ".env file not found. Run ./setup.sh first."
    read -rp "Press Enter to close..."
    exit 1
fi

# --- Program selection ------------------------------------------------------

echo ""
echo "========================================"
echo "  Patreon Downloader Post-Processor"
echo "========================================"
echo ""
echo "  1) Download content  — find links in description.json files"
echo "     and download the associated videos"
echo ""
echo "  2) Fix file prefixes — strip the attachment ID prefix from"
echo "     downloaded filenames"
echo ""

while true; do
    read -rp "Choose a program to run (1 or 2): " choice
    case "$choice" in
        1)
            echo ""
            .venv/bin/python downloadContent.py
            break
            ;;
        2)
            echo ""
            .venv/bin/python prefixFix.py
            break
            ;;
        *)
            echo "Invalid choice. Please enter 1 or 2."
            ;;
    esac
done

echo ""
read -rp "Press Enter to close..."
