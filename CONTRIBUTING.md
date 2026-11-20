# Contributing to VidGrab

Thank you for contributing to VidGrab! This guide explains the repository layout, locked development environment, protocol code generation, test suites, and pull request expectations.

---

## Repository Structure

- `desktop/` — Tauri 2 + React + TypeScript desktop manager (`src/` UI, `src-tauri/` native host shell, `supervisor/` Rust owner/IPC/installation/Ed25519 updater crate).
- `native-host/` — Rust Native Messaging stdio bridge (`vidgrab-native-host`) used by Chrome and Firefox.
- `backend/` — Python FastAPI local engine, SQLite migrations & durable job store (`db/`), isolated per-attempt worker execution (`engine/`), and download/media services (`services/`).
- `packages/protocol/` — Single source of truth for the versioned v1 contract (`generate.py` generates synchronized Python, Rust, TypeScript, and JavaScript bindings).
- `extension/` — Lightweight Vanilla JS browser extension (`manifest.json` for Chrome MV3, `manifest.firefox.json` for Firefox, `popup/`, `options/`, `background/`, `content/`, `lib/`).
- `packaging/` — Reproducible component fetching (`components.lock.json`, `windows.components.lock.json`), PyInstaller freeze spec (`engine.spec`), desktop/AppImage/NSIS bundling, deterministic extension packager (`package_extension.py`), Ed25519 release manifest tooling (`release_manifest.py`), and `THIRD_PARTY.md`.
- `tests/` — Python unit/contract tests (`test_*.py`), Node extension/protocol tests (`check_*.mjs`), offline helpers (`check_*_helpers.py`), Playwright UI checks, and opt-in native/VM integration suites.
- `docs/` — Architecture, ADRs (`DECISIONS.md`), development workflow, validation records, and user guides.
- `installer/` — Retained legacy backend bootstrap scripts (superseded by packaged desktop releases).

---

## Developer Setup

### Prerequisites

- **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/)
- **Rust** stable toolchain (`cargo`, `rustc`)
- **Node.js 22+** and `npm`

### Install Locked Dependencies

```bash
# Python runtime + dev dependencies (add --group packaging if building frozen bundles)
uv sync --locked

# Desktop frontend dependencies
npm --prefix desktop ci --ignore-scripts
```

Do not modify lockfiles (`Cargo.lock`, `uv.lock`, `desktop/package-lock.json`, `packaging/*.lock.json`) unless your change explicitly intends to update a pinned dependency.

---

## Verification & Test Commands

Run these from the repository root before opening a pull request:

```bash
# 1. Verify protocol schema & generated bindings are in sync
.venv/bin/python -m packages.protocol.generate --check

# 2. Python guarded test suite (external network/process guards enabled)
.venv/bin/python -B -m unittest discover -s tests -p "test_*.py"

# 3. Rust supervisor & native-host tests
cargo test --locked -p vidgrab-supervisor -p vidgrab-native-host

# 4. Extension JS syntax & Node unit/contract tests
node --test tests/check_*.mjs

# 5. Desktop TypeScript check & Vite bundle
npm --prefix desktop run build

# 6. Non-destructive extension archive build (never modifies tracked dist/)
.venv/bin/python packaging/package_extension.py --output packaging/out/extension
```

On Windows PowerShell, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

For opt-in Playwright UI tests, signed-update E2E (`tests/check_updates_e2e.py`), frozen engine staging, and VM checks, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

---

## Coding Guidelines

- **Protocol changes:** Update the schema in `packages/protocol/` and regenerate all targets with `.venv/bin/python -m packages.protocol.generate`. Never hand-edit generated client/server contract files.
- **Python (`backend/`, `packaging/`):** Follow PEP 8, use `async`/`await` for I/O and `aiosqlite` access, keep runtime data strictly outside the source/install tree, and ensure temporary cookie jars are created with restricted permissions and removed in `finally` blocks.
- **Rust (`desktop/supervisor/`, `desktop/src-tauri/`, `native-host/`):** Avoid blocking IPC/runtime threads, validate paths against traversal and symlinks/reparse points (`O_NOFOLLOW` / `FILE_FLAG_OPEN_REPARSE_POINT`), and keep diagnostics strictly redacted (never log cookies, tokens, or private keys).
- **Desktop UI (`desktop/src/`):** Keep React components typed cleanly (`tsc --noEmit`), accessible (keyboard navigation, ARIA roles, contrast), and responsive across desktop and narrow viewports.
- **Browser Extension (`extension/`):** Keep the popup/options footprint minimal with Vanilla JS and CSS (no heavy runtime frameworks), keep optional permissions optional (`webRequest`/`webNavigation`), and handle offline/disconnected companion states gracefully.

---

## Submitting a Pull Request

1. Fork the repository and create a focused branch (e.g. `fix/windows-path-escaping` or `feat/queue-filter`).
2. Keep commits clean and descriptive (Conventional Commits style such as `feat(desktop): ...`, `fix(backend): ...`, `docs(release): ...`).
3. Ensure all verification checks above pass and CI (`VidGrab CI`) is green across Linux and Windows.
4. Fill out the pull request template with what changed, how it was tested, and screenshots for UI changes.
