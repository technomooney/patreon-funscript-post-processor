#!/usr/bin/env bash
# Compare a library with the tree snapshot saved by reset.sh; optionally restore old names (see scripts/compare_tree.py).
#   ./compare_tree.sh /path/to/library-root [--creator X] [--only TEXT] [--restore-names [--apply]]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
exec .venv/bin/python scripts/compare_tree.py "$@"
