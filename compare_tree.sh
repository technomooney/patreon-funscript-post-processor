#!/usr/bin/env bash
# Compare a library with the tree snapshot saved by reset.sh; optionally restore old names (see scripts/compare_tree.py).
#   ./compare_tree.sh /path/to/library-root [--creator X] [--only TEXT] [--restore-names [--apply]]
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=scripts/launch_helper.sh
source scripts/launch_helper.sh "$@"
set -euo pipefail
TOOL_ARGS=("$@")
run_tool scripts/compare_tree.py --ask-folder
