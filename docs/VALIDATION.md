# Validation and release gates

## Evidence at the planning checkpoint

Source snapshot: `79f7d7e`. Checks performed without starting the backend:

- Parsed 11 Python source files with `ast.parse`.
- Checked 7 extension JavaScript files with `node --check`.
- Parsed both extension manifests as JSON.

These establish syntax only. No current download, retry, cancellation, GPU, browser integration, installer, or desktop behavior is certified. `backend/test_backend.py` is a network-dependent sanity script; `test_gpu.py` imports a missing module. Planning-link and portable-bundle checks are recorded in [HANDOFF.md](../HANDOFF.md) and the export receipt.

## Deterministic suite to introduce in P01–P05

Use temporary runtime directories/databases and a local HTTP fixture server. External network access should fail the default suite; inject extractor, clock, randomness, process, and update boundaries where needed. Keep tests tied to observable behavior, not the internal refactoring shape.

| Area | Required cases |
| --- | --- |
| Runtime | Import/app creation has no mutation; user-state location injectable; read-only install folder; no writes to repository DB |
| Migrations | Empty DB, legacy DB, each prior schema version, interrupted migration, backup/rollback, invalid data |
| Settings | Partial PATCH preserves other fields; null/out-of-range numbers rejected; concurrent revisions conflict; UI and extension agree |
| Queue | Duplicate enqueue has one effect; stable scheduling with fake time; restart mid-attempt; one owner; terminal states not overwritten by late progress |
| Cancellation | Cancel during extraction/download/FFmpeg; worker and descendants exit; cookies cleaned afterward; unrelated job artifacts untouched |
| Resume/output | Interrupted range download; fragment resume; changed validators; non-resumable server; remux/audio final path; multiple outputs; disk full |
| Throttling | 429 with numeric/date `Retry-After`; 503/timeout backoff; 401/403 requires action; expired format gets bounded reprobe; total attempt budget enforced |
| Discovery | Direct MP4/audio, HLS/DASH, redirects, nested frames, SPA navigation, signed queries preserved, blob fallback and unsupported media |
| Browser context | Probe/download share cookies and referer; same URL under two profiles isolated; expired context requests refresh; no secrets in logs/events |
| Protocol | Version mismatch; native framing with non-ASCII content/oversized messages; pagination; event gap and reconnect snapshot; idempotent retry |
| Connection | Unpaired client rejected; pairing revoked; wrong extension origin rejected; engine-port collision; missing native registration explained |
| Components | Missing tool detected; CPU fallback; unavailable update server; signature/checksum failure; failed update retains known-good bundle |

Fixture media should be small, redistributable, and deterministic. Large/public playlists or authenticated websites are not unit-test prerequisites. Store test credentials only in ephemeral fixtures.

## Browser/lifecycle checks

Validate installed Chrome and Firefox builds on Linux and Windows. Additional Chromium brands, sandboxed distributions, and macOS have separate rows once exercised.

- Fresh install → first app launch → extension connect → fixture probe/download.
- App absent/closed, browser permission denied, registration missing, incompatible version, and engine crash all have distinct recovery paths.
- Popup close, browser close, service-worker restart, native-host exit, and UI window close preserve app-owned jobs.
- Explicit Quit stops the process tree; reopening recovers artifacts/state once. Repeat launches do not create duplicate engines.
- Two browser profiles and the desktop can enqueue and change settings without collisions or silent reset.
- Firefox loads its shared libraries without service-worker-only assumptions; required permissions match actual API usage.
- A missing Linux tray leaves a usable window. Sleep/wake and lost network do not trigger an uncontrolled request burst.

## Platform release matrix

P02 is an early feasibility pass; P07 repeats the full matrix with release artifacts. Record OS/browser/app/engine versions and evidence rather than declaring generic cross-platform support.

| Target | Required package checks |
| --- | --- |
| Linux x86_64 AppImage + `.deb` | Clean target baseline without Python/FFmpeg installed; WebKitGTK/glibc compatibility; X11/Wayland; native-host repair after relocation/update; selected output permissions |
| Windows x64 installer | Standard user; WebView2 missing/present; paths with spaces and non-ASCII; no visible engine console; registry registration; process-tree cleanup |
| macOS later | Real Mac/runner, each advertised architecture; signed/notarized bundle; file access and app relocation; browser host integration |
| Every release target | Upgrade from previous app/schema; interrupted install/update; no-network launch; uninstall preserves media and offers deliberate preference cleanup |

## Load and live-site validation

Establish measurements before tuning concurrency. Proposed stress scenario: 200 queued fixture jobs, two clients, duplicate probes, intermittent 429/503, and repeated reconnects.

- Confirm configured global/per-host/probe/fragment limits and retry deadlines; measure request counts rather than assuming batching reduced upstream requests.
- No duplicate outputs, orphan processes, leaked credentials, or permanently stuck jobs; all work reaches a documented state after faults clear.
- Idle connected clients make no media-site or PyPI requests. UI progress is coalesced while terminal transitions arrive promptly. Record queue latency, peak memory, CPU, and request counts; set numerical release budgets after P02 measurements.

Maintain an opt-in live compatibility table containing date, platform, extractor/engine version, media type, authentication mode, result, and a redacted error category. Cover a common extractor site, generic direct media, HLS, DASH, playlist, signed-in media, and an explicitly unsupported case. Do not store private URLs/cookies in the repository. Live failures inform adapter fixes; they do not make deterministic CI depend on a third-party website.

## Command discipline

Current commands are in [AGENTS.md](../AGENTS.md). P01 must add exact locked setup and focused test commands; later milestones add codegen, frontend checks, and target-native packaging commands when the corresponding configuration exists. Do not document an invented `npm test`, `uv run`, or Tauri build command as currently runnable.

For each checkpoint record the command, platform, result, skipped checks, and remaining blocker in HANDOFF. Commit the state before exporting a bundle and verify a fresh clone matches its recorded HEAD.
