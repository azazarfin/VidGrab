# Decision log

Record new decisions here with date, status, rationale, and the milestone that validates them. User-confirmed choices and engineering proposals are deliberately labeled so a new agent does not mistake a recommendation for an approved implementation.

## ADR-001 — Desktop companion with a reusable Python engine

- **Date:** 2026-11-03. **Status:** product direction confirmed; stack proposed, P02 gate.
- **Decision:** build a standalone download/settings app that also serves the extension. Recommend Tauri 2 + TypeScript/React, a small Rust bridge, and a packaged Python engine.
- **Why:** reuse yt-dlp/Python behavior and web UI skills while moving lifecycle/installation behind one app. Python workers provide enforceable isolation; shared contracts avoid a second business-logic implementation.
- **Tradeoff:** Rust, Python, and a web frontend add build complexity; Tauri depends on OS webviews. Evaluate real installers before investing in UI. PySide6 is the fallback if Tauri packaging/integration is disproportionately complex; Electron would simplify the web runtime but add distribution weight. A full Rust downloader rewrite discards useful Python integration.
- **Gate:** clean Linux/Windows installation, no external language runtime, functioning tray/fallback window and browser-to-app activation. Record measured size/startup results before accepting the stack.

## ADR-002 — Linux and Windows first

- **Date:** 2026-11-03. **Status:** explicitly user-confirmed.
- **Decision:** first usable release requires Linux and Windows; macOS follows using the same engine/protocol boundaries.
- **Proposed starting matrix:** Linux x86_64 and Windows x64; native Chrome and Firefox. Minimum OS versions, other browsers, and ARM64 remain capability-tested scope decisions.
- **Consequence:** P02 and P07 require evidence from both OSes. A Linux-only build is not the first-release completion condition.

## ADR-003 — App-owned lifecycle and native messaging first

- **Date:** 2026-11-03. **Status:** proposed, P02/P04 gates.
- **Decision:** app supervisor owns the engine and workers; browser native host is a replaceable bridge. Use app-owned per-user IPC plus authenticated internal loopback API. Evaluate an explicitly paired loopback extension fallback for sandboxed installations.
- **Why:** browser closure/service-worker suspension must not terminate jobs. Native messaging provides browser registration and app activation without asking users to start a backend or enter a port.
- **Tradeoff:** host registration and browser-specific process lifetime need installer support. A plain unauthenticated localhost server is not the production fallback. Background operation remains visible and controllable in the app.

## ADR-004 — Layered compatibility and bounded retries

- **Date:** 2026-11-03. **Status:** proposed reliability policy, P03/P05 gates.
- **Decision:** extractor-first, then direct-media/manifest detection with shared browser context. Use one bounded request/retry policy and typed failure states; benchmark against explicit fixtures and a live-site matrix.
- **Why:** increasing threads or repeating a 403/429 is not a general reliability strategy. Cookies, referers, expired formats, unsupported streams, and real connectivity failures need different recovery actions.
- **Consequence:** advertise tested capabilities rather than “every site.” Preserve the source snapshot's sanitizer behavior until the user-facing filename/metadata policy is decided.

## ADR-005 — Application state and release components are explicit

- **Date:** 2026-11-03. **Status:** P01 runtime/migration/partial-settings and reviewed index cleanup complete. Revisions and release-component ownership remain P04/P07 proposals.
- **Decision:** keep mutable data outside the repo/install directory; use versioned migrations and partial settings with revisions. Bundle verified media tools; update complete tested component sets through the app instead of startup pip mutation.
- **Why:** clean installations, offline use, rollback, and reproducible diagnosis require a known runtime. A tracked SQLite file is not portable user-state management.
- **Legacy-state cleanup:** ignore `backend/vidgrab.db` and its sidecars; remove only the database's Git index entry. The working file is hash-verified unchanged and older versions remain in history. Never delete user state or rewrite history as cleanup.
- **Consequence:** migration imports are explicit and backed up. Cookie-dependent jobs need fresh authorization after restart. Initial app release and engine bundle can share a release cadence; independent engine updates can follow once their compatibility gate exists.

## ADR-007 — P01 runtime and compatibility boundaries

- **Date:** 2026-11-03. **Status:** P01 complete, verified on Linux; not a desktop/Windows support claim. The user requested stopping after P01; P02 has not started.
- **Decision:** immutable `RuntimeConfig` supplies data/media/log/temp paths; app creation is side-effect-free and each factory instance owns its services. Defaults use per-user data, never automatic source-DB import. Output settings exclude installation and application storage while retaining safe nested/absolute templates.
- **Migrations:** SQLite `user_version` 0/1 → 2; validate schema/settings under a transaction, snapshot before upgrades, roll back failures, reject newer schemas. Explicit import snapshots/stages a selected source, includes committed WAL data, validates final-runtime paths and atomically refuses destination replacement. Filesystem hard-link support and Windows behavior still need target-native evidence.
- **Compatibility:** VidGrab requires Python >=3.11. The selected yt-dlp 2026.6.9 metadata says >=3.10; the older plan's upstream-minimum wording was not accurate for this pin. Tests actually ran under Python 3.14.8. Runtime requirements are hashed exports of `uv.lock`; uv also installs the current TestClient's development HTTP client.
- **Offline/update policy:** startup and health/status never contact PyPI or run pip; remote EJS downloads are disabled. Legacy update fields remain non-authoritative; the app's updater route refuses runtime mutation. Bundled matching media tools and signed updates are later gates.
- **UI scope:** keep the incumbent settings typeface/dark-violet identity while correcting measurable accessibility/overflow issues. The Inter warning is waived only for `extension/options/options.html` and `extension/options/options.css`, with evidence in `.impeccable/config.json`; the user approved the CSS-only value exception on 2026-11-03 after the P01 stop. No broad detector suppression or UI changes from that hook triage. The cookie-copy correction was raised for approval and remains outstanding; cookie persistence behavior was not changed to match that inaccurate claim.
- **Limits:** P01 checks literal output paths, not race-free artifact ownership or arbitrary legacy argument safety. Thread cancellation/retries/sanitizer behavior is preserved for P03/P05; authenticated clients and revision conflicts remain P04. See [VALIDATION.md](VALIDATION.md).

## ADR-006 — Git is the cross-agent/device handoff source

- **Date:** 2026-11-03. **Status:** explicitly user-confirmed.
- **Decision:** checkpoint existing source and ignore/bytecode cleanup, then commit plans and instructions. Export an offline Git bundle; no remote is configured or published in this session.
- **Source checkpoint:** `79f7d7e`, based on `822b9f2`. New working branch: `planning/desktop-companion`; original `main` remains at its prior commit.
- **Excluded current local edits:** `backend/vidgrab.db`, both `dist/vidgrab-*-extension.zip` files, and `store_assets/vidgrab_promo_banner.png`. The bundle includes their older committed versions/history, not these working copies.
- **Consequence:** development can resume from committed code and [HANDOFF.md](../HANDOFF.md) with no chat history. Existing venv, ignored scratch files, credentials, and active download state do not transfer. If remote hosting is later chosen, publish the branch without rewriting these checkpoints.

## Pending decisions by gate

| Gate | Decision/evidence needed | Working assumption |
| --- | --- | --- |
| P02 | Framework, supported OS baselines, tray behavior, sandbox native-host access | Tauri 2; native Chrome/Firefox first |
| P03/P06 | Pause support by transfer/post-processing phase | Explain restart semantics for non-resumable phases |
| P05/P06 | Current Bangla transliteration behavior | Preserve baseline until clarified; separate display metadata from filename policy |
| P07 | Hosting/store IDs, signing ownership, project license and bundled notices | Direct installer/extension distribution; no cloud account required |
| P08 | Mac device access, signing/notarization, supported architectures | Apple Silicon and Intel only where actually validated |
