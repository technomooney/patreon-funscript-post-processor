#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# setup.sh — first-time setup for the Patreon downloader post-processor
# Creates a virtual environment, installs dependencies, and writes .env
# ---------------------------------------------------------------------------

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
# Normalise to lowercase true/false
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
EOF

echo ""
echo ".env written."
echo ""
echo "========================================"
echo "  Setup complete!"
echo "  Run ./run.sh to start the downloader."
echo "========================================"
