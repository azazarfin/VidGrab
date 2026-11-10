# P02 component provenance

This internal Linux lifecycle spike embeds Python and dependencies from `uv.lock`, yt-dlp 2026.6.9, matching yt-dlp-ejs 0.8.0, Deno 2.9.8, and John Van Sickle's FFmpeg/ffprobe 7.0.2 AMD64 static build. Archive SHA-256 values and provenance are in `components.lock.json`; generated `tools/manifest.json` pins extracted executables, and startup verifies them.

- Python: PSF license, https://docs.python.org/3/license.html.
- yt-dlp: upstream code is Unlicense; package metadata/notices are retained, https://github.com/yt-dlp/yt-dlp.
- yt-dlp-ejs: MIT; matching scripts and package metadata are retained, https://github.com/yt-dlp/yt-dlp-ejs.
- Deno: MIT; https://github.com/denoland/deno/blob/v2.9.8/LICENSE.md. Its third-party runtime notices require a complete distribution audit before release.
- FFmpeg static build: GPL version 3; the archive's complete GPL text is bundled as `notices/GPLv3.ffmpeg.txt`. Source/build information: https://johnvansickle.com/ffmpeg/ and https://ffmpeg.org/. This build includes GPL codecs and has a static-glibc DNS limitation. The fixture downloads through Python and postprocesses a local file; it does not establish FFmpeg network-download compatibility.

This file is build provenance, not completed release licensing. Project ownership/license, all embedded dependency notices and applicable source-offer obligations remain the P07 release gate. No public distribution or signing is performed by the spike commands.

## Windows component pin

packaging/windows.components.lock.json selects Gyan FFmpeg 7.0.2 essentials (FFmpeg/ffprobe) and Deno 2.9.8 Windows x64 alongside the existing EJS 0.8.0 pin. Gyan archive SHA-256 is d5308d30872b2739cf53169df61faba8639d39a19b20b91e611c177ef676f64c: the GitHub release API publishes no checksum, so provenance is an HTTPS download followed by exact-byte SHA pinning. Deno archive SHA-256 cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf matches its upstream-published digest. Extraction checks the selected ZIP members; the staged integrity manifest verifies every selected runtime/tool file. License/source redistribution obligations still require the P07 review.
