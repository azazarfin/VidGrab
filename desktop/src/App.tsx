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
