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

- **Date:** 2026-11-03. **Status:** P01 complete, verified on Linux; not a desktop/Windows support claim. The user stopped at this checkpoint, then resumed Linux P02 work on 2026-11-04.
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

## ADR-008 — Linux P02 spike implementation boundary

- **Date:** 2026-11-04. **Status:** Linux development spike verified; full P02 and ADR-001 remain open. The user chose Linux work now and Windows validation later.
- **Implemented:** Tauri 2/React connection window, frozen Python/media tools, private same-user POSIX IPC, bounded engine recovery, owned process-group shutdown and a small native browser bridge. Browser mutations are forwarded once; a lost reply reports an unknown outcome for queue inspection.
- **Build/path corrections:** direct Cargo builds embed the frontend through the default `custom-protocol` feature. Download result paths remain literal in both the downloader and queue; existing Bangla/Unicode directories are preserved without changing title sanitization or the established filename policy. Complete artifact tracking remains P03.
- **Evidence:** 49 guarded deterministic tests, nine native lifecycle cases, compiled WebKit window, mocked rendering at three sizes and installed Chrome/Firefox handoffs pass. Exact versions, commands, staging measurements and limits are in [VALIDATION.md](VALIDATION.md).
- **Deferred gates:** clean Linux packages, resource discovery, relocation/registration, real tray/Wayland, sandboxed browsers and Windows x64. These results do not finalize the framework or authorize P03; the native workspace currently rejects non-Linux builds.

## ADR-009 — Linux preview package paths and registration

- **Date:** 2026-11-05. **Status:** implemented and checked on the Fedora development host; clean baseline, actual installer/AppImage and full P02 acceptance remain open.
- **Decision:** use Tauri's resource resolver for the frozen engine and the executable's sibling native sidecar. Copy the bridge to a stable private user directory outside an AppImage mount. Persist a strict `package_version: 1` launch descriptor containing the app target and selected state/media, with no engine mount path or credential. Preserve explicit staging configuration as a separate launch mode.
- **Preview scope:** default state is `XDG_DATA_HOME/vidgrab-p02/state`, bridge/config is `vidgrab-p02/native`, and media is `~/Downloads/VidGrab-P02`. These defaults avoid sharing the legacy runtime. Chrome/Firefox use the existing development identities and default native-host locations; popup/options still use the legacy API.
- **Repair/removal:** reopening a relocated image updates its target and copied bridge while retaining selected state/media. Validate existing manifests before writes, publish files atomically with private permissions, and refuse foreign/symlink destinations. Disconnection removes matching manifests while preserving user files and selected paths. This is not a complete uninstall implementation.
- **Evidence:** eleven Rust tests, five existing guarded packaging tests, a native release build, a freshly frozen engine and eight headless checks against the actual extracted `.deb`. A failed relocation check exposed the overly broad AppImage-parent guard; its regression failed before the fix and now passes.
- **ABI limit:** the current `.deb` is 138,158,710 bytes (131.76 MiB). An audit of 82 shipped ELF files found glibc 2.42 requirements in bundled `termios`/`libtinfo`; the declared dependency was corrected from 2.39. This is the current preview's requirement, not an approved minimum OS baseline. Build on a selected clean baseline before claiming broader compatibility.
- **Blocked checks:** the AppRun helper is uncached and its download fails DNS in this sandbox. Local socketpair/loopback and bwrap network setup are denied, so full backend, GUI/browser, tray/Wayland and installer checks were not repeated. Windows remains deferred at the user's request.

## ADR-010 — Ubuntu 24.04 LTS clean Linux baseline

- **Date:** 2026-11-06. **Status:** explicitly user-selected target; Ubuntu userspace rebuild/integrity, native GNOME/Wayland/package/browser/FUSE, strict Firefox Snap and opted-in Firefox Flatpak checks pass. Release installer UX and full P02 acceptance remain open.
- **Decision:** build and validate Linux x86_64 AppImage and `.deb` previews on Ubuntu 24.04 LTS. The existing Fedora 43 artifacts remain development evidence.
- **Consequence:** rebuild the Python interpreter/support libraries and Rust binaries on the selected target. Audit every shipped ELF against the actual package dependency; the Fedora bundle's glibc 2.42 requirement must not be lowered to Ubuntu's 2.39 without compatible replacement binaries.
- **Gate:** clean build, frozen self-check, installed package/browser fixture lifecycle, relocation, tray/Wayland and measurements. Selecting the baseline does not certify support or complete P02/ADR-001; Windows remains deferred.
- **2026-11-07 native evidence:** unchanged previews pass in Ubuntu 24.04.5/GNOME 46 on its own 6.8.0-147-generic KVM kernel, including virtual mouse/keyboard tray Close/Open/Quit, normal OS FUSE, CLI removal/repair and preserved completed fixtures. Native Chrome/Firefox content processes retain seccomp/NoNewPrivs; Firefox's AppArmor user-namespace warning prevents a full-confinement claim. Exact versions, receipts and remaining gates are in VALIDATION/HANDOFF.

## ADR-011 — Preserve frozen engine bytes during AppImage assembly

- **Date:** 2026-11-06. **Status:** implemented and verified on the Ubuntu userspace preview; P02/ADR-001 remain open.
- **Evidence:** linuxdeploy rewrote 46 engine ELF files and added Deno RUNPATH, breaking the pinned tool checksum. The raw AppImage correctly failed its frozen self-check.
- **Decision:** finish desktop dependency deployment, restore the complete original engine into a fresh AppDir, audit every ELF against the selected ceiling and run frozen component checks. Package that tree directly with appimagetool, retaining the original component manifest and a recorded runtime input.
- **Boundary:** `prepare_appimage.py` refuses existing/dangling, overlapping, incomplete, ambiguous and escaped inputs. `check_appimage_package.py` covers the embedded runtime, extracted resources, actual relocation and private registration/removal. Ubuntu's glibc 2.39 override is separate from the older Fedora >=2.42 configuration.
- **2026-11-07 evidence:** combined Chrome→Firefox reopen passes after fixing Xvfb's unread stderr pipe. Actual AppImage FUSE resource/tool/registration and GUI/browser checks also pass on the native Ubuntu GNOME VM, including unmount after tray Quit and Unicode relocation/repair. Packaged bytes are unchanged. Release installer UX and Windows acceptance remain open; sandboxed-browser results are recorded in ADR-012/013.

## ADR-012 — Use the WebExtensions portal for tested Ubuntu Firefox Snap

- **Date:** 2026-11-07. **Status:** verified for the selected Ubuntu/Snap configuration; P02 and ADR-001 remain open.
- **Decision:** retain the existing package-generated native manifest and stable copied bridge. Ubuntu's WebExtensions portal finds and launches it for strict Firefox Snap; this configuration needs no loopback fallback or additional registration format.
- **Evidence:** Firefox 159.0.1/Snap revision 9171 on Ubuntu 24.04.5/GNOME 46, with xdg-desktop-portal 1.18.4-1ubuntu2.24.04.3 and GNOME portal 46.2-0ubuntu1. Both unchanged packages pass real Allow input, owner activation, gated download after full browser exit, completed history after restart, `APP_MISSING` and disconnection. The browser AppArmor label is enforced and extension content seccomp/NoNewPrivs are observed; AppImage mounts clean up.
- **Test boundary:** GNOME requires a focused visible app for its permission prompt. Headless failure cached a denial; only the fixture's exact permission is reset inside the guarded guest, and approval uses QMP mouse input. Marionette's system-access flag stays confined to that test browser. Application mutation forwarding, deadlines and confinement settings are unchanged.
- **Remaining:** opted-in Firefox Flatpak evidence and its user-selected setup path are recorded in ADR-013. Other portal versions/distributions need their own checks. Evaluate explicit pairing only where native access is unavailable; do not silently fall back to the legacy unauthenticated API. Release installer UX and Windows acceptance remain pending.

## ADR-013 — Guide Firefox Flatpak users through native portal setup

- **Date:** 2026-11-08. **Status:** guided native setup explicitly user-selected; verified for the selected Ubuntu/Flatpak configuration. P02 and ADR-001 remain open.
- **Decision:** retain the canonical package manifest and copied bridge. Guide Firefox Flatpak users to set the existing integer `widget.use-xdg-desktop-portal.native-messaging` preference to `1`, restart Firefox and approve the real desktop prompt. Do not change their browser settings automatically. No paired loopback fallback is implemented for this tested configuration.
- **Evidence:** official Flathub Firefox 159.0.1, Flatpak 1.14.6, runtime `org.freedesktop.Platform/x86_64/25.08` on Ubuntu 24.04.5/GNOME 46. Stock profiles have integer value `0` with no user override and return missing-host errors despite registration. Explicit value `1` passes real Allow input, native Wayland activation, one gated download surviving full browser exit, restored history, `APP_MISSING` and disconnection in both unchanged packages. Browser/content private Flatpak PID namespaces, seccomp/NoNewPrivs and absent user permission overrides are observed; Firefox's user-namespace warning remains a limit.
- **Setup prerequisite:** GNOME must recognize the Flatpak desktop entry. Rebooting the guest after installation refreshed export directories and resolved portal identity denial. Keep Firefox visible/focused for permission; approval remains real user input. The preview includes optional desktop help and [the full setup guide](BROWSER_CONNECTION.md), while the regular extension popup still uses the legacy API.
- **Test boundary:** shared Snap/Flatpak harnesses use only the named disposable guest and fresh sandbox-visible profiles. Flatpak process IDs are mapped through descendants at the browser namespace depth; cleanup kills the exact owned instance and verifies process exit. No permission override, programmatic grant, native deadline extension or mutation retry was added. Both Snap package cases also pass after extraction of the shared harness.
- **Manual guidance evidence — 2026-11-08:** both rebuilt packages pass virtual Number/Save input, full Control+Q/restart, persisted integer `1`, temporary add-on removal/reload and real GNOME Allow without a preference preseed. Temporary add-ons must be loaded after Firefox restarts; the guide and packaged copy now state that order. Exact package hashes and receipts are in HANDOFF/VALIDATION.
- **Remaining:** terminal-free release installation/removal, other portal/distribution configurations and deferred Windows acceptance. Preview browser controls are verified in ADR-014. The selected guided setup resolves this Flatpak spike; it does not finalize sandboxed-browser support or the framework gate.

## ADR-014 — Preserve explicit Linux browser disconnection

- **Date:** 2026-11-08. **Status:** implemented and verified Linux P02 preview policy; full release installation/removal, Windows and ADR-001 remain open.
- **Decision:** keep the strict `package_version: 1` descriptor for selected app/state/media paths and store browser access separately in private adjacent `browser-connection.json` (`connection_version: 1`, Boolean `enabled`). Absence retains older preview registration behavior. Explicit Disconnect persists `false` before removing matching manifests, and ordinary reopening or AppImage relocation preserves it. The local descriptor/bridge can repair while browser access remains disabled.
- **Enforcement:** each validated packaged native request rereads the preference and returns `BROWSER_DISCONNECTED` before activation or owner contact. Invalid or unsafe preferences are refused. Connect/Repair is explicit; status and registration mutations serialize within the desktop. Browser-origin/framing checks, single-owner lifetime and no replay after a lost mutation reply remain unchanged.
- **Evidence:** 19 Rust cases, the real live-host preference sequence, mocked renderer at three widths, both actual package pointer journeys and native Chrome/Firefox/Snap/manual Flatpak checks pass. Active downloads, completed history/media, selected paths and foreign manifests are preserved. Registration status describes matching manifests; the browser's native test and real portal Allow confirm access independently.
- **Boundary:** this removes browser access while downloads keep running. It does not implement package removal, per-job cancellation, the migrated extension UI or Windows. Full terminal-free installation/removal is the next P02 step.

## ADR-015 — Explicit Ubuntu preview installation and removal

- **Date:** 2026-11-10. **Status:** implemented and verified for the Linux P02 preview; final-package startup review, Windows and ADR-001 remain open.
- **Policy:** Ubuntu owns `.deb` installation/authentication/removal through Files and App Center. VidGrab prepares idle shutdown, persists browser disconnection and opens App Center. AppStream XML and DEP-11 YAML map the installed `vid-grab` package to its VidGrab desktop entry.
- **AppImage:** an explicit Install action creates a per-user managed copy, icon, checksum receipt and Applications shortcut. The native descriptor points to that stable copy. Installing keeps the current download session open; replacing a different/changed installation, foreign files and symlinks are refused. Removal checks ownership and image integrity and deletes only managed files after the owner has stopped.
- **Retention:** preserve media, history, selected paths, native descriptor/bridge and explicit browser disconnection through removal/reinstall. The confirmation initially focuses Keep VidGrab; Escape cancels. Active work disables removal, and native-only `quit_if_idle` requires a positive idle observation covering all unfinished jobs, including jobs outside bounded history.
- **Evidence:** final actual `.deb` and AppImage GUI cycles, seven real-image file cases, backend/native/renderer active-job regressions, fresh Ubuntu engine/native builds and 50/235 shipped-ELF glibc-2.39 audits pass. Full reports, preserved hashes and guest shutdown are recorded in HANDOFF/VALIDATION. One failed App Center test removed five unrelated guest packages; exact versions were restored offline, and the corrected filtered/same-frame harness plus final audit verify every other package unchanged.
- **Boundary:** fixture preview on the named disposable Ubuntu/GNOME guest with virtual input. Updates, interrupted-install recovery, other desktops/distributions, physical-device behavior and Windows need their own acceptance evidence. This completes the preview installation/removal subtask without completing P02 or starting P03.

## Pending decisions by gate

The user chose **Linux work now, Windows validation later** on 2026-11-04. The Linux spike uses Tauri 2/React, a frozen Python engine and a Rust POSIX owner/bridge. This is implementation evidence for ADR-001/003, not final stack acceptance. Windows native IPC/Job Objects and target packaging remain unimplemented; the workspace currently rejects non-Linux builds. Neither target's complete installer gate is waived, and P03 has not started.

| Gate | Decision/evidence needed | Working assumption |
| --- | --- | --- |
| P02 | Framework, baseline acceptance, tray behavior, sandbox native-host access | Tauri 2; Ubuntu 24.04 LTS selected for Linux rebuild; native Chrome/Firefox first |
| P03/P06 | Pause support by transfer/post-processing phase | Explain restart semantics for non-resumable phases |
| P05/P06 | Current Bangla transliteration behavior | Preserve baseline until clarified; separate display metadata from filename policy |
| P07 | Hosting/store IDs, signing ownership, project license and bundled notices | Direct installer/extension distribution; no cloud account required |
| P08 | Mac device access, signing/notarization, supported architectures | Apple Silicon and Intel only where actually validated |
