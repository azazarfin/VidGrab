# P02 component provenance

This internal Linux lifecycle spike embeds Python and dependencies from `uv.lock`, yt-dlp 2026.6.9, matching yt-dlp-ejs 0.8.0, Deno 2.9.8, and John Van Sickle's FFmpeg/ffprobe 7.0.2 AMD64 static build. Archive SHA-256 values and provenance are in `components.lock.json`; generated `tools/manifest.json` pins extracted executables, and startup verifies them.

- Python: PSF license, https://docs.python.org/3/license.html.
- yt-dlp: upstream code is Unlicense; package metadata/notices are retained, https://github.com/yt-dlp/yt-dlp.
- yt-dlp-ejs: MIT; matching scripts and package metadata are retained, https://github.com/yt-dlp/yt-dlp-ejs.
- Deno: MIT; https://github.com/denoland/deno/blob/v2.9.8/LICENSE.md. Its third-party runtime notices require a complete distribution audit before release.
- FFmpeg static build: GPL version 3; the archive's complete GPL text is bundled as `notices/GPLv3.ffmpeg.txt`. Source/build information: https://johnvansickle.com/ffmpeg/ and https://ffmpeg.org/. This build includes GPL codecs and has a static-glibc DNS limitation. The fixture downloads through Python and postprocesses a local file; it does not establish FFmpeg network-download compatibility.

This file is build provenance, not completed release licensing. Project ownership/license, all embedded dependency notices and applicable source-offer obligations remain the P07 release gate. No public distribution or signing is performed by the spike commands.
