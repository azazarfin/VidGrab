import type { ApplicationStatus, BrowserConnection } from '../protocol';

interface Props {
  browserConnection?: BrowserConnection;
  browserConnectionError?: string;
  application?: ApplicationStatus;
  applicationError?: string;
  busy: boolean;
  activeDownloads: boolean;
  onSetBrowserConnection: (enabled: boolean) => Promise<void>;
  onInstallAppImage: () => Promise<void>;
  onOpenSoftwareManager: () => Promise<void>;
  onConfirmRemoval: () => void;
}

const BROWSER_STATES = {
  missing: 'Not registered',
  registered: 'Registered',
  conflict: 'Another installation',
};

export function BrowserView({
  browserConnection,
  browserConnectionError,
  application,
  applicationError,
  busy,
  activeDownloads,
  onSetBrowserConnection,
  onInstallAppImage,
  onOpenSoftwareManager,
  onConfirmRemoval,
}: Props) {
  const registered = Boolean(
    browserConnection?.enabled
    && browserConnection.bridge_ready
    && browserConnection.chrome === 'registered'
    && browserConnection.firefox === 'registered',
  );

  return (
    <div className="browser-stack">
      <section className="browser-connections card" aria-labelledby="browser-title">
        <h2 id="browser-title">Browser connections</h2>
        {browserConnection ? (
          <>
            <p className="browser-connection-summary" role="status">
              {!browserConnection.enabled
                ? 'Disconnected'
                : registered
                ? 'Registered for Chrome and Firefox'
                : 'Connection needs repair'}
            </p>
            <dl className="browser-registration">
              <div>
                <dt>Google Chrome</dt>
                <dd>{BROWSER_STATES[browserConnection.chrome]}</dd>
              </div>
              <div>
                <dt>Firefox</dt>
                <dd>{BROWSER_STATES[browserConnection.firefox]}</dd>
              </div>
            </dl>
            <p className="helper">
              Registration lets the extension ask for access. Confirm access with its native connection test.
            </p>
            <div className="connection-actions">
              {!registered && (
                <button
                  className="secondary"
                  type="button"
                  disabled={busy}
                  data-connection-action="connect"
                  onClick={() => void onSetBrowserConnection(true)}
                >
                  {browserConnection.enabled ? 'Repair connection' : 'Connect browsers'}
                </button>
              )}
              {browserConnection.enabled && (
                <button
                  className="secondary"
                  type="button"
                  disabled={busy}
                  data-connection-action="disconnect"
                  onClick={() => void onSetBrowserConnection(false)}
                >
                  Disconnect browsers
                </button>
              )}
            </div>
            <p className="helper">
              Disconnecting preserves your downloads and history, and stays in effect when VidGrab reopens.
            </p>
          </>
        ) : (
          <p className="helper">
            {browserConnectionError
              ? 'The browser connection settings need attention.'
              : 'Browser connection controls are available in packaged builds.'}
          </p>
        )}
        {browserConnectionError && <p className="error" role="alert">{browserConnectionError}</p>}
      </section>

      {application?.kind !== 'windows' && (
        <details className="browser-setup card">
          <summary>Firefox Flatpak setup</summary>
          <p>Firefox Flatpak needs one browser setting to allow the native connection test.</p>
          <ol>
            <li>Open VidGrab from its current location once to register the browser connection.</li>
            <li>
              In Firefox, open <code>about:config</code>, accept the browser notice if shown, and search for{' '}
              <code className="portal-preference">widget.use-xdg-desktop-portal.native-messaging</code>
            </li>
            <li>Set the existing <strong>Number</strong> value to <code>1</code> and save it. Fully quit Firefox, then reopen it.</li>
            <li>
              After restarting Firefox, load the temporary VidGrab add-on, then run its native connection test with Firefox visible. Choose <strong>Allow</strong> when the desktop asks Firefox to launch <strong>VidGrab</strong>.
            </li>
          </ol>
          <p>If Firefox Flatpak was installed during this desktop session, sign out and back in before connecting so the desktop can recognize it.</p>
        </details>
      )}

      <details className="application-setup card">
        <summary>Install and remove VidGrab</summary>
        {application?.kind === 'appimage' ? (
          <>
            <p className="installation-status" role="status">
              {application.installed ? 'Installed in Applications' : 'Running from an AppImage file'}
            </p>
            {application.installed ? (
              <>
                <p>Open <strong>VidGrab</strong> from Applications. The installed copy stays in its own folder.</p>
                <p className="installation-path">{application.app}</p>
                <p>
                  Removing the installed copy disconnects browser access and quits VidGrab. Your history and saved files are kept.
                </p>
                <button
                  className="secondary critical"
                  type="button"
                  onClick={onConfirmRemoval}
                  disabled={busy || activeDownloads}
                >
                  Remove installed copy…
                </button>
              </>
            ) : (
              <>
                <p>Install a copy and an Applications shortcut for this user. You can keep using this window while the copy is installed.</p>
                <button className="secondary" type="button" onClick={() => void onInstallAppImage()} disabled={busy}>
                  Install AppImage
                </button>
                <p>To remove a portable AppImage, quit VidGrab and move the downloaded file to Trash in Files.</p>
              </>
            )}
          </>
        ) : application?.kind === 'deb' ? (
          <>
            <p className="installation-status">Installed as an Ubuntu package</p>
            <p>
              Prepare for removal here, then use <strong>Manage</strong> in App Center to uninstall <strong>VidGrab</strong>. Browser access is disconnected before the app quits; your history and saved files are kept.
            </p>
            <div className="connection-actions">
              <button className="secondary" type="button" onClick={() => void onOpenSoftwareManager()} disabled={busy}>
                Open App Center
              </button>
              <button
                className="secondary critical"
                type="button"
                onClick={onConfirmRemoval}
                disabled={busy || activeDownloads}
              >
                Prepare to remove…
              </button>
            </div>
          </>
        ) : application?.kind === 'windows' ? (
          <>
            <p className="installation-status">
              {application.installed ? 'Installed for this Windows user' : 'Running from a Windows folder'}
            </p>
            <p>
              Prepare for removal here, then uninstall <strong>VidGrab</strong> in <strong>Settings → Apps → Installed apps</strong>. Your history and saved files are kept.
            </p>
            <div className="connection-actions">
              <button className="secondary" type="button" onClick={() => void onOpenSoftwareManager()} disabled={busy}>
                Open Installed apps
              </button>
              <button
                className="secondary critical"
                type="button"
                onClick={onConfirmRemoval}
                disabled={busy || activeDownloads}
              >
                Prepare to remove…
              </button>
            </div>
          </>
        ) : (
          <p className="helper">{applicationError || 'Installation controls are available in packaged builds.'}</p>
        )}
        {activeDownloads && application?.installed && (
          <p className="helper">Finish active downloads before removing VidGrab.</p>
        )}
      </details>
    </div>
  );
}
