import { useState } from 'react';
import type { ActionParams, Counts, Job } from '../../../packages/protocol/generated/client';
import {
  JOB_STATUS_LABELS,
  formatBytes,
  formatEta,
  formatSpeed,
  formatTimestamp,
  localInputToUtcIso,
  resumeModeLabel,
  utcIsoToLocalInput,
} from './format';

type QueueFilter = 'unfinished' | 'active' | 'paused' | 'scheduled' | 'all';

interface Props {
  jobs: Job[];
  counts?: Counts;
  hasMoreActive: boolean;
  hasMoreAll: boolean;
  busy: boolean;
  connected: boolean;
  hasPendingRequest: boolean;
  onAction: (job: Job, action: ActionParams['action'], extra?: { priority?: number; scheduled_at?: string | null }) => Promise<void>;
  onLoadMore: (scope: 'active' | 'all') => Promise<void>;
  onReveal: (jobId: string) => Promise<void>;
  onOpen: (jobId: string) => Promise<void>;
}

const ACTIVE_STATUSES = new Set(['queued', 'probing', 'downloading', 'postprocessing', 'pausing', 'retry_wait', 'waiting_children', 'waiting_for_children']);

export function QueueView({
  jobs,
  counts,
  hasMoreActive,
  hasMoreAll,
  busy,
  connected,
  hasPendingRequest,
  onAction,
  onLoadMore,
  onReveal,
  onOpen,
}: Props) {
  const [filter, setFilter] = useState<QueueFilter>('unfinished');
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null);
  const [scheduleDraft, setScheduleDraft] = useState('');

  const visibleJobs = jobs.filter(job => {
    if (filter === 'all') return true;
    if (filter === 'paused') return job.status === 'paused' || job.status === 'pausing';
    if (filter === 'scheduled') return job.status === 'scheduled';
    if (filter === 'active') return ACTIVE_STATUSES.has(job.status) || job.status === 'waiting_for_auth';
    return !['complete', 'failed', 'cancelled'].includes(job.status);
  });

  const shownCount = filter === 'all' ? jobs : visibleJobs;

  return (
    <section className="queue card" aria-labelledby="downloads-title">
      <div className="card-header">
        <div>
          <h2 id="downloads-title">Queue</h2>
          {counts && (
            <p className="helper" role="status">
              {jobs.length} shown · {counts.total} total · {counts.unfinished} unfinished · {counts.paused} paused · {counts.scheduled} scheduled
            </p>
          )}
        </div>
        <div className="filter-pills" role="group" aria-label="Filter queue">
          {([
            ['unfinished', 'Unfinished'],
            ['active', 'Active'],
            ['paused', 'Paused'],
            ['scheduled', 'Scheduled'],
            ['all', 'All loaded'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={`pill ${filter === key ? 'active' : ''}`}
              aria-pressed={filter === key}
              onClick={() => setFilter(key)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {shownCount.length ? (
        <ul className="jobs">
          {shownCount.map(job => {
            const isTerminal = ['complete', 'failed', 'cancelled'].includes(job.status);
            const canPause = !isTerminal
              && !['paused', 'pausing', 'cancelling', 'waiting_for_auth'].includes(job.status)
              && job.resume_mode !== 'not_pausable';
            const canResume = job.status === 'paused';
            const canCancel = !isTerminal && job.status !== 'cancelling';
            const canRetry = ['failed', 'cancelled'].includes(job.status);
            const canSchedule = ['queued', 'scheduled', 'paused', 'retry_wait'].includes(job.status);
            const speedText = job.status === 'downloading' ? formatSpeed(job.speed) : '';
            const etaText = job.status === 'downloading' ? formatEta(job.eta) : '';

            return (
              <li className="job" key={job.id} data-status={job.status}>
                <div className="job-main">
                  <span className="job-title">{job.title || 'Download'}</span>
                  <div className="job-meta">
                    <span className="job-status">{JOB_STATUS_LABELS[job.status] || job.status}</span>
                    {job.priority !== 0 && (
                      <span className="meta-chip">Priority {job.priority > 0 ? `+${job.priority}` : job.priority}</span>
                    )}
                    {job.job_kind === 'playlist' && <span className="meta-chip">Playlist</span>}
                    {job.file_size !== null && <span className="meta-chip">{formatBytes(job.file_size)}</span>}
                    {speedText && <span className="meta-chip">{speedText}</span>}
                    {etaText && <span className="meta-chip">ETA {etaText}</span>}
                    {job.scheduled_at && (
                      <span className="meta-chip">Scheduled: {formatTimestamp(job.scheduled_at)}</span>
                    )}
                  </div>
                </div>

                {!isTerminal && (
                  <progress
                    max="100"
                    value={job.progress}
                    aria-label={(job.title || 'Download') + ' progress'}
                  />
                )}

                {!isTerminal && job.status !== 'waiting_for_auth' && (
                  <p className="helper">{resumeModeLabel(job)}</p>
                )}

                {job.file_path && <span className="file-name">{job.file_path}</span>}
                {job.error_code && (
                  <span className="error">
                    {job.error_code}{job.error_message ? `: ${job.error_message}` : ''}
                  </span>
                )}
                {job.status === 'waiting_for_auth' && (
                  <p className="helper">
                    Return to the source page and use the VidGrab browser popup to share fresh cookies or context for this media.
                  </p>
                )}
                {job.error_code === 'FORMAT_UNAVAILABLE' && (
                  <p className="helper">
                    Review this media's current formats in the VidGrab browser popup before starting it again.
                  </p>
                )}
                {(job.status === 'waiting_for_auth' || job.error_code === 'FORMAT_UNAVAILABLE') && (
                  <span className="file-name">{job.url}</span>
                )}

                <div className="job-actions">
                  {canPause && (
                    <button
                      className="secondary compact"
                      type="button"
                      disabled={busy || !connected || hasPendingRequest}
                      onClick={() => onAction(job, 'pause')}
                    >
                      Pause
                    </button>
                  )}
                  {!isTerminal && job.resume_mode === 'not_pausable' && (
                    <button className="secondary compact" type="button" disabled title={resumeModeLabel(job)}>
                      Pause unavailable
                    </button>
                  )}
                  {canResume && (
                    <button
                      className="primary compact"
                      type="button"
                      disabled={busy || !connected || hasPendingRequest}
                      onClick={() => onAction(job, 'resume')}
                    >
                      Resume
                    </button>
                  )}
                  {canRetry && (
                    <button
                      className="secondary compact"
                      type="button"
                      disabled={busy || !connected || hasPendingRequest}
                      onClick={() => onAction(job, 'retry')}
                    >
                      Retry
                    </button>
                  )}
                  {canCancel && (
                    <button
                      className="secondary compact critical"
                      type="button"
                      disabled={busy || !connected || hasPendingRequest}
                      onClick={() => onAction(job, 'cancel')}
                    >
                      Cancel
                    </button>
                  )}
                  {!isTerminal && (
                    <label className="priority-control">
                      <span className="sr-only">Priority for {job.title || 'Download'}</span>
                      <select
                        aria-label={`Priority for ${job.title || job.id}`}
                        value={job.priority}
                        disabled={busy || !connected || hasPendingRequest}
                        onChange={event => onAction(job, 'set_priority', { priority: Number(event.target.value) })}
                      >
                        {[10, 5, 0, -5, -10].map(val => (
                          <option key={val} value={val}>
                            Priority {val > 0 ? `+${val}` : val}
                          </option>
                        ))}
                        {![10, 5, 0, -5, -10].includes(job.priority) && (
                          <option value={job.priority}>Priority {job.priority}</option>
                        )}
                      </select>
                    </label>
                  )}
                  {canSchedule && (
                    <button
                      className="secondary compact"
                      type="button"
                      disabled={busy || !connected || hasPendingRequest}
                      onClick={() => {
                        if (editingScheduleId === job.id) {
                          setEditingScheduleId(null);
                        } else {
                          setEditingScheduleId(job.id);
                          setScheduleDraft(utcIsoToLocalInput(job.scheduled_at));
                        }
                      }}
                    >
                      {job.scheduled_at ? 'Reschedule' : 'Schedule'}
                    </button>
                  )}
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
                </div>

                {editingScheduleId === job.id && (
                  <div className="schedule-editor">
                    <input
                      type="datetime-local"
                      aria-label={`Schedule time for ${job.title || job.id}`}
                      value={scheduleDraft}
                      onChange={event => setScheduleDraft(event.target.value)}
                    />
                    <button
                      className="primary compact"
                      type="button"
                      disabled={busy || !connected || hasPendingRequest || !scheduleDraft}
                      onClick={async () => {
                        await onAction(job, 'set_schedule', { scheduled_at: localInputToUtcIso(scheduleDraft) });
                        setEditingScheduleId(null);
                      }}
                    >
                      Apply schedule
                    </button>
                    {job.scheduled_at && (
                      <button
                        className="secondary compact"
                        type="button"
                        disabled={busy || !connected || hasPendingRequest}
                        onClick={async () => {
                          await onAction(job, 'set_schedule', { scheduled_at: null });
                          setEditingScheduleId(null);
                        }}
                      >
                        Run now
                      </button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="empty">No downloads in this view</p>
      )}

      {(filter === 'all' ? hasMoreAll : hasMoreActive) && (
        <button
          className="secondary"
          type="button"
          disabled={busy || !connected}
          onClick={() => void onLoadMore(filter === 'all' ? 'all' : 'active')}
        >
          Load more downloads
        </button>
      )}
    </section>
  );
}
