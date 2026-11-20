import type { FullDiagnosticsReport, BrowserConnection } from '../protocol';

interface Props {
  downloadDir: string;
  diagnostics: FullDiagnosticsReport | null;
  browserConnection?: BrowserConnection;
  busy: boolean;
  onChangeFolder: () => void;
  onOpenBrowserTab: () => void;
  onOpenDiagnosticsTab: () => void;
  onDismiss: () => void;
}

export function OnboardingBanner({
  downloadDir,
  diagnostics,
  browserConnection,
  busy,
  onChangeFolder,
  onOpenBrowserTab,
  onOpenDiagnosticsTab,
  onDismiss,
}: Props) {
  const engineChecks = diagnostics && 'component_checks' in diagnostics.engine
    ? diagnostics.engine.component_checks
    : [];
  const missingTools = engineChecks.filter(item => !item.available);
  const browserRegistered = Boolean(
    browserConnection?.enabled
    && browserConnection.bridge_ready
    && browserConnection.chrome === 'registered'
    && browserConnection.firefox === 'registered',
  );

  return (
    <section className="card onboarding-banner" aria-labelledby="onboarding-title">
      <div className="card-header">
        <div>
          <p className="eyebrow">First-run check</p>
          <h2 id="onboarding-title">Your standalone download workspace</h2>
        </div>
        <button className="secondary" type="button" onClick={onDismiss} disabled={busy}>
          Finish setup
        </button>
      </div>
      <div className="onboarding-grid">
        <div className="onboarding-item">
          <span className="item-label">Tools &amp; components</span>
          {engineChecks.length === 0 ? (
            <span className="item-value">Checking bundled tools…</span>
          ) : missingTools.length === 0 ? (
            <span className="item-value status-ok">
              Ready ({engineChecks.map(c => c.name).join(', ')})
            </span>
          ) : (
            <span className="item-value status-warn">
              Missing: {missingTools.map(c => c.name).join(', ')}
            </span>
          )}
          <button className="link-button" type="button" onClick={onOpenDiagnosticsTab}>
            Inspect diagnostics
          </button>
        </div>
        <div className="onboarding-item">
          <span className="item-label">Save location</span>
          <span className="item-value path-text">{downloadDir || 'Loading…'}</span>
          <button className="link-button" type="button" onClick={onChangeFolder} disabled={busy}>
            Choose folder…
          </button>
        </div>
        <div className="onboarding-item">
          <span className="item-label">Browser integration</span>
          <span className="item-value">
            {browserConnection
              ? (!browserConnection.enabled ? 'Disconnected (standalone only)' : browserRegistered ? 'Ready for Chrome & Firefox' : 'Needs repair')
              : 'Optional in packaged builds'}
          </span>
          <button className="link-button" type="button" onClick={onOpenBrowserTab}>
            Manage browsers
          </button>
        </div>
      </div>
    </section>
  );
}
