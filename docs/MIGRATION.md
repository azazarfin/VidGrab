# Migrating from Legacy VidGrab to the Desktop Companion

This guide explains how to move from the legacy standalone Python backend (`backend/main.py` via `uvicorn`, `installer/install.sh` / `systemd`, or `installer/install.ps1` / Windows Task Scheduler) to the packaged VidGrab Desktop Companion without losing saved media files or download history.

---

## 1. Stop & Disable Any Legacy Background Service

Before launching the packaged desktop companion, stop any older background server so folders and legacy state do not overlap. **Do not delete your existing download folders (`Downloads/VidGrab` etc.).**

### Manual `uvicorn` terminal session
Press `Ctrl+C` in the terminal running `uvicorn backend.main:app` (or `uvicorn server:app`), or close the session.

### Linux (`systemd --user`)
```bash
systemctl --user disable --now vidgrab-backend.service
rm -f ~/.config/systemd/user/vidgrab-backend.service
systemctl --user daemon-reload
```

### Windows (Task Scheduler)
Run in Command Prompt or PowerShell:
```powershell
schtasks /End /TN "VidGrabBackend"
schtasks /Delete /TN "VidGrabBackend" /F
```
If you no longer need the legacy virtual environment created by `installer/install.sh` or `installer/install.ps1`, you may archive or delete only that virtualenv directory after verifying your migration below.

---

## 2. Import Safe Defaults from Legacy `config.json`

VidGrab can read an existing legacy `config.json` non-destructively (read-only, symlink/reparse-point safe, 256 KiB cap):

1. Open **VidGrab** and go to **Settings → Migrate from legacy VidGrab service**.
2. Click **Import legacy `config.json`…** and select your old `config.json`.
3. Validated settings are applied immediately via the `settings.patch` contract:
   - `download_dir`, `default_format`, `default_quality`, `filename_template`
   - `max_concurrent`, `speed_limit`, `download_threads`, `max_retries`
   - `sponsorblock_enabled`, `sponsorblock_categories`, `sponsorblock_mode`
   - `postprocess_codec`, `postprocess_normalize_audio`, `gpu_accelerated_conversion`
   - `transliterate_bangla_filenames`

### Intentionally Skipped Legacy Keys (With Warnings)
For security, determinism, and supply-chain safety (`ADR-021`, `ADR-030`), two legacy keys are **never** imported and surface explicit warnings in the UI:
- **`custom_ytdlp_args`**: Arbitrary command-line flags are disabled in the packaged VidGrab engine.
- **`auto_update_ytdlp`**: Runtime `pip` / self-updates are replaced by signed application/engine bundle updates under **Settings → Software updates & rollback**.

---

## 3. Import Legacy SQLite Download History (Optional)

If you have an existing legacy `vidgrab.db` SQLite database and want to preserve past queue/history records into a fresh companion data directory (`data_dir` without an existing `vidgrab.db`):

```bash
# After stopping the legacy service completely:
.venv/bin/python -m backend.db /path/to/legacy/vidgrab.db \
  --data-dir /path/to/new-companion-data-dir \
  --download-dir /path/to/Downloads/VidGrab
```

This creates a verified backup snapshot of the legacy database under `data_dir/backups/`, runs schema migrations safely (`PRAGMA quick_check`), and never modifies the source file or downloaded media files.

---

## 4. Backups, Updates & Uninstall Safety

- **Pre-migration & pre-update backups:** Before applying a higher SQLite schema migration or activating a signed engine update, VidGrab snapshots the active database and configuration under `data_dir/backups/` (retaining the 3 most recent backups per category).
- **Automatic rollback:** If an updated engine fails `--self-check`, readiness verification, or crashes prematurely during startup, the supervisor automatically restores the previous known-good bundle and surfaces a redacted status notice in **Settings** and **Diagnostics**.
- **Uninstalling preserves user media:** Removing the `.deb` package, AppImage, or Windows per-user installation removes application/bridge files while leaving your download folder (`download_dir`) and saved media untouched.
