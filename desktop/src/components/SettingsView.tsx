import type { SettingsPatch, SettingsSnapshot } from '../../../packages/protocol/generated/client';
import { DownloadDefaults } from '../DownloadDefaults';
import type { DesktopPreferences, DesktopPreferencesPatch } from '../protocol';

interface Props {
  settings: SettingsSnapshot | null;
  desktopPrefs: DesktopPreferences | null;
  busy: boolean;
  connected: boolean;
  onSaveDefaults: (revision: number, patch: SettingsPatch) => Promise<SettingsSnapshot>;
  onReloadDefaults: () => Promise<SettingsSnapshot>;
  onPickFolder: () => Promise<string | null>;
  onUpdateDesktopPrefs: (patch: DesktopPreferencesPatch) => Promise<void>;
}

export function SettingsView({
  settings,
  desktopPrefs,
  busy,
  connected,
  onSaveDefaults,
  onReloadDefaults,
  onPickFolder,
  onUpdateDesktopPrefs,
}: Props) {
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
    </div>
  );
}
