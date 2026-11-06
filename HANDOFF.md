# Resume VidGrab work here

This file is the current development checkpoint. It is plain Markdown for any agent/editor; chat history and a particular agent application are not required.

## Current state

- **Phase:** P01 complete; Ubuntu 24.04 P02 userspace previews built and checked on 2026-11-06; combined Firefox reopen remains failing. The user resumed on 2026-11-04 and chose **Linux work now, Windows validation later**. Full P02 and ADR-001 remain open; P03 has not started.
- **Branch:** `planning/desktop-companion`.
- **Checkpoint:** the Ubuntu packaging helpers, queue wakeup regression and partial package evidence are committed with this handoff; resolve the commit with `git log -1 --format=%H -- HANDOFF.md`. This is a WIP P02 checkpoint with a recorded browser failure. Keep the dirty ZIPs/banner excluded; no replacement Git bundle was requested.
- **Linux baseline:** the user selected **Ubuntu 24.04 LTS** on 2026-11-06. The rebuild uses pinned Ubuntu userspace on the Fedora host kernel. Its bundled interpreter/support and native binaries now fit glibc 2.39; a complete Ubuntu desktop acceptance gate remains open.
- **Source checkpoint:** `79f7d7e2fecdbd4623dfc0b6d6632daffe349809`, based on `822b9f2`.
- **Planning checkpoint:** `f47ce25`. **P01 implementation:** `7ba96d0` (`refactor(backend): isolate runtime state and deterministic setup`). **P01 cleanup checkpoint:** `cf7ca96`. **Current checkpoint:** the latest commit affecting this file; resolve with `git log -1 --format=%H -- HANDOFF.md`. Existing bundle receipts identify their exported HEAD; the old planning bundle predates P01 and has not been refreshed.
- **Confirmed:** Linux and Windows first; macOS later. Source snapshot plus offline Git bundle selected. No Git remote is configured.
- **Implemented for the Linux spike:** Tauri 2 + TypeScript/React connection window, bundled Python/media tools, Rust engine owner and native bridge. **Still proposed:** final framework acceptance, Windows lifecycle, per-job workers, `/v1` and release installation/update flows.

## Read order

1. [AGENTS.md](AGENTS.md): current commands, gotchas, mandatory API-call minimization.
2. [docs/PLAN.md](docs/PLAN.md): milestone checklist and acceptance criteria.
3. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): ownership, protocol, lifecycle, reliability and packaging design.
4. [docs/DECISIONS.md](docs/DECISIONS.md): confirmed choices versus recommendations and pending decisions.
5. [docs/VALIDATION.md](docs/VALIDATION.md): current evidence and future test/release matrix.

## Next incomplete P02 task

Investigate the reproducible **Firefox reopen failure after the Chrome lifecycle** against the actual Ubuntu `.deb` and AppImage. Both compiled package UIs, frozen MP3/recovery/Quit and Chrome close/reopen pass. Firefox alone passes, and its first combined-session download completes; after explicitly stopping the owner and reopening Firefox, native activation times out. Capture the new desktop process, engine log and launch environment **before browser cleanup**; the current outer diagnostic runs after cleanup and misses the failing process. Do not extend timeouts or replay mutations as a guessed fix.

Run `tests/check_packaged_desktop.py --browser both` in the sealed Ubuntu test image after the fix. Then exercise a real FUSE mount, clean Ubuntu GNOME/KDE tray/Wayland and complete install/uninstall interaction. The userspace/container checks share the Fedora kernel and do not certify those gates. Windows x64 stays deferred and unimplemented; keep P02/ADR-001 open and do not start P03.

Current scratch root: `/tmp/vidgrab-ubuntu-p02-f7mg65zi`. Frozen staging is `bundle/`; Ubuntu `.deb` is `source/target/release/bundle/deb/VidGrab_0.1.0_amd64.deb`; the **corrected** AppImage is `appimage-final/VidGrab_0.1.0_amd64.AppImage`. The image still under `source/target/release/bundle/appimage/` is the original linuxdeploy output with modified Deno bytes and must not be used as a passing package.

Receipts are `deb-headless.json`, `appimage-headless.json`, `clean-install-and-removal-resume.json`, `package-final-clean-image.json`, `package-runtime-deb-firefox.json`, and `package-runtime-final-{deb,image}-both.json`. The last two contain passing UI/Chrome phases and failing combined Firefox results. Screenshots are under `package-ui-final-*`; fixture homes are `validation-home-final-*`. These ignored/ephemeral artifacts need rebuilding on another device. The old Fedora bundle and `.deb` remain historical previews.

Do not run installers, rebuild `dist/`, launch real downloads, publish a remote or export another bundle without the corresponding request.

## Implemented P02 Linux interfaces

- `backend.engine` is an import-safe frozen entrypoint. A private stdin bootstrap supplies a fresh token and selected data/media paths; readiness reports the ephemeral loopback port and verified component versions. The wrapper exposes only authenticated `/health`, `/spike/status` and `/spike/fixture`, rejects browser Origin/wrong Host/oversized bodies and hides legacy routes/WebSockets. Its audit guard forbids external connections.
- `desktop/supervisor` owns private same-user Unix IPC, a single-instance file lock and the engine process group. It performs at most three restarts after the initial engine launch, then exposes a failed state. Quit terminates/reaps owned descendants before releasing the endpoint/lock. Individual worker/cancellation/artifact semantics are still P03.
- `native-host` reads adjacent `host-config.json`; browser framing is four native-endian length bytes plus UTF-8 JSON, capped at 64 KiB. `spike_version: 1`, request ID, operation and parameters are checked. Allowed browser operations are `status`, `activate`, `fixture.download`; local desktop IPC additionally permits Quit. Activation uses a separate POSIX session; mutations are sent once and dropped replies return `REQUEST_OUTCOME_UNKNOWN`.
- `desktop/src` has the minimal connection/fixture/history view with bundled fonts. Connection errors clear after recovery while action errors remain until another action. No-tray window fallback, explicit Quit and engine recovery are part of the native-window check; a real tray/Wayland desktop remains a separate gate.
- Direct Cargo builds default to `custom-protocol` and embed the frontend. Download result paths pass unchanged through the downloader and queue, preserving existing Unicode directories; title sanitization and the existing filename policy remain unchanged. Full post-processing artifact tracking is still P03.
- Both manifests grant native messaging. Chrome's public development key fixes ID `ikjpekhbbaehdhigakbahlhafaojbjpm`; Firefox keeps `vidgrab@azaz.local`, grants alarms and loads shared classic scripts explicitly. `vidGrabNative.send` is diagnostic; popup/options/download UI still uses the legacy `vidGrabAPI` until P04.
- `packaging/components.lock.json`, `fetch_components.py`, `engine.spec`, `stage_spike.py` and `prepare_desktop.py` provide checksum-enforced tools, a complete PyInstaller support tree, fresh staging and target-suffixed Tauri resources. All selected paths are explicit; no real user runtime or registration is chosen by a check. Licensing/release obligations remain in [packaging/THIRD_PARTY.md](packaging/THIRD_PARTY.md) and P07.
- Packaged Linux launch now discovers Tauri engine resources and the sibling native sidecar. A copied bridge under `XDG_DATA_HOME/vidgrab-p02/native` reads a strict `package_version: 1` descriptor with the stable app target and selected state/media, without an engine mount path. Reopening a moved image repairs the target and retains selected paths. The native host uses `--package-config` for this mode and preserves explicit `--config` staging behavior.
- Default preview state/media are `XDG_DATA_HOME/vidgrab-p02/state` and `~/Downloads/VidGrab-P02`, separate from the legacy runtime. Headless `--check-package`, `--register-browser-host` and `--unregister-browser-host` are documented in DEVELOPMENT; tests select temporary home/XDG paths. Removal preserves user files and the descriptor/bridge. Foreign manifests and symlink destinations are refused; no terminal-free connection UI or full uninstaller is implemented yet.

## Implemented P01 interfaces and decisions

- `RuntimeConfig(data_dir, download_dir, logs_dir=None, temp_dir=None)` resolves paths without creating them. `from_env()` supports the four `VIDGRAB_*_DIR` overrides. User data is outside the repository by default; stored download preferences override only the default media directory.
- `backend.app.create_app(runtime=None, start_queue=True, probe_runner=None, download_runner=run_download, clock=time.time, open_folder=...)` owns all mutable service state. Probe fakes receive `(url, runtime)`; download fakes receive keyword options including `runtime`. Factory/import does not configure logging or start I/O. Lifespan performs initialization; `start_queue=False` disables execution.
- `backend/db/migrations.py` uses SQLite `user_version`: versions 0/1 migrate to 2. Snapshots include committed WAL data; failures roll back; newer schemas and invalid settings are refused. `python -m backend.db <source> --data-dir <new-dir>` imports a selected backup/staged copy without starting workers or replacing destination state. Final-path validation is repeated before atomic publication.
- Settings PATCH merges supplied fields under a transaction. Integers are strict/bounded/non-null. Output-path validation is shared by initialization, PATCH, enqueue and execution; it protects source/install and data/log/temp storage, resolves existing symlinks, preserves safe nested/absolute templates and rejects invalid resumed jobs without invoking cleanup there.
- `/health` is local readiness/schema; `/status` retains legacy extension fields without an update lookup; `/update-ytdlp` returns 409. Runtime pip upgrades and remote EJS fetching are disabled. The legacy update preference/private functions remain only for compatibility; P01 did not bundle media tools. The P02 fixture engine now has its own verified tool bundle.
- `pyproject.toml`, `uv.lock` and hashed exported runtime requirements are present. VidGrab requires Python 3.11+; the selected `yt-dlp==2026.6.9` metadata requires >=3.10. This is a project baseline, not a claim that the pin itself requires 3.11.
- Settings keep the incumbent dark/violet identity. Contrast, heading hierarchy, narrow-window clipping, focus/state announcements, warning styling and tiny helper text were fixed. Value-only Inter detector exceptions preserve the existing typeface in the settings HTML and CSS only; the user approved the CSS scope after P01. No file/rule-wide ignores were added.

Still later work: per-job process cancellation/durability, bounded retries, final artifact/sanitizer handling, generated/revisioned protocol and release/Windows integration. The cookie description still incorrectly claims no disk writes; its wording remains unchanged. Actual cookie files are temporary and excluded from persisted job options.

## What is preserved and what is local

Source commit `79f7d7e` captures the pre-existing retry/output/sanitizer changes, removal of hardcoded browser-cookie paths, `.gitignore`, and staged bytecode cleanup. It is a work-in-progress baseline, not a claim those features are correct.

The user approved leaving these current edits local; all were preserved during P01:

| Path | Meaning |
| --- | --- |
| `backend/vidgrab.db` | Preserved, ignored local settings/history/queue database; removed from the current Git tree only |
| `dist/vidgrab-chrome-extension.zip` | Locally generated archive |
| `dist/vidgrab-firefox-extension.zip` | Locally generated archive |
| `store_assets/vidgrab_promo_banner.png` | Modified promotional image |

Their earlier tracked versions remain in Git history and therefore in a full-history bundle. No current working copy has been added to either P01 commit; the cleanup only removes the database's index entry. A fresh checkout of the completed P01 tree does not contain that database. The bundle does not contain the latest working copies, ignored venv/scratch files, installed services or live download state. P01 no longer uses the source DB by default; explicitly importing real legacy state can still resume its jobs on later startup.

## P02 Ubuntu userspace checkpoint — 2026-11-06

- Pinned base image: `docker.io/library/ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852`; observed Ubuntu 24.04.5 LTS/glibc 2.39, Python 3.12.3, Rust/Cargo 1.99.1 and Node 22.22.2. Builds use Ubuntu development packages and a fresh Python freeze; the Fedora kernel is shared.
- **61 guarded deterministic tests passed** on Ubuntu/Python 3.12.3 and Fedora/Python 3.14.8. **11 Rust cases and nine native lifecycle cases passed** on Ubuntu. The enqueue/idle race fails against the committed queue and passes after moving event clearing before the database read. Seven AppDir cases cover original bytes/support symlinks and unsafe/incomplete outputs; the dangling-output case failed before its guard.
- Staging: 318,515,321 bytes before later desktop copying; freeze/stage 18.705 s, self-check 0.633 s, desktop rebuild 58.651 s. Audit: 51 staged ELFs need at most glibc 2.39.
- Ubuntu `.deb`: **135,542,888 bytes / 129.26 MiB**, SHA-256 `c865903c793e089deca74fa15f7364671d2e623ecb02bd304ddeb1e4d14e22ef`. Eight headless checks pass; all 50 shipped ELFs fit glibc 2.39. Fresh install, empty-PATH frozen checks, private registration/disconnection and package removal preserve seeded fixture files with no installed Python/FFmpeg/Deno/Node/Cargo.
- Corrected AppImage: **202,807,800 bytes / 193.41 MiB**, SHA-256 `97803a020ad48d659663ae3454f091276d5a78a01364505b94b5dce081b309b2`. Seven headless checks pass in 4.399 s, including actual file relocation and preserved state/media. All **235 ELFs including the embedded runtime** fit glibc 2.39. A fresh minimal baseline also passes extraction, frozen tools, registration and removal with no developer/media runtimes installed.
- linuxdeploy changed **46 frozen ELF files**, including Deno's RUNPATH and checksum. `packaging/prepare_appimage.py` restores the entire original staged engine into a fresh AppDir, audits it and checks components before image assembly. The integrity manifest stays unchanged. Final assembly reuses the raw image's 944,632-byte runtime, SHA-256 `912e3f80ad18723bdf5da6a52d5301588012090a56bcb0919cce170a47d8de5b`, through appimagetool's `--runtime-file`; the offline finalization/check took 39.596 s. The AppRun wrapper needs OS `bash` through `/usr/bin:/bin`; the frozen engine retains its independent empty-PATH check.
- **Both compiled package UIs and Chrome 157.0.8103.38 pass** MP3, no-tray Close, repeat activation, recovery, explicit Quit and browser close/reopen. Wry is 0.57.1. Registered-package → connected-UI samples: `.deb` **1.085 s**, AppImage **1.445 s**; registration/driver/container preparation and cold-install budgets are excluded. Completed screenshots were reviewed.
- **Firefox 159.0/Gecko 0.37.1 passes alone**. The Chrome→Firefox sequence fails reproducibly at Firefox's second activation in both packages. Its first download completes; the reopened host reports app readiness timeout and cleanup IPC also times out. No native timeout/retry behavior was changed. `tests/check_packaged_desktop.py` checkpoints each phase and captures failure diagnostics; pre-cleanup capture is the exact next task.
- Nested bwrap is denied even after the local capability probe. GUI/browser checks instead use an outer Podman container with private network/PID/IPC/home/tmp, a private D-Bus session, Xvfb and Mesa software rendering. Persistent fixture homes stay under the scratch root. This is userspace/X11 evidence; FUSE, physical tray, Wayland, sandboxed browsers and Windows remain open.
- Preserved legacy DB is still SHA-256 `148dbba8a3bea4f01b0e6dbbca7faf157dea157ff9300f4251a6c47f56c38f8f`; the two dirty ZIPs/banner stay excluded. No real user registration/runtime/service, public download, legacy import, remote push or new bundle was used.

## P02 checkpoint revalidation — 2026-11-06

- The user selected Ubuntu 24.04 LTS for the clean Linux rebuild; ADR-010 records the decision without marking its acceptance gate complete.
- **49 deterministic Python tests passed** in 1.416 s with the approved local-IPC sandbox exception and all external-request/process guards retained. **11 Rust registration/discovery tests passed** with locked offline Cargo.
- **Eight headless extracted-package checks passed** again against the unchanged `.deb` SHA-256 `ad0ba926aca2b776f75a7676b9471abf31562d15cc3a84f0f86520163803dca2`. Extraction was 1.085 s and the total headless check 2.441 s; the receipt is `/tmp/vidgrab-p02-deb-resume-20261106.json`. All 82 shipped ELFs still require at most glibc 2.42. These are Fedora checks, not clean Ubuntu or live package evidence.
- Source review and `git diff --check` passed. The preserved legacy DB remains SHA-256 `148dbba8a3bea4f01b0e6dbbca7faf157dea157ff9300f4251a6c47f56c38f8f`; both dirty extension ZIPs and the promotional banner remain excluded from the 14-path checkpoint.

## P02 package verification — 2026-11-05

- **11 Rust tests passed**, plus **five guarded Python packaging tests**. Ten new registration/discovery cases failed before implementation; the additional AppImage-in-home regression failed before its fix. Existing staged host descriptors remain supported. No application/backend/extension UI source was changed beyond the Rust launch/bridge integration.
- Native desktop/owner/host release builds passed. The old temporary SDK was gone; verified installed GTK/WebKit/tray runtime libraries and pre-generated Rust bindings supplied private linker metadata under `/tmp/vidgrab-link-metadata`. This is Fedora development evidence, not a clean SDK/baseline. Tauri's equivalent Cargo normalization was restored after bundling.
- Fresh frozen engine self-check passed with empty `PATH`. Final staging is **331,583,087 bytes** before its receipt; freeze **21.117 s**, self-check **0.652 s**, metadata desktop rebuild **17.258 s**. Locked runtime/components are unchanged; offline `uv lock --check` passed.
- Actual `.deb` built: **138,158,710 bytes / 131.76 MiB**, final bundle invocation **7.855 s**, SHA-256 `ad0ba926aca2b776f75a7676b9471abf31562d15cc3a84f0f86520163803dca2`. The artifact is a fixture-only development preview, not a clean-install release.
- **Eight extracted-package checks passed** in a temporary home: compiled Tauri resource discovery, frozen self-check, private native registration, origin/frame rejection, foreign-manifest refusal, simulated AppImage relocation preserving selected paths, registration removal preserving fixture files and whole-bundle glibc coverage. Headless check **2.204 s**, extraction **0.971 s**; no GUI or runtime server was started.
- ABI inventory covers **82 ELF files**. Bundled Python `termios` and `libtinfo` require **glibc 2.42**, versus **2.39** for the Rust binaries. The audit rejected the initial >=2.39 declaration; metadata now accurately declares >=2.42. Minimum supported OS/baseline selection is still pending.
- Actual AppImage bundling failed downloading the uncached AppRun helper after its cache was redirected to `/tmp/vidgrab-tauri-cache`; DNS is unavailable. No AppImage exists. Logs/receipt are under `/tmp/vidgrab-p02-*`; they do not transfer with Git.
- Socketpair and loopback bind preflights failed EPERM, as did bwrap network setup. The full 49-test suite, native lifecycle, GUI/browser, tray/Wayland and installer checks were not rerun; external guards were retained. No real registration, runtime, service, public download, installer, remote push or replacement Git bundle was used.
- Preserved legacy DB remains SHA-256 `148dbba8a3bea4f01b0e6dbbca7faf157dea157ff9300f4251a6c47f56c38f8f`. The two dirty extension ZIPs and promotional banner remain deliberately excluded. Full commands, evidence and limits are in DEVELOPMENT/VALIDATION; ADR-009 records the preview layout decision.
- Final source review, new Python AST, Tauri JSON, four Cargo TOML files, 27 local Markdown links and `git diff --check` passed. Intended checkpoint scope is 14 source/test/documentation paths; generated artifacts and the three pre-existing dirty assets are excluded.

## Earlier P02 verification — 2026-11-04

- Fedora 43 x86_64; Python 3.14.8, Rust 1.99.0, Node 22.22.2; PyInstaller 6.22.3, Tauri Rust 2.12.5/JS 2.12.4, React 19.3.1. System GTK 3.24.52, WebKitGTK 2.54.2 and GLib 2.86.5. These are observed versions, not minimum supported baselines.
- **49 deterministic tests passed** with the approved local-IPC exception and external-request guards retained. Includes authenticated fixture API, frozen installation protection, app-owned tool overrides, archive checksums/extraction, complete resource preparation and literal Unicode paths through downloader/queue completion. Both path regressions failed before their fixes. Offline `uv lock --check` passed; no runtime dependency export changed.
- **Nine native lifecycle cases passed** with empty `PATH`: concurrent ownership, invalid origins/frames/operations, WAV → MP3 after native-host exit, completed state after reopen, bounded engine recovery, preserved engine errors, no replay after a dropped mutation reply and reaping a descendant that ignores SIGTERM.
- Mocked desktop rendering passed **1280×900, 620×600 and 390×720**, including connection recovery, action-error retention, fixture completion and overflow. Production TypeScript/Vite build passed; no remote fonts or HTTP requests were allowed.
- Isolated installed **Chrome 157.0.8103.38** and **Firefox 159.0** with Gecko driver 0.37.1 passed stable identity/background imports, missing registration, app launch, active download after browser exit, MP3 completion and state after reopening. Chrome uses a custom profile with its own `NativeMessagingHosts`; Firefox source was temporarily packaged as an XPI. Real user/system registrations were masked. Driver archive SHA-256 was verified before use. The separate mocked extension bootstrap check passed for both manifests.
- Compiled Tauri/Wry **0.57.1** native UI passed embedded frontend loading, no-tray Close fallback, repeated activation, rejected external fixture URL, frozen WAV → MP3 in a directory containing spaces/Bangla, engine recovery and explicit Quit. Empty/completed screenshots were reviewed. Native GUI/browser checks use bwrap network/PID/private-home isolation, a private D-Bus session and Xvfb/Mesa software rendering. They are not physical tray/Wayland, Windows, store package or clean-installer evidence.
- Final combined staging receipt reports **331,583,567 bytes unpacked** before writing the receipt, self-check **0.648 s**, build **18.310 s**; the native desktop binary is **11,710,040 bytes**. Staging reused the already-built desktop executable. These are development-machine staging measurements, not installer size or end-to-end startup acceptance budgets.
- Final checkpoint checks parsed **38 Python files, eight JSON files and five TOML files**, checked **eight extension JavaScript files** and **27 local Markdown links**, and passed `git diff --check`. Frozen runtime requirements still match the committed export apart from comments. No behavior changed after the passing native/default checks; final documentation and staged scope were checked separately.
- Legacy DB SHA-256 remains `148dbba8a3bea4f01b0e6dbbca7faf157dea157ff9300f4251a6c47f56c38f8f`. Both dirty `dist` ZIPs and the promotional banner are deliberately excluded. No installer, real runtime, public download, push or replacement bundle was run.

## Earlier P01 evidence — 2026-11-03

- Platform: Linux x86_64, Python 3.14.8, yt-dlp 2026.6.9; Chrome 155.0.8059.84 and Playwright 1.64.1 for the optional UI check.
- `.venv/bin/python -B -m unittest discover -s tests -v`: **35 tests passed** with the approved local-IPC sandbox exception. Includes import from a read-only copied install, isolated apps, partial/numeric settings, output-path bypasses, migration rollback/WAL/final-target import and fake worker/cookie cleanup.
- `uv lock --check --offline --cache-dir /tmp/vidgrab-uv-cache --python .venv/bin/python --no-python-downloads`: passed. A frozen offline export matches `backend/requirements.txt` apart from its generated command header.
- Opt-in `tests/check_options_ui.py`: all four tabs at 1280×900, 768×900 and 390×844 (**12 combinations**) passed; no page errors or tested overflow; minimum sampled contrast **4.86:1**. Backend calls were mocked, Google Fonts/network blocked, and an isolated profile used. Before/after screenshots were reviewed in `/tmp/vidgrab-ui-before.n3IxAO` and `/tmp/vidgrab-ui-after.9z4AsN`; these ephemeral images are not in Git.
- Syntax: 21 Python files parsed, all 7 extension JS files checked, both manifests and `pyproject.toml` parsed; `bash -n installer/install.sh build.sh` passed. `backend.db --help` passed earlier in this P01 run. Whitespace and local Markdown-link checks passed for the final cleanup.
- Index cleanup: database/sidecar ignore rules and explicit staged scope checked; the preserved database and both ZIPs/promotional image were rehashed unchanged. No application code changed after the recorded 35-test run, so the deterministic suite and UI checks were not repeated for this documentation/index-only cleanup.
- Impeccable: real P01 findings fixed; the targeted detector reported no findings after the HTML Inter exception. This is a focused settings check, not a full WCAG/performance certification or an audit of the untouched popup.
- Post-P01 hook triage, user-approved on 2026-11-03: `impeccable hooks ignore-value overused-font Inter --shared --file extension/options/options.css` added the CSS-only waiver with an approval rationale. Targeted CSS and HTML scans reported no findings; CSS scanning is regex-only, not rendered contrast/accessibility evidence. Only detector config/checkpoint notes changed, so backend/UI checks were not repeated. P02 was unstarted at that checkpoint.
- Not performed: public-site downloads, real legacy import, installer/service/build execution, packaged/frozen engine tests, installed Chrome/Firefox extension integration, Windows, physical touch or desktop tests. No push or replacement bundle was requested.

## Resume on another agent/device

Copy the `.bundle`, its checksum, and export receipt from `.handoff/` to the other device. The folder is ignored; it is not itself committed. Git is sufficient to restore the repository; recreate language environments only when needed.

```sh
git clone -b planning/desktop-companion /path/to/vidgrab-handoff.bundle VidGrab
git -C VidGrab rev-parse HEAD
git -C VidGrab status --short --branch
```

Compare HEAD with the receipt. Open `VidGrab` in the chosen agent app and say:

> Read AGENTS.md and HANDOFF.md, then continue the next incomplete task in docs/PLAN.md. Preserve recorded decisions and local work, batch API/tool calls, and update the handoff with verification and a commit at the next checkpoint.

On the same device, open this repository and use that prompt without cloning. The receipt records the branch for future exports if it differs from this initial branch.

## Checkpoint and export protocol

1. Record current milestone/subtask, decisions, changed interfaces, actual checks/results, blockers, and the exact next action here. Update the plan checkboxes and decision log only when their status changed.
2. Review `git status` and stage explicit intended paths. Commit coherent work with its tests/docs; label incomplete checkpoints as WIP. Never rely on an uncommitted source file for a portable handoff.
3. List any deliberately excluded local files. Verify no unexpected dirty source remains; Git bundles include commits, not unstaged/staged edits. Do not use `git add .` to sweep runtime state into a checkpoint.
4. Create a fresh export outside the source tree (commands work in a shell with Git on each platform):

```sh
git rev-parse HEAD
git branch --show-current
git bundle create ../vidgrab-handoff.bundle --all
git bundle verify ../vidgrab-handoff.bundle
```

5. Save HEAD/branch, SHA-256 of the bundle, validation results, and omitted local paths in an adjacent receipt; transfer both. Clone to a new directory and compare HEAD before treating a handoff as complete. Keep the previous bundle until the new one verifies.
6. When a remote is chosen, add its actual user-provided URL and push the checkpoint branch; a new device can clone it instead. Local commits alone are not remote backup.

Prefer one commit per completed subtask/milestone; include HANDOFF changes in that commit so code and context travel together. If interrupted mid-task, explicitly name the unfinished step and known failing checks instead of marking the whole milestone done.
