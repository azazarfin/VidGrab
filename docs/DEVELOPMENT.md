# VidGrab development

The P02 fixture desktop/engine/native bridge is accepted for the selected Ubuntu/GNOME and Windows 11 preview baselines. P03 backend durability is accepted for the selected controlled fixtures. P04's ten v1 operations, schema-10 action receipts and ordinary media with volatile scoped authentication pass selected Fedora/Ubuntu-userspace/Windows source/fresh-frozen/native checks. Actual client/default/revision/event/permission migration and client/GUI/browser acceptance remain open. Full release acceptance and the migrated download UI remain pending. The ASGI development adapter is `backend.main:app`; it resolves user paths without initializing SQLite or starting workers during import.

## Locked setup and deterministic checks

Run from the repository root with Python 3.11+ and uv. `pyproject.toml` pins the selected direct dependencies; `uv.lock` locks their transitive dependencies and artifacts.

The project baseline is >=3.11; `yt-dlp==2026.6.9` itself declares >=3.10. Existing platform evidence uses Fedora Python 3.14.8, Ubuntu userspace 3.12.3 and Windows 11 3.12.16; Python 3.11 remains unexecuted. All 432 current guarded cases pass on Fedora and Ubuntu userspace in complete discovery runs. Windows discovers 432, with 423 passing and nine POSIX skips. P04 source/fresh-frozen/native runtime checks now cover those selected targets; actual client/GUI/browser acceptance remains open. Read [VALIDATION.md](VALIDATION.md) for each checkpoint and its limits.

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

Startup against an existing selected runtime reconciles interrupted attempts and runs eligible queued jobs. For diagnostics with execution disabled, instantiate `create_app(RuntimeConfig(...), start_queue=False)`. Each factory instance owns its settings store, queue, volatile cookies, probe cache, and WebSocket connections. Ordinary downloads select the P03 per-attempt process executor. Explicitly injected synchronous diagnostic/test runners use a thread adapter; both boundaries stay joined through repeated caller cancellation before cookie cleanup. Windows workers launch detached/suspended, enter their non-inheritable kill-on-close Job Object and resume their unique, verified primary thread before bootstrap; owners launch detached and explicit pipe whitelists remain.

Every engine lifespan, including legacy ASGI and execution-disabled diagnostics, holds `data_dir/engine-owner.lock` before database initialization. A second lifespan is refused; normal shutdown retains the file and closes its lock after execution is joined. This is separate from the desktop supervisor's app lock. Select temporary runtimes for diagnostics. Durable creation identities fence recovery before artifact reads/job resets; an expired 30-second lease never permits replacing a live owner. Leases renew every 10 seconds. Unknown/inaccessible identity refuses recovery; renewal failure marks readiness false and joins execution.

Schema 9 adds settings/job revisions, settings replay receipts and 512 retained invalidations while preserving numbered attempts/literal artifact paths, runtime owners/leases/worker identities and bounded non-secret cookie ownership/ready metadata. Worker identities and ready-cookie verification precede bootstrap delivery. Each attempt exclusively creates `data_dir/attempts/<UUID>`. Version-2 `.vidgrab/artifacts.jsonl` records retain file/directory identities, SHA-256 witnesses and a result footer; concrete post-processing records determine final paths. No-replace hard links and destination-volume witnesses protect publication. After all owner/tree fences, verified success completes the same attempt, unpublished work may requeue and ambiguous publication fails without automatic retry. Persisted cancellation wins; startup/terminal verification stays joined and publication witness cleanup follows the SQLite success commit. Private `.vidgrab/cookies-<UUID>` jars and hard-link witnesses begin empty; ownership commits before any secret write, and flush/fsync precedes ready delivery. Creation/cleanup remain joined. Recovery discards verified jars, handles interrupted unlinks and retains foreign/unsafe files. History deletion preserves pending cookie ownership; individual deletion returns 409. Direct/static-clear-fragment resume, fresh-auth waits/actions, playlist children and idempotent enqueue are implemented. Persisted retry/request budgets pass selected cross-platform guarded/source/fresh-frozen checks; installed-browser auth UI remains planned.

`GET /health` reports local database readiness/schema compatibility; `GET /status` retains the extension's legacy fields without checking PyPI. `POST /update-ytdlp` returns 409 with release-update instructions. The legacy `auto_update_ytdlp` preference is retained for import/client compatibility but never triggers startup work.

## Selected legacy import

Stop the legacy service and choose its database explicitly. Import into a new data directory that contains no `vidgrab.db`; importing never starts downloads:

```sh
.venv/bin/python -B -m backend.db /absolute/path/to/legacy/vidgrab.db --data-dir /absolute/path/to/new-state
```

The operation retains a SQLite snapshot in `backups/`, stages and migrates a copy, and publishes only a validated database. Existing destination state is refused; backup includes committed WAL data. Schema versions 0 (unversioned legacy), 1, 2, 3, 4, 5, 6, 7 and 8 upgrade transactionally to 9; failures roll back and newer versions are refused. History receives no invented attempts or owners. Schema-3 running attempts with no identity remain preserved and recovery refuses them. Stop old services before migration because pre-attempt legacy rows retain their existing compatibility path. Inspect/repair invalid legacy settings on a copy rather than silently resetting them.

The legacy source database, output media, and locally generated browser archives are not fixtures. No installer, persistent service, browser package build, or live download is part of the deterministic checks. Selected Linux/Windows worker checks have their own opt-in evidence; full recovery/resume/retry guarantees remain later P03 gates.

Atomic import publication uses a hard link on the target filesystem; validate support and Windows behavior during P02. Initialization/CLI never chooses or migrates `backend/vidgrab.db` automatically. Importing a real queue can cause jobs to resume when that new runtime is subsequently started normally.

## P03 opt-in worker checks

These commands run owned local processes and loopback fixtures in temporary runtimes. They never select installed application state or real-user native registrations. `check_worker_execution` requires an explicit directory containing checksum-verified `manifest.json`, FFmpeg/ffprobe and Deno; the fresh frozen engine's `_internal/tools` is also suitable. Run from the repository root:

```sh
.venv/bin/python -B -m tests.check_worker_lifecycle
.venv/bin/python -B -m tests.check_worker_execution --tools /absolute/path/to/verified/tools
.venv/bin/python -B -m tests.check_service_recovery --tools /absolute/path/to/verified/tools
.venv/bin/python -B -m tests.check_cookie_crashes --tools /absolute/path/to/verified/tools
.venv/bin/python -B -m tests.check_verified_resume --tools /absolute/path/to/verified/tools --report /absolute/path/to/resume.json
.venv/bin/python -B -m tests.check_resume_crashes --tools /absolute/path/to/verified/tools --report /absolute/path/to/resume-crashes.json
```

After rebuilding a fresh engine with the locked packaging environment and complete bundled tools, repeat all six checks with its absolute executable. On Windows select the `.exe` and use `.venv\Scripts\python.exe`:

```sh
.venv/bin/python -B -m tests.check_worker_lifecycle --engine /absolute/path/to/fresh/vidgrab-engine
.venv/bin/python -B -m tests.check_worker_execution --engine /absolute/path/to/fresh/vidgrab-engine --tools /absolute/path/to/fresh/_internal/tools
.venv/bin/python -B -m tests.check_service_recovery --engine /absolute/path/to/fresh/vidgrab-engine --tools /absolute/path/to/fresh/_internal/tools
.venv/bin/python -B -m tests.check_cookie_crashes --engine /absolute/path/to/fresh/vidgrab-engine --tools /absolute/path/to/fresh/_internal/tools
.venv/bin/python -B -m tests.check_verified_resume --engine /absolute/path/to/fresh/vidgrab-engine --tools /absolute/path/to/fresh/_internal/tools --report /absolute/path/to/frozen-resume.json
.venv/bin/python -B -m tests.check_resume_crashes --engine /absolute/path/to/fresh/vidgrab-engine --tools /absolute/path/to/fresh/_internal/tools --report /absolute/path/to/frozen-resume-crashes.json
```

All six scripts accept `--report /absolute/path/to/report.json`. The owner suite contains eleven entries with one explicit platform skip: Linux skips abrupt Windows owner death, while Windows skips POSIX SIGTERM. The ten media checks cover WAV→MP3/codec validation, Unicode paths, extraction/download/postprocessing cancellation, joined cookie cleanup, unrelated/preexisting-file preservation, staged media/subtitle/thumbnail and cross-volume publication. To execute the cross-volume case, pass `--cross-volume-root` on a different volume: recorded Linux checks select `/dev/shm`; E:-backed Windows checks select `C:\Users\Azaz\AppData\Local\Temp`. `tests/worker_process_support.py` records PID/start identities without an extra process-library dependency. The eight-entry service suite adds owned synthetic before/after-link crashes and a real media result held before SQLite's terminal write to overlap/gated restart/launch retirement and two Linux orphan-owner/group cases. Windows skips those two POSIX cases. Windows readiness verifies kernel ancestry and termination uses a creation-time-verified handle. The eleven-entry cookie suite covers nine creation/delivery/cleanup boundaries, live-engine overlap and a Linux orphan owner; Windows skips that orphan case. Its producer is instrumented source, while `--engine` selects frozen workers and the complete frozen service for restart. Recovered jars are discarded, and unrelated credentials remain untouched. Verified fragment resume plus installed-browser fresh-authentication UI remain later work. Container runs need `--init` to reap orphaned processes; preserve private namespaces and default seccomp.

`tests/check_artifact_reparse.py` is an additional Windows-only opt-in check. It runs thirteen junction/DACL cases under temporary paths, including runtime/lock refusal, changed publication parents, foreign source parents, destination refusal, cookie metadata/file/directory junctions and current-account/SYSTEM-only cookie permissions, with external-network/process guards enabled and no child processes:

```powershell
.venv\Scripts\python.exe -B -m tests.check_artifact_reparse --report E:\temporary-validation\artifact-reparse.json
```

The preceding direct-resume checkpoint passed source/fresh frozen Fedora, Ubuntu-userspace and Windows checks: ten active protocol, ten media, eight Linux/six Windows service and eleven Linux/ten Windows cookie cases, plus four direct-resume/authentication cases and the Windows exact-Job rejection gate. The guarded suite has 253 cases (nine POSIX skips on Windows). Compatibility bundles pass eleven Linux/six Windows native and thirteen Windows junction/DACL cases; Ubuntu's 50 ELFs fit glibc 2.39 on Fedora's kernel. That checkpoint's Windows output is `E:\VidGrabWork\P03\resume-20261114-u7pn2d1c-3`, with source/frozen/native results in its evidence tree. Preserve all earlier results and select fresh roots. Windows checks use elevated Azaz, process-only ExecutionPolicy Bypass and verified tools; frozen/native checks use an empty PATH. [HANDOFF](../HANDOFF.md#p03-direct-http-resume-and-fresh-authentication-checkpoint--2026-11-14) records snapshot 3, the selected retry/freeze results and final audit. The earlier [private-metadata audit limit](../HANDOFF.md#p03-windows-workerprotocol-checkpoint--2026-11-14) remains historical. At that checkpoint, abrupt-service/fragment resume, installed-browser auth UI, playlists/idempotency, persisted retry/request budgets and `/v1` (P04) remained unfinished.

The resume check uses real loopback WAV and source/frozen owned workers. It covers interrupted prefixes, changed validators, foreign-file refusal and typed expired-authentication → fresh-cookie continuation of the same job. Linux skips the additional exact-Windows-Job rejection case; Windows executes it through the host helper. The worker-resume checks do not claim fragmented-protocol resume, actual browser refresh UX or release acceptance. The separate six-case crash suite kills/restarts the entire engine and verifies prefix reuse, retained files, cancellation and fresh-cookie recovery. Its producer/authentication service are source fixtures; frozen mode selects frozen workers and the complete frozen recovery service. Cancellation is seeded in the private database and a tail is simulated after verified worker-tree exit. These do not establish power-loss durability. The new authentication action is a legacy backend interface; the fixture-only frozen API still hides legacy routes.

The latest whole-engine resume checks reuse the already verified frozen engines because production backend/packaging inputs are unchanged. All 36 source/frozen cases pass on Fedora, Ubuntu userspace and Windows, with no skips; results are bound to a 213-file copied snapshot. The preceding 253-case guarded suite and broader native/media/service/cookie results remain historical passing evidence; they were not rerun for this opt-in-only addition. See [HANDOFF](../HANDOFF.md#p03-whole-engine-direct-resume-crash-checkpoint--2026-11-14).

## Opt-in mocked settings browser check (Linux)

`tests/check_options_ui.py` is intentionally outside unittest discovery and uses a separate temporary Playwright environment. The current executable is `/opt/google/chrome/chrome`; this is not a cross-platform or installed-extension test.

```sh
uv run --no-project --with playwright==1.64.1 --python .venv/bin/python --no-python-downloads --cache-dir /tmp/vidgrab-uv-cache python -B tests/check_options_ui.py --screenshots /tmp/vidgrab-options-check
```

The first run downloads the test tool with permission; subsequent runs can add `--offline` before `python`. It creates an isolated headless profile, mocks the backend, blocks all other HTTP requests (including Google Fonts), and checks four tabs across three widths. Screenshots are diagnostic artifacts only. In this agent environment Chrome/local IPC needs the approved sandbox exception. The normal backend lock/environment is not altered by the extra tool.

The settings font warning has one shared, file-scoped Inter exception in `.impeccable/config.json`. Existing typography is intentional for this refinement; other detector rules remain active. See [VALIDATION.md](VALIDATION.md) for actual results and untested UI areas.

## P02 Linux build/staging

The current target is Linux x86_64. The user selected Ubuntu 24.04 LTS for a clean rebuild on 2026-11-06; that baseline is not yet certified. Native development needs Rust/Cargo, Node/npm, GTK 3, WebKitGTK 4.1 and their development headers; installed runtime libraries are still required by Tauri. Python/media tools ship inside the staged engine. No command below installs a service or writes real browser registration.

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

That temporary SDK/cache is not included in Git and must be recreated or replaced with target-native development packages on another machine. Extracted `.deb` resource discovery and headless registration now have evidence; actual AppImage and clean install/uninstall remain P02 work. The old `build.sh` still only replaces extension archives.

## P02 Linux package preview — 2026-11-05

After building the desktop and preparing the complete resources, bundle the already-built executable and run the opt-in extraction check from the repository root:

```sh
npm --prefix desktop run tauri -- bundle --bundles deb --ci
.venv/bin/python -B tests/check_linux_package.py --deb target/release/bundle/deb/VidGrab_0.1.0_amd64.deb --report /tmp/vidgrab-deb-check.json
```

The check requires `dpkg-deb` and `readelf`, extracts into a temporary directory and selects a temporary home/XDG environment. It runs the compiled Tauri resolver, frozen self-check with an empty `PATH`, registration/conflict/removal commands, native origin/frame rejection and a whole-bundle glibc audit. It creates no engine server or real registration. AppImage relocation uses executable placeholders and is explicitly simulated. The report refuses to overwrite an existing file.

Packaged launch with no arguments now discovers resources and installs the selected user's browser bridge before starting the owner. Avoid a casual default launch during diagnostics. The staging layout still requires `--config FILE`. Headless packaged commands are:

| Command | Effect |
| --- | --- |
| `--check-package` | Inspect app/engine/native paths without registration or runtime startup |
| `--register-browser-host` | Copy a private stable bridge and write matching per-user manifests; no engine startup |
| `--unregister-browser-host` | Remove only matching manifests; preserve descriptor, bridge, state and media |

The native host uses `--package-config FILE` internally for the strict installed descriptor. Opening a moved image repairs its launch target while retaining selected state/media. Default preview paths are `XDG_DATA_HOME/vidgrab-p02/{native,state}` and `~/Downloads/VidGrab-P02`; unset XDG bases use `~/.local/share` and `~/.config`. Chrome registration is under `XDG_CONFIG_HOME/google-chrome/NativeMessagingHosts`, Firefox under `~/.mozilla/native-messaging-hosts`. Existing foreign manifests or symlink destinations cause refusal. This preview does not share the legacy runtime or implement a full browser-connection UI/uninstaller.

Current artifact: `target/release/bundle/deb/VidGrab_0.1.0_amd64.deb`, 138,158,710 bytes (131.76 MiB), SHA-256 `ad0ba926aca2b776f75a7676b9471abf31562d15cc3a84f0f86520163803dca2`. The final source-frozen bundle is `/tmp/vidgrab-p02-linux-package-final/bundle`; the headless receipt is `/tmp/vidgrab-p02-deb-receipt.json`. These are ignored/ephemeral development artifacts, not release installers or Git handoff contents.

The whole-bundle audit found 82 ELF files. Rust binaries need glibc 2.39, but bundled Python `termios` and `libtinfo` need 2.42; the `.deb` now declares >=2.42. An older clean baseline must rebuild the Python/support libraries as well as Rust. No minimum supported Linux distribution is certified by these Fedora checks.

This session restored linking from installed runtime libraries using private metadata/symlinks under `/tmp/vidgrab-link-metadata`, with library versions verified through RPM/runtime exports and pre-generated Rust FFI bindings. It is not a full SDK or clean target build. The local native build/bundler used `PKG_CONFIG_PATH=/tmp/vidgrab-link-metadata/lib/pkgconfig`; install proper development packages on a clean build host. Tauri's equivalent `tauri-build` Cargo normalization was restored after bundling.

For the actual AppImage attempt, `XDG_CACHE_HOME=/tmp/vidgrab-tauri-cache` avoided writing the real home cache. Bundling then failed downloading its uncached AppRun helper because DNS is blocked. No AppImage was produced. Do not count the simulated relocation check as AppImage, GUI, tray/Wayland, live browser or install/uninstall evidence.

## Ubuntu 24.04 userspace P02 rebuild and AppImage finalization

Use a clean Ubuntu 24.04 x86_64 build environment with its Python 3.12/support and GTK/WebKit development packages. The observed build image and package receipts are in HANDOFF. The current default Tauri configuration retains the old Fedora >=2.42 preview dependency; the Ubuntu override below is valid after rebuilding and passing the whole-bundle 2.39 ceiling.

From the repository root in that environment, use the existing locked npm/Cargo/uv packaging setup, fresh staging and resource preparation. Before bundling:

```sh
.venv/bin/python -B packaging/check_abi.py --tree /tmp/vidgrab-ubuntu-bundle --max-glibc 2.39 --report /tmp/vidgrab-ubuntu-abi.json
npm --prefix desktop run tauri -- bundle --bundles deb,appimage --config "$PWD/packaging/ubuntu24.bundle.json" --ci
```

Tauri/linuxdeploy modifies frozen ELF resources, including checksum-verified Deno. The raw image fails integrity. Restore the complete original engine after dependency deployment:

```sh
.venv/bin/python -B packaging/prepare_appimage.py --appdir target/release/bundle/appimage/VidGrab.AppDir --bundle /tmp/vidgrab-ubuntu-bundle --output /tmp/vidgrab-final.AppDir --max-glibc 2.39 --report /tmp/vidgrab-appimage-preparation.json
```

Assemble the resulting fresh tree directly with appimagetool and an explicit `--runtime-file`; do not run linuxdeploy over it again. This checkpoint extracts the cached `linuxdeploy-plugin-appimage.AppImage` to obtain `usr/bin/appimagetool`, and takes exactly `--appimage-offset` bytes from the raw Tauri image as its runtime. Validate the ELF prefix and record input/runtime SHA-256. The actual runtime is 944,632 bytes, SHA-256 `912e3f80ad18723bdf5da6a52d5301588012090a56bcb0919cce170a47d8de5b`; finalization uses `ARCH=x86_64`, the tool's own library/bin paths and `--runtime-file`, with network disabled. The scratch command/receipt are `finish-offline-image.sh` and `appimage-runtime-input.json`; regenerate these inputs when moving devices.

```sh
.venv/bin/python -B tests/check_linux_package.py --deb target/release/bundle/deb/VidGrab_0.1.0_amd64.deb --report /tmp/vidgrab-ubuntu-deb-check.json
.venv/bin/python -B tests/check_appimage_package.py --appimage /tmp/VidGrab_0.1.0_amd64.AppImage --max-glibc 2.39 --report /tmp/vidgrab-ubuntu-image-check.json
```

The AppImage checker audits its ELF runtime and extracted tree, verifies frozen tools with empty PATH, and exercises actual image relocation/registration in a temporary home. AppRun uses `/usr/bin/env bash`, so the launcher needs the standard OS utility PATH.

`tests/check_packaged_desktop.py --app FILE --xvfb FILE --geckodriver FILE --screenshots DIR --report FILE` exercises an installed executable or actual AppImage through package registration and its copied bridge, then real UI and native Chrome/Firefox fixtures. `--browser chrome|firefox|both` selects browser phases. It checkpoints results before later failures. Use the existing isolated driver setup and native browser binaries, including `--firefox FILE` when using a vendor archive. A custom Chrome CDP profile receives the package-generated manifest.

The current Ubuntu scripts use an outer Podman container with `--network none --pid private --ipc private`, private Linux tmpfs for `/tmp` and `/root`, selected fixture HOME/XDG paths, private D-Bus, Xvfb and software rendering. Only that fully isolated harness sets `VIDGRAB_VALIDATION_ISOLATED=1`; nested bwrap failed its probes. VFS storage on the NTFS volume ignores Unix modes, so private IPC/homes must live on Linux tmpfs. The application gets `/usr/bin:/bin`; frozen tools retain separate empty-PATH checks. No selected real runtime or user registration is used.

Both UIs and Chrome pass; Firefox alone passes but the combined reopen fails reproducibly. Capture launch diagnostics before browser cleanup and fix that cause before claiming package lifecycle acceptance. Full Ubuntu desktop/FUSE/tray/Wayland and Windows remain open.

## P02 opt-in checks

Commands below use the current session's temporary bundle/tool paths; replace them with freshly staged paths elsewhere. Native checks use only a generated loopback WAV, temporary state and bundled MP3 post-processing. The default 49-test suite remains separate and guarded. The final bundle was freshly staged with `--desktop target/release/vidgrab-desktop` after the downloader and queue Unicode-path fixes; earlier staging trees retain the previous behavior.

```sh
node tests/check_extension_bootstrap.mjs
.venv/bin/python -B tests/check_native_lifecycle.py --bundle /tmp/vidgrab-p02-linux-final/bundle
.venv/bin/python -B tests/check_native_desktop.py --bundle /tmp/vidgrab-p02-linux-final/bundle --xvfb /tmp/vidgrab-tauri-sdk/usr/bin/Xvfb --screenshots /tmp/vidgrab-p02-native-ui
uv run --no-project --offline --with playwright==1.64.1 --python .venv/bin/python --no-python-downloads --cache-dir /tmp/vidgrab-uv-cache python -B tests/check_installed_browsers.py --bundle /tmp/vidgrab-p02-linux-final/bundle --xvfb /tmp/vidgrab-tauri-sdk/usr/bin/Xvfb --geckodriver /tmp/vidgrab-p02-tools/geckodriver
```

The desktop/browser scripts automatically create private bwrap network/PID/home/D-Bus state, mask system native registrations, and use software rendering. They require bwrap, Xvfb, Mesa and the approved local-IPC/browser sandbox exception. Native WebKit uses `/usr/bin/WebKitWebDriver`; installed-browser checks use Chrome `/opt/google/chrome/chrome` and Firefox `/usr/bin/firefox` by default. Chrome automation selects a custom user data directory and places its native manifest in that directory's `NativeMessagingHosts`; `CHROME_CONFIG_HOME` remains separate. This avoids Chrome's default-profile remote-debugging restriction while exercising its actual user-host lookup. Firefox is temporarily installed from a generated XPI. Gecko 0.37.1's `--allow-system-access` is restricted to the isolated extension test page. Its Linux64 archive was checked against Mozilla's release digest `e815130ea95983e162ae91843b48d3a3ce991735635fce83a647afde21e09f7e` before extraction/use; it is a test tool, not a bundled release component.

The display-helper regression needs no X server/browser and stays outside default discovery:

```sh
.venv/bin/python -B tests/check_native_display.py -v
```

`tests/check_packaged_desktop.py` checks an actual installed/extracted desktop executable or AppImage through its canonical copied native bridge. It covers compiled UI, no-tray Close, repeated activation, fixture MP3, recovery, Quit and the combined browser exit/reopen sequence. Select a fresh report and screenshots directory and explicit native browser paths when OS defaults are Snap wrappers. Current artifacts/outer container commands are in HANDOFF.

Inside a sealed test environment with private network/PID/IPC/home/tmp/D-Bus, Xvfb and software rendering, the actual FUSE variant is:

```sh
.venv/bin/python -B tests/check_packaged_desktop.py --app /absolute/path/VidGrab_0.1.0_amd64.AppImage --xvfb /usr/bin/Xvfb --geckodriver /absolute/path/geckodriver --browser both --fuse --screenshots /tmp/vidgrab-package-fuse --report /tmp/vidgrab-package-fuse.json
```

`--fuse` removes extract-and-run selection, verifies the desktop executable's actual image mount and waits for unmount after Quit. It requires `/dev/fuse` and a suitable OS `fusermount`/`fusermount3` with preserved permissions in that environment; default bwrap device isolation does not provide this FUSE setup. The recorded Ubuntu outer-container variant adds `fuse3` and a private Linux-tmpfs helper because NTFS VFS lost its setuid bit. Omit `--fuse` for the default extract-and-run image check or select a desktop binary for `.deb` validation. Engine self-checks still use an empty PATH independently. No native launch timeout or mutation retry is changed by these modes.

For native Ubuntu GNOME/Wayland, normal OS FUSE, content-sandboxed native browser handoff, virtual pointer/keyboard tray interaction and CLI package removal/repair, use [the disposable KVM guest recipe](../tests/ubuntu_vm/README.md). These checks operate on selected fixture homes and actual unchanged preview packages. The GNOME probe belongs only in that guest. Query the current display from the user systemd environment; the GNOME Shell process environment predates display creation. Record guest kernel/versions and copy receipts before shutdown. Native browser seccomp evidence is separate from Snap/Flatpak confinement and paired fallback.

For comparable package startup samples, repeat `tests/check_wayland_desktop.py` sequentially with fresh home/report/screenshot paths, the same verified final packages and the recipe's `PYTHONPATH=/usr/lib/python3/dist-packages`. Its `startup_seconds` starts immediately before the WebDriver session command launches the registered package and ends when `.connection` reports Connected. Registration/descriptor preparation and the WebKitWebDriver process/readiness occur before the timer; screenshots, downloads, recovery and Quit occur afterward. The session command and 0.1-second observation cadence remain included. Use at least three samples per package and report every value, median and range. This is fresh application state with naturally populated OS caches, not a cold-cache/boot benchmark or a release latency budget. Unregister matching hosts in each selected test home after Quit, preserve fixture state/media, and audit/shut down the disposable guest.

The helper regressions require no installed app/GUI; the stream cases bind loopback, and tray cases fake only D-Bus replies. They remain opt-in:

```sh
.venv/bin/python -B -m unittest tests.check_native_display tests.check_wayland_protocol tests.check_fixture_stream tests.check_package_installation_protocol -v
```

`check_package_installation.py` removes/reinstalls the selected `.deb` only in the explicitly named disposable guest and consumes already completed fixture homes. APT needs its local-file acquire step even offline; `--no-download` produces an internal pathname error on Ubuntu. Keep guest networking blocked instead. AppImage removal removes its registered launch path while retaining the original file privately for restoration. This check verifies CLI preview lifecycle/preservation, not terminal-free release uninstaller UX.

For the renderer-only check, serve the built frontend in one terminal and run the mocked command check in another:

```sh
python3 -m http.server 4173 --bind 127.0.0.1 --directory desktop/dist
uv run --no-project --offline --with playwright==1.64.1 --python .venv/bin/python --no-python-downloads --cache-dir /tmp/vidgrab-uv-cache python -B tests/check_desktop_ui.py --screenshots /tmp/vidgrab-p02-desktop-ui
```

This checks three widths with mocked Tauri commands and blocks other HTTP requests. It proves no native IPC, installed extension, Windows, physical tray, Wayland or clean-package behavior.

## Firefox Snap/Flatpak native portal checks — 2026-11-08

[Firefox Flatpak setup](BROWSER_CONNECTION.md) describes the user-selected guided native path. The preview includes packaged setup help, verified browser registration controls and [Ubuntu installation/removal controls](LINUX_INSTALLATION.md); final release/Windows acceptance remains open. Ubuntu's strict Firefox Snap 159.0.1 works through the existing portal without changing a preference. Official Flathub Firefox 159.0.1 has `widget.use-xdg-desktop-portal.native-messaging` set to integer `0` by default; explicit integer `1` enables the tested native portal path. Keep the browser visible/focused for the real GNOME Allow prompt. Restart a desktop session that predates Flatpak installation so GNOME recognizes its exported desktop entry.

Use [the disposable VM recipe](../tests/ubuntu_vm/README.md) for both browser distributions. The compatibility Snap entrypoints and new Flatpak entrypoints share [the QMP host journey](../tests/check_firefox_portal.py) and [guarded guest lifecycle](../tests/firefox_portal_guest.py). Only the explicit Flatpak `--enable-native-portal` flag writes that preference into fresh test profiles. Without it, the stock Flatpak check records a missing-host failure despite registration and then cleans up; that negative receipt is diagnostic evidence, not a passing activation case. The flag never changes Flatpak permission overrides or grants desktop consent.

Both unchanged Ubuntu packages pass the opted-in Flatpak journey and both Snap regressions. Flatpak cleanup uses its exact owned instance, because terminating outer bwrap alone can leave Firefox alive. PID mapping follows only the instance's descendants at the browser namespace depth, including nested content namespaces. Browser/content seccomp and NoNewPrivs remain required. The local helper command now includes eight Flatpak mapping/cleanup regressions:

```sh
.venv/bin/python -B -m unittest tests.check_firefox_flatpak_protocol tests.check_native_display tests.check_wayland_protocol tests.check_fixture_stream tests.check_package_installation_protocol -v
```

Seventeen helpers pass on Fedora/Python 3.14.8 and Ubuntu/Python 3.12.3; exact timings and receipts are in [VALIDATION.md](VALIDATION.md). Those original package bytes predate the help. Later sections and VALIDATION cover the rebuilt help and connection controls. The terminal-free Ubuntu preview installation/removal evidence follows below; final acceptance and Windows keep P02/ADR-001 open. No paired loopback fallback is implemented.

## Manual rebuilt Firefox setup check

Use `tests/check_firefox_flatpak.py --manual-native-portal` with the same disposable VM arguments and fresh home/report/screenshot paths. It is mutually exclusive with `--enable-native-portal` and Flatpak-only. Manual mode opens the actual packaged help and uses observed virtual keyboard/pointer input for the existing Number/Save, Control+Q and restart. It verifies that the temporary development add-on disappeared before reinstalling it after restart, and that the portal preference persisted without a `user.js` override. Desktop permission still requires the real Allow click.

The two rebuilt guidance packages pass this mode. An equal-version local `.deb` can be skipped by APT; explicitly reinstall the selected preview in the disposable guest and compare its installed executable with the package before attributing UI evidence to it. Stop guest apt timers during offline package validation if they hold the lock; never remove package lock files. Restore the timers before shutdown.

```sh
.venv/bin/python -B -m unittest tests.check_gnome_keyboard tests.check_firefox_manual_setup tests.check_firefox_flatpak_protocol tests.check_native_display tests.check_wayland_protocol tests.check_fixture_stream tests.check_package_installation_protocol -v
```

These 30 opt-in helpers pass on Fedora/Python 3.14.8 and native Ubuntu/Python 3.12.3. Keyboard cases fake QMP; geometry cases fake observed windows; two exit cases use real local child processes. No VM, GUI or installed browser is started by the helper command. Actual guide/portal evidence and package hashes are recorded in VALIDATION.

## Packaged browser connection controls

The Linux window has explicit registration status and Connect/Repair/Disconnect actions. `companion_status` includes `browser_connection` and an independent `browser_connection_error`; `set_browser_connection(enabled: bool)` serializes mutations with status reads. A staged `--config` launch has no package-registration controls. CLI `--browser-connection-status` reads the selected home without creating registration/runtime directories or starting the engine; `--register-browser-host` explicitly reconnects, and `--unregister-browser-host` explicitly disconnects.

The unchanged `package_version: 1` descriptor retains selected app/state/media paths. Private adjacent `browser-connection.json` has `connection_version: 1` and a strict Boolean `enabled`; absence keeps older preview behavior. Disconnect persists `false` before removing matching manifests, preserves foreign manifests and all user files, and survives normal reopening or AppImage relocation. Opening a disconnected image repairs its local descriptor/bridge without registering browsers. Copied packaged hosts reread this preference for every validated request and return `BROWSER_DISCONNECTED` before activation/owner contact. Invalid/unsafe preferences are refused. Browser origin/frame checks and the no-mutation-replay rule are unchanged.

`tests/check_browser_connections.py` and `tests/browser_connections_guest.py` are opt-in actual-package checks in the named disposable Ubuntu GNOME VM. They use observed QMP pointer clicks for disconnection/reconnection, hold one fixture download across disconnect, reopen without automatic registration, preserve completed history/media, and refuse a foreign test manifest. They inspect the copied bridge's actual replies. This is separate from browser portal permission and package uninstallation.

Both rebuilt Ubuntu previews pass the actual controls journey and native Chrome/Firefox, manual Flatpak and strict Snap checks. On the host, select the running disposable VM and fresh paths:

```sh
python3 -B tests/check_browser_connections.py --vm /tmp/CONTROL/vm.json \
  --app /usr/bin/vidgrab-desktop \
  --home /home/vidgrabtest/validation/controls-deb \
  --guest-report /home/vidgrabtest/validation/controls-deb.json \
  --report /tmp/controls-deb-outer.json --screenshots /tmp/controls-deb-screenshots
```

Repeat with the guest AppImage, `--fuse` and fresh paths. These pointer journeys preserve a gated download and reopened history, reject a copied host while disconnected, and refuse a foreign manifest. They do not install or uninstall a package.

`check_wayland_browsers.py` now cleans canonical registrations and only its verified copied Chrome diagnostic alias, preserving state/media and refusing foreign or symlink aliases. Failure diagnostics are captured before cleanup. Its updated Chrome→Firefox journey passes for both packages. Refresh the VM's copied read-only source snapshot after host edits and verify hashes; changing the repository alone does not update that input.

Nine existing native lifecycle cases pass with the new compiled owner/bridge. `NativeLifecycleChecks.test_live_packaged_host_observes_connection_changes` keeps one host alive while toggling its private packaged preference false/true/false; the real installed Ubuntu bridge passes it. Use a valid fixture URL with an explicit local port. The old bridge fails the revocation regression. Package hashes, runtime boundaries, audit and shutdown receipts are in VALIDATION/HANDOFF.


## Packaged application installation and removal

The Ubuntu preview follows [the terminal-free guide](LINUX_INSTALLATION.md). The
AppImage creates a managed copy, icon and checksum receipt under the selected
`XDG_DATA_HOME/vidgrab-p02/application`, with an Applications shortcut under
`XDG_DATA_HOME/applications`. Its native descriptor points to that stable copy.
Opening the same portable image keeps that target. A different image, changed
managed bytes, foreign destinations and symlinks are refused; app replacement
and update policy remain P07 work.

The packaged executable adds `--installation-status`, `--install-appimage` and
`--remove-appimage`. Status does not start the engine or create user storage.
Removal preserves state/media, the descriptor and explicit browser disconnection.
An owner lock blocks headless removal while the app is running. Desktop removal
uses the local-only `quit_if_idle` operation, waits for engine/group shutdown,
then removes the managed copy or opens App Center for `.deb` removal. The engine's
`downloads_active` covers all unfinished jobs, including ones outside its latest
50 history rows. Missing/unknown readiness never authorizes idle shutdown.

`tests/check_application_installation.py` runs seven file lifecycle cases with
private temporary HOME/XDG paths against a selected actual AppImage. It starts no
engine or GUI. Run it in the Ubuntu test environment with a fresh report:

```sh
python3 -B tests/check_application_installation.py \
  --appimage /tmp/SELECTED/VidGrab_0.1.0_amd64.AppImage \
  --report /tmp/SELECTED/application-files.json
```

`tests/check_release_installation.py` drives actual Files, Applications, the
compiled window and App Center using the named disposable guest's virtual input.
It requires an already selected fixture descriptor/state/media and the package
folder open in Files. Reports/screenshots must be fresh and inside the private
VM control root. The authentication file is private test data and is never
included in a report. Example after the fixture history exists and browsers are
disconnected:

```sh
.venv/bin/python -B tests/check_release_installation.py \
  --vm /tmp/CONTROL/vm.json \
  --package /home/vidgrabtest/packages/release-ux/VidGrab_0.1.0_amd64.deb \
  --fixture-root /home/vidgrabtest/validation/release-ux-gui \
  --password-file /tmp/CONTROL/guest-authentication \
  --report /tmp/CONTROL/release-cycle.json \
  --screenshots /tmp/CONTROL/release-cycle \
  --steps launch-applications history prepare-remove deb-remove save \
          deb-install launch-applications history quit save
```

App Center exposes its installed heading before the rows finish loading and can
retain package state while open. The harness reopens it, waits for rows, filters
VidGrab and reads the name/button from the same screenshot before removal. It
waits for dpkg's desktop/icon triggers and verifies all other OS packages retain
their settled states. AT-SPI, Tesseract and GdkPixbuf observations are restricted
to the named VM; cropped OCR preserves original screen coordinates. Flutter
filter typing uses bounded virtual HID holds, and the resulting sole VidGrab row
must be visible before any mutation. These checks do not certify KDE, physical
input, Windows or the migrated extension download UI.

## Windows frozen/native spike

Use the Windows MSVC toolchain and process-only tool environment from the disposable guest checkpoint. Builds, uv cache, managed Python, Cargo target and components remain on E:\VidGrabWork. The Windows component lock is packaging/windows.components.lock.json; select it explicitly with fetch_components.py --lock. stage_spike.py infers executable names and platform support from its target. prepare_desktop.py requires the target-suffixed .exe sidecar on Windows. All stage outputs must be fresh; do not replace a passing bundle casually.

Run guarded backend checks from the source root and locked native checks with cargo test --locked --offline -p vidgrab-supervisor -p vidgrab-native-host -- --test-threads=1. Windows registration integration checks require exact HKCU production host keys initially absent and clean their owned registrations afterward. Native lifecycle is opt-in:

```powershell
.venv\Scripts\python.exe -B tests\check_windows_native.py --bundle E:\VidGrabWork\Build\bundle-win-2 --report E:\VidGrabWork\Logs\SELECTED-native.json
```

The diagnostic owner accepts --config FILE --request status (also activate, quit and quit_if_idle); it never starts a second owner for a request. Native framing reads are bounded on Windows; activation must keep its explicit handle whitelist. The old bridge in bundle-win-1 remains an opt-in negative fixture via --native-host and --test test_activation_leaves_no_native_stdio_handle_in_the_running_app. Do not use it as a working bundle. Current checks run elevated Azaz; actual desktop/browser/standard-user installer gates remain open.

## Windows Tauri/NSIS preview build

On the selected Windows MSVC build guest, prepare_desktop.py --bundle SELECTED --target x86_64-pc-windows-msvc prepares the complete frozen engine/sidecar. Run the locked frontend/Tauri command from the source root:

```powershell
npm --prefix desktop run tauri -- build --config E:\VidGrabWork\Source\vidgrab\packaging\windows.bundle.json --bundles nsis --ci
```

The override uses currentUser NSIS, an offline WebView2 payload, the existing PNG-derived ICO and a pinned template with Programs/VidGrab-P02 as the default per-user install directory. Build `cargo build --locked --offline --release -p vidgrab-supervisor -p vidgrab-native-host` before Tauri bundling: NSIS embeds the supervisor from beside the main build executable. Its Windows-only `--installer-check-destination`, `--installer-verify`, `--installer-prepare-install`, `--installer-prepare-uninstall` and `--installer-record` operations each take an absolute package root. Setup validates ownership before WebView2 or any destination write, then records canonical root, all file hashes and directory names in a private `LOCALAPPDATA/vidgrab-p02/application/windows-installation.json` receipt. Maintenance runs the embedded checker, verifies the complete installed package and sends local `quit_if_idle` once if necessary; it waits for shutdown and disconnects without starting another owner. Existing app/registry command strings never establish trust. Missing/changed/foreign/reparse destinations are refused; earlier preview packages without receipts must be removed through their independently verified original uninstallers before fresh installation. Interrupted-install/update recovery remains P07. Ordinary Windows launches use USERPROFILE/LOCALAPPDATA, not HOME/XDG. The Windows renderer helper is tests/check_windows_desktop_ui.py; use the same local built frontend server as the Linux mocked check. Actual GUI probes currently remain private, ephemeral and scoped to the fresh standard test account; their receipt/connection locations are in HANDOFF. Actual native browsers, tray, trusted destination refusals and standard-user GUI install/remove/reinstall pass. The scoped controlled-unavailable WebView2/offline per-user install/render and final startup/size review now pass; P02/ADR-001 are accepted for the selected preview baselines. See VALIDATION for the retained machine files and measurement limits.

Windows GUI controls and exact-package refusal receipts are summarized in [HANDOFF](../HANDOFF.md). Serialize the private interactive task helper, use SCP/-File for long PowerShell scripts, and use UTF-8 with BOM for Windows PowerShell 5 scripts containing Unicode. Actual Firefox, final GUI install/remove/reinstall, Unicode and no-tray checks pass. Read receipt/descriptor JSON with `Get-Content -Encoding UTF8`; Rust emits UTF-8 without a BOM. Native helpers link the Windows MSVC CRT statically via the root `.cargo/config.toml`, while frozen Python carries its support DLLs. Installer receipt privacy and second-folder refusals are mandatory. Present/controlled-unavailable WebView2 and final startup/size review pass for the selected VM. For Windows `--config FILE` checks, explicitly set process-only `WEBVIEW2_USER_DATA_FOLDER` to a selected test/profile directory: an explicit launch otherwise selected an executable-adjacent cache and correctly made installer ownership verification fail. Normal packaged launches use `%LOCALAPPDATA%\com.vidgrab.desktop`; do not delete user caches or weaken receipt validation. The accepted timings use this existing standard-user cache and fresh explicit engine state. Machine discovery keys are restored only after the per-user prerequisite render/Quit; Windows can remove a redundant user runtime after machine discovery returns. Never repeat the physical removal attempt or treat the registry mask as a physically clean OS.


## Verified static fragment resume checks (P03 WIP)

The guarded suite contains 279 cases, including 26 fragment cases. `tests/check_fragment_resume.py` is opt-in: twelve real-media cases cover both static clear HLS and DASH with source/frozen owned workers, repeated cancellation, changed manifests, foreign prefixes, fresh legacy-backend authentication, whole-engine crashes and seeded persisted cancellation. Verified FFmpeg creates clear fMP4 media. Manifests use the existing numeric-loopback `/fixture.wav` endpoint without widening the frozen spike's allowlist. Download witnesses verify full aggregate SHA-256; AAC compares decoded PCM/codec/duration, while service MP3 checks codec and input witnesses.

```sh
.venv/bin/python -B -m tests.check_fragment_resume --tools /absolute/verified/tools --report /temporary/fragments-source.json
.venv/bin/python -B -m tests.check_fragment_resume --engine /absolute/vidgrab-engine --tools /absolute/engine/_internal/tools --report /temporary/fragments-frozen.json
```

Reuse requires static clear full-file streams, bounded fresh manifests/plans (1 MiB each/4,096 fragments), and strong HEAD/GET effective-URL/ETag/exact-length agreement. Unsupported HEAD or weak validators download fresh. The verified lane is sequential and adds HEAD requests; shared persisted request budgets remain P03. Windows uses `.venv\Scripts\python.exe`, process-only ExecutionPolicy Bypass and existing verified E:. Select fresh output roots; preserve prior freezes. Current snapshot 3/runners are under `packaging/out/p03-fragment-validation-lkiedtn6/`. No real-site, browser, native Ubuntu-kernel, power-loss or release claim is added; see [HANDOFF](../HANDOFF.md#p03-static-fragment-resume-checkpoint--2026-11-14).

The selected checkpoint passes 279 guarded cases per platform and all 72 corrected source/frozen fragment cases. Snapshot 3 only corrects the opt-in FFmpeg generation cwd/relative names plus notes; Linux freezes/unchanged general checks from snapshot 2 remain applicable. Windows source publication uses a retained initial timeout plus unchanged isolated recheck; preserve both reports and the failed source-2 initialization fixture. The final audit binds 59 retrieved Windows evidence files, process cleanup and seven notes. See VALIDATION/HANDOFF for exact accepted paths and limits.


## Playlist child and enqueue identity checks (P03 WIP)

At the playlist checkpoint, the default suite contained 316 guarded cases; its cross-platform evidence covers four disjoint 315-case groups plus one separately bound cookie-scope/parent-retry regression. The latest suite has 355 cases, as recorded below. Eight opt-in loopback cases exercise independent child publication, selected positions, replay/reopen, extraction cancellation, child-preserving retry, nested refusal and whole-engine death before/after expansion. WAV checks compare complete decoded PCM SHA-256. Run from the root with verified tools/fresh report paths:

```sh
.venv/bin/python -B -m tests.check_playlist_jobs --tools /absolute/path/to/verified/tools --report /absolute/path/to/playlists.json
.venv/bin/python -B -m tests.check_playlist_jobs --engine /absolute/path/to/fresh/vidgrab-engine --tools /absolute/path/to/fresh/_internal/tools --report /absolute/path/to/frozen-playlists.json
```

Legacy `POST /downloads` accepts optional `Idempotency-Key`: replay returns the original `task_id`, conflict is 409, removed history is 410. Keys use 1–128 visible ASCII characters; semantic requests require finite JSON within 64 KiB. Receipts cap at 10,000 without expiry. `is_playlist` or nonempty `playlist_items` selects bounded raw expansion. Positive indices/ascending hyphen ranges select through position 4,096, with at most 128 children/48 KiB plan. Generated metadata carries Referer only; cookies remain volatile/site-scoped and foreign children may wait for authentication. Parent retry preserves child IDs/completed output; individual child history removal returns 409.

All 48 playlist/60 media source/fresh-frozen cases, native compatibility and Windows junction/DACL checks pass. Backend/freeze inputs match snapshot 1; the additional guarded test/final notes are separately bound. Retain the ABI report-name refusal and fresh-path 50-ELF audit at glibc 2.39. At that playlist checkpoint, request/retry budgets, browser auth UI and full P03 acceptance remained open. See [the checkpoint](../HANDOFF.md#p03-playlist-children-and-idempotent-enqueue-checkpoint--2026-11-14); the retry/request-budget checks below are later.

## Opt-in retry/request-budget checks

Run actual engine-death/throttling checks with verified tools and temporary runtimes. Source and fresh-frozen modes verify persisted upstream deadlines, spent counts, due promotion, final WAV SHA-256/MP3 codec, worker exit and cancellation after engine death:

```sh
.venv/bin/python -B -m tests.check_request_budgets --tools /absolute/path/to/verified/tools --report /absolute/path/to/source-budgets.json
.venv/bin/python -B -m tests.check_request_budgets --engine /absolute/path/to/fresh/vidgrab-engine --tools /absolute/path/to/fresh/_internal/tools --report /absolute/path/to/frozen-budgets.json
```

The fixture advances only its persisted due time while the engine is stopped; it never sleeps through the upstream deadline or replays the enqueue mutation. Guarded tests additionally cover actual loopback redirect/proxy transport, body cleanup/sticky failures, finite request/attempt/probe bounds, owner/recovery fencing and migration rollback. Production retries use persisted jittered deadlines and only the pinned urllib HTTP(S) lane; inner yt-dlp retries, parallel fragments and external network downloaders are disabled. See [ADR-023](DECISIONS.md#adr-023--persisted-retries-and-shared-request-authority) for selected limits. All 355 guarded cases per platform (nine Windows POSIX skips), selected source/fresh-frozen boundary checks, native compatibility and Windows junction/DACL checks pass. Final runners/engines are under `packaging/out/p03-budget-validation-ch1x4tm4/`, using immutable snapshot 3 (234 files; archive SHA-256 `f0abea18561dc4d64812a1159336a1c7c14de5ecd4162670c29a6c0c384a966a`). Windows output is `E:\VidGrabWork\P03\budgets-20261114-ch1x4tm4-3`; preserve earlier roots and the verified existing E: disk. The media-terminal/pre-child-commit producer uses explicit source instrumentation after response/worker exit; frozen mode still uses frozen workers and full frozen recovery. Failure receipts and all three Windows generations remain hash-bound. Five guarded test files subsequently received only AST-equivalent EOF-newline cleanup, bound by `test-eof-formatting-4.json`; runtime/freeze inputs match snapshot 3. [HANDOFF](../HANDOFF.md#p03-retry-and-request-budget-checkpoint--2026-11-14) records the final audit and next scoped P03 acceptance review.

## P04 contract/settings foundation checks

Run from the repository root; the generator is import-safe and never chooses a runtime:

```sh
.venv/bin/python -B -m packages.protocol.generate
.venv/bin/python -B -m packages.protocol.generate --check
.venv/bin/python -B -m unittest tests.test_protocol tests.test_settings_lifecycle -v
node tests/check_protocol_client.mjs
node tests/check_extension_bootstrap.mjs
cargo test --release --locked --offline
```

`packages/protocol/README.md` describes the ten operations, framing, mutation keys, scoped authentication and snapshot rules. Edit Pydantic models or `client-runtime.js`, then regenerate all checked outputs/mirrors. Desktop `npm run build` includes TypeScript checking. Locked offline `cargo check --release -p vidgrab-desktop` additionally needs the target GTK/WebKit development metadata; it is a compile check, without a GUI/package acceptance claim. The Fedora checkpoint uses private pkg-config metadata backed by verified installed libraries, recorded in `local-sdk.json`. Do not reuse it on another target. GdkPixbuf RPM `2.44.6^really2.44.4` has exported ABI 2.44.4.

`tests/check_native_lifecycle.py --bundle <fresh-staged-bundle> --case NativeLifecycleChecks.test_versioned_control_replays_settings_and_downloads_after_owner_restart` is opt-in. Run it with private bwrap network/PID/home state; native children use empty PATH/PYTHONPATH/PYTHONHOME. It drives separate Chrome-shaped and Firefox-shaped CLI callers with numeric-loopback fixture media and temporary state, without registering a real host or launching a browser. The accepted whole frozen suite has twelve cases. The source variant injects an explicitly verified tool directory into an owned executable shim; this is source instrumentation, not a shipped entrypoint. `process-inputs-2.json`, `native-source-final.json` and `native-frozen-final.json` retain exact commands and paths. Use fresh staging; preserve old bundles and failure receipts.

Current evidence is ignored `packaging/out/p04-contract-_czh9xwn/`, via `p04-contract-current.json`. The fresh Fedora freeze keeps locked components/dependencies and has the retained optional absent-`curl_cffi` collection warning. It does not establish Ubuntu/Windows or installed-browser/UI/release compatibility. Native ordinary-media execution, probes/actions/auth refresh, browser preferences/permissions and full generated-client migration remain P04 follow-up work.

## P04 ordinary-media and authentication checks

The current packaged entrypoint enables ordinary HTTP(S) under P03 request/attempt authority. The factory retains explicit `media_mode="ordinary"` selection and a fixture default. `/spike/fixture` keeps its per-job guard; legacy routes/WebSockets stay hidden and pairing stays disabled. Importing the factory does not select/start a runtime.

```sh
.venv/bin/python -B -m packages.protocol.generate --check
.venv/bin/python -B -m unittest tests.test_control_media -v
.venv/bin/python -B -m unittest discover -s tests -v
node tests/check_protocol_client.mjs
node tests/check_extension_bootstrap.mjs
```

The 36 media cases include strict options, context TTL/capacity/scope, actual loopback redirect credential stripping, typed HTTP/admission deadlines, owner/receipt rollback, joined cancellation and schema-9 upgrade/restart replay. All 432 guarded cases pass on Fedora. The existing guard class remains unchanged; the cancellation observer now forwards the optional action argument and the fixture-guard test imports its owning module.

After a fresh locked freeze/native build, run `tests/check_native_lifecycle.py --bundle <fresh-staged-bundle>` in the documented private bwrap network/PID/home environment. The suite has thirteen cases; `NativeLifecycleChecks.test_ordinary_probe_cookie_actions_and_auth_replay_after_owner_restart` is the new focused journey. It uses generated loopback WAVs on ordinary/protected paths, two native caller shapes, owned workers/bundled FFmpeg, fresh auth/cancel/retry and owner restart. It verifies real outputs, volatile headers and empty durable cookie ownership. Native children keep empty PATH/PYTHONPATH/PYTHONHOME; source mode injects verified tools via a diagnostic shim. No browser/GUI/registration or public media is involved.

Fedora implementation evidence is `packaging/out/p04-media-wo0l0rm5/`, via `p04-media-current.json`. `runtime-inputs-final.json` binds 61 inputs; `process-inputs-final.json` records verified tools and unused state/media paths; `bundle-frozen/` is a fresh Fedora freeze. `bundle-source-final/` uses the current freshly rebuilt native binaries; `native-source-current.json`, `native-frozen-final.json` and the final guarded receipt retain exact commands/current executed tests. Earlier source bundles/logs remain preserved. Preserve the earlier foundation bundles and every failed feature/redirect/REST/mode/adapter/generator/hardlink receipt, plus the original native SQLite warning and rejected audit. Final native checks close read/fixture connections explicitly; this opt-in-only fix retains every assertion and leaves the guarded/runtime/frozen inputs unchanged. The optional absent-`curl_cffi` collection warning remains; installed SDK metadata was hash-verified for this Fedora host. The target checks below extend this runtime evidence; actual client migration is next.

## P04 target validation checks

`tests/native_control_checks.py` shares the existing versioned settings/replay and ordinary-media/authentication journeys between Linux and Windows. Five extracted method ASTs are unchanged; Linux retains thirteen cases and Windows now has eight. Windows Firefox-shaped callers pass the temporary native manifest path and extension ID. These scripts use generated loopback media and temporary runtimes; they neither launch installed browsers nor install a real runtime.

Run the Linux suite in the documented private bwrap environment with a fresh bundle. On the selected disposable Windows VM, run from the copied source root with fresh report paths:

```powershell
& .\.venv\Scripts\python.exe -B -m tests.check_windows_native --bundle <fresh-bundle> --report <fresh-native-report.json>
& .\.venv\Scripts\python.exe -B -m tests.check_artifact_reparse --report <fresh-reparse-report.json>
```

The accepted Windows source adapter is diagnostic `bundle-source-embedded/`: an isolated in-process `Py_Main` loader uses the verified Python DLL, explicit stdlib/site-package/source paths and scoped DLL lookup. Its Python shim supplies interpreter metadata and verified components before calling the unchanged engine entrypoint. Owned workers launch through source Python. A wrapper that spawns another interpreter correctly fails the supervisor's `ready.pid == owned.id()` check; preserve that check. The genuine fresh frozen engine is tested separately. Loader/builder inputs, Python/native hashes and exact commands remain in `source-embedded-inputs.json` and associated receipts; none of this diagnostic adapter is shipped.

The two Windows registration fixtures exercise canonical HKCU keys and hold a shared mutex from their empty-key checks through cleanup. The default parallel Rust suite passes; all four values return to null. These synthetic fixture writes are distinct from installed-browser/installer acceptance. SID-private standard-user metadata remains unreadable by Azaz and its contents were not reverified.

Evidence is `packaging/out/p04-target-validation-i65553hs/`, via `p04-target-current.json`. `target-acceptance.json` verifies twenty accepted command steps, complete 432-case discovery on each target (nine Windows POSIX skips), generated clients/bootstraps, Rust fixtures, source/frozen native checks, thirteen Windows junction/DACL cases and all 50 Ubuntu ELFs against glibc 2.39. Ubuntu 24.04.5/Python 3.12.3 shares the Fedora kernel; Windows uses Python 3.12.16. Private Rust 1.99.1/Node 22.22.2 SDK archives were officially checksum-verified. All 61 production inputs match the prior Fedora media checkpoint; the two 261-file snapshots differ only in the Windows-only registration test mutex.

Preserve `ubuntu-validation/`, both Windows roots/bundles, all 68 retrieved Windows evidence files, SDK/snapshot receipts and every failure. Two empty uv locks were identity-verified and moved into preserved evidence; test temp directories are empty. Only the exact owned idle Ubuntu container was removed. The final read-only libvirt query timed out and was retained without retry; pinned Windows OS/disk/process and independent local audits pass. Windows stays on existing E: and Ubuntu remains stopped; recover missing Ubuntu VM controls before startup. Actual browser/desktop callers, defaults, revisions/events and permission reporting remain the next P04 task. These checks establish no new GUI, installed-browser or installer acceptance.
