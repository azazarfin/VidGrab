import { useState } from 'react';
import type { FullDiagnosticsReport } from '../protocol';
import { formatBytes } from './format';

interface Props {
  report: FullDiagnosticsReport | null;
  loading: boolean;
  busy: boolean;
  onRefresh: () => Promise<void>;
  onRestartEngine: () => Promise<void>;
  onExportDiagnostics: () => Promise<void>;
}

export function DiagnosticsView({
  report,
  loading,
  busy,
  onRefresh,
  onRestartEngine,
  onExportDiagnostics,
}: Props) {
  const [copyStatus, setCopyStatus] = useState('');
  const engine = report && 'component_checks' in report.engine ? report.engine : null;
  const engineError = report && 'error' in report.engine ? report.engine.error : null;

  async function handleCopyDiagnostics() {
    if (!report) return;
    setCopyStatus('');
    const serialized = JSON.stringify(report, null, 2);
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(serialized);
      } else {
        throw new Error('Clipboard API unavailable');
      }
      setCopyStatus('Redacted diagnostics copied to clipboard.');
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = serialized;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand('copy');
      textarea.remove();
      setCopyStatus(
        copied
          ? 'Redacted diagnostics copied to clipboard.'
          : 'Could not access clipboard; use Export report instead.',
      );
    }
  }

  return (
    <div className="diagnostics-stack">
      <section className="card" aria-labelledby="diagnostics-heading">
        <div className="card-header">
          <div>
            <h2 id="diagnostics-heading">Diagnostics &amp; components</h2>
            <p className="helper">
              Inspect bundled dependencies, storage health, recent redacted errors, or recover the engine.
            </p>
          </div>
          <div className="inline-actions">
            <button className="secondary compact" type="button" onClick={() => void onRefresh()} disabled={busy || loading}>
              {loading ? 'Checking…' : 'Refresh'}
            </button>
            <button className="secondary compact" type="button" onClick={() => void handleCopyDiagnostics()} disabled={busy || !report}>
              Copy redacted diagnostics
            </button>
            <button className="secondary compact" type="button" onClick={() => void onExportDiagnostics()} disabled={busy}>
              Export report…
            </button>
            <button className="secondary compact critical" type="button" onClick={() => void onRestartEngine()} disabled={busy}>
              Restart engine
            </button>
          </div>
        </div>
        {copyStatus && <p className="announcement" role="status">{copyStatus}</p>}

        {report && (
          <dl className="browser-registration">
            <div>
              <dt>Supervisor state</dt>
              <dd>{report.supervisor.state}</dd>
            </div>
            <div>
              <dt>Engine generation</dt>
              <dd>{report.supervisor.engine_generation ?? 1}</dd>
            </div>
            <div>
              <dt>Tray host</dt>
              <dd>{report.desktop_preferences.tray_available ? 'Available' : 'Unavailable (visible-window fallback)'}</dd>
            </div>
            {engine && (
              <>
                <div>
                  <dt>Media mode</dt>
                  <dd>{engine.media_mode}</dd>
                </div>
                <div>
                  <dt>Database schema</dt>
                  <dd>v{engine.schema_version}</dd>
                </div>
                <div>
                  <dt>Download folder</dt>
                  <dd className="path-text">
                    {engine.download_dir} ({engine.download_dir_writable ? 'writable' : 'not writable'})
                  </dd>
                </div>
                <div>
                  <dt>Free disk space</dt>
                  <dd>{formatBytes(engine.disk_free_bytes)}</dd>
                </div>
              </>
            )}
          </dl>
        )}

        {engineError && <p className="error" role="alert">Engine diagnostics unavailable: {engineError}</p>}
      </section>

      {engine && (
        <section className="card" aria-labelledby="components-heading">
          <h3 id="components-heading">Components</h3>
          <ul className="components-list">
            {engine.component_checks.map(item => (
              <li className="component-row" key={item.name}>
                <div>
                  <strong>{item.name}</strong>
                  {item.error && <p className="error">{item.error}</p>}
                </div>
                <span className={item.available ? 'status-ok' : 'status-warn'}>
                  {item.available ? (item.version || 'Available') : 'Missing'}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {engine && (
        <section className="card" aria-labelledby="recent-errors-heading">
          <h3 id="recent-errors-heading">Recent redacted errors</h3>
          {engine.recent_errors.length ? (
            <ul className="recent-errors">
              {engine.recent_errors.map((line, index) => (
                <li key={index} className="path-text">{line}</li>
              ))}
            </ul>
          ) : (
            <p className="helper">No recent errors recorded.</p>
          )}
        </section>
      )}

      {report?.engine_log_tail && report.engine_log_tail.length > 0 && (
        <section className="card" aria-labelledby="engine-log-heading">
          <h3 id="engine-log-heading">Engine log tail (redacted)</h3>
          <ul className="log-tail-list">
            {report.engine_log_tail.map((line, index) => (
              <li key={index} className="path-text">{line}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
