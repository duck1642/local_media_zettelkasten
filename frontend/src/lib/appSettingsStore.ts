import { derived, get, writable } from 'svelte/store';
import { apiFetch } from './api';
import { normalizeTileMinWidth, type VaultLayoutMode } from './layout';
import { log as uiLog } from './logger';

export const DEFAULT_INSPECTOR_WIDTH = 400;
export const MIN_INSPECTOR_WIDTH = 320;
export const MAX_INSPECTOR_WIDTH = 760;

type PlatformConcurrency = { workers: number; jitter_range: [number, number] };
type VideoTaggingSettings = {
  enabled: boolean;
  frame_count: number;
  merge_min_frames: number;
  merge_high_confidence: number;
};
type TaggingSettings = {
  enabled: boolean;
  backend: 'pixai' | 'wd';
  model_repo: string;
  model_path: string;
  profiles: Record<string, {
    thresholds: Record<string, number>;
    category_map: Record<string, string | null>;
    label_map: Record<string, Record<string, string>>;
  }>;
  device: 'auto' | 'cpu' | 'cuda';
  display_source: 'yaml' | 'database';
  threshold: number;
  max_tags: number;
  fail_ingestion_on_error: boolean;
  video: VideoTaggingSettings;
};

export type AppSettings = {
  schema_version: 1;
  ui: {
    vault_layout_mode: VaultLayoutMode;
    vault_tile_min_width: number;
    inspector_visible: boolean;
    inspector_width: number;
    privacy_blur: boolean;
    ram_tracking_enabled: boolean;
  };
  webview: { devtools_enabled: boolean; context_menu_enabled: boolean };
  logging: { level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL' };
  network: { proxy: string; user_agent: string };
  ingestion: {
    concurrency: { global_max_workers: number; platforms: Record<string, PlatformConcurrency> };
    accepted_media: { extensions: string[]; mime_types: string[] };
    processing: { flatten_transparency: boolean; thumbnail_format: 'jpeg' | 'webp' | 'png'; background_preset: 'white' | 'black' | 'custom'; custom_color: [number, number, number] };
  };
  tagging: TaggingSettings;
};

export const appSettings = writable<AppSettings | null>(null);
export const persistedAppSettings = writable<AppSettings | null>(null);
export const appSettingsLoading = writable(false);
export const appSettingsSaving = writable(false);
export const appSettingsError = writable('');
const savedSettingsText = writable('');
let currentEtag = '';
let loadPromise: Promise<AppSettings> | null = null;

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value));
}

function normalize(value: AppSettings): AppSettings {
  const next = clone(value);
  next.ui.vault_tile_min_width = normalizeTileMinWidth(next.ui.vault_tile_min_width);
  next.ui.inspector_width = normalizeInspectorWidth(next.ui.inspector_width);
  next.ui.privacy_blur = Boolean(next.ui.privacy_blur);
  next.ui.ram_tracking_enabled = Boolean(next.ui.ram_tracking_enabled);
  next.ingestion.processing.flatten_transparency = Boolean(next.ingestion.processing.flatten_transparency);
  next.ingestion.processing.thumbnail_format ??= 'jpeg';
  next.tagging.enabled = next.tagging.enabled !== false;
  next.tagging.backend ??= 'wd';
  next.tagging.model_path ??= '';
  next.tagging.profiles ??= {};
  next.tagging.threshold = Number(next.tagging.threshold ?? 0.35);
  next.tagging.max_tags = Number(next.tagging.max_tags ?? 30);
  return next;
}

async function requireOk(response: Response) {
  if (response.ok) return response;
  const payload = await response.json().catch(() => ({}));
  const detail = payload?.detail;
  const message = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `HTTP ${response.status}`;
  throw new Error(message);
}

export function normalizeInspectorWidth(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return DEFAULT_INSPECTOR_WIDTH;
  return Math.max(MIN_INSPECTOR_WIDTH, Math.min(MAX_INSPECTOR_WIDTH, Math.round(numeric)));
}

export const appSettingsDirty = derived(
  [appSettings, savedSettingsText],
  ([$settings, $saved]) => Boolean($settings) && JSON.stringify($settings) !== $saved
);

export async function loadAppSettings(force = false): Promise<AppSettings> {
  const current = get(appSettings);
  if (current && !force) return current;
  if (loadPromise && !force) return loadPromise;
  appSettingsLoading.set(true);
  appSettingsError.set('');
  loadPromise = apiFetch('/api/app/settings')
    .then(requireOk)
    .then(async (response) => {
      currentEtag = response.headers.get('etag') || '';
      const next = normalize(await response.json());
      appSettings.set(next);
      persistedAppSettings.set(next);
      savedSettingsText.set(JSON.stringify(next));
      return next;
    })
    .catch((error) => {
      appSettingsError.set(String(error instanceof Error ? error.message : error));
      uiLog('ERROR', 'Failed to load app settings', { error: String(error) });
      throw error;
    })
    .finally(() => {
      appSettingsLoading.set(false);
      loadPromise = null;
    });
  return loadPromise;
}

export async function saveAppSettings(nextValue: AppSettings | null = get(appSettings)) {
  if (!nextValue) return null;
  const next = normalize(nextValue);
  appSettingsSaving.set(true);
  appSettingsError.set('');
  try {
    if (!currentEtag) throw new Error('App settings must be loaded before saving');
    const response = await requireOk(await apiFetch('/api/app/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'If-Match': currentEtag },
      body: JSON.stringify(next)
    }));
    const saved = normalize(await response.json());
    currentEtag = response.headers.get('etag') || currentEtag;
    appSettings.set(saved);
    persistedAppSettings.set(saved);
    savedSettingsText.set(JSON.stringify(saved));
    return saved;
  } catch (error) {
    appSettingsError.set(String(error instanceof Error ? error.message : error));
    uiLog('ERROR', 'Failed to save app settings', { error: String(error) });
    throw error;
  } finally {
    appSettingsSaving.set(false);
  }
}

export async function updateAppSettings(mutator: (draft: AppSettings) => void, save = false) {
  const base = get(appSettings) || await loadAppSettings();
  const draft = clone(base);
  mutator(draft);
  const next = normalize(draft);
  appSettings.set(next);
  if (save) await saveAppSettings(next);
  return next;
}

export const saveCurrentAppSettings = () => saveAppSettings(get(appSettings));
export const setVaultLayoutMode = (mode: VaultLayoutMode) => updateAppSettings((draft) => { draft.ui.vault_layout_mode = mode; }, true);
export const setRamTrackingEnabled = (enabled: boolean) => updateAppSettings((draft) => { draft.ui.ram_tracking_enabled = enabled; }, true);
export const setInspectorWidth = (width: number) => updateAppSettings((draft) => { draft.ui.inspector_width = normalizeInspectorWidth(width); }, true);

export function setVaultTileMinWidthLocal(width: number) {
  const current = get(appSettings);
  if (!current) return null;
  const next = clone(current);
  next.ui.vault_tile_min_width = normalizeTileMinWidth(width);
  appSettings.set(next);
  return next;
}
