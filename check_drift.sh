#!/usr/bin/env bash
# Report differences between a library on disk and its database (read-only unless --accept is used).
# Usage: ./check_drift.sh /path/to/library-root [--csv out.csv] [--accept ID] [--accept-all KIND]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
exec .venv/bin/python scripts/check_library_drift.py "$@"
