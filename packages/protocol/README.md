# VidGrab v1 control contract

`backend/api/models.py` is the Pydantic source. From the repository root, run `.venv/bin/python -B -m packages.protocol.generate` and use `--check` to reject stale outputs without writing. Edit `client-runtime.js` for shared runtime behavior, then regenerate. Generated schema, TypeScript/module/classic clients, Rust operation table and `extension/lib/protocol.js` travel together; export performs no application/database/startup I/O.

| Operation | Parameters/result |
| --- | --- |
| `capabilities.get` | Empty parameters; protocol range, message bounds, verified component versions and explicit feature/fixture/pairing flags. |
| `settings.get` | Empty parameters; full app settings plus revision. |
| `settings.patch` | `expected_revision`, partial `patch` and a stable mutation key; stale writes return `REVISION_CONFLICT`. |
| `downloads.list` | Optional `limit` (1–50) and `after` ID; bounded jobs, continuation and complete aggregate counts. |
| `downloads.enqueue` | HTTP(S) media, bounded subtitle/embedding/playlist options and optional scoped `auth_ref`; stable key returns the original job on replay. |
| `events.read` | Optional instance/cursor/limit (1–100); retained invalidations or a consistent paginated snapshot. |
| `probes.create` | HTTP(S) URL and optional exact URL/job-scoped reference; bounded format/entry results and typed auth/admission/upstream failures. |
| `auth.create` | Engine instance, exact URL, optional job, bounded fresh cookies/User-Agent/Referer and TTL; returns a volatile expiring reference. |
| `downloads.action` | Job ID, `expected_revision`, cancel/retry and optional retry auth reference; keyed original accepted status/revision. |
| `downloads.authenticate` | Waiting job ID, current revision and fresh job-scoped reference; keyed acceptance requeues the same job. |

Requests contain `protocol_version: 1`, a 1–128 visible-ASCII `request_id`, operation, parameters and a mutation `idempotency_key`. Reuse the same key and intent after a lost reply; clients do not retry automatically. A settings replay returns its original snapshot without reapplying it, so fetch current settings before editing. Enqueue replay retains the original accepted record after app defaults change. Keys and receipts are scoped to the selected local runtime, with bounded persistent capacity.

Native messaging carries the envelope inside P02 `{spike_version: 1, id, op: "v1.request", params}`. The native host negotiates first; the authenticated engine secret stays inside the owner. Native frames allow 65,536 bytes, inner v1 messages 64,512 and persisted settings snapshots 63,488. Versioned integers are exact browser integers; legacy wide speed values require an explicit repair. Clients validate IDs/version, strict operation results and typed errors.

The 512 retained events contain invalidations, without job URLs/settings values/cookies. Cursor gaps, future cursors or an engine-instance change require resnapshot. A snapshot contains a first job page bounded by the requested limit (capped at 25) and total/unfinished counts; follow `next_after` or refresh pages to get more jobs. This foundation provides explicit event reads rather than a live subscription.

Schema 10 adds at most 10,000 persistent action receipts. Accepted action state/revision and receipt commit together under the owner fence. Cancelling active work records accepted `cancelling`, joins its worker and reaches its later terminal state; replay still returns the original acceptance. Cancelling an already cancelled job records a no-op acceptance. Retry/authentication replay runs before credentials or execution wakeups, including after an engine restart. Omitted added enqueue fields retain the schema-9 identity and original app defaults. Fetch current jobs before applying a new action; stale revisions return `REVISION_CONFLICT`, incompatible states `STATE_CONFLICT`.

Browser contexts are volatile and bind engine instance, exact input URL and optional job. They allow at most 128 live contexts, 10,000 volatile key receipts, bounded cookies/headers and TTLs of 30–900 seconds capped by cookie expiry. Replay does not extend expiry or revive contexts. Engine change/expiry returns `AUTH_CONTEXT_EXPIRED`; wrong URL/job returns `AUTH_CONTEXT_SCOPE`. Cookies and forwarded headers stay outside SQLite/job options. Before actual urllib requests, Cookie/Referer are stripped for every other origin, including scheme/port changes and redirects. Probe identities/cache do not cross contexts. Job refresh requires a job-scoped reference.

The packaged entrypoint enables ordinary HTTP(S) through P03 request/attempt authority and verified bundled tools. The factory defaults to fixture mode; `/spike/fixture` retains its per-job network guard. Arbitrary paths, headers, options and executable arguments are unavailable. Packaged execution disables legacy custom/extra arguments, and pairing stays disabled. Probe format limits are 1–100, entry limits 1–128; request smaller limits if a result exceeds the message budget. Authentication/permanent errors require explicit action; local admission and upstream throttling retain distinct typed provenance/deadlines.

Browser preferences remain in browser storage. Both backgrounds expose `vidGrabCompanion`; the desktop exposes `companionControl` through its Tauri adapter. Current popup/options and desktop screens still await migration. [ADR-026](../../docs/DECISIONS.md#adr-026--p04-ordinary-media-and-scoped-authentication) and [the media checks](../../docs/VALIDATION.md#p04-ordinary-media-actions-and-authentication--2026-11-15) define current Fedora evidence and remaining P04 target/client gates.
