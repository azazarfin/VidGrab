# VidGrab agent notes

## Resume and checkpoint

- Read `HANDOFF.md` before continuing the desktop-companion work; it points to the plan, architecture, decisions, validation, and exact next task. P01 runtime interfaces are implemented; desktop, worker-process, native-host and `/v1` interfaces remain planned.
- The user requested stopping after P01 on 2026-11-03. P01 is complete; P02 has not started. Do not begin P02 until the user requests continuation beyond this checkpoint.
- At each meaningful checkpoint, update `HANDOFF.md` with the remaining subtask, decisions and actual checks; commit it with the related work. Follow its export protocol when switching devices. A Git bundle carries committed files, not local edits.

## Mandatory: minimize API calls

- For every task, use the fewest API/tool calls needed to complete the work correctly and safely. Maximize useful work per call; this is mandatory, not a preference.
- Batch independent reads, searches, checks, and edits into a single call whenever supported, using `multi_tool_use.parallel` or `execute` where available. Do not split work into separate calls when it can be combined.
- Reuse gathered context, avoid redundant calls, and never poll background jobs. Make another call only when missing information, a dependent operation, required verification, or a safety check makes it necessary; never skip these requirements just to reduce the count.

## Commands and packaging

Run backend commands from the repository root so `backend.*` imports resolve. Locked development setup and checks require Python 3.11+ and uv:

```sh
uv sync --frozen
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -B -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

- See `docs/DEVELOPMENT.md` for hashed pip installation, environment overrides, selected legacy import and the opt-in mocked browser check. The pinned yt-dlp declares Python >=3.10; VidGrab's project baseline is >=3.11. Actual current execution evidence is Linux/Python 3.14.8, not Windows or Python 3.11.
- FFmpeg must be on `PATH` for merging/post-processing; it is not installed by the requirements or installers.
- Package with `bash build.sh` (requires `zip`, not an npm build). Git records the shell scripts as non-executable, so prefer `bash` over the documented `./...` invocations.
- Edit `extension/`, not `dist/`. Packaging deletes all of `dist/`, replaces the tracked browser ZIPs, and removes its unpacked folders. Despite the docs, no `dist/chrome/` or `dist/firefox/` remains. Load `extension/` unpacked in Chrome; use `dist/vidgrab-firefox-extension.zip` for Firefox temporary loading.
- `bash installer/install.sh` installs and starts a persistent Linux user service; Windows installation registers `VidGrabBackend`. If the Linux service owns port 8000, stop it with `systemctl --user stop vidgrab-backend.service` before running the development server. Logs: `journalctl --user -u vidgrab-backend.service -f`.

## Wiring and state

- Extension code uses classic scripts and the global `vidGrabAPI` from `extension/lib/api.js`, not ES modules. Both manifests are MV3, but Chrome uses `background.service_worker` and Firefox uses `background.scripts`; preserve the browser-specific wiring.
- Detection travels through `video_detected` messages into `detected_videos_<tabId>` storage. The current overlay hands off `pendingVideoUrl` to the popup; it does not directly enqueue a download.
- `backend/app.py:create_app` owns per-app settings, queue/scheduler lifespan, probe cache, cookies and WebSocket connections. `backend/main.py` is the import-safe ASGI adapter. Factory/import performs no database initialization, directory creation, logging configuration or worker startup. Pass `RuntimeConfig(...)` and `start_queue=False` for isolated diagnostics; no `DB_PATH` globals remain.
- Downloads still run via `asyncio.to_thread`; progress callbacks cross to the queue's event loop with `asyncio.run_coroutine_threadsafe`. Use one backend process per runtime. Active-thread shutdown, cancellation, retry/backoff, sanitizer/output bookkeeping and single-instance enforcement are not fixed by P01; see P02/P03/P05.
- Data defaults to Linux XDG user data or Windows `%LOCALAPPDATA%\VidGrab`. `VIDGRAB_DATA_DIR`, `VIDGRAB_LOG_DIR`, `VIDGRAB_TEMP_DIR` and `VIDGRAB_DOWNLOAD_DIR` override paths before adapter import. Startup migrates only that runtime and can resume its queued/interrupted jobs. Never start a selected real runtime casually.
- `backend/vidgrab.db` is preserved, ignored legacy user state, not a disposable fixture or the runtime default. P01 removed only its Git index entry; the local file was hash-verified unchanged, and older committed versions remain in history. Never delete it as cleanup. Import is explicit through `python -m backend.db`; schema versions 0/1 upgrade transactionally to 2 with snapshots, rollback and newer-schema rejection. Import never starts the app or overwrites destination state.
- `RuntimeConfig.validate_output` resolves literal directory/template paths, including existing symlinks, and excludes source/install and application storage. Settings initialization/PATCH, enqueue and execution share it. Nested and absolute templates outside protected storage retain their existing behavior; this is not race-free dynamic artifact ownership, which remains P03.
- Startup, `/health` and `/status` never query PyPI or invoke pip. `/health` checks readiness/schema locally; legacy `/status` returns `yt_dlp_latest: null` and `update_available: false`. `/update-ytdlp` returns 409. `auto_update_ytdlp` is retained only for compatibility; old private updater functions are not called by the app.
- Settings must align across `DEFAULT_CONFIG`, `ConfigModel`, and `extension/options/options.js`. Defaults populate missing database keys; `PATCH /config` preserves omitted fields. Numeric settings are strict, bounded and non-null; revisions/conflict handling remain P04. The normalization key is `postprocess_normalize_audio`, not `API.md`'s older `postprocess_normalize`.
- Browser-only settings (backend URL, cookie forwarding, notifications) live in `chrome.storage.local`, not SQLite. Forwarded cookies stay outside persisted download options, use temporary Netscape files during downloads, and are removed in the queue's `finally` cleanup; preserve this separation.

## Verification limits

- The default suite is `unittest` under `tests/`, with temporary runtimes, fake boundaries and external-network/process guards. `backend/test_backend.py` now delegates to it rather than initializing the legacy DB or probing public sites. No configured lint, formatter, typecheck or CI exists.
- In the restricted Linux agent sandbox, local socketpair writes can fail with EPERM and stall asyncio/aiosqlite/TestClient. Use the approved sandbox exception for the deterministic suite; keep its own external-request guards enabled. Do not poll a stalled run or start a live backend as a workaround.
- `tests/check_options_ui.py` is opt-in Python Playwright, outside unittest discovery. It mocks `/config` and `/status`, blocks other HTTP requests, and launches isolated headless Chrome; it is not installed-extension, Firefox, Windows or physical-touch evidence. The file-scoped Inter exception is in `.impeccable/config.json`; do not broaden it.
- `test_gpu.py` currently imports missing `backend.gpu_detector`; it is not a working verification command.
- Serve the overlay fixture with `python3 -m http.server 8080 --directory .` and open `http://localhost:8080/test_overlay.html`. Static serving does not provide `chrome.*` APIs; verify extension interactions in installed Chrome and Firefox builds.
