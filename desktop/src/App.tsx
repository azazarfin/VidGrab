import { useEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import {
  companionSession,
  exportDiagnosticsFile,
  getCompanionStatus,
  getDesktopPreferences,
  getDiagnosticsReport,
  installAppImageNative,
  openDownload,
  openSoftwareManagerNative,
  pickDownloadFolder,
  quitCompanionNative,
  removeApplicationNative,
  restartEngine,
  revealDownload,
  setBrowserConnectionNative,
  setDesktopPreferences,
} from './protocol';
import type {
  DesktopPreferences,
  DesktopPreferencesPatch,
  FullDiagnosticsReport,
  Snapshot,
} from './protocol';
import type {
  ActionParams,
  ControlState,
  DownloadParams,
  Job,
  SettingsPatch,
  SettingsSnapshot,
} from '../../packages/protocol/generated/client';
import { OnboardingBanner } from './components/OnboardingBanner';
import { AddDownloadCard } from './components/AddDownloadCard';
import { QueueView } from './components/QueueView';
import { HistoryView } from './components/HistoryView';
import { SettingsView } from './components/SettingsView';
import { BrowserView } from './components/BrowserView';
import { DiagnosticsView } from './components/DiagnosticsView';

type DesktopTab = 'queue' | 'history' | 'settings' | 'browser' | 'diagnostics';

const CONNECTION_LABELS = {
  starting: 'Starting',
  ready: 'Connected',
  recovering: 'Reconnecting',
  failed: 'Engine stopped',
};

export function App() {
  const [activeTab, setActiveTab] = useState<DesktopTab>('queue');
  const [snapshot, setSnapshot] = useState<Snapshot>({ state: 'starting', tray_available: false });
  const [control, setControl] = useState<ControlState>(companionSession.state);
  const [activeJobs, setActiveJobs] = useState<Job[]>([]);
  const [activeNextAfter, setActiveNextAfter] = useState<string | null>(null);
  const [desktopPrefs, setDesktopPrefs] = useState<DesktopPreferences | null>(null);
  const [diagnostics, setDiagnostics] = useState<FullDiagnosticsReport | null>(null);
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [actionError, setActionError] = useState('');
  const [announcement, setAnnouncement] = useState('');
  const [removalError, setRemovalError] = useState('');
  const removalDialog = useRef<HTMLDialogElement>(null);
  const confirmQuitDialog = useRef<HTMLDialogElement>(null);

  function explain(reason: unknown): string {
    const code = (reason as { code?: string })?.code;
    if (code === 'REVISION_CONFLICT') {
      return 'This item changed in another window. Your edits are kept; review its current state before trying again.';
    }
    if (companionSession.pending) {
      return 'The request may have succeeded. Check it before submitting another change.';
    }
    return reason instanceof Error ? reason.message : String(reason);
  }

  async function refreshControl(force = false) {
    const current = await companionSession.sync(force);
    setControl(current);
    return current;
  }

  async function refreshDiagnostics() {
    setDiagnosticsLoading(true);
    try {
      const report = await getDiagnosticsReport();
      setDiagnostics(report);
    } catch (reason) {
      setActionError(explain(reason));
    } finally {
      setDiagnosticsLoading(false);
    }
  }

  useEffect(() => {
    let disposed = false;
    let timeout: ReturnType<typeof setTimeout>;
    async function refresh() {
      const [native, versioned] = await Promise.allSettled([
        getCompanionStatus(),
        companionSession.sync(),
      ]);
      if (!disposed) {
        if (native.status === 'fulfilled') setSnapshot(native.value);
        else setSnapshot(current => ({ ...current, state: 'recovering' }));
        if (versioned.status === 'fulfilled') setControl(versioned.value);
        else setControl(companionSession.state);
        setConnectionError(
          versioned.status === 'rejected'
            ? explain(versioned.reason)
            : native.status === 'rejected'
            ? explain(native.reason)
            : '',
        );
        timeout = setTimeout(refresh, 2000);
      }
    }
    void refresh();
    void getDesktopPreferences().then(prefs => {
      if (!disposed) setDesktopPrefs(prefs);
    }).catch(() => {});
    void getDiagnosticsReport().then(report => {
      if (!disposed) setDiagnostics(report);
    }).catch(() => {});
    return () => {
      disposed = true;
      clearTimeout(timeout);
    };
  }, []);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let mounted = true;
    void (async () => {
      try {
        const stop = await listen('vidgrab://confirm-quit', () => {
          if (mounted) confirmQuitDialog.current?.showModal();
        });
        if (mounted) unlisten = stop;
        else stop();
      } catch {
        // Ignore in standalone browser renderer tests where Tauri event plugin isn't mocked.
      }
    })();
    return () => {
      mounted = false;
      if (unlisten) unlisten();
    };
  }, []);

  const snapshotCursor = control.snapshot?.cursor ?? null;
  const unfinishedCount = control.snapshot?.counts?.unfinished ?? 0;

  useEffect(() => {
    if (!control.connected || !control.snapshot) {
      return;
    }
    if (unfinishedCount <= 0) {
      setActiveJobs([]);
      setActiveNextAfter(null);
      return;
    }
    let cancelled = false;
    void companionSession.read('downloads.list', { view: 'active', limit: 25 }).then(page => {
      if (!cancelled) {
        setActiveJobs(page.jobs);
        setActiveNextAfter(page.next_after);
      }
    }).catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [control.connected, snapshotCursor, unfinishedCount]);

  useEffect(() => {
    if (activeTab === 'diagnostics') {
      void refreshDiagnostics();
    }
  }, [activeTab]);

  async function enqueueSingle(params: DownloadParams) {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      await companionSession.mutate('downloads.enqueue', params);
      await refreshControl();
      setAnnouncement('Download queued.');
    } catch (reason) {
      const message = explain(reason);
      setActionError(message);
      throw new Error(message);
    } finally {
      setBusy(false);
    }
  }

  async function enqueueBatch(items: DownloadParams[]): Promise<{ submitted: number; total: number }> {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    let submitted = 0;
    try {
      for (const item of items) {
        await companionSession.mutate('downloads.enqueue', item);
        submitted += 1;
      }
      await refreshControl();
      setAnnouncement(`${submitted} download${submitted === 1 ? '' : 's'} queued.`);
      return { submitted, total: items.length };
    } catch (reason) {
      setActionError(explain(reason));
      if (!companionSession.pending) {
        try { await refreshControl(); } catch {}
      }
      return { submitted, total: items.length };
    } finally {
      setBusy(false);
    }
  }

  async function saveDefaults(revision: number, patch: SettingsPatch): Promise<SettingsSnapshot> {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      await companionSession.mutate('settings.patch', { expected_revision: revision, patch });
      const current = await refreshControl(true);
      setAnnouncement('Defaults saved.');
      return current.snapshot!.settings;
    } catch (reason) {
      const message = explain(reason);
      setActionError(message);
      throw new Error(message);
    } finally {
      setBusy(false);
    }
  }

  async function jobAction(
    job: Job,
    action: ActionParams['action'],
    extra: { priority?: number; scheduled_at?: string | null } = {},
  ) {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const params: ActionParams = {
        job_id: job.id,
        expected_revision: job.revision,
        action,
        ...extra,
      };
      await companionSession.mutate('downloads.action', params);
      await refreshControl();
      const label = action === 'delete_history'
        ? 'History entry removed (files on disk kept).'
        : 'Download action accepted.';
      setAnnouncement(label);
    } catch (reason) {
      setActionError(explain(reason));
      if (!companionSession.pending) {
        try { await refreshControl(); } catch {}
      }
    } finally {
      setBusy(false);
    }
  }

  async function clearHistory() {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      await companionSession.mutate('downloads.clear_history', {});
      await refreshControl();
      setAnnouncement('Finished history cleared (saved files kept).');
    } catch (reason) {
      setActionError(explain(reason));
    } finally {
      setBusy(false);
    }
  }

  async function checkRequest() {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      await companionSession.replay();
      await refreshControl(true);
      setAnnouncement('Request checked. Current downloads and settings refreshed.');
    } catch (reason) {
      setActionError(explain(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateDesktopPrefs(patch: DesktopPreferencesPatch) {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const updated = await setDesktopPreferences(patch);
      setDesktopPrefs(updated);
      if (patch.onboarding_completed === undefined) {
        setAnnouncement('Desktop preferences saved.');
      }
    } catch (reason) {
      setActionError(explain(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleOnboardingFolder() {
    if (!control.snapshot) return;
    setActionError('');
    try {
      const picked = await pickDownloadFolder();
      if (!picked) return;
      await saveDefaults(control.snapshot.settings.revision, { download_dir: picked });
    } catch (reason) {
      setActionError(explain(reason));
    }
  }

  async function handleRestartEngine() {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const next = await restartEngine();
      setSnapshot(current => ({ ...current, ...next }));
      await refreshDiagnostics();
      setAnnouncement('Engine restart requested.');
    } catch (reason) {
      setActionError(explain(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleExportDiagnostics() {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const savedPath = await exportDiagnosticsFile();
      if (savedPath) setAnnouncement(`Diagnostics saved to ${savedPath}.`);
    } catch (reason) {
      setActionError(explain(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleReveal(jobId: string) {
    setActionError('');
    try {
      await revealDownload(jobId);
    } catch (reason) {
      setActionError(explain(reason));
    }
  }

  async function handleOpen(jobId: string) {
    setActionError('');
    try {
      await openDownload(jobId);
    } catch (reason) {
      setActionError(explain(reason));
    }
  }

  async function quit() {
    setBusy(true);
    try {
      await quitCompanionNative();
    } catch (reason) {
      setActionError(String(reason));
      setBusy(false);
    }
  }

  async function setBrowserConnection(enabled: boolean) {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const connection = await setBrowserConnectionNative(enabled);
      setSnapshot(current => ({ ...current, browser_connection: connection, browser_connection_error: undefined }));
      setAnnouncement(enabled ? 'Browser registration saved.' : 'Browser access disconnected. Downloads keep running.');
    } catch (reason) {
      setActionError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function installAppImage() {
    setBusy(true);
    setActionError('');
    setAnnouncement('');
    try {
      const application = await installAppImageNative();
      setSnapshot(current => ({ ...current, application, application_error: undefined }));
      setAnnouncement('Installed in Applications. Your current download session stays open.');
    } catch (reason) {
      setActionError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function removeApplication() {
    setBusy(true);
    setRemovalError('');
    try {
      const application = await removeApplicationNative();
      setSnapshot(current => ({ ...current, application }));
      removalDialog.current?.close();
    } catch (reason) {
      setRemovalError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function openAppCenter() {
    setActionError('');
    try {
      await openSoftwareManagerNative();
    } catch (reason) {
      setActionError(String(reason));
    }
  }

  function confirmRemoval() {
    setRemovalError('');
    removalDialog.current?.showModal();
  }

  const application = snapshot.application;
  const counts = control.snapshot?.counts;
  const activeDownloads = snapshot.state === 'ready' && (snapshot.downloads_active === true || (counts?.unfinished || 0) > 0);
  const connected = control.connected && snapshot.state === 'ready';
  const mergedJobsMap = new Map<string, Job>();
  for (const job of control.snapshot?.jobs || []) {
    mergedJobsMap.set(job.id, job);
  }
  for (const job of activeJobs) {
    const existing = mergedJobsMap.get(job.id);
    if (!existing || job.revision >= existing.revision) {
      mergedJobsMap.set(job.id, job);
    }
  }
  const jobs = [...mergedJobsMap.values()];
  const hasPendingRequest = Boolean(companionSession.pending);
  const showOnboarding = Boolean(desktopPrefs && !desktopPrefs.onboarding_completed);
  const historyRefreshKey = `${control.snapshot?.cursor ?? 0}:${counts?.history ?? 0}:${counts?.total ?? 0}`;

  async function loadMoreQueue(scope: 'active' | 'all') {
    setBusy(true);
    setActionError('');
    try {
      if (scope === 'active' || activeNextAfter) {
        if (activeNextAfter) {
          const page = await companionSession.read('downloads.list', {
            view: 'active',
            limit: 25,
            after: activeNextAfter,
          });
          setActiveJobs(prev => {
            const byId = new Map(prev.map(item => [item.id, item]));
            for (const item of page.jobs) byId.set(item.id, item);
            return [...byId.values()];
          });
          setActiveNextAfter(page.next_after);
        }
      }
      if (scope === 'all' && control.snapshot?.next_after) {
        setControl(await companionSession.loadMore());
      }
    } catch (reason) {
      setActionError(explain(reason));
    } finally {
      setBusy(false);
    }
  }

  const navItems: Array<{ id: DesktopTab; label: string; count?: number }> = [
    { id: 'queue', label: 'Queue', count: counts?.unfinished },
    { id: 'history', label: 'History', count: counts?.history },
    { id: 'settings', label: 'Settings' },
    { id: 'browser', label: 'Browser' },
    { id: 'diagnostics', label: 'Diagnostics' },
  ];

  return (
    <div className="app-frame">
      <header className="toolbar">
        <div className="brand">
          <img src="/vidgrab.png" alt="" width="32" height="32" />
          <span>VidGrab</span>
        </div>
        <div className="toolbar-status">
          <span className="connection" data-state={snapshot.state} role="status">
            {connected ? 'Connected' : CONNECTION_LABELS[snapshot.state] === 'Connected' ? 'Connecting' : CONNECTION_LABELS[snapshot.state]}
          </span>
          {snapshot.state === 'failed' && (
            <button className="secondary compact" type="button" onClick={() => void handleRestartEngine()} disabled={busy}>
              Restart engine
            </button>
          )}
        </div>
      </header>

      <div className="workspace">
        <nav className="sidebar-nav" aria-label="Manager sections">
          {navItems.map(item => (
            <button
              key={item.id}
              type="button"
              className={`nav-tab ${activeTab === item.id ? 'active' : ''}`}
              data-desktop-tab={item.id}
              aria-current={activeTab === item.id ? 'page' : undefined}
              onClick={() => setActiveTab(item.id)}
            >
              <span>{item.label}</span>
              {item.count !== undefined && <span className="nav-badge">{item.count}</span>}
            </button>
          ))}
        </nav>

        <main className="main-panel">
          {connectionError && <p className="error banner-alert" role="alert">{connectionError}</p>}
          {actionError && <p className="error banner-alert" role="alert">{actionError}</p>}
          {hasPendingRequest && (
            <div className="pending-review" role="status">
              <span>A control request outcome is unknown. Check it before submitting new changes.</span>
              <button className="secondary compact" type="button" onClick={() => void checkRequest()} disabled={busy}>
                Check request
              </button>
            </div>
          )}
          <p className="announcement" role="status">{announcement}</p>

          {snapshot.state === 'failed' && (
            <div className="card engine-failed-card" role="alert">
              <p className="error">The background download engine stopped unexpectedly.</p>
              <button className="primary compact" type="button" onClick={() => void handleRestartEngine()} disabled={busy}>
                Restart engine
              </button>
            </div>
          )}

          {activeTab === 'queue' && (
            <>
              {showOnboarding && (
                <OnboardingBanner
                  downloadDir={control.snapshot?.settings.values.download_dir || ''}
                  diagnostics={diagnostics}
                  browserConnection={snapshot.browser_connection}
                  busy={busy}
                  onChangeFolder={() => void handleOnboardingFolder()}
                  onOpenBrowserTab={() => setActiveTab('browser')}
                  onOpenDiagnosticsTab={() => setActiveTab('diagnostics')}
                  onDismiss={() => void handleUpdateDesktopPrefs({ onboarding_completed: true })}
                />
              )}
              <AddDownloadCard
                busy={busy}
                connected={connected}
                defaultFormat={control.snapshot?.settings.values.default_format || ''}
                downloadDir={control.snapshot?.settings.values.download_dir || ''}
                hasPendingRequest={hasPendingRequest}
                onEnqueue={enqueueSingle}
                onBatchEnqueue={enqueueBatch}
                onError={setActionError}
              />
              <QueueView
                jobs={jobs}
                counts={counts}
                hasMoreActive={Boolean(activeNextAfter)}
                hasMoreAll={Boolean(control.snapshot?.next_after || activeNextAfter)}
                busy={busy}
                connected={connected}
                hasPendingRequest={hasPendingRequest}
                onAction={jobAction}
                onLoadMore={loadMoreQueue}
                onReveal={handleReveal}
                onOpen={handleOpen}
              />
            </>
          )}

          {activeTab === 'history' && (
            <HistoryView
              connected={connected}
              busy={busy}
              hasPendingRequest={hasPendingRequest}
              refreshKey={historyRefreshKey}
              counts={counts}
              onAction={jobAction}
              onClearHistory={clearHistory}
              onReveal={handleReveal}
              onOpen={handleOpen}
              onError={setActionError}
            />
          )}

          {activeTab === 'settings' && (
            <SettingsView
              settings={control.snapshot?.settings || null}
              desktopPrefs={desktopPrefs}
              busy={busy}
              connected={connected}
              onSaveDefaults={saveDefaults}
              onReloadDefaults={async () => (await refreshControl(true)).snapshot!.settings}
              onPickFolder={pickDownloadFolder}
              onUpdateDesktopPrefs={handleUpdateDesktopPrefs}
            />
          )}

          {activeTab === 'browser' && (
            <BrowserView
              browserConnection={snapshot.browser_connection}
              browserConnectionError={snapshot.browser_connection_error}
              application={application}
              applicationError={snapshot.application_error}
              busy={busy}
              activeDownloads={activeDownloads}
              onSetBrowserConnection={setBrowserConnection}
              onInstallAppImage={installAppImage}
              onOpenSoftwareManager={openAppCenter}
              onConfirmRemoval={confirmRemoval}
            />
          )}

          {activeTab === 'diagnostics' && (
            <DiagnosticsView
              report={diagnostics}
              loading={diagnosticsLoading}
              busy={busy}
              onRefresh={refreshDiagnostics}
              onRestartEngine={handleRestartEngine}
              onExportDiagnostics={handleExportDiagnostics}
            />
          )}
        </main>
      </div>

      <footer className="footer">
        <p>
          {snapshot.tray_available && desktopPrefs?.close_to_tray !== false
            ? 'Closing this window keeps VidGrab running in the tray.'
            : !snapshot.tray_available
            ? 'This desktop has no tray. Keep this window open; use Quit VidGrab to close the app.'
            : 'Closing this window quits VidGrab.'}
        </p>
        <button
          className="secondary"
          type="button"
          onClick={() => {
            if (activeDownloads) {
              confirmQuitDialog.current?.showModal();
            } else {
              void quit();
            }
          }}
          disabled={busy}
        >
          Quit VidGrab
        </button>
      </footer>

      <dialog className="removal-dialog" ref={removalDialog} aria-labelledby="remove-title" aria-describedby="remove-description">
        <h2 id="remove-title">{application?.kind === 'appimage' ? 'Remove the installed copy?' : 'Prepare to remove VidGrab?'}</h2>
        <p id="remove-description">
          {application?.kind === 'appimage'
            ? 'VidGrab will disconnect browser access, quit and remove its installed AppImage and Applications shortcut. Your history and saved files are kept.'
            : application?.kind === 'windows'
            ? 'VidGrab will disconnect browser access, quit and open Installed apps. Uninstall VidGrab there. Your history and saved files are kept.'
            : 'VidGrab will disconnect browser access, quit and open App Center. Use Manage to uninstall the Ubuntu package. Your history and saved files are kept.'}
        </p>
        {activeDownloads && <p className="helper">Finish active downloads before removing VidGrab.</p>}
        {removalError && <p className="error" role="alert">{removalError}</p>}
        <div className="dialog-actions">
          <button className="secondary" type="button" autoFocus disabled={busy} onClick={() => removalDialog.current?.close()}>
            Keep VidGrab
          </button>
          <button className="secondary critical" type="button" disabled={busy || activeDownloads} onClick={() => void removeApplication()}>
            {busy ? 'Working…' : application?.kind === 'appimage' ? 'Remove and quit' : 'Disconnect and quit'}
          </button>
        </div>
      </dialog>

      <dialog className="removal-dialog" ref={confirmQuitDialog} aria-labelledby="confirm-quit-title" aria-describedby="confirm-quit-description">
        <h2 id="confirm-quit-title">Quit while downloads are active?</h2>
        <p id="confirm-quit-description">
          Active downloads will be interrupted safely and can be resumed when VidGrab opens again.
        </p>
        <div className="dialog-actions">
          <button className="secondary" type="button" autoFocus onClick={() => confirmQuitDialog.current?.close()}>
            Keep running
          </button>
          <button className="secondary critical" type="button" onClick={() => { confirmQuitDialog.current?.close(); void quit(); }}>
            Quit VidGrab
          </button>
        </div>
      </dialog>
    </div>
  );
}
