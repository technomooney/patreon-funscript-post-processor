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

# --- .env configuration -----------------------------------------------------

echo ""
echo "========================================"
echo "  Configure settings (press Enter to keep the default)"
echo "========================================"
echo ""

# PIXELDRAIN_API_KEY
if [ -f ".env" ]; then
    existing_key=$(grep -oP '(?<=PIXELDRAIN_API_KEY=).*' .env 2>/dev/null || echo "")
else
    existing_key=""
fi
if [ -n "$existing_key" ]; then
    read -rp "Pixeldrain API key [current: ${existing_key}]: " PIXELDRAIN_API_KEY
    PIXELDRAIN_API_KEY="${PIXELDRAIN_API_KEY:-$existing_key}"
else
    read -rp "Pixeldrain API key (leave blank to download anonymously): " PIXELDRAIN_API_KEY
fi

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

# IWARA_EMAIL
if [ -f ".env" ]; then
    existing_iwara_email=$(grep -oP '(?<=IWARA_EMAIL=).*' .env 2>/dev/null || echo "")
else
    existing_iwara_email=""
fi
if [ -n "$existing_iwara_email" ]; then
    read -rp "iwara.tv email [current: ${existing_iwara_email}]: " IWARA_EMAIL
    IWARA_EMAIL="${IWARA_EMAIL:-$existing_iwara_email}"
else
    read -rp "iwara.tv email (leave blank to skip iwara.tv downloads): " IWARA_EMAIL
fi

# IWARA_PASSWORD
if [ -f ".env" ]; then
    existing_iwara_pass=$(grep -oP '(?<=IWARA_PASSWORD=).*' .env 2>/dev/null || echo "")
else
    existing_iwara_pass=""
fi
if [ -n "$existing_iwara_pass" ]; then
    read -rsp "iwara.tv password [press Enter to keep current]: " IWARA_PASSWORD
    echo ""
    IWARA_PASSWORD="${IWARA_PASSWORD:-$existing_iwara_pass}"
else
    read -rsp "iwara.tv password (leave blank to skip iwara.tv downloads): " IWARA_PASSWORD
    echo ""
fi

# IWARA_SECRET
if [ -f ".env" ]; then
    existing_iwara_secret=$(grep -oP '(?<=IWARA_SECRET=).*' .env 2>/dev/null || echo "5nFp9kmbNnHdAFhaqMvt")
else
    existing_iwara_secret="5nFp9kmbNnHdAFhaqMvt"
fi
echo ""
echo "  iwara.tv signing secret — only change this if downloads start"
echo "  failing with 403 errors. To find the new value, open iwara.tv"
echo "  in your browser, go to DevTools > Sources, press Ctrl+Shift+F,"
echo "  and search for the old secret or 'X-Version' to find the new one."
echo ""
read -rp "iwara.tv signing secret [${existing_iwara_secret}]: " IWARA_SECRET
IWARA_SECRET="${IWARA_SECRET:-$existing_iwara_secret}"

# Write .env
cat > .env <<EOF
# Pixeldrain API key — found at https://pixeldrain.com/user/api
# Leave blank to download as anonymous (public files only).
PIXELDRAIN_API_KEY=${PIXELDRAIN_API_KEY}

# Run the browser in headless mode (no visible window).
# Set to false if sites start blocking the automation.
BROWSER_HEADLESS=${BROWSER_HEADLESS}

# Maximum resolution to download (e.g. 1080, 720, 2160).
# Downloads the highest quality available up to this value.
MAX_RESOLUTION=${MAX_RESOLUTION}

# iwara.tv account credentials — required for 18+ content.
# Leave both blank to skip iwara.tv downloads.
IWARA_EMAIL=${IWARA_EMAIL}
IWARA_PASSWORD=${IWARA_PASSWORD}

# iwara.tv CDN signing secret — embedded in the iwara.tv frontend JS.
# If downloads return 403, find the new value in DevTools > Sources,
# search all files (Ctrl+Shift+F) for the old secret or 'X-Version'.
IWARA_SECRET=${IWARA_SECRET}
EOF

echo ""
echo ".env written."
echo ""
echo "========================================"
echo "  Setup complete!"
echo "  Run ./run.sh to start the downloader."
echo "========================================"
echo ""
read -rp "Press Enter to close..."
