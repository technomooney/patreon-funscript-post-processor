#!/usr/bin/env bash
# Reset a library for a fresh download, keeping a tree snapshot and leaving .manual folders alone. Two steps, outside the menu:
#   ./reset.sh /path/to/library-root                    plan + HTML report + confirmation token (changes nothing)
#   ./reset.sh /path/to/library-root --confirm TOKEN    apply (resumable)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=scripts/launch_helper.sh
source scripts/launch_helper.sh "$@"
set -euo pipefail
TOOL_ARGS=("$@")
run_tool scripts/reset_library.py
