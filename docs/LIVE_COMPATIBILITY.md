# Live probe compatibility

Recorded on **2026-11-19**, using VidGrab's source `probes.create` operation, pinned **yt-dlp 2026.6.9 / EJS 0.8.0**, verified bundled tools and fresh isolated state. Each row runs once, sequentially, with the production request limits and host cooldowns. No download is enqueued. Format counts and playlist entry counts describe metadata; media execution and native/browser journeys require separate checks.

The same **302-file source snapshot** runs on the Fedora host (**Python 3.12.13**), Ubuntu **24.04.5 userspace / Python 3.12.3** and Windows 11 **Python 3.12.16**. Ubuntu uses a private container network on Fedora's **7.2.15-100.fc43.x86_64** kernel; this is separate from the stopped Ubuntu GNOME VM. Windows remains on its original disks with the desktop open; the new evidence uses C: because E: is full.

All executed rows use **public, anonymous access**. “Metadata” requires a nonempty format list; “Playlist” requires nonempty entries. The unsupported row matches only the specific `UNSUPPORTED_MEDIA` error, so a transport or authentication failure cannot pass that expectation.

| Sample / media type | Fedora host | Ubuntu userspace | Windows 11 |
| --- | --- | --- | --- |
| YouTube video / common extractor | `UNSUPPORTED_MEDIA` — mismatch | `UNSUPPORTED_MEDIA` — mismatch | `UNSUPPORTED_MEDIA` — mismatch |
| W3C Sintel trailer / direct MP4 | Metadata: 1 format | `AUTH_REQUIRED` — mismatch | Metadata: 1 format |
| Mux Big Buck Bunny / HLS | Metadata: 5 formats | Metadata: 5 formats | Metadata: 5 formats |
| Akamai Envivio / DASH | Metadata: 10 formats | Metadata: 10 formats | Metadata: 10 formats |
| YouTube small playlist | Playlist: 2 entries | Playlist: 2 entries | Playlist: 2 entries |
| IANA example page / unsupported | Expected `UNSUPPORTED_MEDIA` | Expected `UNSUPPORTED_MEDIA` | Expected `UNSUPPORTED_MEDIA` |
| Signed-in media | Pending by user choice | Pending by user choice | Pending by user choice |

The video failure category does not establish an extractor, transport or site root cause. Ubuntu's direct-media rejection is also unresolved. Preserve these first observations before further diagnosis; no adapter fix, retry or permission workaround follows from this matrix. P05 remains open for download/native/browser live compatibility, the signed-in row, Chrome fresh-grant support/UX and the separately recorded Windows localhost native-error-delivery limit. The passing controlled format/context/failure fixtures retain their original scope.

Public sample URLs are declared in `tests/check_live_sites.py`. The playlist comes from the pinned `YoutubeTabIE` two-entry test case. Reports omit source URLs, titles, entries, cookies, auth references, response bodies and exception messages; they retain only public labels, counts, durations and allowlisted error categories. Custom source specifications stay outside the repository. This runner rejects credential-bearing URLs, cookie fields and signed-in execution.

Evidence is under ignored `packaging/out/p05-live-_86fn7fh/`, via `p05-live-current.json`. It retains the immutable input snapshot, helper red/green logs, original failed Ubuntu `slirp4netns` startup, corrected private `pasta` run, Windows readiness/path failures, live rows and verified archives. Both selected targets pass five offline helper checks and finish with zero jobs, attempts, probe authorities, request leases or unreleased owners. Original isolated state and copied state witnesses are retained as recorded in the evidence receipts.
