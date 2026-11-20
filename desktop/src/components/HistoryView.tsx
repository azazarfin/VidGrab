import { useEffect, useRef, useState } from 'react';
import type { ActionParams, Counts, Job } from '../../../packages/protocol/generated/client';
import { companionSession } from '../protocol';
import { JOB_STATUS_LABELS, formatBytes, formatTimestamp } from './format';

interface Props {
  connected: boolean;
  busy: boolean;
  hasPendingRequest: boolean;
  refreshKey: string;
  counts?: Counts;
  onAction: (job: Job, action: ActionParams['action']) => Promise<void>;
  onClearHistory: () => Promise<void>;
  onReveal: (jobId: string) => Promise<void>;
  onOpen: (jobId: string) => Promise<void>;
  onError: (message: string) => void;
}

export function HistoryView({
  connected,
  busy,
  hasPendingRequest,
  refreshKey,
  counts,
  onAction,
  onClearHistory,
  onReveal,
  onOpen,
  onError,
}: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [nextAfter, setNextAfter] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'complete' | 'failed' | 'cancelled'>('all');
  const clearDialogRef = useRef<HTMLDialogElement>(null);

  async function load(after: string | null = null, append = false) {
    if (!connected) return;
    setLoading(true);
    try {
      const page = await companionSession.read('downloads.list', {
        view: 'history',
        limit: 25,
        after,
      });
      setJobs(prev => (append ? [...prev, ...page.jobs] : page.jobs));
      setNextAfter(page.next_after);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(null, false);
  }, [connected, refreshKey]);

  const normalizedQuery = query.trim().toLowerCase();
  const filteredJobs = jobs.filter(job => {
    if (statusFilter !== 'all' && job.status !== statusFilter) return false;
    if (!normalizedQuery) return true;
    return [
      job.title || 'Download',
      JOB_STATUS_LABELS[job.status] || job.status,
      job.url,
      job.file_path,
      job.error_code,
      job.error_message,
    ].some(field => Boolean(field && field.toLowerCase().includes(normalizedQuery)));
  });

  return (
    <section className="card history-view" aria-labelledby="history-title">
      <div className="card-header">
        <div>
          <h2 id="history-title">History</h2>
          <p className="helper">
            {counts ? `${counts.history} finished, failed, or cancelled` : `${jobs.length} shown`} · Deleting history keeps files saved on disk.
          </p>
        </div>
        <div className="inline-actions">
          <button
            className="secondary compact"
            type="button"
            disabled={busy || loading || !connected}
            onClick={() => void load(null, false)}
          >
            Refresh
          </button>
          <button
            className="secondary compact critical"
            type="button"
            disabled={busy || !connected || hasPendingRequest || (counts ? counts.history === 0 : jobs.length === 0)}
            onClick={() => clearDialogRef.current?.showModal()}
          >
            Clear history
          </button>
        </div>
      </div>

      <div className="history-filters">
        <input
          id="history-search"
          type="search"
          aria-label="Search history"
          placeholder="Search title, URL, file, or error…"
          value={query}
          onChange={event => setQuery(event.target.value)}
        />
        <select
          id="history-status-filter"
          aria-label="Filter history status"
          value={statusFilter}
          onChange={event => setStatusFilter(event.target.value as typeof statusFilter)}
        >
          <option value="all">All statuses</option>
          <option value="complete">Finished</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
      </div>

      {loading && jobs.length === 0 ? (
        <p className="helper">Loading history…</p>
      ) : filteredJobs.length ? (
        <ul className="jobs">
          {filteredJobs.map(job => (
            <li className="job" key={job.id} data-status={job.status}>
              <div className="job-main">
                <span className="job-title">{job.title || 'Download'}</span>
                <div className="job-meta">
                  <span className="job-status">{JOB_STATUS_LABELS[job.status] || job.status}</span>
                  {job.file_size !== null && <span className="meta-chip">{formatBytes(job.file_size)}</span>}
                  {job.completed_at && <span className="meta-chip">{formatTimestamp(job.completed_at)}</span>}
                </div>
              </div>
              {job.file_path && <span className="file-name">{job.file_path}</span>}
              {job.error_code && (
                <span className="error">
                  {job.error_code}{job.error_message ? `: ${job.error_message}` : ''}
                </span>
              )}
              {job.error_code === 'FORMAT_UNAVAILABLE' && (
                <>
                  <p className="helper">Review this media's current formats in the VidGrab browser popup before starting it again.</p>
                  <span className="file-name">{job.url}</span>
                </>
              )}
              <div className="job-actions">
                {job.status === 'complete' && job.file_path && (
                  <>
                    <button className="secondary compact" type="button" onClick={() => onOpen(job.id)} disabled={busy}>
                      Open
                    </button>
                    <button className="secondary compact" type="button" onClick={() => onReveal(job.id)} disabled={busy}>
                      Show in folder
                    </button>
                  </>
                )}
                {['failed', 'cancelled'].includes(job.status) && (
                  <button
                    className="secondary compact"
                    type="button"
                    disabled={busy || !connected || hasPendingRequest}
                    onClick={() => onAction(job, 'retry')}
                  >
                    Retry
                  </button>
                )}
                <button
                  className="secondary compact"
                  type="button"
                  disabled={busy || !connected || hasPendingRequest}
                  onClick={() => onAction(job, 'delete_history')}
                >
                  Remove entry
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : jobs.length > 0 ? (
        <p className="empty">No matching history entries</p>
      ) : (
        <p className="empty">No completed or past downloads yet</p>
      )}

      {nextAfter && (
        <button
          className="secondary"
          type="button"
          disabled={busy || loading || !connected}
          onClick={() => void load(nextAfter, true)}
        >
          Load more history
        </button>
      )}

      <dialog
        className="removal-dialog"
        ref={clearDialogRef}
        aria-labelledby="clear-history-title"
        aria-describedby="clear-history-description"
      >
        <h2 id="clear-history-title">Clear finished history?</h2>
        <p id="clear-history-description">
          Only finished, failed, and cancelled history records are removed. Downloaded files saved on disk are kept.
        </p>
        <div className="dialog-actions">
          <button
            className="secondary"
            type="button"
            autoFocus
            disabled={busy}
            onClick={() => clearDialogRef.current?.close()}
          >
            Keep history
          </button>
          <button
            className="secondary critical"
            type="button"
            disabled={busy}
            onClick={() => {
              clearDialogRef.current?.close();
              void onClearHistory();
            }}
          >
            Clear history records
          </button>
        </div>
      </dialog>
    </section>
  );
}
