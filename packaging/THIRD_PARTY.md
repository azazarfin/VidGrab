# P02 component provenance

This internal Linux lifecycle build embeds Python and dependencies from `uv.lock`, yt-dlp 2026.6.9, matching yt-dlp-ejs 0.8.0, Deno 2.9.8, and BtbN's FFmpeg/ffprobe `n7.1.5-12-g1fdbca85aa` (`linux64-gpl-7.1`) build (replacing the earlier John Van Sickle 7.0.2 static build whose static glibc 2.28 crashed on Ubuntu 24.04's glibc 2.39 gconv cache). Archive SHA-256 values and provenance are in `components.lock.json`; generated `tools/manifest.json` pins extracted executables, and startup verifies them.

- Python: PSF license, https://docs.python.org/3/license.html.
- yt-dlp: upstream code is Unlicense; package metadata/notices are retained, https://github.com/yt-dlp/yt-dlp.
- yt-dlp-ejs: MIT; matching scripts and package metadata are retained, https://github.com/yt-dlp/yt-dlp-ejs.
- Deno: MIT; https://github.com/denoland/deno/blob/v2.9.8/LICENSE.md. Its third-party runtime notices require a complete distribution audit before release.
- FFmpeg Linux build (`BtbN/FFmpeg-Builds` `autobuild-2026-07-31-14-10`, `n7.1.5-12-g1fdbca85aa`): GPL version 3; the archive's `LICENSE.txt` complete GPL text is bundled as `notices/GPLv3.ffmpeg.txt`. Source/build information: https://github.com/BtbN/FFmpeg-Builds and https://ffmpeg.org/. This build includes GPL codecs and dynamically links standard system glibc (`>=2.28`) libraries (`libm`, `libdl`, `librt`, `libpthread`, `libc`, `ld-linux-x86-64`, `libmvec`, `libgcc_s`).

This file is build provenance, not completed release licensing. Project ownership/license, all embedded dependency notices and applicable source-offer obligations remain the P07 release gate. No public distribution or signing is performed by the spike commands.

## Windows component pin

packaging/windows.components.lock.json selects Gyan FFmpeg 7.0.2 essentials (FFmpeg/ffprobe) and Deno 2.9.8 Windows x64 alongside the existing EJS 0.8.0 pin. Gyan archive SHA-256 is d5308d30872b2739cf53169df61faba8639d39a19b20b91e611c177ef676f64c: the GitHub release API publishes no checksum, so provenance is an HTTPS download followed by exact-byte SHA pinning. Deno archive SHA-256 cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf matches its upstream-published digest. Extraction checks the selected ZIP members; the staged integrity manifest verifies every selected runtime/tool file. License/source redistribution obligations still require the P07 review.

## Windows installer template and WebView2

windows.installer.nsi derives from Tauri upstream commit 52081e0 under its MIT/Apache-2.0 license. Original template SHA-256 is d00089d3b1a2c4a30d7b6e098079100cb8f25e980f84d4515aca4597b95e967d. Preview changes select the per-user Programs/VidGrab-P02 directory and replace forceful Restart Manager closure with VidGrab idle-only hooks. The Windows bundle embeds the Microsoft WebView2 offline installer via Tauri; exact payload/version/signature inventory and redistribution review remain required before release. The first actual preview uses an existing system WebView2 runtime; missing-runtime acceptance is not yet claimed.
