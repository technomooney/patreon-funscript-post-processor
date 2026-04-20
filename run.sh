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
echo "  1) Fix file prefixes — strip the attachment ID prefix from"
echo "     downloaded filenames (run this first)"
echo ""
echo "  2) Download content       — find links in description.json files"
echo "     and download the associated videos and files"
echo ""
echo "  3) Check funscript match  — find videos missing a funscript and"
echo "     report fuzzy-match suggestions"
echo ""
echo "  4) Generate HTML          — build a description.html visual overview"
echo "     in each post folder"
echo ""
echo "  5) Sync new folders       — copy folders that are new in the Patreon"
echo "     downloader output into the post-processor working directory"
echo ""
echo "  6) Fix garbled names      — four-pass cleanup pipeline:"
echo "     • detect video files with wrong/missing extension (magic bytes)"
echo "     • detect funscripts with wrong/missing .funscript extension"
echo "     • decode percent-encoded or mojibake filenames"
echo "     • fuzzy-match funscript names to their video and rename to match"
echo "     All changes written to CSV reports in _reports/"
echo ""
echo "  7) Dedupe only            — clean leftover temp files and remove"
echo "     exact duplicate files without running a full download"
echo ""

while true; do
    read -rp "Choose a program to run (1-7): " choice
    case "$choice" in
        1)
            echo ""
            .venv/bin/python prefixFix.py
            break
            ;;
        2)
            echo ""
            .venv/bin/python downloadContent.py
            break
            ;;
        3)
            echo ""
            .venv/bin/python check_funscripts.py
            break
            ;;
        4)
            echo ""
            .venv/bin/python generate_html.py
            break
            ;;
        5)
            echo ""
            .venv/bin/python sync_new_folders.py
            break
            ;;
        6)
            echo ""
            .venv/bin/python fix_garbled_names.py
            break
            ;;
        7)
            echo ""
            .venv/bin/python dedupe_only.py
            break
            ;;
        *)
            echo "Invalid choice. Please enter 1-7."
            ;;
    esac
done

echo ""
read -rp "Press Enter to close..."
