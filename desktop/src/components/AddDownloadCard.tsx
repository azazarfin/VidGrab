import { useState } from 'react';
import type { ClipboardEvent, FormEvent } from 'react';
import type { DownloadOptions, DownloadParams, ProbeFormat, ProbeResult } from '../../../packages/protocol/generated/client';
import { companionSession, pickUrlListFile } from '../protocol';
import { formatBytes, localInputToUtcIso, validateHttpUrl } from './format';

interface Props {
  busy: boolean;
  connected: boolean;
  defaultFormat: string;
  downloadDir: string;
  hasPendingRequest: boolean;
  onEnqueue: (params: DownloadParams) => Promise<void>;
  onBatchEnqueue: (items: DownloadParams[]) => Promise<{ submitted: number; total: number }>;
  onError: (message: string) => void;
}

function formatOptionLabel(fmt: ProbeFormat): string {
  const parts = [fmt.resolution || 'audio/other', fmt.ext];
  if (fmt.tbr) parts.push(`${Math.round(fmt.tbr)} kbps`);
  const size = fmt.filesize ?? fmt.filesize_approx;
  if (size) parts.push(formatBytes(size));
  return parts.join(' · ');
}

export function AddDownloadCard({
  busy,
  connected,
  defaultFormat,
  downloadDir,
  hasPendingRequest,
  onEnqueue,
  onBatchEnqueue,
  onError,
}: Props) {
  const [url, setUrl] = useState('');
  const [showOptions, setShowOptions] = useState(false);
  const [showPaste, setShowPaste] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<ProbeResult | null>(null);
  const [selectedFormatId, setSelectedFormatId] = useState('');
  const [audioOnly, setAudioOnly] = useState(false);
  const [audioFormat, setAudioFormat] = useState<'mp3' | 'm4a' | 'opus' | 'wav' | 'flac'>('mp3');
  const [priority, setPriority] = useState(0);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleLocal, setScheduleLocal] = useState('');
  const [downloadSubtitles, setDownloadSubtitles] = useState(false);
  const [embedThumbnail, setEmbedThumbnail] = useState(false);
  const [batchUrls, setBatchUrls] = useState<Array<{ url: string; selected: boolean; error?: string }>>([]);
  const [batchSource, setBatchSource] = useState('');
  const [batchTruncated, setBatchTruncated] = useState(false);

  function buildParams(targetUrl: string, includeProbe = false): DownloadParams {
    const validUrl = validateHttpUrl(targetUrl);
    const params: DownloadParams = {
      url: validUrl,
      audio_only: audioOnly,
      audio_format: audioFormat,
      priority,
    };
    if (scheduleEnabled) {
      params.scheduled_at = localInputToUtcIso(scheduleLocal);
    }
    const options: DownloadOptions = {};
    if (downloadSubtitles) options.download_subtitles = true;
    if (embedThumbnail) options.embed_thumbnail = true;

    if (includeProbe && probeResult && probeResult.url === validUrl) {
      if (probeResult.title) {
        params.title = probeResult.title;
      }
      if (probeResult.is_playlist) {
        options.is_playlist = true;
      } else if (selectedFormatId && !audioOnly) {
        params.format_id = selectedFormatId;
        params.probe_id = probeResult.probe_id;
      }
    }

    if (Object.keys(options).length > 0) {
      params.options = options;
    }
    return params;
  }

  function applyUrlLines(rawLines: string[], sourceLabel: string, alreadyTruncated = false) {
    const cleaned = rawLines
      .map(line => line.trim())
      .filter(line => line.length > 0 && !line.startsWith('#'));
    const truncated = alreadyTruncated || cleaned.length > 200;
    const bounded = cleaned.slice(0, 200);
    const entries = bounded.map(raw => {
      try {
        return { url: validateHttpUrl(raw), selected: true };
      } catch (err) {
        return {
          url: raw,
          selected: false,
          error: err instanceof Error ? err.message : 'Invalid URL',
        };
      }
    });
    setBatchSource(sourceLabel);
    setBatchTruncated(truncated);
    setBatchUrls(entries);
  }

  async function handleInspectFormats() {
    onError('');
    let validUrl: string;
    try {
      validUrl = validateHttpUrl(url);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
      return;
    }
    setProbing(true);
    try {
      const result = await companionSession.read('probes.create', { url: validUrl });
      setProbeResult(result);
      setSelectedFormatId('');
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setProbing(false);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    try {
      const params = buildParams(url, true);
      await onEnqueue(params);
      setUrl('');
      setProbeResult(null);
      setSelectedFormatId('');
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function handleImportList() {
    onError('');
    try {
      const picked = await pickUrlListFile();
      if (!picked) return;
      applyUrlLines(picked.urls, picked.path, picked.truncated);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  function handleReviewPasted() {
    onError('');
    if (!pasteText.trim()) return;
    applyUrlLines(pasteText.split(/\r?\n/), 'pasted URL list');
    setPasteText('');
    setShowPaste(false);
  }

  function handleUrlPaste(event: ClipboardEvent<HTMLInputElement>) {
    const pasted = event.clipboardData?.getData('text') || '';
    if (!/[\r\n]/.test(pasted)) return;
    event.preventDefault();
    onError('');
    applyUrlLines(pasted.split(/\r?\n/), 'pasted URLs');
  }

  async function handleEnqueueBatch() {
    const chosenIndices: number[] = [];
    const items: DownloadParams[] = [];
    try {
      batchUrls.forEach((entry, index) => {
        if (entry.selected && !entry.error) {
          chosenIndices.push(index);
          items.push(buildParams(entry.url, false));
        }
      });
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
      return;
    }
    if (!items.length) return;
    const result = await onBatchEnqueue(items);
    if (result.submitted === result.total) {
      setBatchUrls([]);
      setBatchSource('');
      setBatchTruncated(false);
    } else if (result.submitted > 0) {
      const done = new Set(chosenIndices.slice(0, result.submitted));
      setBatchUrls(prev => prev.filter((_, index) => !done.has(index)));
    }
  }

  return (
    <section className="card add-download-card" aria-labelledby="add-download-heading">
      <div className="card-header">
        <h2 id="add-download-heading">Add URL or batch</h2>
        <div className="inline-actions">
          <button
            id="toggle-media-options"
            className="secondary compact"
            type="button"
            onClick={() => setShowOptions(open => !open)}
            aria-expanded={showOptions}
          >
            {showOptions ? 'Hide options' : 'Options'}
          </button>
          <button
            className="secondary compact"
            type="button"
            onClick={() => setShowPaste(open => !open)}
            aria-expanded={showPaste}
            disabled={busy || !connected || hasPendingRequest}
          >
            Paste URL list
          </button>
          <button
            className="secondary compact"
            type="button"
            onClick={handleImportList}
            disabled={busy || !connected || hasPendingRequest}
          >
            Import URL list…
          </button>
        </div>
      </div>

      <form className="download-form" onSubmit={handleSubmit}>
        <label htmlFor="media-url">Media URL</label>
        <input
          id="media-url"
          type="url"
          value={url}
          required
          maxLength={16384}
          placeholder="https://example.com/video"
          aria-describedby="download-help"
          onPaste={handleUrlPaste}
          onChange={event => {
            const next = event.target.value;
            setUrl(next);
            if (probeResult && next.trim() !== probeResult.url) {
              setProbeResult(null);
              setSelectedFormatId('');
            }
          }}
        />
        <div className="download-submit-actions">
          <button
            className="secondary"
            type="button"
            onClick={() => void handleInspectFormats()}
            disabled={busy || probing || !connected || hasPendingRequest || !url.trim()}
          >
            {probing ? 'Inspecting…' : 'Inspect formats'}
          </button>
          <button className="primary" type="submit" disabled={busy || probing || !connected || hasPendingRequest}>
            {busy ? 'Working…' : 'Download'}
          </button>
        </div>

        {probeResult && (
          <div className="probe-preview" aria-label="Inspected media formats">
            <div className="batch-header">
              <div>
                <strong>{probeResult.title || 'Inspected media'}</strong>
                <p className="helper">
                  {probeResult.is_playlist
                    ? `Playlist (${probeResult.entry_count} entr${probeResult.entry_count === 1 ? 'y' : 'ies'})`
                    : `${probeResult.format_count} format${probeResult.format_count === 1 ? '' : 's'} inspected`}
                </p>
              </div>
              <button
                className="secondary compact"
                type="button"
                onClick={() => {
                  setProbeResult(null);
                  setSelectedFormatId('');
                }}
                disabled={busy}
              >
                Clear inspection
              </button>
            </div>
            {!probeResult.is_playlist && probeResult.formats && probeResult.formats.length > 0 && !audioOnly && (
              <div className="field-inline">
                <label htmlFor="inspected-format-select">Format / resolution</label>
                <select
                  id="inspected-format-select"
                  value={selectedFormatId}
                  onChange={event => setSelectedFormatId(event.target.value)}
                  disabled={busy}
                >
                  <option value="">App default ({defaultFormat || 'bestvideo+bestaudio/best'})</option>
                  {probeResult.formats.map(fmt => (
                    <option key={fmt.format_id} value={fmt.format_id}>
                      {formatOptionLabel(fmt)}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>
        )}

        {showOptions && (
          <div className="media-options-grid">
            <label className="checkbox-label">
              <input
                id="option-audio-only"
                type="checkbox"
                checked={audioOnly}
                onChange={event => setAudioOnly(event.target.checked)}
              />
              Audio only
            </label>
            {audioOnly && (
              <label className="field-inline">
                Audio format
                <select
                  value={audioFormat}
                  onChange={event => setAudioFormat(event.target.value as typeof audioFormat)}
                >
                  {['mp3', 'm4a', 'opus', 'wav', 'flac'].map(format => (
                    <option key={format} value={format}>{format.toUpperCase()}</option>
                  ))}
                </select>
              </label>
            )}
            <label className="field-inline">
              Priority
              <select
                aria-label="Priority"
                value={priority}
                onChange={event => setPriority(Number(event.target.value))}
              >
                <option value={10}>Urgent (+10)</option>
                <option value={5}>High (+5)</option>
                <option value={0}>Normal (0)</option>
                <option value={-5}>Low (-5)</option>
                <option value={-10}>Background (-10)</option>
              </select>
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={downloadSubtitles}
                onChange={event => setDownloadSubtitles(event.target.checked)}
              />
              Download subtitles
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={embedThumbnail}
                onChange={event => setEmbedThumbnail(event.target.checked)}
              />
              Embed thumbnail
            </label>
            <div className="schedule-row">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={scheduleEnabled}
                  onChange={event => setScheduleEnabled(event.target.checked)}
                />
                Schedule start time
              </label>
              {scheduleEnabled && (
                <input
                  type="datetime-local"
                  aria-label="Scheduled start time"
                  value={scheduleLocal}
                  onChange={event => setScheduleLocal(event.target.value)}
                  required
                />
              )}
            </div>
          </div>
        )}

        <p className="helper" id="download-help">
          App format: {defaultFormat || 'Loading defaults…'}. Save location: {downloadDir || 'Loading…'}.
        </p>
      </form>

      {showPaste && (
        <div className="paste-preview">
          <label htmlFor="batch-paste-input">Paste URLs (one per line)</label>
          <textarea
            id="batch-paste-input"
            rows={4}
            value={pasteText}
            placeholder={"https://example.com/video-1\nhttps://example.com/video-2"}
            onChange={event => setPasteText(event.target.value)}
            disabled={busy}
          />
          <div className="inline-actions">
            <button
              className="secondary compact"
              type="button"
              onClick={handleReviewPasted}
              disabled={busy || !pasteText.trim()}
            >
              Review pasted URLs
            </button>
            <button
              className="secondary compact"
              type="button"
              onClick={() => {
                setPasteText('');
                setShowPaste(false);
              }}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {batchUrls.length > 0 && (
        <div className="batch-preview" aria-label="Imported URL list">
          <div className="batch-header">
            <span className="helper">
              {batchUrls.filter(item => item.selected && !item.error).length} of {batchUrls.length} URLs ready from <span className="path-text">{batchSource}</span>
              {batchTruncated ? ' (truncated to 200 lines)' : ''}
            </span>
            <div className="inline-actions">
              <button
                className="secondary compact"
                type="button"
                onClick={() => { setBatchUrls([]); setBatchSource(''); }}
                disabled={busy}
              >
                Clear list
              </button>
              <button
                className="primary compact"
                type="button"
                onClick={handleEnqueueBatch}
                disabled={busy || !connected || hasPendingRequest || !batchUrls.some(item => item.selected && !item.error)}
              >
                Queue selected
              </button>
            </div>
          </div>
          <ul className="batch-list">
            {batchUrls.map((entry, idx) => (
              <li key={`${entry.url}-${idx}`} className="batch-item">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={entry.selected}
                    disabled={Boolean(entry.error)}
                    onChange={event => {
                      const checked = event.target.checked;
                      setBatchUrls(prev => prev.map((row, r) => (r === idx ? { ...row, selected: checked } : row)));
                    }}
                  />
                  <span className="path-text">{entry.url}</span>
                </label>
                {entry.error && <span className="error">{entry.error}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
