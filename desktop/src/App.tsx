import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { invoke } from '@tauri-apps/api/core';

interface Job {
  id: string;
  title: string;
  status: string;
  progress: number;
  file_path?: string;
  error_message?: string;
}

interface Snapshot {
  state: 'starting' | 'ready' | 'recovering' | 'failed';
  tray_available: boolean;
  jobs?: Job[];
}

const states = { starting: 'Starting', ready: 'Connected', recovering: 'Reconnecting', failed: 'Engine stopped' };
const jobStates: Record<string, string> = { queued: 'Queued', downloading: 'Downloading', postprocessing: 'Converting', complete: 'Finished', failed: 'Failed' };

export function App() {
  const [snapshot, setSnapshot] = useState<Snapshot>({ state: 'starting', tray_available: false });
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [actionError, setActionError] = useState('');
  const [announcement, setAnnouncement] = useState('');

  useEffect(() => {
    let disposed = false;
    let timeout: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const current = await invoke<Snapshot>('companion_status');
        if (!disposed) {
          setSnapshot(current);
          setConnectionError('');
        }
      } catch (reason) {
        if (!disposed) {
          setSnapshot(current => ({ ...current, state: 'recovering' }));
          setConnectionError(String(reason));
        }
      } finally {
        if (!disposed) timeout = setTimeout(refresh, 1000);
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timeout); };
  }, []);

  async function download(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      await invoke('download_fixture', { url });
      setAnnouncement('Test audio queued.');
    } catch (reason) {
      setActionError(String(reason));
    } finally {
      setBusy(false);
    }
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

  return (
    <main className="shell">
      <header className="toolbar">
        <div className="brand"><img src="/vidgrab.png" alt="" width="32" height="32" /><span>VidGrab</span></div>
        <span className="connection" data-state={snapshot.state} role="status">{states[snapshot.state]}</span>
      </header>
      <section className="intro" aria-labelledby="title">
        <p className="eyebrow">Desktop companion</p>
        <h1 id="title">Connection test</h1>
        <p>Try a local audio download. VidGrab keeps it running when the browser closes.</p>
      </section>
      <details className="browser-setup">
        <summary>Firefox Flatpak setup</summary>
        <p>Firefox Flatpak needs one browser setting to allow the native connection test in this preview.</p>
        <ol>
          <li>Open VidGrab from its current location once to register the browser connection.</li>
          <li>In Firefox, open <code>about:config</code>, accept the browser notice if shown, and search for
            <code className="portal-preference">widget.use-xdg-desktop-portal.native-messaging</code>
          </li>
          <li>Set the existing <strong>Number</strong> value to <code>1</code> and save it. Fully quit Firefox, then reopen it.</li>
          <li>Keep Firefox visible when starting the extension’s native connection test. Choose <strong>Allow</strong> when the desktop asks Firefox to launch <strong>VidGrab P02 desktop companion</strong>.</li>
        </ol>
        <p>If Firefox Flatpak was installed during this desktop session, sign out and back in before connecting so the desktop can recognize it.</p>
        <p>The app’s Connected status means VidGrab is ready. Confirm Firefox access with the native connection test; the preview’s regular extension popup still uses the earlier backend.</p>
      </details>
      <form className="download-form" onSubmit={download}>
        <label htmlFor="fixture-url">Fixture URL</label>
        <input id="fixture-url" type="url" value={url} required maxLength={2048}
          placeholder="http://127.0.0.1:8080/fixture.wav" aria-describedby="fixture-help"
          onChange={event => setUrl(event.target.value)} />
        <button className="primary" type="submit" disabled={busy || snapshot.state !== 'ready'}>{busy ? 'Working…' : 'Download test audio'}</button>
        <p className="helper" id="fixture-help">Use the local fixture URL from the validation server. The test saves an MP3.</p>
      </form>
      {connectionError && <p className="error" role="alert">{connectionError}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
      <p className="announcement" role="status">{announcement}</p>
      {snapshot.state === 'failed' && <p className="error">Quit and reopen VidGrab to restart the engine.</p>}
      <section className="queue" aria-labelledby="downloads-title">
        <h2 id="downloads-title">Test downloads</h2>
        {snapshot.jobs?.length ? <ul className="jobs">{snapshot.jobs.map(job => (
          <li className="job" key={job.id}>
            <span className="job-title">{job.title || 'Test audio'}</span>
            <span className="job-status">{jobStates[job.status] || job.status}</span>
            {job.status !== 'complete' && job.status !== 'failed' && <progress max="100" value={job.progress || 0} aria-label={`${job.title} progress`} />}
            {job.file_path && <span className="file-name">{job.file_path.split(/[\\/]/).pop()}</span>}
            {job.error_message && <span className="error">{job.error_message}</span>}
          </li>
        ))}</ul> : <p className="empty">No test downloads yet</p>}
      </section>
      <footer className="footer">
        <p>{snapshot.tray_available
          ? 'Closing this window keeps VidGrab running in the tray.'
          : 'This desktop has no tray. Keep this window open; use Quit VidGrab to close the app.'}</p>
        <button className="secondary" type="button" onClick={quit} disabled={busy}>Quit VidGrab</button>
      </footer>
    </main>
  );
}
