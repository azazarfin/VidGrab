import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import type { SettingsPatch, SettingsSnapshot } from '../../packages/protocol/generated/client';

interface Props {
  settings: SettingsSnapshot;
  busy: boolean;
  save: (revision: number, patch: SettingsPatch) => Promise<SettingsSnapshot>;
  reload: () => Promise<SettingsSnapshot>;
  onPickFolder?: () => Promise<string | null>;
}

interface DraftFields {
  default_format: string;
  default_quality: string;
  download_dir: string;
  filename_template: string;
  max_concurrent: string;
  download_threads: string;
  speed_limit: string;
  max_retries: string;
  postprocess_codec: string;
  postprocess_normalize_audio: boolean;
  sponsorblock_enabled: boolean;
  sponsorblock_mode: string;
  transliterate_bangla_filenames: boolean;
}

const QUALITY_OPTIONS = [
  { value: 'best', label: 'Best' },
  { value: '2160', label: '2160p (4K)' },
  { value: '1440', label: '1440p (2K)' },
  { value: '1080', label: '1080p' },
  { value: '720', label: '720p' },
  { value: '480', label: '480p' },
];

const CODEC_OPTIONS = [
  { value: 'none', label: 'No conversion' },
  { value: 'mp4', label: 'MP4' },
  { value: 'mkv', label: 'MKV' },
  { value: 'webm', label: 'WebM' },
];

export function bytesToKibString(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0';
  return String(Math.max(1, Math.round(bytes / 1024)));
}

export function kibToBytes(kib: number): number {
  if (!Number.isFinite(kib) || kib <= 0) return 0;
  return Math.round(kib * 1024);
}

function fields(settings: SettingsSnapshot): DraftFields {
  return {
    default_format: settings.values.default_format,
    default_quality: settings.values.default_quality,
    download_dir: settings.values.download_dir,
    filename_template: settings.values.filename_template,
    max_concurrent: String(settings.values.max_concurrent),
    download_threads: String(settings.values.download_threads),
    speed_limit: bytesToKibString(settings.values.speed_limit),
    max_retries: String(settings.values.max_retries),
    postprocess_codec: settings.values.postprocess_codec,
    postprocess_normalize_audio: settings.values.postprocess_normalize_audio,
    sponsorblock_enabled: settings.values.sponsorblock_enabled,
    sponsorblock_mode: settings.values.sponsorblock_mode,
    transliterate_bangla_filenames: settings.values.transliterate_bangla_filenames,
  };
}

export function DownloadDefaults({ settings, busy, save, reload, onPickFolder }: Props) {
  const [base, setBase] = useState(settings);
  const [draft, setDraft] = useState<DraftFields>(() => fields(settings));
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!dirty) {
      setBase(settings);
      setDraft(fields(settings));
    }
  }, [settings, dirty]);

  function apply(snapshot: SettingsSnapshot) {
    setBase(snapshot);
    setDraft(fields(snapshot));
    setDirty(false);
    setError('');
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError('');
    const patch: SettingsPatch = {};

    for (const key of ['default_format', 'default_quality', 'download_dir', 'filename_template', 'postprocess_codec', 'sponsorblock_mode'] as const) {
      if (draft[key] !== base.values[key]) patch[key] = draft[key];
    }
    for (const key of ['postprocess_normalize_audio', 'sponsorblock_enabled', 'transliterate_bangla_filenames'] as const) {
      if (draft[key] !== base.values[key]) patch[key] = draft[key];
    }

    const concurrent = Number(draft.max_concurrent);
    if (!Number.isSafeInteger(concurrent) || concurrent < 1 || concurrent > 10) {
      setError('Choose between 1 and 10 concurrent downloads.');
      return;
    }
    if (concurrent !== base.values.max_concurrent) patch.max_concurrent = concurrent;

    const threads = Number(draft.download_threads);
    if (!Number.isSafeInteger(threads) || threads < 1 || threads > 32) {
      setError('Choose between 1 and 32 download threads.');
      return;
    }
    if (threads !== base.values.download_threads) patch.download_threads = threads;

    const speedLimitKib = Number(draft.speed_limit);
    if (!Number.isSafeInteger(speedLimitKib) || speedLimitKib < 0 || !Number.isSafeInteger(speedLimitKib * 1024)) {
      setError('Speed limit must be 0 (unlimited) or a positive KiB/s integer.');
      return;
    }
    if (draft.speed_limit !== bytesToKibString(base.values.speed_limit)) {
      patch.speed_limit = kibToBytes(speedLimitKib);
    }

    const maxRetries = Number(draft.max_retries);
    if (!Number.isSafeInteger(maxRetries) || maxRetries < 0 || maxRetries > 10) {
      setError('Max retries must be between 0 and 10.');
      return;
    }
    if (maxRetries !== base.values.max_retries) patch.max_retries = maxRetries;

    try {
      apply(await save(base.revision, patch));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function clearLegacyArgs() {
    setError('');
    try {
      const updated = await save(base.revision, { custom_ytdlp_args: '' });
      setBase(updated);
      if (!dirty) {
        setDraft(fields(updated));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  const change = <K extends keyof DraftFields>(key: K, value: DraftFields[K]) => {
    setDraft(current => ({ ...current, [key]: value }));
    setDirty(true);
  };

  async function browseFolder() {
    if (!onPickFolder) return;
    setError('');
    try {
      const selected = await onPickFolder();
      if (selected) {
        change('download_dir', selected);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  const hasLegacyArgs = Boolean(base.values.custom_ytdlp_args?.trim());
  const hasCustomQuality = !QUALITY_OPTIONS.some(item => item.value === draft.default_quality);
  const hasCustomCodec = !CODEC_OPTIONS.some(item => item.value === draft.postprocess_codec);

  return (
    <section className="download-defaults card" aria-labelledby="defaults-heading">
      <h2 id="defaults-heading">Download defaults</h2>
      <p className="helper">The desktop and browser extension share these app settings.</p>
      {hasLegacyArgs && (
        <div className="pending-review legacy-args-banner" role="alert">
          <span>
            Legacy custom yt-dlp arguments are stored and block new downloads until cleared.
          </span>
          <button
            className="secondary compact"
            type="button"
            disabled={busy}
            onClick={() => void clearLegacyArgs()}
          >
            Clear legacy arguments
          </button>
        </div>
      )}
      <form className="defaults-form" onSubmit={submit}>
        <label htmlFor="default-format">Default format</label>
        <input
          id="default-format"
          value={draft.default_format}
          onChange={event => change('default_format', event.target.value)}
          disabled={busy}
        />

        <label htmlFor="default-quality">Preferred quality</label>
        <select
          id="default-quality"
          value={draft.default_quality}
          onChange={event => change('default_quality', event.target.value)}
          disabled={busy}
        >
          {QUALITY_OPTIONS.map(option => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
          {hasCustomQuality && (
            <option value={draft.default_quality}>{draft.default_quality}</option>
          )}
        </select>

        <label htmlFor="download-folder">Download folder</label>
        <div className="folder-picker-row">
          <input
            id="download-folder"
            value={draft.download_dir}
            required
            onChange={event => change('download_dir', event.target.value)}
            disabled={busy}
          />
          {onPickFolder && (
            <button className="secondary compact" type="button" onClick={() => void browseFolder()} disabled={busy}>
              Browse…
            </button>
          )}
        </div>

        <label htmlFor="filename-template">Filename template</label>
        <input
          id="filename-template"
          value={draft.filename_template}
          onChange={event => change('filename_template', event.target.value)}
          disabled={busy}
        />

        <div className="settings-two-col">
          <div>
            <label htmlFor="concurrent-downloads">Concurrent downloads</label>
            <input
              id="concurrent-downloads"
              type="number"
              min="1"
              max="10"
              step="1"
              required
              value={draft.max_concurrent}
              onChange={event => change('max_concurrent', event.target.value)}
              disabled={busy}
            />
          </div>
          <div>
            <label htmlFor="download-threads">Fragments / threads per job</label>
            <input
              id="download-threads"
              type="number"
              min="1"
              max="32"
              step="1"
              required
              value={draft.download_threads}
              onChange={event => change('download_threads', event.target.value)}
              disabled={busy}
            />
          </div>
          <div>
            <label htmlFor="speed-limit">Speed limit (KiB/s, 0 unlimited)</label>
            <input
              id="speed-limit"
              type="number"
              min="0"
              step="1"
              required
              value={draft.speed_limit}
              onChange={event => change('speed_limit', event.target.value)}
              disabled={busy}
            />
          </div>
          <div>
            <label htmlFor="max-retries">Automatic retries</label>
            <input
              id="max-retries"
              type="number"
              min="0"
              max="10"
              step="1"
              required
              value={draft.max_retries}
              onChange={event => change('max_retries', event.target.value)}
              disabled={busy}
            />
          </div>
        </div>

        <div className="settings-two-col">
          <div>
            <label htmlFor="postprocess-codec">Post-processing codec</label>
            <select
              id="postprocess-codec"
              value={draft.postprocess_codec}
              onChange={event => change('postprocess_codec', event.target.value)}
              disabled={busy}
            >
              {CODEC_OPTIONS.map(codec => (
                <option key={codec.value} value={codec.value}>{codec.label}</option>
              ))}
              {hasCustomCodec && (
                <option value={draft.postprocess_codec}>{draft.postprocess_codec}</option>
              )}
            </select>
          </div>
          <div>
            <label htmlFor="sponsorblock-mode">SponsorBlock mode</label>
            <select
              id="sponsorblock-mode"
              value={draft.sponsorblock_mode}
              onChange={event => change('sponsorblock_mode', event.target.value)}
              disabled={busy || !draft.sponsorblock_enabled}
            >
              <option value="remove">Remove segments</option>
              <option value="mark">Mark chapters</option>
            </select>
          </div>
        </div>

        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={draft.postprocess_normalize_audio}
            onChange={event => change('postprocess_normalize_audio', event.target.checked)}
            disabled={busy}
          />
          Normalize audio loudness during conversion
        </label>

        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={draft.sponsorblock_enabled}
            onChange={event => change('sponsorblock_enabled', event.target.checked)}
            disabled={busy}
          />
          Enable SponsorBlock integration
        </label>

        <label className="checkbox-label">
          <input
            id="transliterate-bangla"
            type="checkbox"
            checked={draft.transliterate_bangla_filenames}
            onChange={event => change('transliterate_bangla_filenames', event.target.checked)}
            disabled={busy}
          />
          Transliterate Bangla titles to Latin filenames
        </label>

        {dirty && base.revision !== settings.revision && (
          <p className="error">Settings changed in another window. Your edits are kept; reload before saving.</p>
        )}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="connection-actions">
          <button className="primary" type="submit" disabled={busy || !dirty}>Save defaults</button>
          <button
            className="secondary"
            type="button"
            disabled={busy}
            onClick={() => {
              setError('');
              void reload().then(apply).catch(reason => setError(reason instanceof Error ? reason.message : String(reason)));
            }}
          >
            Reload defaults
          </button>
        </div>
      </form>
    </section>
  );
}
