import type { SettingsPatch, SettingsSnapshot } from '../../../packages/protocol/generated/client';
import { DownloadDefaults } from '../DownloadDefaults';
import type {
  DesktopPreferences,
  DesktopPreferencesPatch,
  LegacyConfigImport,
  UpdateStatus,
} from '../protocol';

interface Props {
  settings: SettingsSnapshot | null;
  desktopPrefs: DesktopPreferences | null;
  updateStatus: UpdateStatus | null;
  legacyImport: LegacyConfigImport | null;
  activeDownloads: boolean;
  busy: boolean;
  connected: boolean;
  onSaveDefaults: (revision: number, patch: SettingsPatch) => Promise<SettingsSnapshot>;
  onReloadDefaults: () => Promise<SettingsSnapshot>;
  onPickFolder: () => Promise<string | null>;
  onUpdateDesktopPrefs: (patch: DesktopPreferencesPatch) => Promise<void>;
  onCheckUpdates: () => Promise<void>;
  onApplyUpdate: () => Promise<void>;
  onRollbackUpdate: () => Promise<void>;
  onImportLegacyConfig: () => Promise<void>;
}

export function SettingsView({
  settings,
  desktopPrefs,
  updateStatus,
  legacyImport,
  activeDownloads,
  busy,
  connected,
  onSaveDefaults,
  onReloadDefaults,
  onPickFolder,
  onUpdateDesktopPrefs,
  onCheckUpdates,
  onApplyUpdate,
  onRollbackUpdate,
  onImportLegacyConfig,
}: Props) {
  const availableVersion =
    updateStatus?.pending_update?.version ||
    (updateStatus?.last_check?.status === 'update_available'
      ? updateStatus.last_check.available_version
      : null);

  return (
    <div className="settings-stack">
      {settings ? (
        <DownloadDefaults
          settings={settings}
          busy={busy || !connected}
          save={onSaveDefaults}
          reload={onReloadDefaults}
          onPickFolder={onPickFolder}
        />
      ) : (
        <section className="card">
          <h2>Download defaults</h2>
          <p className="helper">Waiting for the engine to load settings…</p>
        </section>
      )}

      <section className="card" aria-labelledby="desktop-behavior-heading">
        <h2 id="desktop-behavior-heading">Desktop &amp; tray behavior</h2>
        <p className="helper">
          Control how VidGrab starts with your desktop session and behaves when closed.
        </p>
        {desktopPrefs ? (
          <div className="prefs-list">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={desktopPrefs.start_at_login}
                disabled={busy || !desktopPrefs.packaged}
                onChange={event => void onUpdateDesktopPrefs({ start_at_login: event.target.checked })}
              />
              Start VidGrab when I sign in
            </label>
            <p className="helper">
              {!desktopPrefs.packaged
                ? 'Login startup is available in packaged builds.'
                : desktopPrefs.autostart_state === 'registered'
                ? 'Autostart entry is registered for this user.'
                : desktopPrefs.autostart_state === 'conflict'
                ? 'A foreign startup entry already exists; VidGrab will not overwrite it.'
                : desktopPrefs.autostart_error
                ? desktopPrefs.autostart_error
                : 'VidGrab does not start automatically at login.'}
            </p>

            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={desktopPrefs.start_minimized}
                disabled={busy || !desktopPrefs.start_at_login || !desktopPrefs.tray_available}
                onChange={event => void onUpdateDesktopPrefs({ start_minimized: event.target.checked })}
              />
              Start minimized to tray on login
            </label>

            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={desktopPrefs.close_to_tray}
                disabled={busy || !desktopPrefs.tray_available}
                onChange={event => void onUpdateDesktopPrefs({ close_to_tray: event.target.checked })}
              />
              Closing this window keeps VidGrab running in the tray
            </label>
            {!desktopPrefs.tray_available && (
              <p className="helper">
                This desktop environment has no system tray. Keep this window open while downloads run; use Quit VidGrab to close the app.
              </p>
            )}
            <p className="helper">
              Scheduled downloads run while VidGrab is open and the computer is awake; missed times run on next startup.
            </p>
          </div>
        ) : (
          <p className="helper">Loading desktop preferences…</p>
        )}
      </section>

      <section className="card" aria-labelledby="updates-heading">
        <h2 id="updates-heading">Software updates &amp; rollback</h2>
        <p className="helper">
          Updates are cryptographically signed, create a pre-update database backup, and wait for active transfers to finish before switching engines.
        </p>

        {updateStatus ? (
          <>
            <dl className="browser-registration">
              <div>
                <dt>Application version</dt>
                <dd>v{updateStatus.app_version}</dd>
              </div>
              <div>
                <dt>Active engine bundle</dt>
                <dd>v{updateStatus.engine_bundle_version}</dd>
              </div>
              <div>
                <dt>Release channel</dt>
                <dd>{updateStatus.channel} ({updateStatus.platform})</dd>
              </div>
              <div>
                <dt>Stored pre-update backups</dt>
                <dd>{updateStatus.backups_count ?? 0}</dd>
              </div>
            </dl>

            {updateStatus.rollback_notice && (
              <p className="warning-banner" role="status">{updateStatus.rollback_notice}</p>
            )}

            {updateStatus.last_check && (
              <p className="helper" role="status">
                {updateStatus.last_check.status === 'update_available'
                  ? `Update v${updateStatus.last_check.available_version ?? ''} is available on the ${updateStatus.last_check.channel} channel.`
                  : updateStatus.last_check.status === 'up_to_date'
                  ? `Up to date on the ${updateStatus.last_check.channel} channel.`
                  : updateStatus.last_check.status === 'offline'
                  ? 'Update server unreachable; local downloads and settings continue normally.'
                  : `Update check failed: ${updateStatus.last_check.message || 'Unknown error'}`}
              </p>
            )}

            {activeDownloads && (availableVersion || updateStatus.rollback_available) && (
              <p className="helper">
                Finish or pause active downloads before switching or rolling back the engine bundle.
              </p>
            )}

            <div className="connection-actions">
              <button
                className="secondary"
                type="button"
                disabled={busy}
                onClick={() => void onCheckUpdates()}
              >
                Check for updates
              </button>
              {availableVersion && (
                <button
                  className="primary"
                  type="button"
                  disabled={busy || activeDownloads}
                  onClick={() => void onApplyUpdate()}
                >
                  {`Install update v${availableVersion}`}
                </button>
              )}
              {updateStatus.rollback_available && (
                <button
                  className="secondary critical"
                  type="button"
                  disabled={busy || activeDownloads}
                  onClick={() => void onRollbackUpdate()}
                >
                  {`Roll back to v${updateStatus.previous_version || updateStatus.app_version}`}
                </button>
              )}
            </div>
          </>
        ) : (
          <p className="helper">Update status is unavailable.</p>
        )}
      </section>

      <section className="card" aria-labelledby="legacy-migration-heading">
        <h2 id="legacy-migration-heading">Migrate from legacy VidGrab service</h2>
        <p className="helper">
          Import safe download defaults from a legacy <code>config.json</code> without modifying or deleting your existing media files.
        </p>

        <div className="connection-actions">
          <button
            className="secondary"
            type="button"
            disabled={busy || !connected || !settings}
            onClick={() => void onImportLegacyConfig()}
          >
            Import legacy config.json…
          </button>
        </div>

        {legacyImport && (
          <div className="legacy-import-summary">
            <p className="helper">
              Imported {legacyImport.imported_keys.length} setting{legacyImport.imported_keys.length === 1 ? '' : 's'} from{' '}
              <span className="path-text">{legacyImport.source_path}</span>.
            </p>
            {legacyImport.warnings.length > 0 && (
              <ul className="recent-errors">
                {legacyImport.warnings.map((warning, index) => (
                  <li key={index}>{warning}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        <details className="browser-setup">
          <summary>How to stop an older background VidGrab server</summary>
          <p>Stop any standalone legacy server before using the desktop companion so ports and folders do not conflict (see <code>docs/MIGRATION.md</code>):</p>
          <ol>
            <li>
              <strong>Manual server:</strong> Stop any running <code>uvicorn server:app</code> terminal session.
            </li>
            <li>
              <strong>Linux systemd user service:</strong>{' '}
              <code>systemctl --user disable --now vidgrab-backend.service</code>
            </li>
            <li>
              <strong>Windows Task Scheduler:</strong> Disable or remove the legacy <code>VidGrabBackend</code> scheduled task.
            </li>
          </ol>
        </details>
      </section>
    </div>
  );
}
