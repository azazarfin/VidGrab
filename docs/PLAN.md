# VidGrab desktop-companion plan

Status: P01 and P02 are complete for the selected Ubuntu 24.04/GNOME x86_64 and Windows 11 x64 preview baselines. Both platform reviews, packaged lifecycle/browser gates and measured size/startup results pass on 2026-11-10. Windows missing-WebView2 behavior uses reversible SDK discovery unavailability and an actual offline per-user install/render journey; physical runtime absence is not claimed. ADR-001 is accepted for this feasibility scope. P03 started on 2026-11-11 and its scoped backend acceptance passes on 2026-11-15. P04 is in progress: ten generated v1 operations, schema-10 action receipts and scoped volatile authentication support ordinary media and pass selected Fedora source/fresh-frozen checks. Actual client migration, changed target compatibility and final release remain separate gates.

## Approach

Turn VidGrab into a local desktop download manager with a browser companion. Keep the Python/yt-dlp investment, bundle the runtime and media tools, and make one application responsible for queue persistence, process lifetime, updates, and settings. Prove packaging and browser integration early, before building the full interface.

## Scope and decisions

- **User-confirmed:** first release on Linux and Windows; macOS follows. Preserve existing source in Git and provide an offline bundle for cross-device continuation.
- **First-release scope:** video/audio, selected playlist items, subtitles/thumbnails, direct media and supported HLS/DASH, authenticated downloads using explicitly shared browser context, queue recovery, scheduling, app-managed settings, and installation without a terminal or separate Python/FFmpeg setup.
- **Coverage boundary:** site support depends on extractors, accessible media, authentication, and server behavior. Report unsupported/DRM-protected media, login requirements, and throttling distinctly. Universal site support is not an acceptance criterion.
- **Later scope:** Safari extension, mobile clients, cloud account/download synchronization, general image-gallery scraping, and arbitrary recording of browser playback. Developer handoff across devices is included now; moving live downloads between devices is a separate feature.
- **Proposed stack:** Tauri 2 desktop shell, TypeScript/React desktop UI, packaged Python engine, SQLite, and a small native-messaging bridge. This is an engineering recommendation, subject to P02, not a user-selected framework.

## Ordered milestones

Each milestone ends with a focused commit and an updated [HANDOFF.md](../HANDOFF.md). Follow [ARCHITECTURE.md](ARCHITECTURE.md) for boundaries and [VALIDATION.md](VALIDATION.md) for evidence. Do not mark a milestone complete solely because its UI or package builds.

### [x] P00 — Preserve the baseline and implementation context

- Source checkpoint: `79f7d7e`; preserve current changes, including the existing sanitizer, without representing them as verified fixes.
- Record the architecture, decisions, release gates, known limitations, and next task. Commit the planning files and verify that a Git bundle can be cloned independently.
- Acceptance: the bundle reproduces the committed source and instructions; omitted local state is explicitly recorded.

### [x] P01 — Isolate runtime state and establish deterministic checks

- [x] Add `backend/runtime.py` and `backend/app.py:create_app`; place runtime directories outside source/install and media outside application storage. Replace imported `DB_PATH` globals with injected configuration; isolate settings, queue, cookies, probes and connections.
- [x] Introduce schema versions 0/1 → 2 with transactional migration, snapshots/rollback, validated explicit legacy import and newer-schema rejection.
- [x] Stop tracking the legacy runtime database in a separate reviewed cleanup, preserving and hash-verifying the local file. Database/sidecars are ignored; older commits remain unchanged.
- [x] Add `pyproject.toml`, `uv.lock`, hashed requirements and isolated tests with fake extractor/download/clock/process boundaries. Remove startup pip mutation and remote EJS fetches; make health/status local-only.
- [x] Cover partial settings, strict bounded/non-null integers and runtime/output-path compatibility. VidGrab's baseline is Python 3.11+; pinned yt-dlp 2026.6.9 itself declares >=3.10. Execution evidence is Linux/Python 3.14.8; do not invent Python 3.11 or Windows results.
- [x] Acceptance evidence: import/app creation and 35 deterministic tests perform no external requests or real downloads and preserve the legacy DB. A mocked settings check passes 12 viewport/tab combinations. Exact commands/results and limits are in [DEVELOPMENT.md](DEVELOPMENT.md) and [VALIDATION.md](VALIDATION.md).
- Implementation commit: `7ba96d0` (`refactor(backend): isolate runtime state and deterministic setup`). The separate cleanup checkpoint is identified in [HANDOFF.md](../HANDOFF.md).

### [x] P02 — Prove packaged lifecycle and browser connection on both target OSes

- [x] Add a minimal Linux `desktop/` Tauri/React shell, packaged fixture engine, `native-host/`, component locks, clean staging and resource-preparation commands.
- [x] Bundle Python, yt-dlp, FFmpeg/ffprobe, Deno and matching EJS on Linux. Frozen imports, checksum/version checks and a loopback WAV → MP3 fixture work with an empty `PATH`.
- [x] Prove Linux private IPC, concurrent single ownership, native-host-independent lifetime, bounded engine recovery, mutation reply handling and explicit process-group cleanup through nine native lifecycle cases.
- [x] Compiled native window and installed Chrome/Firefox handoff checks pass against the final frozen bundle; exact evidence is in VALIDATION. Mocked rendering passes three viewport sizes. Unicode directories, no-tray Close, repeated activation, engine recovery and explicit Quit are covered.
- [x] Implement Linux package resource discovery, a stable per-user native bridge and launch descriptor, relocation repair and registration removal. Eleven Rust cases and eight compiled headless `.deb` extraction checks pass; the initial Fedora preview is 131.76 MiB and requires glibc 2.42. That check simulates relocation and preserves state/media.
- [x] Rebuild on pinned **Ubuntu 24.04 LTS userspace**: fresh Python/support and native binaries fit glibc 2.39; 61 guarded Python, 11 Rust and nine native lifecycle cases pass. This shares the Fedora kernel.
- [x] Build actual Ubuntu `.deb` and a corrected AppImage; frozen tools, whole-artifact ABI, private registration/removal, actual image relocation and minimal-baseline install/removal pass. Restore the immutable frozen engine after linuxdeploy and audit the AppImage runtime too.
- [x] Finish actual package browser lifecycle: both UIs and the combined Chrome→Firefox reopen pass in both packages. Pre-cleanup diagnostics identified Xvfb stderr backpressure; a failing overflow regression now passes with a temporary log. Native launch deadlines/mutation semantics and package bytes are unchanged.
- [x] Exercise actual FUSE: mounted tools/resource discovery, registration/removal, compiled UI, both browser lifecycles and unmount after Quit pass in Ubuntu userspace on the Fedora kernel. The isolated test uses the OS FUSE helper with preserved Unix modes.
- [x] Validate both unchanged packages in a native Ubuntu 24.04.5/GNOME 46 KVM guest with its own 6.8 kernel: native Wayland, visible tray, compositor Close, hidden download, menu Open/Quit, activation/recovery and normal FUSE unmount. Virtual USB mouse/keyboard tray interaction also passes.
- [x] Repeat native Chrome→Firefox handoff with content sandboxes enabled and observed `Seccomp=2`/`NoNewPrivs=1`. Both packages retain active downloads after browser exit and completed history after restart. Firefox's AppArmor user-namespace warning limits the confinement claim.
- [x] Exercise guest-only CLI disconnection, apt `.deb` removal/reinstall and actual Unicode AppImage relocation/launch-path removal/restoration. Copied bridges report `APP_MISSING`; descriptors repair and completed state/media hashes are preserved.
- [x] Validate strict Firefox Snap through Ubuntu's WebExtensions portal and the canonical package bridge for both `.deb` and actual FUSE AppImage: real Allow input, activation, download after full browser exit, restored history, missing-app and disconnection. AppArmor enforcement and content seccomp/NoNewPrivs are observed; no fallback is needed in this tested configuration.
- [x] Evaluate Firefox Flatpak 159.0.1 with stock permissions: default portal preference `0` reports missing host; explicit integer `1` passes the same lifecycle in both packages. The user selected [guided native setup](BROWSER_CONNECTION.md) on 2026-11-08. Other configurations retain a conditional pairing decision; no fallback is implemented.
- [x] Rebuild the setup help into both Ubuntu previews and follow it through visible Number/Save, full Firefox restart, temporary add-on reload and real Allow in fresh Flatpak profiles. No preference preseed; package and confinement checks pass.
- [x] Implement and validate terminal-free Linux browser registration/status/Connect/Repair/Disconnect in both actual Ubuntu previews. Explicit disconnection survives reopen/relocation, blocks copied/live hosts, preserves active downloads/history and refuses foreign manifests. Native Chrome/Firefox, strict Snap and manual Flatpak handoff pass again.
- [x] Complete terminal-free Ubuntu preview installation/removal: Files/App Center `.deb` install/uninstall and managed AppImage/Applications install/remove/reinstall pass with preserved state/media and browser disconnection. Idle removal checks every unfinished job; actual-image file ownership/active-owner checks pass. See [the installation guide](LINUX_INSTALLATION.md) and the 2026-11-10 evidence in VALIDATION.
- [x] Record final-package startup measurements separately from registration and driver setup and complete the Linux ADR-001 acceptance review. Three fresh-state runs per final package pass: `.deb` median 0.854 s (0.839–2.034 s), FUSE AppImage median 1.478 s (1.456–1.760 s). These include the WebDriver launch/session command and observation overhead; no cold-cache or release latency budget is claimed. The Linux review supports retaining the proposed stack for Windows validation. KDE, other distributions and physical-device behavior need separate evidence before support claims.
- [x] Implement Windows native core and frozen tools: SID-private named pipes, kernel peer identities, single-owner lock, kill-on-close Job Objects, explicit handle whitelists and HKCU Chrome/Firefox registration in both views. 66 guarded Python/14 Windows Rust/five frozen native lifecycle checks pass; bounded EOF regression fails against the old bridge. Elevated Azaz execution does not establish standard-user acceptance.
- [x] Port/build the Windows Tauri desktop and per-user NSIS preview; actual standard-user silent install/resource discovery/WebView2 render and active-removal rejection pass. Windows/Linux renderer and native regression checks pass.
- [x] Complete actual Windows tray Close/Open/Quit and Chrome activation/download after full browser exit; preserve the same owner/engine and completed history. Job-context and loaded-bridge regressions pass.
- [x] Exercise the rebuilt NSIS preview through standard-user graphical same-version reinstall and Finish/Run; retain disconnection and restore registrations through real Connect input.
- [x] Complete actual Windows Firefox/content-token and browser-independent download/history checks; embed a trusted installer checker and pass eight real foreign/changed/missing/reparse destination refusals. The new receipt verifies the complete package before maintenance.
- [x] Complete fresh Windows GUI install/remove/reinstall in a folder with spaces/Bangla, exact-package active refusal, preserved history/media/disconnection and Unicode state/media/no-tray/recovery/reopen. Ten actual installer refusals and all 89 installed/archive files pass.
- [x] Complete Windows present/controlled-unavailable WebView2 behavior and final-package startup/size review. Actual SDK discovery fails with both machine keys hidden; the exact NSIS supplies a per-user runtime offline without elevation and renders through it before hash-verified restoration. Machine files remain; physical absence is a separate claim. Installer 301.96 MiB/installed files 308.88 MiB; three fresh explicit application runtimes with existing selected WebView/warm OS caches yield median 1.225 s (1.179–1.789 s). See HANDOFF for overhead, failed attempts and limits.
- [x] Acceptance: both selected preview OS baselines pass packaged installation/lifecycle/browser gates, measurements are recorded and ADR-001 is accepted for P02. No release/physical-hardware/additional-OS or physically runtime-free Windows claim is added. Both guests shut down normally; protected state/media/assets remain. P03 continues under the milestone below.
- Suggested commit: `feat(desktop): prove bundled engine and browser bridge lifecycle`.

### [x] P03 — Make downloads durable and cancellation enforceable

**Accepted on 2026-11-15 for the selected controlled backend fixtures.** Schema 8 covers owned attempts, verified publication/direct/static-fragment prefixes, fresh-auth waits, playlist children/idempotent enqueue and persisted retry/request authority. The [acceptance matrix](VALIDATION.md#p03-scoped-acceptance-review--2026-11-15) maps every P03 case to hash-verified Linux/Windows evidence and six added deterministic checks. Coverage comprises the prior 355 guarded cases plus six additions (361 total, nine Windows POSIX skips); the thirteen affected output/retry cases pass on each platform. Runtime/frozen inputs are unchanged. [ADR-024](DECISIONS.md#adr-024--scoped-p03-backend-acceptance) records the decision and limits.

- [x] Extract queue/state-machine and retry policy from `backend/queue_manager.py`. Ordinary attempts use owned worker processes; descendants exit before terminal state or cookie cleanup. Explicit diagnostic adapters retain joined threads.
- [x] Add verified resumable artifacts, bounded per-item playlist jobs, keyed idempotent enqueue, persisted retry deadlines and fenced crash recovery. Concrete post-processing records determine final output paths.
- [x] Share request authority across probing, downloads, fragments and retries; honor numeric/date `Retry-After`, jittered backoff and finite budgets; authentication/permanent failures do not retry automatically.
- [x] Acceptance: deterministic interruption, cancellation, keyed duplicate submission, throttling and multi-output/disk-full cases pass on Linux and Windows within the recorded scope. Expired-format reprobe remains explicitly P05; browser authentication UI, arbitrary parent races, power loss and final release require separate gates.

### [ ] P04 — Version the app/extension contract and synchronize settings

**Foundation and ordinary-media control implemented on 2026-11-15; P04 remains open.** [ADR-026](DECISIONS.md#adr-026--p04-ordinary-media-and-scoped-authentication) and [the media checkpoint](VALIDATION.md#p04-ordinary-media-actions-and-authentication--2026-11-15) extend [ADR-025](DECISIONS.md#adr-025--p04-contract-and-settings-foundation). All 432 guarded Fedora cases, generated clients, Rust/renderer checks and thirteen source/fresh-frozen native lifecycle cases pass; changed Ubuntu/Windows/browser/GUI inputs are not certified.

- [x] Add import-safe `/v1` models/schema and generated module/classic/TypeScript clients plus Rust validation inputs; implement IDs, stable mutation keys, typed errors, schema-9 revisions/receipts, capabilities and bounded reconnect snapshots.
- [x] Carry six fixture-only operations through compatible native framing and the authenticated internal API. Negotiate capabilities before versioned mutations, retain independent ownership and hide packaged legacy routes/WebSockets.
- [x] Add ordinary-media probe/actions/fresh-auth operations, schema-10 action receipts and explicit safe per-job/volatile scoped auth-context models; enable ordinary native execution while retaining the guarded fixture endpoint.
- [ ] Migrate extension/desktop callers and authoritative download defaults to the app. Synchronize browser preferences while reporting actual granted permissions separately.
- [ ] Validate changed source/frozen/native and actual client behavior on both selected target baselines. Pairing/revocation remains an unimplemented optional transport; advertise it as disabled.
- Acceptance remains: both actual clients see consistent queue/settings state; stale updates conflict explicitly; reconnects do not duplicate jobs; incompatible versions give an upgrade message.
- Foundation commit: `feat(protocol): add versioned control and settings revisions`. Media-control commit: `feat(protocol): add ordinary media and scoped authentication`.

### [ ] P05 — Expand detection and make failures actionable

- Add a normalized media-candidate model covering page/frame URL, media URL, observed content type, title, and browser context. Handle SPA navigation, nested frames, audio/source elements, and deduplication.
- Add permission-gated observation of media request metadata and HLS/DASH manifests. Keep signed URLs intact; treat `blob:` as a browser-local hint, not a downloadable URL.
- Use the same scoped cookie/header context for probe and download, coalesce identical probes, bound caches, and re-probe expired formats once. Return useful choices for login, retry time, unsupported media, and engine updates.
- Acceptance: controlled fixtures pass for direct media, HLS, DASH, cookies, referer, redirects, expiring URLs, and playlists. A separately recorded live-site matrix measures current compatibility.
- Suggested commit: `feat(extension): add context-aware media discovery`.

### [ ] P06 — Build the standalone manager and connection UX

- Add app screens for Add URL/batch, active queue, history, settings, browser connections, and diagnostics. Support file/folder selection, priorities, retry, cancel, schedule, and capability-aware pause/resume.
- Provide first-run dependency checks, background/login toggles, clear Quit behavior, and recovery after engine failure. Keep a visible-window fallback when a system tray is unavailable.
- Reduce the extension to detection, format selection, quick queue actions, status, and Open in app; retain usable behavior when the app is missing/offline or permissions are denied.
- Acceptance: the app works without the extension, downloads survive popup/browser closure, and both clients reflect changed settings without reopening.
- Suggested commit: `feat(desktop): add download manager and companion onboarding`.

### [ ] P07 — Release Linux and Windows with repeatable updates

- Add native CI builds and installer checks: Linux x86_64 AppImage + Debian package; Windows x64 per-user installer. Pin component versions, checksums, build inputs, and protocol compatibility.
- Replace destructive source packaging with clean staging; create Chrome/Firefox artifacts from shared configuration plus browser-specific overlays. Test both installed packages, not just unpacked sources.
- Add signed application/engine-bundle updates with rollback, migration backups, third-party notices, and redacted diagnostics. Import legacy configuration and explain how to stop an existing legacy service.
- Acceptance: [the release matrix](VALIDATION.md) passes on clean target machines; update/uninstall preserves user media; a request to an unavailable update server does not prevent local use.
- Suggested commit: `build(release): package and validate Linux and Windows`.

### [ ] P08 — Extend the same architecture to macOS

- Build native Apple Silicon and Intel artifacts where dependencies remain supported; validate WKWebView, file dialogs, tray/menu behavior, native-host registration, and process cleanup.
- Add Developer ID signing/notarization and DMG distribution; record exact supported OS versions from actual CI and device validation.
- Acceptance: the same lifecycle/download suite passes on a Mac. Safari extension work remains a separate milestone.
- Suggested commit: `build(macos): add signed companion distribution`.

## Follow-up decisions at the relevant gate

- P02: exact minimum OS versions, Linux distro baseline, ARM64 priority, and sandboxed-browser support validated on available machines.
- P05/P06: retain the current Bangla-to-Latin behavior as an optional filename preference or remove it; do not silently change it while moving modules.
- P07: release hosting, store extension IDs, signing credentials/budget, and project license ownership (README says MIT but no LICENSE file is currently tracked).

These do not block P01. Record answers in [DECISIONS.md](DECISIONS.md) instead of relying on chat history.
