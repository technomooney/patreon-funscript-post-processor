#!/usr/bin/env bash
# Report differences between a library on disk and its database (read-only unless --accept is used).
# Usage: ./check_drift.sh /path/to/library-root [--csv out.csv] [--accept ID] [--accept-all KIND]
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=scripts/launch_helper.sh
source scripts/launch_helper.sh "$@"
set -euo pipefail
TOOL_ARGS=("$@")
run_tool scripts/check_library_drift.py --ask-folder
