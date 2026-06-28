#!/bin/bash
# ==============================================================================
# VidGrab Extension Build & Packaging Script
# ==============================================================================
set -e

# Visual formatting
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}====================================================${NC}"
echo -e "${CYAN}          VidGrab Extension Builder                  ${NC}"
echo -e "${CYAN}====================================================${NC}"

# Resolve paths
PROJECT_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DIST_DIR="$PROJECT_ROOT/dist"
EXTENSION_SRC="$PROJECT_ROOT/extension"

# Check zip availability
if ! command -v zip &> /dev/null; then
    echo "Error: 'zip' utility is not installed. Please install 'zip' and try again."
    exit 1
fi

echo -e "Cleaning previous builds..."
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR/chrome"
mkdir -p "$DIST_DIR/firefox"

# 1. Build Chrome extension
echo -e "Bundling ${GREEN}Chrome Extension${NC}..."
cp -R "$EXTENSION_SRC"/* "$DIST_DIR/chrome/"
# Chrome uses the default manifest.json
rm -f "$DIST_DIR/chrome/manifest.firefox.json"

# 2. Build Firefox extension
echo -e "Bundling ${GREEN}Firefox Extension${NC}..."
cp -R "$EXTENSION_SRC"/* "$DIST_DIR/firefox/"
# Replace manifest.json with manifest.firefox.json
cp "$EXTENSION_SRC/manifest.firefox.json" "$DIST_DIR/firefox/manifest.json"
rm -f "$DIST_DIR/firefox/manifest.firefox.json"

# 3. Create ZIP packages
echo -e "Packaging ZIP distributions..."

cd "$DIST_DIR/chrome"
zip -r "../vidgrab-chrome-extension.zip" . > /dev/null
echo -e "Created: ${CYAN}dist/vidgrab-chrome-extension.zip${NC}"

cd "$DIST_DIR/firefox"
zip -r "../vidgrab-firefox-extension.zip" . > /dev/null
echo -e "Created: ${CYAN}dist/vidgrab-firefox-extension.zip${NC}"

# Clean up unpacked directories
rm -rf "$DIST_DIR/chrome"
rm -rf "$DIST_DIR/firefox"

echo -e "\n${GREEN}====================================================${NC}"
echo -e "${GREEN}            Build Process Complete                   ${NC}"
echo -e "${GREEN}====================================================${NC}"
