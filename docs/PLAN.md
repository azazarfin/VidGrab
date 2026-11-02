# VidGrab desktop-companion plan

Status: P01 runtime implementation and checks are complete; its separate database-index cleanup is pending. The desktop application is not implemented.

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

### [ ] P01 — Isolate runtime state and establish deterministic checks

- [x] Add `backend/runtime.py` and `backend/app.py:create_app`; place runtime directories outside source/install and media outside application storage. Replace imported `DB_PATH` globals with injected configuration; isolate settings, queue, cookies, probes and connections.
- [x] Introduce schema versions 0/1 → 2 with transactional migration, snapshots/rollback, validated explicit legacy import and newer-schema rejection.
- [ ] Stop tracking the legacy runtime database in a separate reviewed cleanup, preserving and hash-verifying the local file.
- [x] Add `pyproject.toml`, `uv.lock`, hashed requirements and isolated tests with fake extractor/download/clock/process boundaries. Remove startup pip mutation and remote EJS fetches; make health/status local-only.
- [x] Cover partial settings, strict bounded/non-null integers and runtime/output-path compatibility. VidGrab's baseline is Python 3.11+; pinned yt-dlp 2026.6.9 itself declares >=3.10. Execution evidence is Linux/Python 3.14.8; do not invent Python 3.11 or Windows results.
- [x] Acceptance evidence: import/app creation and 35 deterministic tests perform no external requests or real downloads and preserve the legacy DB. A mocked settings check passes 12 viewport/tab combinations. Exact commands/results and limits are in [DEVELOPMENT.md](DEVELOPMENT.md) and [VALIDATION.md](VALIDATION.md).
- Suggested commit: `refactor(backend): isolate runtime state and deterministic app setup`.

### [ ] P02 — Prove packaged lifecycle and browser connection on both target OSes

- Add a minimal `desktop/` shell, packaged engine entrypoint, `native-host/`, and `packaging/` definitions. Ship a trivial local-fixture download before building the full manager.
- Bundle Python, yt-dlp, FFmpeg/ffprobe, Deno, and matching EJS support. Verify frozen imports and target-specific resource paths on Linux and Windows machines without developer tools installed.
- Prove tray/window behavior, single instance, engine crash recovery, and a browser-launched app that survives browser exit. Validate Windows process/job handling and Linux desktop/tray integration.
- Prove Chrome and Firefox native-host registration, stable extension IDs, and Firefox background imports. Evaluate the paired-loopback fallback for sandboxed browsers and an AppImage moved to another path.
- Acceptance: install, connect, download, close browser, reopen app, and uninstall on both OSes. Capture installer size/startup measurements and finalize ADR-001. A scaffold-only result does not pass this gate.
- Suggested commit: `feat(desktop): prove bundled engine and browser bridge lifecycle`.

### [ ] P03 — Make downloads durable and cancellation enforceable

- Extract queue/state-machine and retry policy from `backend/queue_manager.py`. Run each active extraction/download in an owned worker process; isolate FFmpeg descendants and stop them before terminal state or cleanup.
- Add resumable artifacts, per-item playlist jobs, idempotent enqueue, persisted retry deadlines, and crash recovery. Track final output paths from completed post-processing rather than guessed extensions.
- Add one request budget across probing, downloads, fragments, and retries; honor `Retry-After`, use jittered backoff, and distinguish transient errors from authentication/permanent failures.
- Acceptance: the deterministic interruption, cancellation, duplicate submission, throttling, and multi-output cases in VALIDATION pass on Linux and Windows.
- Suggested commit: `feat(engine): add durable jobs and bounded recovery`.

### [ ] P04 — Version the app/extension contract and synchronize settings

- Add `/v1` models and generated clients in `packages/protocol/`; introduce request IDs, idempotency keys, typed errors, settings revisions, capabilities, and reconnectable event snapshots.
- Implement the native bridge and authenticated internal API from the P02 spike. Keep native messaging separate from the long-lived download owner; add pairing/revocation for the optional loopback transport.
- Replace extension-side authoritative download defaults with app-owned settings. Synchronize browser preferences while reporting actual browser-granted permissions separately.
- Acceptance: two clients see consistent queue/settings state; stale updates conflict explicitly; reconnects do not duplicate jobs; incompatible versions produce an upgrade message.
- Suggested commit: `feat(protocol): unify desktop and extension control`.

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
