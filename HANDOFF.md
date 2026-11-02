# Resume VidGrab work here

This file is the current development checkpoint. It is plain Markdown for any agent/editor; chat history and a particular agent application are not required.

## Current state

- **Phase:** P00 planning complete; application implementation begins at P01. There is no desktop app yet.
- **Branch:** `planning/desktop-companion`.
- **Source checkpoint:** `79f7d7e2fecdbd4623dfc0b6d6632daffe349809`, based on `822b9f2`.
- **Planning checkpoint:** the commit containing this file; resolve with `git log -1 --format=%H -- HANDOFF.md`. The bundle receipt identifies its exact exported HEAD without embedding a self-referential hash here.
- **Confirmed:** Linux and Windows first; macOS later. Source snapshot plus offline Git bundle selected. No Git remote is configured.
- **Proposed:** Tauri 2 + TypeScript/React shell, bundled Python engine, app-owned workers, native-messaging bridge and shared protocol. Validate framework and packaging in P02.

## Read order

1. [AGENTS.md](AGENTS.md): current commands, gotchas, mandatory API-call minimization.
2. [docs/PLAN.md](docs/PLAN.md): milestone checklist and acceptance criteria.
3. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): ownership, protocol, lifecycle, reliability and packaging design.
4. [docs/DECISIONS.md](docs/DECISIONS.md): confirmed choices versus recommendations and pending decisions.
5. [docs/VALIDATION.md](docs/VALIDATION.md): current evidence and future test/release matrix.

## Next executable task: P01 runtime isolation

1. Check `git status --short --branch` and `git log -3 --oneline`. Preserve any new local work before starting; create a focused branch from this checkpoint if useful.
2. Inspect `backend/config.py`, `backend/main.py`, `backend/queue_manager.py`, and `backend/test_backend.py`. Plan injection of runtime paths before starting the app: the current tracked DB may contain queued downloads and startup can run pip.
3. Add runtime configuration/app factory and isolated tests as specified by P01. Start with database location injection, no-side-effect app creation, and partial-settings behavior; then explicit migrations and local health.
4. Record the exact new setup/test commands and results. Update this checkpoint with the next incomplete P01 subtask; finish with a focused commit before exporting again.

No further user decision blocks P01. P02 needs Windows and Linux execution evidence; framework/OS support is not considered validated by this plan.

## What is preserved and what is local

Source commit `79f7d7e` captures the pre-existing retry/output/sanitizer changes, removal of hardcoded browser-cookie paths, `.gitignore`, and staged bytecode cleanup. It is a work-in-progress baseline, not a claim those features are correct.

The user approved leaving these current edits local:

| Path | Meaning |
| --- | --- |
| `backend/vidgrab.db` | Current settings/history/queue database |
| `dist/vidgrab-chrome-extension.zip` | Locally generated archive |
| `dist/vidgrab-firefox-extension.zip` | Locally generated archive |
| `store_assets/vidgrab_promo_banner.png` | Modified promotional image |

Their earlier tracked versions still exist in Git and therefore in a full-history bundle. The bundle does not contain the latest working copies, ignored venv/scratch files, installed services, or live download state. Do not auto-start the backend from a clone against its legacy tracked database. P01 replaces this runtime layout.

## Verification at this checkpoint

- Baseline: 11 Python files parsed, 7 JS files passed `node --check`, both manifests parsed.
- Documentation: relative file links, milestone IDs and whitespace checked before committing.
- Export: bundle verification and fresh-clone HEAD/tree comparison are required; exact hashes/results are recorded in the adjacent `receipt.json` generated for the bundle.
- Not performed: live downloads, legacy sanity script, installer/build execution, browser integration, or desktop tests. No runtime application fixes were made in the planning step.

## Resume on another agent/device

Copy the `.bundle`, its checksum, and export receipt from `.handoff/` to the other device. The folder is ignored; it is not itself committed. Git is sufficient to restore the repository; recreate language environments only when needed.

```sh
git clone -b planning/desktop-companion /path/to/vidgrab-handoff.bundle VidGrab
git -C VidGrab rev-parse HEAD
git -C VidGrab status --short --branch
```

Compare HEAD with the receipt. Open `VidGrab` in the chosen agent app and say:

> Read AGENTS.md and HANDOFF.md, then continue the next incomplete task in docs/PLAN.md. Preserve recorded decisions and local work, batch API/tool calls, and update the handoff with verification and a commit at the next checkpoint.

On the same device, open this repository and use that prompt without cloning. The receipt records the branch for future exports if it differs from this initial branch.

## Checkpoint and export protocol

1. Record current milestone/subtask, decisions, changed interfaces, actual checks/results, blockers, and the exact next action here. Update the plan checkboxes and decision log only when their status changed.
2. Review `git status` and stage explicit intended paths. Commit coherent work with its tests/docs; label incomplete checkpoints as WIP. Never rely on an uncommitted source file for a portable handoff.
3. List any deliberately excluded local files. Verify no unexpected dirty source remains; Git bundles include commits, not unstaged/staged edits. Do not use `git add .` to sweep runtime state into a checkpoint.
4. Create a fresh export outside the source tree (commands work in a shell with Git on each platform):

```sh
git rev-parse HEAD
git branch --show-current
git bundle create ../vidgrab-handoff.bundle --all
git bundle verify ../vidgrab-handoff.bundle
```

5. Save HEAD/branch, SHA-256 of the bundle, validation results, and omitted local paths in an adjacent receipt; transfer both. Clone to a new directory and compare HEAD before treating a handoff as complete. Keep the previous bundle until the new one verifies.
6. When a remote is chosen, add its actual user-provided URL and push the checkpoint branch; a new device can clone it instead. Local commits alone are not remote backup.

Prefer one commit per completed subtask/milestone; include HANDOFF changes in that commit so code and context travel together. If interrupted mid-task, explicitly name the unfinished step and known failing checks instead of marking the whole milestone done.
