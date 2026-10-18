# Sourced by the double-clickable wrappers (reset.sh, migrate.sh, ...). Not run directly.
# Usage: source scripts/launch_helper.sh "$0" "$@"   -- must be sourced as the first thing after `set`
# - relaunches in a terminal emulator when started from a file manager
# - with no arguments, asks for the folder (drag-and-drop into the terminal works)
# - keeps the window open at the end so the output can be read
if [ ! -t 0 ]; then
    SELF="$(realpath "${BASH_SOURCE[1]}")"
    for term in konsole gnome-terminal xfce4-terminal lxterminal mate-terminal xterm; do
        if command -v "$term" &>/dev/null; then
            case "$term" in
                gnome-terminal) exec gnome-terminal -- bash "$SELF" "$@" ;;
                *)              exec "$term" -e bash "$SELF" "$@" ;;
            esac
        fi
    done
    echo "No terminal emulator found. Please run this script from a terminal." >&2
    exit 1
fi

# run_tool <python script> [--ask-folder]: runs the tool, then waits for Enter if we were started with no arguments
run_tool() {
    local script="$1" ask="${2:-}"
    if [ ! -d ".venv" ]; then
        echo "Virtual environment not found. Run ./setup.sh first."
        read -rp "Press Enter to close..."
        exit 1
    fi
    local interactive=0
    if [ "${#TOOL_ARGS[@]}" -eq 0 ]; then
        interactive=1
        if [ "$ask" = "--ask-folder" ]; then
            read -rp "Folder (drag it here, then Enter): " folder
            folder="${folder%\'}"; folder="${folder#\'}"
            [ -z "$folder" ] && exit 1
            TOOL_ARGS=("$folder")
        fi
    fi
    set +e
    .venv/bin/python "$script" "${TOOL_ARGS[@]}"
    local code=$?
    set -e
    if [ "$interactive" -eq 1 ]; then read -rp "Press Enter to close..."; fi
    exit "$code"
}
