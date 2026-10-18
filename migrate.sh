#!/usr/bin/env bash
# Convert existing libraries to the shared library database. Dry run unless --apply is given.
# Usage: ./migrate.sh /path/to/library-root [more roots] [--apply] [--report file.json]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
exec .venv/bin/python scripts/migrate_library_data.py "$@"
