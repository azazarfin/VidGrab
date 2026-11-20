# VidGrab Third-Party Software Notices and Component Provenance

VidGrab's own source code (desktop application, supervisor, native messaging bridge, browser extension, and Python backend orchestration) is licensed under the MIT License (see the bundled `LICENSE` file).

Release packages (`.deb`, Linux AppImage, Windows per-user NSIS installer, and signed engine update bundles) bundle and redistribute the third-party components listed below. Notices in this directory (`LICENSE`, `THIRD_PARTY.md`, `GPLv3.ffmpeg.txt`, and `FONTS_LICENSE.txt` / `extension/fonts/LICENSE.txt`) are staged into every package and verified by SHA-256 during signed updates.

---

## 1. Python & Locked Python Packages (`uv.lock`)

- **CPython (Python 3.11+ embedded runtime):** Python Software Foundation License Version 2 (<https://docs.python.org/3/license.html>).
- **yt-dlp (`2026.6.9`):** Upstream source code is dedicated to the public domain under The Unlicense (<https://github.com/yt-dlp/yt-dlp>); upstream package metadata and license notices are retained in PyInstaller metadata.
- **yt-dlp-ejs (`0.8.0`):** MIT License (<https://github.com/yt-dlp/yt-dlp-ejs>); matching scripts and package metadata are retained.
- **Locked Python dependencies (`uv.lock`):** FastAPI (MIT), Uvicorn (BSD-3-Clause), Pydantic & pydantic-settings (MIT), Starlette (BSD-3-Clause), aiosqlite (MIT), httpx (BSD-3-Clause), websockets (BSD-3-Clause), certifi (MPL-2.0), packaging (Apache-2.0 OR BSD-2-Clause), and PyInstaller bootloader/freeze runtime (GPLv2 with PyInstaller exception permitting freezing independent applications).

## 2. JavaScript Runtime (`Deno`)

- **Deno (`v2.9.8`):** MIT License (<https://github.com/denoland/deno/blob/v2.9.8/LICENSE.md>).
  - Linux (`x86_64-unknown-linux-gnu`): `deno.zip`, SHA-256 `e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609` (matches official Deno v2.9.8 release asset digest).
  - Windows (`x86_64-pc-windows-msvc`): `deno-x86_64-pc-windows-msvc.zip`, SHA-256 `cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf` (matches official Deno v2.9.8 release asset digest).
  - Deno embeds V8 (BSD-3-Clause), Rust crates (MIT/Apache-2.0), and Tokio/Hyper ecosystems per Deno's upstream `THIRD_PARTY_LICENSES` documentation at <https://github.com/denoland/deno/tree/v2.9.8>.

## 3. FFmpeg & ffprobe (GPLv3)

FFmpeg and ffprobe executables are redistributed as standalone external tools invoked via subprocesses; the complete GNU General Public License Version 3 text extracted from each pinned upstream archive is bundled alongside this notice as `GPLv3.ffmpeg.txt`.

- **Linux (`x86_64-unknown-linux-gnu`):** `BtbN/FFmpeg-Builds` `autobuild-2026-07-31-14-10`, build `n7.1.5-12-g1fdbca85aa` (`ffmpeg-n7.1.5-12-g1fdbca85aa-linux64-gpl-7.1.tar.xz`, SHA-256 `c1e6caf48923dd8e6bc5e54d51ba70c321175b8162ae9c414c392990e72f0e79`, License: GPL-3.0). Dynamically links standard system glibc (`>=2.28`) libraries (`libm`, `libdl`, `librt`, `libpthread`, `libc`, `ld-linux-x86-64`, `libmvec`, `libgcc_s`). Replaces the earlier John Van Sickle 7.0.2 static build whose static glibc 2.28 gconv crashed on Ubuntu 24.04 glibc 2.39.
- **Windows (`x86_64-pc-windows-msvc`):** `GyanD/codexffmpeg` release `7.0.2` (`ffmpeg-7.0.2-essentials_build.zip`, SHA-256 `d5308d30872b2739cf53169df61faba8639d39a19b20b91e611c177ef676f64c`, License: GPL-3.0).
- **Corresponding Source Offer:** Exact upstream sources and build recipes for these GPL-3.0 FFmpeg builds are available at <https://ffmpeg.org/>, <https://github.com/BtbN/FFmpeg-Builds>, and <https://github.com/GyanD/codexffmpeg> (tag `n7.1.5` / `7.0.2`). In accordance with GPLv3 Section 6, if you require the corresponding source code directly from the VidGrab distributor, submit a request via the VidGrab repository issue tracker referencing the exact release version and SHA-256 pin above.

## 4. Desktop Shell, Installer & WebView Runtime

- **Tauri 2 & Rust Crates (`Cargo.lock`):** Tauri (`2.x`), `serde`, `serde_json`, `sha2`, `getrandom`, `libc`, `windows-sys`, `gtk-rs` / `gio` are licensed under `MIT OR Apache-2.0`.
- **Linux AppImage Runtime (`linuxdeploy` / `AppImageKit`):** MIT License; dynamically packaged against the Ubuntu 24.04 (`glibc <= 2.39`) compatibility baseline verified by `packaging/check_abi.py`.
- **Windows NSIS Installer Template:** `packaging/windows.installer.nsi` derives from Tauri upstream commit `52081e0` under its MIT/Apache-2.0 license (original template SHA-256 `d00089d3b1a2c4a30d7b6e098079100cb8f25e980f84d4515aca4597b95e967d`). Release customization selects per-user `currentUser` installation and replaces forceful Restart Manager closure with `vidgrab-supervisor.exe` idle-only hooks (`windows.nsis.nsh`). NSIS itself uses the zlib/libpng license.
- **Microsoft Edge WebView2 Offline Installer (`Windows`):** Bundled in Windows NSIS releases via Tauri's `offlineInstaller` mode (`.exe` redistributable) subject to the Microsoft Edge WebView2 Software License Terms; installed per-user without elevation only if a compatible system WebView2 runtime is absent.

## 5. Desktop & Extension Frontend Assets

- **Fonts (`Source Sans 3`, `Space Grotesk`):** SIL Open Font License Version 1.1 (`extension/fonts/LICENSE.txt`, bundled in release notices as `FONTS_LICENSE.txt`).
- **React 19, Vite, `@tauri-apps/api`:** MIT License (`desktop/package-lock.json`).
