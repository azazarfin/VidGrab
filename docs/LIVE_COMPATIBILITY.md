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

Separate diagnostic attempts on **2026-11-19**, based on `e08ce09`, preserve the matrix above. The original YouTube sample reports `VIDEO_UNAVAILABLE` after two HTTP 200 responses on every runtime, with nested DownloadError/ExtractorError and no observed request-budget failure. The pinned extractor can surface player playability reasons when it has no formats; the retained signal does not establish the exact site-side cause. A separately named short YouTube sample returns formats everywhere; the evidence points to the original sample's availability without establishing removal, regional scope or a general extractor failure.

| Separate diagnostic/control sample | Fedora host | Ubuntu userspace | Windows 11 |
| --- | --- | --- | --- |
| Original YouTube video / diagnostic | `VIDEO_UNAVAILABLE`, HTTP 200 × 2 | `VIDEO_UNAVAILABLE`, HTTP 200 × 2 | `VIDEO_UNAVAILABLE`, HTTP 200 × 2 |
| Original W3C MP4 / diagnostic | HTTP 403 → `AUTH_REQUIRED` | HTTP 200 → 1 format | HTTP 403 → `AUTH_REQUIRED` |
| YouTube Me at the zoo / separate control | Metadata: 11 formats | Metadata: 11 formats | Metadata: 11 formats |
| MDN CC0 flower MP4 / separate control | Metadata: 1 format | Metadata: 1 format | Metadata: 1 format |
| Signed-in media | Pending by user choice | Pending by user choice | Pending by user choice |

The W3C results vary from the first attempts, including which runtime receives rejection. HTTP 403 is mapped to `AUTH_REQUIRED` by the unchanged wire policy; this does not prove a signed-in session is needed, or explain the upstream variation. No production fix follows. The new samples are public declarations in `tests/check_live_diagnostics.py`; they supplement the failed rows and do not replace them.

Two immutable **304-file snapshots** preserve all **132 runtime inputs**. Five new/five original helpers pass per runtime; twelve diagnostic/control rows finish with joined probes and zero durable jobs/attempts/probes/leases/unreleased owners. Allowlisted signals/statuses/types/codes are extracted in memory without retaining raw messages, response bodies, titles, URLs or credentials. Ubuntu remains a private userspace container on the Fedora kernel; native VM/browser acceptance is separate.

Evidence is `packaging/out/p05-live-diagnosis-fuln3gct/`, via `p05-live-diagnosis-current.json`. Ubuntu/Windows archives verify **642/641 files**, each retaining two sources and two runtime trees. Verified copies/archive creation precede exact exited Ubuntu-container removal; original host/Windows state remains. The prior failed container, stopped native VM chains, original Windows disks/full E:/open desktop and previous SDK/WebView2/private-metadata limits remain preserved. Next: bounded actual public downloads and selected frozen/native/browser live journeys. Signed-in testing and Chrome fresh-grant support/UX remain open; P05 is not accepted. [Validation](VALIDATION.md#p05-public-live-diagnostic-checkpoint--2026-11-19) records the checks.

Bounded source-worker download observations on **2026-11-19**, based on `5fde6c8`, supplement both probe tables. Each actual media case uses v1 enqueue and the unchanged process executor, with a 120-second job observation, 64 MiB sampled storage threshold and 250 ms checks. A first retry/auth state or guard limit triggers one explicit joined cancellation. These bounds do not impose a hard wire-byte ceiling or alter the production request policy. Completed files receive bundled ffprobe, duration checks where source metadata supplies duration, full FFmpeg decode and unchanged-byte hashes.

| Actual source-worker download lane | Fedora host | Ubuntu userspace | Windows 11 |
| --- | --- | --- | --- |
| YouTube short sample / probe quality | `AUTH_REQUIRED`, cancelled | `AUTH_REQUIRED`, cancelled | `AUTH_REQUIRED`, cancelled |
| MDN flower / original probe quality | Unclassified enqueue rejection | Unclassified enqueue rejection | Unclassified enqueue rejection |
| Mux BBB / HLS | Full decode/hash pass | Unclassified JSON failure | Full decode/hash pass |
| Akamai Envivio / DASH | `TRANSPORT_ERROR`, cancelled | `TRANSPORT_ERROR`, cancelled | Actual 120-second guard cancellation |
| Unsupported HTML | Rejected without enqueue | Rejected without enqueue | Rejected without enqueue |
| MDN flower / separately chosen app defaults | Full decode/hash pass | Full decode/hash pass | Full decode/hash pass |
| Playlist download | Pending | Pending | Pending |
| Signed-in media | Pending by user choice | Pending by user choice | Pending by user choice |

The two successful HLS outputs match **20,049,872 bytes**, SHA-256 `345aff26ba2fbc3ea91511e41b389aa0b309fea73214773635a170e7daa3929b`, **634.633 seconds**, one video/one audio stream. The three direct outputs match **1,128,375 bytes**, SHA-256 `0cd83d944a6ca7822b4a8306cecc60a36e859b041f6702c6a1ad9ead78924451`, **5.055 seconds**, one video/one audio stream. Source-1's original failed runner reports remain failed after the final audit-column error; separate closed-runtime audits verify their existing rows/bytes and release state without rerunning the public cases. Final-source full five-row execution was not repeated.

The direct app-defaults path is explicitly selected in a separate fresh lane. Code inspection identifies the unknown-quality refusal path for probe-bound selection; the original enqueue reply code was hidden by its older summarizer and remains unclassified. No automatic selection fallback or production repair is added. Ubuntu HLS retains `Expecting value: line 1 column 1 (char 0)` with unproven cause. DASH transport/time-limit observations and anonymous YouTube download rejection remain independent failures; metadata success is not download acceptance.

Three immutable **306-file snapshots** preserve all **132 runtime inputs**. Ten new/five diagnostic/five original helpers pass; six actual download runtimes retain **12 jobs/12 attempts** and finish with no unfinished jobs, probe authorities, leases or unreleased owners. Evidence is `packaging/out/p05-live-downloads-fi2plk_d/`, via `p05-live-downloads-current.json`: Ubuntu/Windows archives verify **1,621/972 files**, with two runtime trees each. Original host/Windows state/media, failed Ubuntu rootfs and all prior failures remain. The broader failed-container tmp capture includes inherited inputs, with copied identities distinguished. The exact successful Ubuntu container is removed only after verification; the native VM remains stopped and Windows original disks/full E:/open desktop stay preserved.

Next: diagnose the failed live worker rows, then bounded playlist and selected frozen/native/browser journeys, alongside Chrome fresh-grant support/UX. Signed-in testing remains pending by explicit user choice; P05 is not accepted. [Validation](VALIDATION.md#p05-public-live-download-checkpoint--2026-11-19) records reporting corrections, preservation and limitations.

Separate in-worker diagnostic observations on **2026-11-19**, based on `f7181d0`, instrument the unchanged `backend.engine.worker` subprocess in memory through an opt-in `--observe-worker` wrapper (`tests/check_live_worker_diagnostics.py`) without altering any of the **132 runtime inputs** or prior failed rows. Each runtime executes the four previously failing public cases sequentially with the same 120-second/64 MiB guards and allowlisted redacted telemetry (stages, HTTP method/status counts, symbolic `errno`, fragment counters, `ffmpeg`/`ffprobe` return codes and allowlisted error frames).

| In-worker diagnostic lane | Fedora host | Ubuntu userspace | Windows 11 |
| --- | --- | --- | --- |
| YouTube short sample / probe quality | `AUTH_REQUIRED` (`GET:403` req #3), cancelled (3.6 s) | `AUTH_REQUIRED` (`GET:403` req #3), cancelled (3.6 s) | `AUTH_REQUIRED` (`GET:403` req #3), cancelled (4.6 s) |
| MDN flower / probe quality | Enqueue `FORMAT_UNAVAILABLE` (0.6 s) | Enqueue `FORMAT_UNAVAILABLE` (0.6 s) | Enqueue `FORMAT_UNAVAILABLE` (0.6 s) |
| Mux BBB / HLS | Full decode/hash pass (`132` reqs, 86.7 s) | `ffprobe` exit **`-11`** (`SIGSEGV`) → `JSONDecodeError` (13.1 s) | Full decode/hash pass (`132` reqs, 19.5 s) |
| Akamai Envivio / DASH | `TIME_LIMIT` guard at 75/98 GETs (`175` reqs, 120.6 s) | Full decode/hash pass (`198` reqs, 38.3 s) | Full decode/hash pass (`198` reqs, 23.1 s) |
| Signed-in media | Pending by user choice | Pending by user choice | Pending by user choice |

Directly observed root-cause signals across all three runtimes:
- **YouTube (`youtube-zoo-control`):** identical 3-request sequence (`GET:200`, `POST:200`, then `GET:403` at request 3) inside `backend.services.resume.real_download` → `_open` → `BudgetedRequests.open`, mapped to `RequestFailure("AUTH_REQUIRED")` with zero `PO_TOKEN_NOTICE` or transport errors. The upstream reason for returning HTTP 403 to the anonymous stream request is not inferred beyond that status.
- **Direct MP4 (`mdn-flower-control` / `probe-quality`):** directly captures typed v1 enqueue code **`FORMAT_UNAVAILABLE`** before worker launch; `probe_format_summary` records `has_ext=true` with `height/width/resolution/vcodec/acodec` all absent, matching `ProbeWitnesses.resolve`'s refusal on `GenericIE` direct-MP4 metadata.
- **Ubuntu HLS (`mux-bbb`):** completes all 64/64 fragments (`132` requests: `GET:200` × 68, `HEAD:200` × 64, zero HTTP/transport errors), passes `ffmpeg -version` and `ffprobe -version` (`exit_code: 0`), then fails inside `FFmpegFixupM3u8PP` → `FFmpegPostProcessor.get_metadata_object` when the bundled Ubuntu `ffprobe` media-probe invocation exits with **`-11` (`SIGSEGV`)** and empty stdout/stderr; `get_metadata_object` passes empty stdout to `json.loads` (`JSONDecodeError`). Host and Windows outputs match the prior **20,049,872-byte** SHA-256 `345aff26ba2fbc3ea91511e41b389aa0b309fea73214773635a170e7daa3929b`.
- **DASH (`akamai-envivio`):** Ubuntu and Windows complete all 98/98 fragments (`198` requests: `GET:200` × 100, `HEAD:200` × 98; three `ffmpeg` calls exit `0`) and full-decode/hash with identical **3,134,388 bytes**, SHA-256 `0cccf691c0a3a5adc803d60cdc6320925307e06db0f0f569c840197709ef5131`, **193.579 seconds**, 0 video / 1 audio stream. Fedora host hits the 120-second guard with zero HTTP/transport errors after completing 98/98 sequential `HEAD` probes and 75/98 sequential `GET` fragments (`175` requests), confirming sequential per-fragment `HEAD` + `GET` serialization under `MAX_PER_HOST=1` as the 120-second bound driver; earlier host/Ubuntu `TRANSPORT_ERROR` rows remain preserved independently.

Two immutable **308-file snapshots** preserve all **132 runtime inputs** (`source-1.zip` on host/Ubuntu; `source-2.zip` adjusting only the Windows `WSAECONNRESET` errno test expectation in `tests/check_live_worker_diagnostic_helpers.py`, verified on host/Windows). Twenty-nine offline live helpers pass per runtime; all three runtimes retain **9 jobs / 9 attempts** total with zero unfinished jobs, probes, leases or unreleased owners. Evidence is `packaging/out/p05-live-worker-diagnosis-ipk_bxqf/`, via `p05-live-worker-diagnosis-current.json` (**395** indexed files; Ubuntu/Windows archives verify **20/31** files). Next: investigate/fix the confirmed Ubuntu bundled `ffprobe` segfault and sequential fragment `HEAD` cost if scoped, then bounded playlist, frozen/native/browser live lanes and Chrome fresh-grant support/UX. Signed-in testing remains pending by explicit user choice; P05 is not accepted. [Validation](VALIDATION.md#p05-public-live-worker-diagnostic-checkpoint--2026-11-19) records full preservation and audit receipts.

Bounded anonymous source-v1 playlist downloads on **2026-11-19**, based on `eb10767`, run the pinned two-entry YouTube playlist (`playlist_items: "1-2"`, `options.is_playlist: true`, `format_id: "best"`) through actual owned workers with per-worker redacted `--observe-worker` telemetry (`tests/check_live_playlists.py`) across all three runtimes without altering any of the **132 runtime inputs**:

| Bounded public playlist lane | Fedora host | Ubuntu userspace | Windows 11 |
| --- | --- | --- | --- |
| YouTube 2-entry playlist (`1-2`) | Parent expanded → 2 children (`AUTH_REQUIRED` + `ADMISSION_LIMITED`), group cancelled (5.1 s) | Parent expanded → 2 children (`AUTH_REQUIRED` + `ADMISSION_LIMITED`), group cancelled (6.1 s) | Parent expanded → 2 children (`AUTH_REQUIRED` + `ADMISSION_LIMITED`), group cancelled (8.5 s) |
| Signed-in playlist | Pending by user choice | Pending by user choice | Pending by user choice |

Directly observed across all three runtimes:
- **Parent expansion (Worker #1):** completes cleanly on all three runtimes (`GET:200` × 1, `POST:200` × 1, zero errors) and atomically commits **2 child media jobs** (`playlist_parents: 1`, `playlist_children: 2`).
- **Child workers (Workers #2 & #3):** one child acquires the per-host lease and hits `GET:403` → `RequestFailure("AUTH_REQUIRED")` (`waiting_for_auth`) after 3–4 requests (`GET:200`, `POST:200`, `[GET:302 on host]`, `GET:403`); the sibling child launched concurrently by default queue concurrency (`max_concurrent_downloads: 3`) hits `backend.services.request_budget.acquire` → `RequestFailure("ADMISSION_LIMITED")` (`retry_wait`, `0` HTTP requests) while the first child holds the `MAX_PER_HOST=1` lease.
- **Group cancellation & cleanup:** one v1 `downloads.action` `cancel` on the parent atomically cancels the parent and both children, joins all three owned workers, and leaves **3 downloads (1 parent, 2 children) / 3 attempts (1 complete, 2 failed)** with zero unfinished jobs, probe authorities, leases or unreleased owners on every runtime.

One immutable **310-file snapshot** (`source-1.zip`, `16d72942...`) preserves all **132 runtime inputs**; **37** offline live helpers pass per runtime. Evidence is `packaging/out/p05-live-playlists-ssohkagh/`, via `p05-live-playlists-current.json` (**383** indexed files; Ubuntu/Windows archives verify **18/23** files). Signed-in testing remains pending by explicit user choice; P05 is not accepted. [Validation](VALIDATION.md#p05-public-live-playlist-checkpoint--2026-11-19) records full preservation and audit receipts.

Read-only `ffprobe`/`ffmpeg` binary root-cause observations on **2026-11-20**, based on `af3ee84`, inspect the unchanged bundled **7.0.2-static** (`johnvansickle.com`, ELF) and **7.0.2-essentials** (`gyan.dev`, PE) tools offline against the retained Ubuntu Mux BBB MPEG-TS output (`22,418,060` bytes, SHA-256 `dca82b4374ee198b5a605dc03b0924152f6122e594df577a3e9ce8b63a9db0d3`) through `tests/check_live_ffprobe_diagnostics.py` across all three runtimes with zero production file changes:

| Read-only `ffprobe`/`ffmpeg` `gconv` diagnosis | Fedora host | Ubuntu 24.04.5 userspace | Windows 11 |
| --- | --- | --- | --- |
| Bundled tool binary | Static Debian 8 / glibc 2.28 ELF (`has_embedded_multiarch_gconv_path=true`) | Static Debian 8 / glibc 2.28 ELF (`has_embedded_multiarch_gconv_path=true`) | Gyan `7.0.2` PE (`is_pe=true`, no `gconv`) |
| Target `/usr/lib/x86_64-linux-gnu/gconv` | Absent (`false`); uses `/usr/lib64/gconv/` | Present (`gconv-modules.cache` = glibc 2.39 format) | Not applicable (`false`) |
| Default env (`GCONV_PATH` unset) | `ffprobe` JSON, `ffprobe -v trace`, `ffmpeg -f null -` exit `0` | All 3 exit **`-11` (`SIGSEGV`)** at `MPEGTS_SDT_TAG_0X48_DVB_SERVICE` | All 3 exit `0` |
| Isolated env (`GCONV_PATH=""` or nonexistent path) | All 3 exit `0` (`mpegts`, 2 streams) | All 3 exit `0` (`mpegts`, 2 streams, full decode `1.26–1.31 s`) | All 3 exit `0` |

This directly isolates why Ubuntu HLS failed while Fedora host and Windows passed: Debian 8 static glibc 2.28 compiles in `/usr/lib/x86_64-linux-gnu/gconv`; when `ffprobe`/`ffmpeg` parses DVB SDT descriptor `0x48` on MPEG-TS via `iconv_open()`, Ubuntu 24.04.5's multiarch `gconv-modules.cache` (glibc 2.39 format) triggers `SIGSEGV`, whereas Fedora (`/usr/lib64/gconv/`) and Windows PE never hit that cache, and `GCONV_PATH` isolation immediately restores clean `0` exits in Ubuntu.

Two immutable **313-file snapshots** preserve all **132 runtime inputs** (`source-1.zip` retaining the initial placeholder sample-hash failure; `source-2.zip` matching the retained Ubuntu MPEG-TS SHA-256). **45** offline live helpers pass per runtime. Evidence is `packaging/out/p05-live-ffprobe-diagnosis-3ustvbuc/`, via `p05-live-ffprobe-diagnosis-current.json` (**710** indexed files; Ubuntu archives verify **12/11** files and Windows archive verifies **20** files / **21** members).

Replacing the static Linux FFmpeg pin with dynamic glibc `>=2.28` **BtbN `n7.1.5-12-g1fdbca85aa`** on **2026-11-20** resolves the Ubuntu `ffprobe`/`ffmpeg` `gconv` crash across offline diagnostics and live HLS download while keeping all **132 runtime inputs** unchanged:

| Post-BtbN `n7.1.5` re-verification | Fedora host | Ubuntu 24.04.5 userspace | Windows 11 |
| --- | --- | --- | --- |
| Read-only MPEG-TS `ffprobe`/`ffmpeg` diag (`GCONV_PATH` unset & isolated) | 9/9 exit `0` (`has_embedded_multiarch_gconv_path=false`) | 9/9 exit `0` (`crashes_with_default_env=false`) | Retained PE 7.0.2 (`9/9` pass) |
| Live `mux-bbb` HLS (`--observe-worker`, frozen `n7.1.5` tools) | Prior pass (`345aff26...`) | **Complete / full decode PASS** (`132` reqs, 5/5 tool exit `0`, 120.4 s) | Prior pass (`345aff26...`) |
| Signed-in media | Pending by user choice | Pending by user choice | Pending by user choice |

Live Ubuntu `mux-bbb` (`source-1.zip` **314** tracked files, SHA-256 `afa8c8ee3d6308f52d2561a9bf8ab5017d3f69fe7c00403bcfb9a152f4eecc7a`) outputs **20,049,872 bytes** (**634.633 seconds**, 1 video / 1 audio stream, `full_decode_passed=true`, SHA-256 `ad3571f80c63ddc47b0cb5cb689ef0754c63b623c9d6471cbc31cefc2488586b`, differing from `7.0.2`'s digest only in muxer Lavf encoder version metadata) and cleans up with **1 download / 1 attempt** and zero unreleased authorities. Evidence is `packaging/out/p05-linux-ffmpeg-btbn/`, via `p05-linux-ffmpeg-btbn-current.json` (**487** indexed files; Ubuntu `37`-file / Windows `10`-file archives verify). Next: complete remaining selected frozen/native/browser live lanes, live-site matrix updates and Chrome fresh-grant support/UX; signed-in remains pending by explicit user choice and P05 is not accepted. [Validation](VALIDATION.md#p05-linux-ffmpeg-btbn-715-replacement-checkpoint--2026-11-20) records full preservation and audit receipts.

Bounded frozen/native/browser live lanes on **2026-11-20** (`tests/check_live_native.py`, based on `b798d6f` plus the new test runner/helpers in immutable **316-file `source-4.zip`**, SHA-256 `60f103e10991b2df270ef7d57de4dbe69dba0884ce8bca6f00e6ae0eea9f6062`) execute real `vidgrab-native-host` → `vidgrab-supervisor` / compiled `vidgrab-desktop` → frozen `vidgrab-engine` → frozen `--worker` pipelines without changing any of the **132 runtime inputs**:

| Bounded frozen / native / browser live lane | Fedora host (`BtbN n7.1.5`) | Ubuntu 24.04.5 userspace (`BtbN n7.1.5`) | Ubuntu 24.04.5 GNOME VM (`WebKit` + `BtbN n7.1.5`) | Windows 11 VM (`WebView2` + `Gyan 7.0.2`) |
| --- | --- | --- | --- | --- |
| Frozen-native: MDN flower / explicit defaults | Complete, full decode (`0cd83d944a...`, 2.7 s) | Complete, full decode (`0cd83d944a...`, 3.2 s) | Complete, full decode (`0cd83d944a...`, 4.7 s) | Complete, full decode (`0cd83d944a...`, 3.0 s) |
| Frozen-native: Mux BBB / HLS probe quality | Complete, full decode (`ad3571f80c...`, 12.3 s) | Complete, full decode (`ad3571f80c...`, 15.8 s) | Complete, full decode (`ad3571f80c...`, 18.3 s) | Complete, full decode (`345aff26ba...`, 19.8 s) |
| Frozen-native: YouTube short sample | `AUTH_REQUIRED`, guard-cancelled (3.6 s) | `AUTH_REQUIRED`, guard-cancelled (3.7 s) | `AUTH_REQUIRED`, guard-cancelled (5.6 s) | `AUTH_REQUIRED`, guard-cancelled (3.8 s) |
| Frozen-native: Unsupported HTML | Rejected without enqueue (`UNSUPPORTED_MEDIA`, 0.7 s) | Rejected without enqueue (`UNSUPPORTED_MEDIA`, 0.6 s) | Fails `ENGINE_UNAVAILABLE` at 2 s supervisor read timeout | Rejected without enqueue (`UNSUPPORTED_MEDIA`, 0.6 s) |
| Browser-live Chrome: MDN flower / `#format-modal` defaults | — | — | Complete, full decode (`0cd83d944a...`, Chrome 157.0.8103.38, 8.3 s) | Complete, full decode (`0cd83d944a...`, Chrome 157.0.8103.51, 8.8 s) |
| Browser-live Firefox: Mux BBB / `#format-modal` lowest resolution | — | — | Complete, full decode (`ad3571f80c...`, Firefox 160.0, 24.1 s) | Complete, full decode (`345aff26ba...`, Firefox 160.0.1, 26.6 s) |
| Signed-in media | Pending by user choice | Pending by user choice | Pending by user choice | Pending by user choice |

Key cross-platform observations from the frozen/native/browser runs:
- **Linux/Windows media equivalence & browser/desktop UI journeys:** Fedora host, Ubuntu userspace, Ubuntu GNOME Wayland VM, and Windows 11 all produce exact **1,128,375-byte** (`0cd83d944a...`) MDN outputs and **20,049,872-byte** (`ad3571f80c...` on BtbN `n7.1.5` / `345aff26ba...` on Gyan `7.0.2`) Mux HLS outputs across both frozen-native IPC and live installed Chrome/Firefox `#format-modal` → native host → compiled `vidgrab-desktop` → frozen engine → popup History `#task-card-*` flows, with cleanup leaving zero unfinished jobs, probes, or request leases (`unreleased_owners=0` on POSIX, `1` released closed Job Object owner row on Windows).
- **Ubuntu GNOME VM `iana-example` 2-second supervisor read timeout finding:** inside the ephemeral QEMU/SLIRP Ubuntu GNOME VM, `vidgrab-engine` completed `GenericIE` `Unsupported URL` extraction cleanly in `engine.log`, but SLIRP probe latency pushed `probes.create` past `vidgrab-supervisor`'s hardcoded `2`-second `engine_request` TCP read timeout (`desktop/supervisor/src/transport.rs`), surfacing `ENGINE_UNAVAILABLE` to the frozen-native lane.
- **Preserved initial harness/platform failures:** initial Linux `TMPDIR` `sun_path > 107` `ENAMETOOLONG`, Windows `WindowsBrowserTask` path/root mismatch, Windows `owner.lock`/Job-Object owner release semantics (`live-native-20261120` & `-2` retained on `C:`), Podman missing-`--init` PID-1 zombie reaping timeout (`ubuntu-work-3`), and Ubuntu VM missing `gi` `PYTHONPATH` failure remain preserved rather than hidden.

Eight offline helpers pass on all runtimes and **474** host guarded tests pass (`136.122 s`). Evidence is `packaging/out/p05-live-native-current.json` (`packaging/out/p05-live-native-KQ5YLexx/`, **2,072** indexed files; archives verified for Ubuntu userspace `d9a69e17...`, Ubuntu GNOME VM `ebf35677...`, and Windows 11 `0553c509...`). Chrome fresh-grant support/UX is recorded under [ADR-028](DECISIONS.md#adr-028--scoped-media-discovery-and-format-review) (`c99ff95`). **P05 is accepted within this recorded controlled-fixture and bounded anonymous live-compatibility scope; signed-in media remains pending by explicit user choice, all preserved failures and the unresolved Windows localhost `ENGINE_UNAVAILABLE` on HTTP 429 limit remain open records, and P06 has not started.** [Validation](VALIDATION.md#p05-scoped-acceptance-review--2026-11-20) records full acceptance receipts.
