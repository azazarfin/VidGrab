import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { companionSession } from './protocol';
import { DownloadDefaults } from './DownloadDefaults';
import type { ControlState, Job, SettingsPatch, SettingsSnapshot } from '../../packages/protocol/generated/client';

interface Snapshot {
  state: 'starting' | 'ready' | 'recovering' | 'failed';
  tray_available: boolean;
  downloads_active?: boolean;
  browser_connection?: BrowserConnection;
  browser_connection_error?: string;
  application?: ApplicationStatus;
  application_error?: string;
}

interface ApplicationStatus {
  kind: 'deb' | 'appimage' | 'windows';
  installed: boolean;
  app: string;
  shortcut?: string;
}

interface BrowserConnection {
  enabled: boolean;
  bridge_ready: boolean;
  chrome: 'missing' | 'registered' | 'conflict';
  firefox: 'missing' | 'registered' | 'conflict';
}

const states = { starting: 'Starting', ready: 'Connected', recovering: 'Reconnecting', failed: 'Engine stopped' };
const jobStates: Record<string, string> = { queued: 'Queued', downloading: 'Downloading', postprocessing: 'Converting', complete: 'Finished', failed: 'Failed', cancelled: 'Cancelled', cancelling: 'Cancelling', waiting_for_auth: 'Fresh cookies needed', retry_wait: 'Retry scheduled', waiting_children: 'Waiting for playlist items' };

export function App() {
  const [snapshot, setSnapshot] = useState<Snapshot>({ state: 'starting', tray_available: false });
  const [url, setUrl] = useState('');
  const [control, setControl] = useState<ControlState>(companionSession.state);
  const [audioOnly, setAudioOnly] = useState(false);
  const [audioFormat, setAudioFormat] = useState('mp3');
  const [busy, setBusy] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [actionError, setActionError] = useState('');
  const [announcement, setAnnouncement] = useState('');
  const [removalError, setRemovalError] = useState('');
  const removalDialog = useRef<HTMLDialogElement>(null);

  function explain(reason: unknown) {
    const code = (reason as { code?: string })?.code;
    if (code === 'REVISION_CONFLICT') return 'This item changed in another window. Your edits are kept; review its current state before trying again.';
    if (companionSession.pending) return 'The request may have succeeded. Check it before submitting another change.';
    return reason instanceof Error ? reason.message : String(reason);
  }

  async function refreshControl(force = false) {
    const current = await companionSession.sync(force);
    setControl(current);
    return current;
  }

  useEffect(() => {
    let disposed = false;
    let timeout: ReturnType<typeof setTimeout>;
    async function refresh() {
      const [native, versioned] = await Promise.allSettled([
        invoke<Snapshot>('companion_status'), companionSession.sync(),
      ]);
      if (!disposed) {
        if (native.status === 'fulfilled') setSnapshot(native.value);
        else setSnapshot(current => ({ ...current, state: 'recovering' }));
        if (versioned.status === 'fulfilled') setControl(versioned.value);
        else setControl(companionSession.state);
        setConnectionError(versioned.status === 'rejected' ? explain(versioned.reason)
          : native.status === 'rejected' ? explain(native.reason) : '');
        timeout = setTimeout(refresh, 2000);
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timeout); };
  }, []);

  async function download(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setActionError(''); setAnnouncement('');
    try {
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
        throw new Error('Enter an HTTP or HTTPS media URL without embedded credentials.');
      }
      await companionSession.mutate('downloads.enqueue', { url, audio_only: audioOnly,
        audio_format: audioFormat as 'mp3' | 'm4a' | 'opus' | 'wav' | 'flac' });
      await refreshControl();
      setUrl(''); setAnnouncement('Download queued.');
    } catch (reason) { setActionError(explain(reason)); }
    finally { setBusy(false); }
  }

  async function saveDefaults(revision: number, patch: SettingsPatch): Promise<SettingsSnapshot> {
    setBusy(true); setActionError(''); setAnnouncement('');
    try {
      await companionSession.mutate('settings.patch', { expected_revision: revision, patch });
      const current = await refreshControl(true);
      setAnnouncement('Defaults saved.');
      return current.snapshot!.settings;
    } catch (reason) {
      const message = explain(reason); setActionError(message); throw new Error(message);
    } finally { setBusy(false); }
  }

  async function jobAction(job: Job, action: 'cancel' | 'retry') {
    setBusy(true); setActionError(''); setAnnouncement('');
    try {
      await companionSession.mutate('downloads.action', { job_id: job.id, expected_revision: job.revision, action });
      await refreshControl(); setAnnouncement('Download action accepted.');
    } catch (reason) {
      setActionError(explain(reason));
      if (!companionSession.pending) { try { await refreshControl(); } catch {} }
    } finally { setBusy(false); }
  }

  async function checkRequest() {
    setBusy(true); setActionError(''); setAnnouncement('');
    try {
      await companionSession.replay();
      await refreshControl(true); setAnnouncement('Request checked. Current downloads and settings refreshed.');
    } catch (reason) { setActionError(explain(reason)); }
    finally { setBusy(false); }
  }

  async function quit() {
    setBusy(true);
    try {
      await invoke('quit_companion');
    } catch (reason) {
      setActionError(String(reason));
      setBusy(false);
    }
  }

  async function setBrowserConnection(enabled: boolean) {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const connection = await invoke<BrowserConnection>('set_browser_connection', { enabled });
      setSnapshot(current => ({ ...current, browser_connection: connection, browser_connection_error: undefined }));
      setAnnouncement(enabled ? 'Browser registration saved.' : 'Browser access disconnected. Downloads keep running.');
    } catch (reason) {
      setActionError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function installAppImage() {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const application = await invoke<ApplicationStatus>('install_appimage');
      setSnapshot(current => ({ ...current, application, application_error: undefined }));
      setAnnouncement('Installed in Applications. Your current download session stays open.');
    } catch (reason) {
      setActionError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function removeApplication() {
    setBusy(true);
    setRemovalError('');
    try {
      const application = await invoke<ApplicationStatus>('remove_application');
      setSnapshot(current => ({ ...current, application }));
      removalDialog.current?.close();
    } catch (reason) {
      setRemovalError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function openAppCenter() {
    setActionError('');
    try { await invoke('open_software_manager'); }
    catch (reason) { setActionError(String(reason)); }
  }

  function confirmRemoval() {
    setRemovalError('');
    removalDialog.current?.showModal();
  }

  const browserConnection = snapshot.browser_connection;
  const registered = browserConnection?.enabled && browserConnection.bridge_ready
    && browserConnection.chrome === 'registered' && browserConnection.firefox === 'registered';
  const browserStates = { missing: 'Not registered', registered: 'Registered', conflict: 'Another installation' };
  const application = snapshot.application;
  const activeDownloads = snapshot.downloads_active === true || (control.snapshot?.counts.unfinished || 0) > 0;
  const connected = control.connected && snapshot.state === 'ready';
  const jobs = control.snapshot?.jobs || [];

  return (
    <main className="shell">
      <header className="toolbar">
        <div className="brand"><img src="/vidgrab.png" alt="" width="32" height="32" /><span>VidGrab</span></div>
        <span className="connection" data-state={snapshot.state} role="status">{connected ? 'Connected' : states[snapshot.state] === 'Connected' ? 'Connecting' : states[snapshot.state]}</span>
      </header>
      <section className="intro" aria-labelledby="title">
        <h1 id="title">Downloads</h1>
        <p>Download from an HTTP or HTTPS media URL. VidGrab keeps the queue running when the browser closes.</p>
      </section>
      {control.snapshot && <DownloadDefaults settings={control.snapshot.settings} busy={busy || !connected}
        save={saveDefaults} reload={async () => (await refreshControl(true)).snapshot!.settings} />}
      <details className="application-setup">
        <summary>Install and remove VidGrab</summary>
        {application?.kind === 'appimage' ? <>
          <p className="installation-status" role="status">{application.installed ? 'Installed in Applications' : 'Running from an AppImage file'}</p>
          {application.installed ? <>
            <p>Open <strong>VidGrab (AppImage preview)</strong> from Applications. The installed copy stays in its own folder.</p>
            <p className="installation-path">{application.app}</p>
            <p>Removing the installed copy disconnects browser access and quits VidGrab. Your history and saved files are kept. The AppImage you downloaded stays available for reinstalling.</p>
            <button className="secondary critical" type="button" onClick={confirmRemoval}
              disabled={busy || activeDownloads}>Remove installed copy…</button>
          </> : <>
            <p>Install a copy and an Applications shortcut for this user. You can keep using this window while the copy is installed.</p>
            <button className="secondary" type="button" onClick={installAppImage} disabled={busy}>Install AppImage</button>
            <p>To remove a portable AppImage, quit VidGrab and move the downloaded file to Trash in Files.</p>
          </>}
        </> : application?.kind === 'deb' ? <>
          <p className="installation-status">Installed as an Ubuntu package</p>
          <p>To install again, open the <strong>.deb</strong> file in Files with <strong>App Center</strong> and select <strong>Install</strong>.</p>
          <p>Prepare for removal here, then use <strong>Manage</strong> in App Center to uninstall <strong>VidGrab</strong>. Browser access is disconnected before the app quits; your history and saved files are kept.</p>
          <div className="connection-actions">
            <button className="secondary" type="button" onClick={openAppCenter} disabled={busy}>Open App Center</button>
            <button className="secondary critical" type="button" onClick={confirmRemoval}
              disabled={busy || activeDownloads}>Prepare to remove…</button>
          </div>
        </> : application?.kind === 'windows' ? <>
          <p className="installation-status">{application.installed ? 'Installed for this Windows user' : 'Running from a Windows preview folder'}</p>
          <p>Open the <strong>VidGrab installer</strong> to install or reinstall for your Windows account.</p>
          <p>Prepare for removal here, then uninstall <strong>VidGrab</strong> in <strong>Settings → Apps → Installed apps</strong>. Your history and saved files are kept.</p>
          <div className="connection-actions">
            <button className="secondary" type="button" onClick={openAppCenter} disabled={busy}>Open Installed apps</button>
            <button className="secondary critical" type="button" onClick={confirmRemoval} disabled={busy || activeDownloads}>Prepare to remove…</button>
          </div>
        </> : <p className="helper">{snapshot.application_error || 'Installation controls are available in packaged builds.'}</p>}
        {activeDownloads && application?.installed && <p className="helper">Finish active downloads before removing VidGrab.</p>}
      </details>
      <section className="browser-connections" aria-labelledby="browser-title">
        <h2 id="browser-title">Browser connections</h2>
        {browserConnection ? <>
          <p className="browser-connection-summary" role="status">{!browserConnection.enabled
            ? 'Disconnected' : registered ? 'Registered for Chrome and Firefox' : 'Connection needs repair'}</p>
          <dl className="browser-registration">
            <div><dt>Google Chrome</dt><dd>{browserStates[browserConnection.chrome]}</dd></div>
            <div><dt>Firefox</dt><dd>{browserStates[browserConnection.firefox]}</dd></div>
          </dl>
          <p className="helper">Registration lets the extension ask for access. Confirm access with its native connection test.</p>
          <div className="connection-actions">
            {!registered && <button className="secondary" type="button" disabled={busy}
              data-connection-action="connect"
              onClick={() => setBrowserConnection(true)}>{browserConnection.enabled ? 'Repair connection' : 'Connect browsers'}</button>}
            {browserConnection.enabled && <button className="secondary" type="button" disabled={busy}
              data-connection-action="disconnect"
              onClick={() => setBrowserConnection(false)}>Disconnect browsers</button>}
          </div>
          <p className="helper">Disconnecting preserves your downloads and history, and stays in effect when VidGrab reopens.</p>
        </> : <p className="helper">{snapshot.browser_connection_error
          ? 'The browser connection settings need attention.' : 'Browser connection controls are available in packaged builds.'}</p>}
        {snapshot.browser_connection_error && <p className="error" role="alert">{snapshot.browser_connection_error}</p>}
      </section>
      {application?.kind !== 'windows' && <details className="browser-setup">
        <summary>Firefox Flatpak setup</summary>
        <p>Firefox Flatpak needs one browser setting to allow the native connection test in this preview.</p>
        <ol>
          <li>Open VidGrab from its current location once to register the browser connection.</li>
          <li>In Firefox, open <code>about:config</code>, accept the browser notice if shown, and search for
            <code className="portal-preference">widget.use-xdg-desktop-portal.native-messaging</code>
          </li>
          <li>Set the existing <strong>Number</strong> value to <code>1</code> and save it. Fully quit Firefox, then reopen it.</li>
          <li>After restarting Firefox, load the temporary VidGrab add-on, then run its native connection test with Firefox visible. Choose <strong>Allow</strong> when the desktop asks Firefox to launch <strong>VidGrab P02 desktop companion</strong>.</li>
        </ol>
        <p>If Firefox Flatpak was installed during this desktop session, sign out and back in before connecting so the desktop can recognize it.</p>
        <p>The app’s Connected status means VidGrab is ready. Confirm Firefox access with the native connection test; the extension popup now uses this native connection for downloads and settings.</p>
      </details>}
      <form className="download-form" onSubmit={download}>
        <label htmlFor="media-url">Media URL</label>
        <input id="media-url" type="url" value={url} required maxLength={16384}
          placeholder="https://example.com/video" aria-describedby="download-help"
          onChange={event => setUrl(event.target.value)} />
        <button className="primary" type="submit" disabled={busy || !connected}>{busy ? 'Working…' : 'Download'}</button>
        <div className="media-options">
          <label className="checkbox-label"><input type="checkbox" checked={audioOnly} onChange={event => setAudioOnly(event.target.checked)} />Audio only</label>
          {audioOnly && <label>Audio format<select value={audioFormat} onChange={event => setAudioFormat(event.target.value)}>
            {['mp3', 'm4a', 'opus', 'wav', 'flac'].map(format => <option key={format} value={format}>{format.toUpperCase()}</option>)}
          </select></label>}
        </div>
        <p className="helper" id="download-help">App format: {control.snapshot?.settings.values.default_format || 'Loading defaults…'}.
          Save location: {control.snapshot?.settings.values.download_dir || 'Loading…'}.</p>
      </form>
      {connectionError && <p className="error" role="alert">{connectionError}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
      {companionSession.pending && <button className="secondary" type="button" onClick={checkRequest} disabled={busy}>Check request</button>}
      <p className="announcement" role="status">{announcement}</p>
      {snapshot.state === 'failed' && <p className="error">Quit and reopen VidGrab to restart the engine.</p>}
      <section className="queue" aria-labelledby="downloads-title">
        <h2 id="downloads-title">Downloads</h2>
        {control.snapshot && <p className="helper" role="status">{jobs.length} shown · {control.snapshot.counts.total} total · {control.snapshot.counts.unfinished} unfinished</p>}
        {jobs.length ? <ul className="jobs">{jobs.map(job => (
          <li className="job" key={job.id}>
            <span className="job-title">{job.title || 'Download'}</span>
            <span className="job-status">{jobStates[job.status] || job.status}</span>
            {!['complete', 'failed', 'cancelled'].includes(job.status) && <progress max="100" value={job.progress} aria-label={(job.title || 'Download') + ' progress'} />}
            {job.file_path && <span className="file-name">{job.file_path}</span>}
            {job.error_code && <span className="error">{job.error_code}</span>}
            {job.status === 'waiting_for_auth' && <p className="helper">Sign in to this source site, then use Share fresh cookies in the VidGrab browser popup.</p>}
            {job.status === 'waiting_for_auth' && <span className="file-name">{job.url}</span>}
            <div className="job-actions">
              {['failed', 'cancelled'].includes(job.status) && <button className="secondary" type="button" disabled={busy || !connected} onClick={() => jobAction(job, 'retry')}>Retry</button>}
              {!['complete', 'failed', 'cancelled', 'cancelling'].includes(job.status) && <button className="secondary" type="button" disabled={busy || !connected} onClick={() => jobAction(job, 'cancel')}>Cancel</button>}
            </div>
          </li>
        ))}</ul> : <p className="empty">No downloads yet</p>}
        {control.snapshot?.next_after && <button className="secondary" type="button" disabled={busy || !connected} onClick={() => {
          setBusy(true); void companionSession.loadMore().then(setControl).catch(reason => setActionError(explain(reason))).finally(() => setBusy(false));
        }}>Load more downloads</button>}
      </section>
      <footer className="footer">
        <p>{snapshot.tray_available
          ? 'Closing this window keeps VidGrab running in the tray.'
          : 'This desktop has no tray. Keep this window open; use Quit VidGrab to close the app.'}</p>
        <button className="secondary" type="button" onClick={quit} disabled={busy}>Quit VidGrab</button>
      </footer>
      <dialog className="removal-dialog" ref={removalDialog} aria-labelledby="remove-title" aria-describedby="remove-description">
        <h2 id="remove-title">{application?.kind === 'appimage' ? 'Remove the installed copy?' : 'Prepare to remove VidGrab?'}</h2>
        <p id="remove-description">{application?.kind === 'appimage'
          ? 'VidGrab will disconnect browser access, quit and remove its installed AppImage and Applications shortcut. Your history and saved files are kept.'
          : application?.kind === 'windows'
          ? 'VidGrab will disconnect browser access, quit and open Installed apps. Uninstall VidGrab there. Your history and saved files are kept.'
          : 'VidGrab will disconnect browser access, quit and open App Center. Use Manage to uninstall the Ubuntu package. Your history and saved files are kept.'}</p>
        {activeDownloads && <p className="helper">Finish active downloads before removing VidGrab.</p>}
        {removalError && <p className="error" role="alert">{removalError}</p>}
        <div className="dialog-actions">
          <button className="secondary" type="button" autoFocus disabled={busy} onClick={() => removalDialog.current?.close()}>Keep VidGrab</button>
          <button className="secondary critical" type="button" disabled={busy || activeDownloads} onClick={removeApplication}>
            {busy ? 'Working…' : application?.kind === 'appimage' ? 'Remove and quit' : 'Disconnect and quit'}
          </button>
        </div>
      </dialog>
    </main>
  );
}
