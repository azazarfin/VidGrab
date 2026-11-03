# VidGrab development

The P02 Linux spike adds a fixture-only desktop/engine/native bridge. Full desktop release acceptance, Windows and the migrated download UI remain pending. The ASGI development adapter is `backend.main:app`; it resolves user paths without initializing SQLite or starting workers during import.

## Locked setup and deterministic checks

Run from the repository root with Python 3.11+ and uv. `pyproject.toml` pins the selected direct dependencies; `uv.lock` locks their transitive dependencies and artifacts.

The project baseline is >=3.11; `yt-dlp==2026.6.9` itself declares >=3.10. The current verified interpreter is Linux Python 3.14.8, not Windows or Python 3.11. Desktop packaging remains P02.

```sh
uv sync --frozen
.venv/bin/python -B -m unittest discover -s tests -v
```

On Windows, use `.venv\Scripts\python.exe -B -m unittest discover -s tests -v`. The default suite uses temporary state, fake extractor/download/clock/folder-opening adapters, and an external-network guard. The read-only import test launches a controlled child Python process with an audit guard. No public media sites are contacted.

In Codex's restricted Linux sandbox, Python's local socketpair writes can be denied and cause asyncio/aiosqlite/TestClient to stall. Run this suite with the approved sandbox exception for local IPC; the suite's external-request guard stays enabled.

For pip-based legacy installation:

```sh
python3 -m venv venv
venv/bin/python -m pip install --require-hashes -r backend/requirements.txt
```

The exported pip requirements contain runtime dependencies. The uv development environment additionally installs the HTTP client used by Starlette's test client. Regenerate requirements only after deliberately updating and validating the lock:

```sh
uv lock
uv export --frozen --format requirements-txt --no-dev --no-emit-project --output-file backend/requirements.txt
```

## Runtime ownership

On Linux, data defaults to `$XDG_DATA_HOME/vidgrab` or `~/.local/share/vidgrab`. On Windows, it defaults to `%LOCALAPPDATA%\VidGrab`. Logs and temporary files use `logs/` and `tmp/` under the data directory; media defaults to the user's `Downloads/VidGrab`. These directories are created only during explicit initialization/lifespan startup.

Use `VIDGRAB_DATA_DIR`, `VIDGRAB_LOG_DIR`, `VIDGRAB_TEMP_DIR`, and `VIDGRAB_DOWNLOAD_DIR` to override paths before importing the ASGI adapter. Application storage and default media paths must be outside the source/install tree. Only the download-directory default is overridden by a previously saved preference.

Media/output settings must also be outside data/log/temp storage. Settings initialization/PATCH, per-job overrides and execution all use `RuntimeConfig.validate_output`. Literal templates are resolved for validation, including existing symlinks; safe nested and absolute templates remain unchanged. Invalid path PATCH is rejected before directory creation or settings mutation. This does not establish race-free dynamic artifacts or safe arbitrary legacy yt-dlp arguments; those remain P03/P04.

```sh
VIDGRAB_DATA_DIR=/tmp/vidgrab-dev-state VIDGRAB_DOWNLOAD_DIR=/tmp/vidgrab-dev-media .venv/bin/python -B -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Startup against an existing selected runtime resumes its queued/interrupted jobs. For diagnostics with execution disabled, instantiate `create_app(RuntimeConfig(...), start_queue=False)`. Each factory instance owns its settings store, queue, volatile cookies, probe cache, and WebSocket connections.

`GET /health` reports local database readiness/schema compatibility; `GET /status` retains the extension's legacy fields without checking PyPI. `POST /update-ytdlp` returns 409 with release-update instructions. The legacy `auto_update_ytdlp` preference is retained for import/client compatibility but never triggers startup work.

## Selected legacy import

Stop the legacy service and choose its database explicitly. Import into a new data directory that contains no `vidgrab.db`; importing never starts downloads:

```sh
.venv/bin/python -B -m backend.db /absolute/path/to/legacy/vidgrab.db --data-dir /absolute/path/to/new-state
```

The operation retains a SQLite snapshot in `backups/`, stages and migrates a copy, and publishes only a validated database. Existing destination state is refused. SQLite backup includes committed WAL data. Schema versions 0 (unversioned legacy) and 1 upgrade transactionally to 2; failures roll back, and newer versions are refused. Inspect/repair invalid legacy settings on a copy rather than silently resetting them.

The legacy source database, output media, and locally generated browser archives are not fixtures. No installer, persistent service, browser package build, or live download is part of the deterministic checks. Windows execution and full process/cancellation/retry guarantees remain later milestone gates.

Atomic import publication uses a hard link on the target filesystem; validate support and Windows behavior during P02. Initialization/CLI never chooses or migrates `backend/vidgrab.db` automatically. Importing a real queue can cause jobs to resume when that new runtime is subsequently started normally.

## Opt-in mocked settings browser check (Linux)

`tests/check_options_ui.py` is intentionally outside unittest discovery and uses a separate temporary Playwright environment. The current executable is `/opt/google/chrome/chrome`; this is not a cross-platform or installed-extension test.

```sh
uv run --no-project --with playwright==1.64.1 --python .venv/bin/python --no-python-downloads --cache-dir /tmp/vidgrab-uv-cache python -B tests/check_options_ui.py --screenshots /tmp/vidgrab-options-check
```

The first run downloads the test tool with permission; subsequent runs can add `--offline` before `python`. It creates an isolated headless profile, mocks the backend, blocks all other HTTP requests (including Google Fonts), and checks four tabs across three widths. Screenshots are diagnostic artifacts only. In this agent environment Chrome/local IPC needs the approved sandbox exception. The normal backend lock/environment is not altered by the extra tool.

The settings font warning has one shared, file-scoped Inter exception in `.impeccable/config.json`. Existing typography is intentional for this refinement; other detector rules remain active. See [VALIDATION.md](VALIDATION.md) for actual results and untested UI areas.

## P02 Linux build/staging

The current target is Linux x86_64. Native development needs Rust/Cargo, Node/npm, GTK 3, WebKitGTK 4.1 and their development headers; installed runtime libraries are still required by Tauri. Python/media tools ship inside the staged engine. A clean target package baseline has not been certified. No command below installs a service or writes real browser registration.

Install the locked optional build group, then fetch the pinned archives once with network permission or use an existing verified cache:

```sh
uv sync --frozen --group packaging
.venv/bin/python -B packaging/fetch_components.py --cache /tmp/vidgrab-p02-components --output /tmp/vidgrab-p02-tools --offline
.venv/bin/python -B packaging/stage_spike.py --components /tmp/vidgrab-p02-tools --output /tmp/vidgrab-p02-new/bundle --data-dir /tmp/vidgrab-p02-new/state --download-dir /tmp/vidgrab-p02-new/media
npm --prefix desktop ci
npm --prefix desktop run build
.venv/bin/python -B packaging/prepare_desktop.py --bundle /tmp/vidgrab-p02-new/bundle
cargo build --locked --offline --release -p vidgrab-desktop
cp target/release/vidgrab-desktop /tmp/vidgrab-p02-new/bundle/vidgrab-desktop
```

Use a fresh staging output; the stage command refuses existing trees. Runtime/media must be outside both source and staging. Its generated `host-config.json` selects the CLI owner by default; `--desktop /absolute/path/to/already-built/vidgrab-desktop` selects that binary during staging. Opt-in checks select the desktop with fresh per-test configuration. Resource preparation copies the complete `engine/_internal` support tree and the `x86_64-unknown-linux-gnu` native-host suffix into ignored Tauri directories.

The desktop crate defaults to `custom-protocol`, which embeds the frontend for direct Cargo builds. Disable default features for a Vite development build. The current Fedora environment stages verified development headers under `/tmp/vidgrab-tauri-sdk` rather than installing packages; its native build used:

```sh
PKG_CONFIG_PATH=/tmp/vidgrab-tauri-sdk/usr/lib64/pkgconfig:/tmp/vidgrab-tauri-sdk/usr/share/pkgconfig cargo build --locked --offline --release -p vidgrab-desktop
```

That temporary SDK/cache is not included in Git and must be recreated or replaced with target-native development packages on another machine. AppImage/`.deb` resource discovery, registration/relocation and clean installation remain P02 work; the old `build.sh` still only replaces extension archives.

## P02 opt-in checks

Commands below use the current session's temporary bundle/tool paths; replace them with freshly staged paths elsewhere. Native checks use only a generated loopback WAV, temporary state and bundled MP3 post-processing. The default 49-test suite remains separate and guarded. The final bundle was freshly staged with `--desktop target/release/vidgrab-desktop` after the downloader and queue Unicode-path fixes; earlier staging trees retain the previous behavior.

```sh
node tests/check_extension_bootstrap.mjs
.venv/bin/python -B tests/check_native_lifecycle.py --bundle /tmp/vidgrab-p02-linux-final/bundle
.venv/bin/python -B tests/check_native_desktop.py --bundle /tmp/vidgrab-p02-linux-final/bundle --xvfb /tmp/vidgrab-tauri-sdk/usr/bin/Xvfb --screenshots /tmp/vidgrab-p02-native-ui
uv run --no-project --offline --with playwright==1.64.1 --python .venv/bin/python --no-python-downloads --cache-dir /tmp/vidgrab-uv-cache python -B tests/check_installed_browsers.py --bundle /tmp/vidgrab-p02-linux-final/bundle --xvfb /tmp/vidgrab-tauri-sdk/usr/bin/Xvfb --geckodriver /tmp/vidgrab-p02-tools/geckodriver
```

The desktop/browser scripts automatically create private bwrap network/PID/home/D-Bus state, mask system native registrations, and use software rendering. They require bwrap, Xvfb, Mesa and the approved local-IPC/browser sandbox exception. Native WebKit uses `/usr/bin/WebKitWebDriver`; installed-browser checks use Chrome `/opt/google/chrome/chrome` and Firefox `/usr/bin/firefox` by default. Chrome automation selects a custom user data directory and places its native manifest in that directory's `NativeMessagingHosts`; `CHROME_CONFIG_HOME` remains separate. This avoids Chrome's default-profile remote-debugging restriction while exercising its actual user-host lookup. Firefox is temporarily installed from a generated XPI. Gecko 0.37.1's `--allow-system-access` is restricted to the isolated extension test page. Its Linux64 archive was checked against Mozilla's release digest `e815130ea95983e162ae91843b48d3a3ce991735635fce83a647afde21e09f7e` before extraction/use; it is a test tool, not a bundled release component.

For the renderer-only check, serve the built frontend in one terminal and run the mocked command check in another:

```sh
python3 -m http.server 4173 --bind 127.0.0.1 --directory desktop/dist
uv run --no-project --offline --with playwright==1.64.1 --python .venv/bin/python --no-python-downloads --cache-dir /tmp/vidgrab-uv-cache python -B tests/check_desktop_ui.py --screenshots /tmp/vidgrab-p02-desktop-ui
```

This checks three widths with mocked Tauri commands and blocks other HTTP requests. It proves no native IPC, installed extension, Windows, physical tray, Wayland or clean-package behavior.
