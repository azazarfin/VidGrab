# 🛠️ Contributing to VidGrab

Thank you for your interest in contributing to VidGrab! This guide outlines the project structure, development workflows, and coding conventions to help you get started.

---

## 📂 Repository Structure

- `backend/`: Fast API server, queue systems, SQLite models, and `yt-dlp` hooks.
  - `main.py`: Lifespan events, router compilation, CORS configurations.
  - `queue_manager.py`: Downloader task concurrency queues and scheduling logic.
  - `downloader.py`: Direct wrapper around `yt_dlp.YoutubeDL` with progress streaming callbacks.
  - `config.py`: Local settings schema, persisted to SQLite.
- `extension/`: Raw source code of the extension.
  - `manifest.json` / `manifest.firefox.json`: Browser manifests.
  - `popup/`: Popup controls, progress tabs, download forms, formats selection.
  - `options/`: Options interface containing accordion card sections.
  - `background/service-worker.js`: Background task routing, badge handlers, browser native notifications.
  - `content/`: Floating download buttons overlays and mutation scanners.
  - `lib/api.js`: Client side wrapper mapping REST endpoints and WebSocket channels.
- `installer/`: Bootstrap installation scripts.
- `dist/`: Output packaged archives created by build commands.

---

## 💻 Developer Setup

1. **Clone the Repository**:
   Ensure you have Python 3.10+ and Node/NPM/zip command utilities available.

2. **Initialize Local Virtual Environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r backend/requirements.txt
   ```

3. **Run the Backend in Auto-Reload Mode**:
   ```bash
   uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
   ```

4. **Serve static assets for testing options/popups**:
   ```bash
   python3 -m http.server 8080 --directory .
   ```

5. **Package and Load the Extension**:
   ```bash
   ./build.sh
   ```
   Open your browser developer page and load unpacked extensions pointing to the unpacked directories inside `dist/`.

---

## 🎨 Coding Guidelines

### Python (Backend)
- Use standard PEP 8 naming conventions.
- Always use `async`/`await` for I/O operations and database queries.
- Database access is managed asynchronously via `aiosqlite`. Ensure connections are properly closed or managed via `async with` context managers.
- Keep critical credentials or netscape cookies strictly in-memory or inside secure temporal files (deleted inside `finally` blocks).

### Javascript (Frontend Extension)
- Keep styling rules strictly inside CSS documents. Do not write inline styles unless elements require dynamic relative positions (e.g. the floating overlay button tracking video coordinates).
- Use Vanilla JS without heavyweight frameworks. Keep the popup footprint minimal.
- Namespace API requests through `lib/api.js` wrapper class instance (`VidGrabAPI`) to avoid code replication.
- Always handle disconnected states: if backend pings fail, trigger `onBackendOffline()` to lock operations.

---

## 🚀 Submitting Changes

1. Fork the repository and create your feature branch: `git checkout -b feature/my-amazing-feature`.
2. Commit your changes with descriptive messages: `git commit -m 'Add support for custom output directories'`.
3. Verify your changes end-to-end: run installer audits, build extension packages, and test operations in both Chrome and Firefox.
4. Open a Pull Request detailing the changes, testing steps, and attaching screenshots of any modified user interfaces.
