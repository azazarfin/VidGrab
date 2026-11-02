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

- **Date:** 2026-11-03. **Status:** proposed, P01/P07 gates.
- **Decision:** keep mutable data outside the repo/install directory; use versioned migrations and partial settings with revisions. Bundle verified media tools; update complete tested component sets through the app instead of startup pip mutation.
- **Why:** clean installations, offline use, rollback, and reproducible diagnosis require a known runtime. A tracked SQLite file is not portable user-state management.
- **Consequence:** migration imports are explicit and backed up. Cookie-dependent jobs need fresh authorization after restart. Initial app release and engine bundle can share a release cadence; independent engine updates can follow once their compatibility gate exists.

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
