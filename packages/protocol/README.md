# VidGrab v1 control foundation

`backend/api/models.py` is the Pydantic source. From the repository root, run `.venv/bin/python -B -m packages.protocol.generate` and use `--check` to reject stale outputs without writing. Edit `client-runtime.js` for shared runtime behavior, then regenerate. Generated schema, TypeScript/module/classic clients, Rust operation table and `extension/lib/protocol.js` travel together; export performs no application/database/startup I/O.

| Operation | Parameters/result |
| --- | --- |
| `capabilities.get` | Empty parameters; protocol range, message bounds, verified component versions and explicit feature/fixture/pairing flags. |
| `settings.get` | Empty parameters; full app settings plus revision. |
| `settings.patch` | `expected_revision`, partial `patch` and a stable mutation key; stale writes return `REVISION_CONFLICT`. |
| `downloads.list` | Optional `limit` (1–50) and `after` ID; bounded jobs, continuation and complete aggregate counts. |
| `downloads.enqueue` | Allowlisted media fields and a stable mutation key; returns the original job on identical replay. |
| `events.read` | Optional instance/cursor/limit (1–100); retained invalidations or a consistent paginated snapshot. |

Requests contain `protocol_version: 1`, a 1–128 visible-ASCII `request_id`, operation, parameters and a mutation `idempotency_key`. Reuse the same key and intent after a lost reply; clients do not retry automatically. A settings replay returns its original snapshot without reapplying it, so fetch current settings before editing. Enqueue replay retains the original accepted record after app defaults change. Keys and receipts are scoped to the selected local runtime, with bounded persistent capacity.

Native messaging carries the envelope inside P02 `{spike_version: 1, id, op: "v1.request", params}`. The native host negotiates first; the authenticated engine secret stays inside the owner. Native frames allow 65,536 bytes, inner v1 messages 64,512 and persisted settings snapshots 63,488. Versioned integers are exact browser integers; legacy wide speed values require an explicit repair. Clients validate IDs/version, strict operation results and typed errors.

The 512 retained events contain invalidations, without job URLs/settings values/cookies. Cursor gaps, future cursors or an engine-instance change require resnapshot. A snapshot contains a first job page bounded by the requested limit (capped at 25) and total/unfinished counts; follow `next_after` or refresh pages to get more jobs. This foundation provides explicit event reads rather than a live subscription.

The execution lane is fixture-only: numeric loopback `/fixture.wav`, no forwarded cookies or arbitrary per-job options. New executable legacy settings are rejected, and pairing is disabled. Browser preferences remain in browser storage. Both backgrounds expose `vidGrabCompanion`; the desktop exposes `companionControl` through its Tauri adapter. Current popup/options and desktop screens are not yet migrated. See [ADR-025](../../docs/DECISIONS.md#adr-025--p04-contract-and-settings-foundation) for remaining P04 work and validation limits.
