import type { Job } from '../../../packages/protocol/generated/client';

export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: 'Queued',
  scheduled: 'Scheduled',
  probing: 'Inspecting media',
  downloading: 'Downloading',
  postprocessing: 'Converting',
  pausing: 'Pausing…',
  paused: 'Paused',
  complete: 'Finished',
  failed: 'Failed',
  cancelled: 'Cancelled',
  cancelling: 'Cancelling…',
  waiting_for_auth: 'Fresh browser context needed',
  retry_wait: 'Retry scheduled',
  waiting_children: 'Waiting for playlist items',
  waiting_for_children: 'Waiting for playlist items',
};

export function validateHttpUrl(raw: string): string {
  const trimmed = raw.trim();
  const parsed = new URL(trimmed);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error('Enter an HTTP or HTTPS media URL without embedded credentials.');
  }
  return trimmed;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes / 1024;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${units[idx]}`;
}

export function formatSpeed(bytesPerSec: number | null | undefined): string {
  if (!bytesPerSec || !Number.isFinite(bytesPerSec) || bytesPerSec <= 0) return '';
  return `${formatBytes(bytesPerSec)}/s`;
}

export function formatEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '';
  const total = Math.round(seconds);
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hrs > 0) return `${hrs}h ${mins}m`;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

export function formatTimestamp(isoOrEpoch: string | number | null | undefined): string {
  if (isoOrEpoch === null || isoOrEpoch === undefined || isoOrEpoch === '') return '';
  const date = typeof isoOrEpoch === 'number' ? new Date(isoOrEpoch * 1000) : new Date(isoOrEpoch);
  if (Number.isNaN(date.getTime())) return String(isoOrEpoch);
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function localInputToUtcIso(localValue: string): string {
  const trimmed = localValue.trim();
  if (!trimmed) throw new Error('Choose a date and time.');
  const date = new Date(trimmed);
  if (Number.isNaN(date.getTime())) throw new Error('Invalid date/time value.');
  return date.toISOString();
}

export function utcIsoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function resumeModeLabel(job: Job): string {
  if (job.resume_mode === 'verified_partial') {
    return 'Supports verified partial resume';
  }
  if (job.resume_mode === 'not_pausable') {
    return 'Cannot pause during conversion or playlist expansion';
  }
  return 'Pause interrupts safely; resume may restart this stream';
}
