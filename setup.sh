#!/usr/bin/env bash

# ---------------------------------------------------------------------------
# setup.sh — first-time setup for the Patreon downloader post-processor
# Creates a virtual environment, installs dependencies, and writes .env
# ---------------------------------------------------------------------------

# If not running inside a terminal (e.g. double-clicked in a file manager),
# relaunch this script inside the first terminal emulator we can find.
if [ ! -t 0 ]; then
    SELF="$(realpath "${BASH_SOURCE[0]}")"
    for term in gnome-terminal konsole xfce4-terminal lxterminal mate-terminal xterm; do
        if command -v "$term" &>/dev/null; then
            case "$term" in
                gnome-terminal) exec gnome-terminal -- bash "$SELF" ;;
                konsole)        exec konsole -e bash "$SELF" ;;
                *)              exec "$term" -e bash "$SELF" ;;
            esac
        fi
    done
    echo "No terminal emulator found. Please run this script from a terminal." >&2
    exit 1
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Virtual environment ----------------------------------------------------

if [ -d ".venv" ]; then
    echo "Virtual environment already exists, skipping creation."
else
    echo "Creating virtual environment..."
    python3 -m venv .venv
    echo "Virtual environment created."
fi

echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "Dependencies installed."

# --- .env configuration (non-sensitive settings only) -----------------------

echo ""
echo "========================================"
echo "  Configure settings (press Enter to keep the default)"
echo "========================================"
echo ""

# BROWSER_HEADLESS
if [ -f ".env" ]; then
    existing_headless=$(grep -oP '(?<=BROWSER_HEADLESS=).*' .env 2>/dev/null || echo "false")
else
    existing_headless="false"
fi
read -rp "Run browser headless? (true/false) [${existing_headless}]: " BROWSER_HEADLESS
BROWSER_HEADLESS="${BROWSER_HEADLESS:-$existing_headless}"
BROWSER_HEADLESS=$(echo "$BROWSER_HEADLESS" | tr '[:upper:]' '[:lower:]')
if [[ "$BROWSER_HEADLESS" != "true" && "$BROWSER_HEADLESS" != "false" ]]; then
    echo "Invalid value for BROWSER_HEADLESS, defaulting to false."
    BROWSER_HEADLESS="false"
fi

# MAX_RESOLUTION
if [ -f ".env" ]; then
    existing_res=$(grep -oP '(?<=MAX_RESOLUTION=).*' .env 2>/dev/null || echo "1080")
else
    existing_res="1080"
fi
read -rp "Maximum download resolution (e.g. 2160, 1080, 720, 480) [${existing_res}]: " MAX_RESOLUTION
MAX_RESOLUTION="${MAX_RESOLUTION:-$existing_res}"
if ! [[ "$MAX_RESOLUTION" =~ ^[0-9]+$ ]]; then
    echo "Invalid value for MAX_RESOLUTION, defaulting to 1080."
    MAX_RESOLUTION="1080"
fi

# Write .env (credentials are stored in the OS keyring, not here)
# IWARA_SECRET is not written here — it is managed automatically by the
# downloader when a 403 is detected and will be appended to .env at that time.
cat > .env <<EOF
# Run the browser in headless mode (no visible window).
# Set to false if sites start blocking the automation.
BROWSER_HEADLESS=${BROWSER_HEADLESS}

# Maximum resolution to download (e.g. 1080, 720, 2160).
# Downloads the highest quality available up to this value.
MAX_RESOLUTION=${MAX_RESOLUTION}
EOF

echo ""
echo ".env written."

# --- Credentials (stored securely in the OS keyring) ------------------------

echo ""
echo "========================================"
echo "  Credential Setup"
echo "========================================"
.venv/bin/python setup_credentials.py

echo "========================================"
echo "  Setup complete!"
echo "  Run ./run.sh to start the downloader."
echo "========================================"
echo ""
read -rp "Press Enter to close..."
