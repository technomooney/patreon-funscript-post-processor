#!/usr/bin/env bash
# Reset a library for a fresh download, keeping a tree snapshot and leaving .manual folders alone. Two steps, outside the menu:
#   ./reset.sh /path/to/library-root                    plan + HTML report + confirmation token (changes nothing)
#   ./reset.sh /path/to/library-root --confirm TOKEN    apply (resumable)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
exec .venv/bin/python scripts/reset_library.py "$@"
