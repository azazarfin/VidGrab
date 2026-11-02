# 🎥 VidGrab — Cross-Browser Video Downloader & Queue Manager

VidGrab is a premium, local-first video downloader extension for Google Chrome and Mozilla Firefox. It is powered by a high-performance Python backend wrapping `yt-dlp` and `ffmpeg`, providing a unified queue manager, download history, automated SponsorBlock segment cutting, subtitles/thumbnail integration, custom command-line arguments, and schedule-for-later planning.

## Desktop companion roadmap

A standalone download-manager app is planned for **Linux and Windows first**, with macOS following. See [the implementation plan](docs/PLAN.md), [target architecture](docs/ARCHITECTURE.md), and [HANDOFF.md](HANDOFF.md) to continue development across agents or devices. The desktop app is not implemented yet; the setup below describes the existing extension/backend.

---

## 🏗️ Architecture Overview

VidGrab uses a hybrid architecture: a lightweight, responsive browser extension UI communicates via REST and WebSockets with a locally hosted background Python service.

```mermaid
graph TD
    Browser[Browser Extension: Chrome / Firefox]
    Popup[Popup UI: Manager & Queue]
    Background[Service Worker / Background Script]
    Content[Content Scripts: Detector & Overlay]
    Backend[Local Python Backend: FastAPI]
    DB[(SQLite Database)]
    ytdlp[yt-dlp Engine]
    ffmpeg[FFmpeg System Utility]
    
    Content -->|Video detection events| Background
    Popup -->|REST Requests & WebSocket Progress| Backend
    Background -->|REST & WebSocket connection| Backend
    Background -->|Cookie injection jar| Backend
    Backend -->|Settings, queue & history| DB
    Backend -->|Download operations| ytdlp
    ytdlp -->|Transcoding, subtitles & thumbnails| ffmpeg
```

---

## ✨ Features

- **Hybrid Video Detection**: Content scripts scan page DOMs dynamically and display a smooth, floating download button above active video elements.
- **Unified Download Manager**: Track active speeds, ETA, and progress bar updates in real-time via WebSocket streaming.
- **Power-User post-processing**: Change format containers (MP4/MKV/WebM), extract audios (MP3/FLAC/Opus), and normalize volume levels automatically.
- **SponsorBlock Integration**: Cut out sponsors, intros, outros, or self-promotions from YouTube videos automatically during download.
- **Cookie Jar Forwarding**: Securely forwards active browser cookies to download age-gated, private, or member-only videos.
- **Task Scheduler**: Schedule downloads to start automatically during off-peak hours (e.g. overnight).
- **Pinned Engine**: Uses a locked `yt-dlp` release; startup and status checks stay offline. Install a newer VidGrab release to update the engine.
- **Custom yt-dlp Flags**: Input custom CLI flags with warning verification and sanitization blocks to filter dangerous inputs.

---

## 🚀 Installation & Setup

Backend development requires Python 3.11+. For locked setup, isolated tests, runtime paths and explicit legacy-state import, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). The standalone desktop app is still planned; the commands below are the legacy extension/backend workflow. Keep the current unauthenticated backend bound to loopback only.

### 1. Run the Backend Installer

VidGrab provides automated install scripts to set up Python virtual environments, fetch dependencies, and register background processes.

#### 🐧 Linux & 🍎 macOS
Open your terminal and run:
```bash
bash installer/install.sh
```
*On Linux, this automatically configures and starts a user-level background systemd service (`vidgrab-backend.service`).*

#### 🪟 Windows
Open PowerShell as Administrator and run:
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
.\installer\install.ps1
```
*This installs local dependencies and registers a Windows Task Scheduler task to launch the backend silently on startup.*

---

### 2. Load the Extension in Your Browser

Load `extension/` directly as the unpacked Chrome source. To produce browser archives:
```bash
bash build.sh
```
This replaces all of `dist/` with browser ZIPs; it leaves no unpacked browser folders. Do not run it when preserving locally modified archives.

#### Google Chrome / Chromium-based:
1. Open Chrome and go to `chrome://extensions/`.
2. Toggle **Developer mode** in the top right.
3. Click **Load unpacked** and select `extension/`.

#### Mozilla Firefox:
1. Open Firefox and enter `about:debugging` in the address bar.
2. Click **This Firefox** in the sidebar.
3. Click **Load Temporary Add-on...** and select `dist/vidgrab-firefox-extension.zip`.

---

## 🛠️ Diagnostics & Commands

If running the backend manually, execute uvicorn directly from the virtual environment:
```bash
cd VideoDownloadedExtension
./venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### Systemd Service Logs (Linux):
```bash
# View live logs
journalctl --user -u vidgrab-backend.service -f

# Manage background service state
systemctl --user stop vidgrab-backend.service
systemctl --user start vidgrab-backend.service
systemctl --user restart vidgrab-backend.service
```

---

## 📝 License
VidGrab is open-source and released under the MIT License.
