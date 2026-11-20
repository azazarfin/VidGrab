import { invoke } from '@tauri-apps/api/core';
import { createClient, createSession } from '../../packages/protocol/generated/client';
import type {
  Capabilities,
  ComponentCheck,
  Counts,
  DiagnosticsSnapshot,
  SettingsPatch,
} from '../../packages/protocol/generated/client';

export const companionControl = createClient(request => invoke('protocol_request', { request }));
export const companionSession = createSession(companionControl, () => crypto.randomUUID());

export interface ApplicationStatus {
  kind: 'deb' | 'appimage' | 'windows';
  installed: boolean;
  app: string;
  shortcut?: string;
}

export interface BrowserConnection {
  enabled: boolean;
  bridge_ready: boolean;
  chrome: 'missing' | 'registered' | 'conflict';
  firefox: 'missing' | 'registered' | 'conflict';
}

export interface UpdateLastCheck {
  checked_at: number;
  status: 'up_to_date' | 'update_available' | 'offline' | 'error';
  available_version?: string | null;
  channel: string;
  notes?: string | null;
  message?: string | null;
}

export interface UpdateBackupSummary {
  id: string;
  reason: string;
  version: string;
  created_at: number;
  files_count: number;
}

export interface UpdateStatus {
  app_version: string;
  engine_bundle_version: string;
  protocol_version: number;
  platform: string;
  channel: string;
  rollback_available: boolean;
  previous_version?: string;
  pending_update?: {
    version: string;
    channel: string;
    sha256: string;
    notices_sha256: string;
    notes?: string | null;
  } | null;
  rollback_notice?: string | null;
  last_check?: UpdateLastCheck | null;
  backups_count?: number;
  backups?: UpdateBackupSummary[];
  error?: string;
}

export interface LegacyConfigImport {
  source_path: string;
  patch: SettingsPatch;
  imported_keys: string[];
  skipped_keys: string[];
  warnings: string[];
}

export interface Snapshot {
  state: 'starting' | 'ready' | 'recovering' | 'failed';
  tray_available: boolean;
  downloads_active?: boolean;
  supervisor_pid?: number;
  engine_pid?: number | null;
  engine_generation?: number;
  app_version?: string;
  engine_bundle_version?: string;
  rollback_notice?: string | null;
  browser_connection?: BrowserConnection;
  browser_connection_error?: string;
  application?: ApplicationStatus;
  application_error?: string;
}

export interface DesktopPreferences {
  preferences_version: 1;
  start_at_login: boolean;
  close_to_tray: boolean;
  start_minimized: boolean;
  onboarding_completed: boolean;
  tray_available: boolean;
  packaged: boolean;
  autostart_state: 'missing' | 'registered' | 'conflict' | 'unavailable' | 'error';
  autostart_error?: string;
}

export interface DesktopPreferencesPatch {
  start_at_login?: boolean;
  close_to_tray?: boolean;
  start_minimized?: boolean;
  onboarding_completed?: boolean;
}

export interface UrlListImport {
  path: string;
  urls: string[];
  truncated: boolean;
}

export interface FullDiagnosticsReport {
  supervisor: Snapshot & { error?: string };
  engine: (DiagnosticsSnapshot & { error?: string }) | { error: string };
  desktop_preferences: DesktopPreferences;
  updates?: UpdateStatus;
  browser_connection?: BrowserConnection;
  browser_connection_error?: string;
  application?: ApplicationStatus;
  application_error?: string;
  engine_log_tail?: string[];
}

export type { Capabilities, ComponentCheck, Counts, DiagnosticsSnapshot };

export function getCompanionStatus(): Promise<Snapshot> {
  return invoke<Snapshot>('companion_status');
}

export function getDesktopPreferences(): Promise<DesktopPreferences> {
  return invoke<DesktopPreferences>('get_desktop_preferences');
}

export function setDesktopPreferences(preferences: DesktopPreferencesPatch): Promise<DesktopPreferences> {
  return invoke<DesktopPreferences>('set_desktop_preferences', { preferences });
}

export function restartEngine(): Promise<Snapshot> {
  return invoke<Snapshot>('restart_engine');
}

export function pickDownloadFolder(): Promise<string | null> {
  return invoke<string | null>('pick_download_folder');
}

export function pickUrlListFile(): Promise<UrlListImport | null> {
  return invoke<UrlListImport | null>('pick_url_list_file');
}

export function pickAndImportLegacyConfig(): Promise<LegacyConfigImport | null> {
  return invoke<LegacyConfigImport | null>('pick_and_import_legacy_config');
}

export function getUpdateStatus(): Promise<UpdateStatus> {
  return invoke<UpdateStatus>('update_status');
}

export function checkForUpdates(url?: string, channel?: string): Promise<UpdateStatus> {
  return invoke<UpdateStatus>('check_for_updates', { url, channel });
}

export function applyUpdate(url?: string, channel?: string): Promise<UpdateStatus> {
  return invoke<UpdateStatus>('apply_update', { url, channel });
}

export function rollbackUpdate(): Promise<UpdateStatus> {
  return invoke<UpdateStatus>('rollback_update');
}

export function revealDownload(jobId: string): Promise<void> {
  return invoke<void>('reveal_download', { jobId });
}

export function openDownload(jobId: string): Promise<void> {
  return invoke<void>('open_download', { jobId });
}

export function getDiagnosticsReport(): Promise<FullDiagnosticsReport> {
  return invoke<FullDiagnosticsReport>('diagnostics_report');
}

export function exportDiagnosticsFile(): Promise<string | null> {
  return invoke<string | null>('export_diagnostics_file');
}

export function setBrowserConnectionNative(enabled: boolean): Promise<BrowserConnection> {
  return invoke<BrowserConnection>('set_browser_connection', { enabled });
}

export function installAppImageNative(): Promise<ApplicationStatus> {
  return invoke<ApplicationStatus>('install_appimage');
}

export function removeApplicationNative(): Promise<ApplicationStatus> {
  return invoke<ApplicationStatus>('remove_application');
}

export function openSoftwareManagerNative(): Promise<void> {
  return invoke<void>('open_software_manager');
}

export function quitCompanionNative(): Promise<void> {
  return invoke<void>('quit_companion');
}
