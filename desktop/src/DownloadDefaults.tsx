import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import type { SettingsPatch, SettingsSnapshot } from '../../packages/protocol/generated/client';

interface Props {
  settings: SettingsSnapshot;
  busy: boolean;
  save: (revision: number, patch: SettingsPatch) => Promise<SettingsSnapshot>;
  reload: () => Promise<SettingsSnapshot>;
}

function fields(settings: SettingsSnapshot) {
  return {
    default_format: settings.values.default_format,
    download_dir: settings.values.download_dir,
    filename_template: settings.values.filename_template,
    max_concurrent: String(settings.values.max_concurrent),
  };
}

export function DownloadDefaults({ settings, busy, save, reload }: Props) {
  const [base, setBase] = useState(settings);
  const [draft, setDraft] = useState(() => fields(settings));
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!dirty) { setBase(settings); setDraft(fields(settings)); }
  }, [settings, dirty]);

  function apply(snapshot: SettingsSnapshot) {
    setBase(snapshot); setDraft(fields(snapshot)); setDirty(false); setError('');
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError('');
    const patch: SettingsPatch = {};
    for (const key of ['default_format', 'download_dir', 'filename_template'] as const) {
      if (draft[key] !== base.values[key]) patch[key] = draft[key];
    }
    const concurrent = Number(draft.max_concurrent);
    if (!Number.isSafeInteger(concurrent) || concurrent < 1 || concurrent > 10) {
      setError('Choose between 1 and 10 concurrent downloads.'); return;
    }
    if (concurrent !== base.values.max_concurrent) patch.max_concurrent = concurrent;
    try { apply(await save(base.revision, patch)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }

  const change = (key: keyof typeof draft, value: string) => {
    setDraft(current => ({ ...current, [key]: value })); setDirty(true);
  };
  return (
    <details className="download-defaults">
      <summary>Download defaults</summary>
      <p className="helper">The desktop and browser extension use these app settings.</p>
      <form className="defaults-form" onSubmit={submit}>
        <label htmlFor="default-format">Default format</label>
        <input id="default-format" value={draft.default_format} onChange={event => change('default_format', event.target.value)} disabled={busy} />
        <label htmlFor="download-folder">Download folder</label>
        <input id="download-folder" value={draft.download_dir} required onChange={event => change('download_dir', event.target.value)} disabled={busy} />
        <label htmlFor="filename-template">Filename template</label>
        <input id="filename-template" value={draft.filename_template} onChange={event => change('filename_template', event.target.value)} disabled={busy} />
        <label htmlFor="concurrent-downloads">Concurrent downloads</label>
        <input id="concurrent-downloads" type="number" min="1" max="10" step="1" required value={draft.max_concurrent}
          onChange={event => change('max_concurrent', event.target.value)} disabled={busy} />
        {dirty && base.revision !== settings.revision && <p className="error">Settings changed in another window. Your edits are kept; reload before saving.</p>}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="connection-actions">
          <button className="primary" type="submit" disabled={busy || !dirty}>Save defaults</button>
          <button className="secondary" type="button" disabled={busy} onClick={() => {
            setError(''); void reload().then(apply).catch(reason => setError(reason instanceof Error ? reason.message : String(reason)));
          }}>Reload defaults</button>
        </div>
      </form>
    </details>
  );
}
