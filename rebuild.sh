#!/usr/bin/env bash
# Rebuild a library's pixeldrain-sourced files from their sources. Two steps, deliberately outside the menu:
#   ./rebuild.sh /path/to/library-root                                   plan + HTML report + confirmation token
#   ./rebuild.sh /path/to/library-root --confirm TOKEN [--decisions f]   apply (resumable)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
exec .venv/bin/python scripts/rebuild_library.py "$@"
