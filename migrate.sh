#!/usr/bin/env bash
# Convert existing libraries to the shared library database. Dry run unless --apply is given.
# Usage: ./migrate.sh /path/to/library-root [more roots] [--apply] [--report file.json]
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=scripts/launch_helper.sh
source scripts/launch_helper.sh "$@"
set -euo pipefail
TOOL_ARGS=("$@")
run_tool scripts/migrate_library_data.py --ask-folder
