#!/bin/bash
# ==============================================================================
# VidGrab Backend Installer (Linux / macOS)
# ==============================================================================
set -e

# Visual formatting
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${BLUE}====================================================${NC}"
echo -e "${CYAN}          VidGrab Backend Service Installer          ${NC}"
echo -e "${BLUE}====================================================${NC}"

# 1. Resolve paths
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( dirname "$SCRIPT_DIR" )"
VENV_DIR="$PROJECT_ROOT/venv"

echo -e "Project Root: ${CYAN}$PROJECT_ROOT${NC}"

# 2. Check dependencies
echo -e "\n${BLUE}[1/4] Auditing system dependencies...${NC}"

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed or not in PATH.${NC}"
    echo "Please install Python 3.11+ and try again."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo -e "Found Python: ${GREEN}v$PYTHON_VERSION${NC}"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo -e "${RED}VidGrab requires Python 3.11 or newer.${NC}"
    exit 1
fi

# Check FFmpeg (Warning only)
if ! command -v ffmpeg &> /dev/null; then
    echo -e "${YELLOW}Warning: FFmpeg was not found on your system.${NC}"
    echo -e "${YELLOW}         VidGrab will still work, but advanced features like${NC}"
    echo -e "${YELLOW}         audio extraction, video recoding, and subtitle embedding${NC}"
    echo -e "${YELLOW}         will be disabled.${NC}"
    echo -e "${YELLOW}         Recommendation: Install ffmpeg via your package manager.${NC}"
else
    FFMPEG_PATH=$(command -v ffmpeg)
    echo -e "Found FFmpeg: ${GREEN}$FFMPEG_PATH${NC}"
fi

# 3. Create virtual environment
echo -e "\n${BLUE}[2/4] Setting up Python virtual environment...${NC}"
if [ -d "$VENV_DIR" ]; then
    echo -e "Virtual environment already exists at ${CYAN}$VENV_DIR${NC}"
else
    python3 -m venv "$VENV_DIR"
    echo -e "Created virtual environment at ${GREEN}$VENV_DIR${NC}"
fi

# 4. Install dependencies
echo -e "\n${BLUE}[3/4] Installing Python libraries...${NC}"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install --require-hashes -r "$PROJECT_ROOT/backend/requirements.txt"
echo -e "Dependencies installed ${GREEN}successfully${NC}."

# 5. Configure systemd user service (Linux only)
echo -e "\n${BLUE}[4/4] Configuring background service...${NC}"
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Verify systemctl is available
    if command -v systemctl &> /dev/null; then
        USER_SERVICE_DIR="$HOME/.config/systemd/user"
        mkdir -p "$USER_SERVICE_DIR"
        
        SERVICE_FILE="$USER_SERVICE_DIR/vidgrab-backend.service"
        
        echo -e "Creating systemd user unit at ${CYAN}$SERVICE_FILE${NC}"
        cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=VidGrab Backend Service
After=network.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
WorkingDirectory=$PROJECT_ROOT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
        
        echo -e "Reloading systemd daemon..."
        systemctl --user daemon-reload
        
        echo -e "Enabling and starting service..."
        systemctl --user enable vidgrab-backend.service
        systemctl --user restart vidgrab-backend.service
        
        echo -e "\n${GREEN}Service configured and started successfully!${NC}"
        echo -e "Backend running background instance at: ${CYAN}http://127.0.0.1:8000${NC}"
    else
        echo -e "${YELLOW}systemctl not found. Please start the backend manually:${NC}"
        echo -e "  ${CYAN}cd $PROJECT_ROOT && ./venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000${NC}"
    fi
elif [[ "$OSTYPE" == "darwin"* ]]; then
    echo -e "${GREEN}Dependencies configured successfully for macOS.${NC}"
    echo -e "To start the backend daemon in the background, you can use launchd or run uvicorn in a screen/tmux session:"
    echo -e "  ${CYAN}cd $PROJECT_ROOT && ./venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000${NC}"
else
    echo -e "${YELLOW}Unsupported OS type: $OSTYPE.${NC}"
    echo -e "To run backend manually:"
    echo -e "  ${CYAN}cd $PROJECT_ROOT && ./venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000${NC}"
fi

echo -e "\n${GREEN}====================================================${NC}"
echo -e "${GREEN}            VidGrab Installation Complete            ${NC}"
echo -e "${GREEN}====================================================${NC}"
if [[ "$OSTYPE" == "linux-gnu"* ]] && command -v systemctl &> /dev/null; then
    echo -e "Check logs:    ${CYAN}journalctl --user -u vidgrab-backend.service -f${NC}"
    echo -e "Stop service:  ${CYAN}systemctl --user stop vidgrab-backend.service${NC}"
    echo -e "Start service: ${CYAN}systemctl --user start vidgrab-backend.service${NC}"
fi
echo -e "===================================================="
