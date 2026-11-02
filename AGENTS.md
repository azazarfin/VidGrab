# VidGrab agent notes

## Resume and checkpoint

- Read `HANDOFF.md` before continuing the desktop-companion work; it points to the plan, architecture, decisions, validation, and exact next task. Planned APIs/paths are not current implementation.
- At each meaningful checkpoint, update `HANDOFF.md` with the remaining subtask, decisions and actual checks; commit it with the related work. Follow its export protocol when switching devices. A Git bundle carries committed files, not local edits.

## Mandatory: minimize API calls

- For every task, use the fewest API/tool calls needed to complete the work correctly and safely. Maximize useful work per call; this is mandatory, not a preference.
- Batch independent reads, searches, checks, and edits into a single call whenever supported, using `multi_tool_use.parallel` or `execute` where available. Do not split work into separate calls when it can be combined.
- Reuse gathered context, avoid redundant calls, and never poll background jobs. Make another call only when missing information, a dependent operation, required verification, or a safety check makes it necessary; never skip these requirements just to reduce the count.

## Commands and packaging

Run backend commands from the repository root so `backend.*` imports resolve. Setup and development server (Python 3.10+):

```sh
python3 -m venv venv
./venv/bin/python -m pip install -r backend/requirements.txt
./venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

- FFmpeg must be on `PATH` for merging/post-processing; it is not installed by the requirements or installers.
- Package with `bash build.sh` (requires `zip`, not an npm build). Git records the shell scripts as non-executable, so prefer `bash` over the documented `./...` invocations.
- Edit `extension/`, not `dist/`. Packaging deletes all of `dist/`, replaces the tracked browser ZIPs, and removes its unpacked folders. Despite the docs, no `dist/chrome/` or `dist/firefox/` remains. Load `extension/` unpacked in Chrome; use `dist/vidgrab-firefox-extension.zip` for Firefox temporary loading.
- `bash installer/install.sh` installs and starts a persistent Linux user service; Windows installation registers `VidGrabBackend`. If the Linux service owns port 8000, stop it with `systemctl --user stop vidgrab-backend.service` before running the development server. Logs: `journalctl --user -u vidgrab-backend.service -f`.

## Wiring and state

- Extension code uses classic scripts and the global `vidGrabAPI` from `extension/lib/api.js`, not ES modules. Both manifests are MV3, but Chrome uses `background.service_worker` and Firefox uses `background.scripts`; preserve the browser-specific wiring.
- Detection travels through `video_detected` messages into `detected_videos_<tabId>` storage. The current overlay hands off `pendingVideoUrl` to the popup; it does not directly enqueue a download.
- `backend/main.py` owns database initialization and queue/scheduler lifespan. Downloads run via `asyncio.to_thread`; progress callbacks cross back to the queue's event loop with `asyncio.run_coroutine_threadsafe`. Queue state, WebSockets, and cookies are process-local; use a single backend process.
- `backend/vidgrab.db` is tracked mutable application state, not a disposable fixture. Startup migrates it via `config.init_db()` and resumes queued/interrupted downloads. `DB_PATH` is also imported into `queue_manager.py`; isolated tests must redirect both bindings.
- Startup defaults to checking PyPI and upgrading `yt-dlp` via pip in the running interpreter when `auto_update_ytdlp` is enabled. `/status` also queries PyPI (cached for ten minutes).
- Settings must align across `DEFAULT_CONFIG`, `ConfigModel`, and `extension/options/options.js`. Defaults only populate missing database keys. `PATCH /config` dumps the full model, so omitted fields reset to defaults. The actual audio-normalization key is `postprocess_normalize_audio`, not `API.md`'s `postprocess_normalize`.
- Browser-only settings (backend URL, cookie forwarding, notifications) live in `chrome.storage.local`, not SQLite. Forwarded cookies stay outside persisted download options, use temporary Netscape files during downloads, and are removed in the queue's `finally` cleanup; preserve this separation.

## Verification limits

- No configured lint, formatter, typecheck, test runner, or CI exists. The backend sanity command is `./venv/bin/python backend/test_backend.py`: it initializes the real database, queries PyPI, and probes Vimeo. Probe failures are only printed, and its `TestClient` does not enter app lifespan; success is not queue/lifecycle coverage.
- `test_gpu.py` currently imports missing `backend.gpu_detector`; it is not a working verification command.
- Serve the overlay fixture with `python3 -m http.server 8080 --directory .` and open `http://localhost:8080/test_overlay.html`. Static serving does not provide `chrome.*` APIs; verify extension interactions in installed Chrome and Firefox builds.
