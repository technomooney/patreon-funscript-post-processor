#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# run.sh — launch the Patreon downloader post-processor
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

if [ ! -f ".env" ]; then
    echo ".env file not found. Run ./setup.sh first."
    exit 1
fi

.venv/bin/python downloadContent.py
