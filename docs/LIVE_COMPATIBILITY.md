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
