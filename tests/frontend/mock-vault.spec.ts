import { expect, test, type Page, type Route } from '@playwright/test';
import manifestData from '../fixtures/mock-vault/manifest.json';
import { installAppStateRoutes, makeAppSettings } from './app-state-fixture';

type MockItem = {
  hash: string;
  artist: string;
  platform: string;
  source_url: string;
  url: string;
  thumbnail_url: string;
  [key: string]: unknown;
};

const manifest = manifestData as {
  items: MockItem[];
  review: unknown[];
  queues: Record<string, string[]>;
  expectations: Record<string, string | number>;
};

function cloneItems() {
  return manifest.items.map((item) => ({ ...item }));
}

function facetItems(items: MockItem[], key: string) {
  const counts = new Map<string, number>();
  for (const item of items) {
    if (key === 'wd_tag') {
      const wd = item.wd_tags as { rating?: string; characters?: string[]; general?: string[] } | undefined;
      const values = [
        wd?.rating,
        ...(Array.isArray(wd?.characters) ? wd.characters : []),
        ...(Array.isArray(wd?.general) ? wd.general : [])
      ];
      for (const tag of values) {
        const value = String(tag || '').trim();
        if (value) counts.set(value, (counts.get(value) || 0) + 1);
      }
      continue;
    }
    if (key === 'topic' || key === 'topics') {
      const topics = Array.isArray(item.topics) ? item.topics : [];
      for (const topic of topics) {
        const value = String(topic || '').trim();
        if (value) counts.set(value, (counts.get(value) || 0) + 1);
      }
      continue;
    }
    const value = String(item[key] || '').trim();
    if (value) counts.set(value, (counts.get(value) || 0) + 1);
  }
  return [...counts.entries()].map(([value, count]) => ({ value, count }));
}

function facetCountMap(items: MockItem[], key: string) {
  return Object.fromEntries(facetItems(items, key).map((item) => [item.value, item.count]));
}

function canonicalPlatformDisplay(value: string) {
  const key = String(value || '').trim().toLowerCase();
  if (key === 'x' || key.startsWith('twitter')) return 'X';
  if (key === 'pixiv') return 'Pixiv';
  if (key === 'instagram') return 'Instagram';
  if (key === 'pinterest') return 'Pinterest';
  if (key === 'youtube') return 'YouTube';
  if (key === 'local') return 'Local';
  return value;
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

type MockLogEntry = string | Record<string, unknown>;

function defaultMockLogs(): MockLogEntry[] {
  return [
    {
      timestamp: '2026-06-16 10:00:00',
      level: 'CRITICAL',
      module: 'system',
      message: 'Disk almost full',
      platform: 'Pixiv',
      run_id: 'run-42'
    },
    {
      timestamp: '2026-06-16 10:00:01',
      level: 'NOTICE',
      module: 'custom',
      message: 'Custom level visible',
      platform: 'X',
      trace_id: 'trace-42'
    },
    'raw console line'
  ];
}

function logSseBody(entries: MockLogEntry[]) {
  return entries
    .map((entry) => `data: ${typeof entry === 'string' ? entry : JSON.stringify(entry)}\n\n`)
    .join('');
}

async function installMockVaultApi(
  page: Page,
  options: {
    memoryFails?: boolean;
    onReviewAction?: () => Promise<void>;
    onLocalIngestStart?: (payload?: any) => Promise<void> | void;
    onMaintenanceAction?: (action: 'auth' | 'metadata' | 'review') => Promise<void> | void;
    onItemsRequest?: (url: URL) => Promise<void> | void;
    testJxlThumbnail?: boolean;
    onItemPatch?: (payload: any) => Promise<void> | void;
    metadataRebuildResponse?: unknown;
    metadataStatusSequence?: unknown[];
    onWorkspaceAction?: (action: 'add' | 'active' | 'delete', payload?: any) => Promise<void> | void;
    workspaceDeleteCleanupStatus?: 'complete' | 'pending';
    onVaultAction?: (action: 'merge-preview' | 'merge' | 'delete' | 'health' | 'repair' | 'backup' | 'export' | 'import-preview' | 'import' | 'restore-preview' | 'restore', payload?: any) => Promise<void> | void;
    logClearFails?: boolean;
    onLogStream?: (url: URL) => Promise<void> | void;
    logEntriesForStream?: (url: URL) => MockLogEntry[];
    appSettingsPutStatus?: number;
    appSettingsIncludeEtag?: boolean;
    workspaceLoadLegacy?: boolean;
  } = {}
) {
  let items = cloneItems();
  if (options.testJxlThumbnail) {
    items[0] = { ...items[0], mime_type: 'image/jxl', extension: '.jxl', thumbnail_url: `/api/thumbnails/${items[0].hash}` };
  }
  let failNextArtistMerge = false;
  let artistDetails = facetItems(items, 'artist').map((entry, index) => ({
    id: index + 1,
    name: entry.value,
    name_norm: entry.value.toLowerCase(),
    kind: 'artist',
    notes: '',
    item_count: entry.count,
    aliases: [] as Array<{ id: number; alias: string; alias_norm: string }>,
    links: [] as Array<{ id: number; platform: string; url: string; handle: string; is_primary: boolean }>
  }));
  const platformDetails = facetItems(items, 'platform').map((entry, index) => ({
    id: index + 1,
    key_norm: entry.value.toLowerCase(),
    display_name: canonicalPlatformDisplay(entry.value),
    kind: 'source',
    item_count: entry.count,
    alias_count: 0
  }));
  let nextArtistAliasId = 1;
  let nextArtistLinkId = 1;
  const reviewItems = JSON.parse(JSON.stringify(manifest.review));
  const queueContent: Record<string, string> = {
    normal: manifest.queues.normal.join('\n'),
    force: manifest.queues.force.join('\n'),
    failed: manifest.queues.failed.join('\n')
  };
  const localStatus = {
    running: false,
    phase: 'idle',
    run_id: null,
    scanned: 0,
    staged: 0,
    queued: 0,
    processed: 0,
    summary: { ingested: 0, review: 0, failed: 0, duplicate: 0 },
    results: [],
    failed_paths: [],
    last_defaults: {},
    last_skip_similarity: false,
    started_at: null,
    finished_at: null,
    stop_requested: false
  };
  let metadataStatusIndex = 0;
  let workspaceActive = 'default';
  let workspaceItems = [
    {
      id: 'default',
      name: 'Default',
      config_path: 'C:/Users/Test/.lmz/default/config.yaml',
      active: true,
      exists: true,
      managed: false,
      can_delete_all: false
    },
    {
      id: 'obsidian-main',
      name: 'Obsidian Main',
      config_path: 'C:/ObsidianVault/lmz/config.yaml',
      active: false,
      exists: true,
      managed: true,
      can_delete_all: true
    },
    {
      id: 'legacy-workspace',
      name: 'Legacy Workspace',
      config_path: 'C:/Legacy/lmz/config.yaml',
      active: false,
      exists: true,
      managed: false,
      can_delete_all: false
    }
  ];
  let vaultActive = 'default';
  let vaultItems = [
    {
      id: 'default',
      name: 'Default',
      root: 'C:/ObsidianVault/lmz/data/vaults/default',
      active: true,
      exists: true,
      item_count: items.length
    },
    {
      id: 'archive',
      name: 'Archive',
      root: 'C:/ObsidianVault/lmz/data/vaults/archive',
      active: false,
      exists: true,
      item_count: 2
    }
  ];
  const appSettings = makeAppSettings('masonry');
  appSettings.ui.inspector_width = 360;
  appSettings.ui.ram_tracking_enabled = true;

  await page.route('**/api/items**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const detailMatch = url.pathname.match(/\/api\/items\/([a-f0-9]{64})(?:\/.*)?$/);
    if (detailMatch) {
      const hash = detailMatch[1];
      const item = items.find((entry) => entry.hash === hash);
      if (!item) return fulfillJson(route, { detail: 'not found' }, 404);
      if (request.method() === 'PATCH') {
        const patch = JSON.parse(request.postData() || '{}');
        await options.onItemPatch?.(patch);
        items = items.map((entry) => {
          if (entry.hash !== hash) return entry;
          const next = { ...entry, ...patch };
          if ('wd_rating' in patch || 'wd_character_tags' in patch || 'wd_tags' in patch) {
            next.wd_tags = {
              rating: patch.wd_rating || '',
              characters: Array.isArray(patch.wd_character_tags) ? patch.wd_character_tags : [],
              general: Array.isArray(patch.wd_tags) ? patch.wd_tags : []
            };
          }
          return next;
        });
        const updated = items.find((entry) => entry.hash === hash);
        return fulfillJson(route, {
          ...updated,
          topic_counts: facetCountMap(items, 'topic'),
          wd_tag_counts: facetCountMap(items, 'wd_tag')
        });
      }
      return fulfillJson(route, {
        ...item,
        topic_counts: facetCountMap(items, 'topic'),
        wd_tag_counts: facetCountMap(items, 'wd_tag')
      });
    }

    await options.onItemsRequest?.(url);
    const limit = Math.max(1, Math.min(Number(url.searchParams.get('limit') || 50), 100000));
    return fulfillJson(route, {
      items: items.slice(0, limit),
      has_more: items.length > limit,
      next_cursor: items.length > limit ? String(limit) : null
    });
  });

  await installAppStateRoutes(page, appSettings, {
    putStatus: options.appSettingsPutStatus,
    includeEtag: options.appSettingsIncludeEtag
  });
  await page.route('**/api/workspaces**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (/\/api\/workspaces\/[^/]+\/load$/.test(url.pathname) && request.method() === 'POST') {
      if (options.workspaceLoadLegacy) {
        return fulfillJson(route, {
          detail: { code: 'unsupported_workspace_config', message: 'unknown fields: tagging, ui' }
        }, 422);
      }
      return fulfillJson(route, { status: 'success', active_workspace: 'default', active_vault: 'default' });
    }
    if (url.pathname === '/api/workspaces' && request.method() === 'GET') {
      return fulfillJson(route, { active: workspaceActive, items: workspaceItems });
    }
    if (url.pathname === '/api/workspaces/active' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      await options.onWorkspaceAction?.('active', payload);
      workspaceActive = String(payload.id || workspaceActive);
      workspaceItems = workspaceItems.map((item) => ({ ...item, active: item.id === workspaceActive }));
      return fulfillJson(route, { status: 'success', active: workspaceActive, restart_required: true, items: workspaceItems });
    }
    if (url.pathname === '/api/workspaces' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      await options.onWorkspaceAction?.('add', payload);
      workspaceItems = [
        ...workspaceItems,
        {
          id: 'obsidian-workspace',
          name: payload.name || 'LMZ Workspace',
          config_path: `${payload.path}/lmz/config.yaml`,
          active: false,
          exists: true,
          managed: true,
          can_delete_all: true
        }
      ];
      return fulfillJson(route, { status: 'success', active: workspaceActive, restart_required: false, items: workspaceItems });
    }
    const deleteMatch = url.pathname.match(/\/api\/workspaces\/([^/]+)$/);
    if (deleteMatch && request.method() === 'DELETE') {
      const id = decodeURIComponent(deleteMatch[1]);
      const mode = url.searchParams.get('mode') || 'unregister';
      await options.onWorkspaceAction?.('delete', { id, mode });
      workspaceItems = workspaceItems.filter((item) => item.id !== id);
      const cleanupStatus = options.workspaceDeleteCleanupStatus || (mode === 'unregister' ? 'not_requested' : 'complete');
      return fulfillJson(route, {
        status: 'success',
        active: workspaceActive,
        mode,
        cleanup_status: cleanupStatus,
        cleanup_path: cleanupStatus === 'pending' ? 'C:/ObsidianVault/.lmz-delete-pending' : '',
        items: workspaceItems
      });
    }
    return fulfillJson(route, { detail: 'not found' }, 404);
  });
  await page.route('**/api/vaults**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const utilityMatch = url.pathname.match(/\/api\/vaults\/([^/]+)\/(health|repair|backup|export)$/);
    if (url.pathname === '/api/vaults' && request.method() === 'GET') {
      return fulfillJson(route, { active: vaultActive, items: vaultItems });
    }
    const deleteMatch = url.pathname.match(/\/api\/vaults\/([^/]+)$/);
    if (deleteMatch && request.method() === 'DELETE') {
      const id = decodeURIComponent(deleteMatch[1]);
      await options.onVaultAction?.('delete', { id, confirm: url.searchParams.get('confirm') === 'true' });
      if (id === vaultActive) return fulfillJson(route, { detail: 'cannot delete active vault' }, 400);
      vaultItems = vaultItems.filter((vault) => vault.id !== id);
      return fulfillJson(route, { status: 'success', items: vaultItems });
    }
    if ((url.pathname === '/api/vaults/merge-preview' || url.pathname === '/api/vaults/merge') && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      const action = url.pathname.endsWith('merge-preview') ? 'merge-preview' : 'merge';
      await options.onVaultAction?.(action, payload);
      if (action === 'merge') {
        vaultItems = [
          ...vaultItems,
          {
            id: 'merged-vault',
            name: payload.name || 'Merged Vault',
            root: 'C:/ObsidianVault/lmz/data/vaults/merged-vault',
            active: false,
            exists: true,
            item_count: 4
          }
        ];
      }
      return fulfillJson(route, {
        status: action === 'merge' ? 'success' : 'preview',
        name: payload.name || 'Merged Vault',
        vault: 'merged-vault',
        source_vault_ids: payload.source_vault_ids || [],
        sources: [
          { id: 'default', items: 3, duplicates: 0, importable: 3 },
          { id: 'archive', items: 2, duplicates: 1, importable: 1 }
        ],
        total_items: 5,
        duplicates: 1,
        importable: 4,
        possible_similar: 0,
        similarity: 'unsupported',
        imported: action === 'merge' ? 4 : undefined,
        skipped: action === 'merge' ? 1 : undefined,
        items: vaultItems
      });
    }
    if (utilityMatch) {
      const action = utilityMatch[2] as 'health' | 'repair' | 'backup' | 'export';
      const payload = request.method() === 'POST' ? JSON.parse(request.postData() || '{}') : undefined;
      await options.onVaultAction?.(action, payload);
      if (action === 'health') {
        return fulfillJson(route, {
          status: 'success',
          vault: utilityMatch[1],
          issue_count: 3,
          missing_files: { asset: ['missing.jpg'], note: [], wd: [], thumb: [] },
          orphans: { assets: ['orphan.jpg'], notes: [], wd_cache: [], thumbnails: [] },
          facet_drift: ['topic']
        });
      }
      if (action === 'repair') {
        if (payload.actions?.includes('rebuild_thumbnails')) {
          return fulfillJson(route, { status: 'success', thumbnails: { checked: 3, generated: 2, skipped: 0, failed: 1 } });
        }
        return fulfillJson(route, { status: 'success', after: { issue_count: 0, missing_files: {}, orphans: {}, facet_drift: [] } });
      }
      const extension = action === 'backup' ? 'lmzbackup.zip' : 'lmzvault.zip';
      return fulfillJson(route, { status: 'success', package_path: `C:/ObsidianVault/lmz/${action}s/default.${extension}` });
    }
    if (url.pathname === '/api/vaults/import-preview' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      await options.onVaultAction?.('import-preview', payload);
      return fulfillJson(route, {
        status: 'preview',
        package_path: payload.package_path,
        package_fingerprint: 'fingerprint-imported',
        package_type: 'lmz_vault_export',
        package_version: 1,
        source_vault: { id: 'exported', name: 'Exported Vault' },
        contents: { db: true, assets: true, notes: true, review: false },
        counts: { items: 3, files: 7 },
        suggested_target_name: 'Exported Vault',
        suggested_target_id: 'exported-vault',
        target_name: payload.target_name || 'Exported Vault',
        target_id: (payload.target_name || 'Exported Vault').toLowerCase().replace(/\s+/g, '-'),
        target_exists: false,
        warnings: []
      });
    }
    if (url.pathname === '/api/vaults/restore-preview' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      await options.onVaultAction?.('restore-preview', payload);
      return fulfillJson(route, {
        status: 'preview',
        package_path: payload.package_path,
        package_fingerprint: 'fingerprint-restored',
        package_type: 'lmz_vault_backup',
        package_version: 1,
        created_at: '2026-06-14T00:00:00+00:00',
        source_vault: { id: 'default', name: 'Default' },
        contents: { db: true, assets: true, notes: true, review: true, logs: true },
        counts: { items: 3, files: 12 },
        file_count: 12,
        target_name: 'Restored Default',
        target_id: 'restored-default'
      });
    }
    if (url.pathname === '/api/vaults/import' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      await options.onVaultAction?.('import', payload);
      vaultItems = [...vaultItems, { id: 'imported', name: payload.target_name || 'Imported', root: 'C:/Imported', active: false, exists: true, item_count: 0 }];
      return fulfillJson(route, { status: 'success', vault: 'imported', items: vaultItems });
    }
    if (url.pathname === '/api/vaults/restore' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      await options.onVaultAction?.('restore', payload);
      vaultItems = [...vaultItems, { id: 'restored-default', name: 'Restored Default', root: 'C:/RestoredDefault', active: false, exists: true, item_count: 3 }];
      return fulfillJson(route, { status: 'success', vault: 'restored-default', name: 'Restored Default', items: vaultItems });
    }
    return fulfillJson(route, { detail: 'not found' }, 404);
  });
  await page.route('**/api/session-key', async (route) => fulfillJson(route, { key: 'mock-key' }));
  await page.route('**/api/stats', async (route) => fulfillJson(route, { total_items: items.length }));
  await page.route('**/api/artists**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const mergeMatch = url.pathname.match(/\/api\/artists\/(\d+)\/(merge-preview|merge)$/);
    const aliasMatch = url.pathname.match(/\/api\/artists\/(\d+)\/aliases(?:\/(\d+))?$/);
    const linkMatch = url.pathname.match(/\/api\/artists\/(\d+)\/links(?:\/(\d+))?$/);
    const detailMatch = url.pathname.match(/\/api\/artists\/(\d+)$/);

    if (mergeMatch) {
      if (mergeMatch[2] === 'merge' && failNextArtistMerge) {
        failNextArtistMerge = false;
        return fulfillJson(route, { detail: 'merge failed' }, 500);
      }
      const target = artistDetails.find((entry) => entry.id === Number(mergeMatch[1]));
      if (!target) return fulfillJson(route, { detail: 'not found' }, 404);
      const payload = JSON.parse(request.postData() || '{}');
      const sourceIds = Array.isArray(payload.source_artist_ids) ? payload.source_artist_ids.map(Number) : [];
      const sources = artistDetails.filter((entry) => sourceIds.includes(entry.id));
      const sourceNames = new Set(sources.map((entry) => entry.name));
      const affected = items.filter((item) => sourceNames.has(String(item.artist || ''))).length;
      const preview = {
        target: { id: target.id, name: target.name },
        sources: sources.map((entry) => ({ id: entry.id, name: entry.name })),
        affected_items: affected,
        aliases: {
          add: sources.map((entry) => ({ value: entry.name })),
          move: sources.flatMap((entry) => entry.aliases.map((alias) => ({ value: alias.alias }))),
          duplicates: [],
          conflicts: []
        },
        links: {
          move: sources.flatMap((entry) => entry.links.map((link) => ({ url: link.url }))),
          duplicates: []
        },
        notes_appended: sources.filter((entry) => entry.notes).length,
        source_artists_deleted: sources.length
      };
      if (mergeMatch[2] === 'merge') {
        items = items.map((item) => sourceNames.has(String(item.artist || '')) ? { ...item, artist: target.name } : item);
        target.aliases.push(...sources.map((entry) => ({
          id: nextArtistAliasId++,
          alias: entry.name,
          alias_norm: entry.name.toLowerCase()
        })));
        target.aliases.push(...sources.flatMap((entry) => entry.aliases));
        target.links.push(...sources.flatMap((entry) => entry.links));
        target.notes = [target.notes, ...sources.filter((entry) => entry.notes).map((entry) => `--- merged from ${entry.name} ---\n${entry.notes}`)]
          .filter(Boolean)
          .join('\n\n');
        artistDetails = artistDetails.filter((entry) => !sourceIds.includes(entry.id));
        target.item_count = items.filter((item) => item.artist === target.name).length;
        return fulfillJson(route, { ...preview, merged: true, target_detail: target });
      }
      return fulfillJson(route, preview);
    }

    if (aliasMatch) {
      const artist = artistDetails.find((entry) => entry.id === Number(aliasMatch[1]));
      if (!artist) return fulfillJson(route, { detail: 'not found' }, 404);
      if (request.method() === 'POST') {
        const payload = JSON.parse(request.postData() || '{}');
        const alias = { id: nextArtistAliasId++, alias: String(payload.alias || ''), alias_norm: String(payload.alias || '').toLowerCase() };
        artist.aliases.push(alias);
        return fulfillJson(route, alias);
      }
      if (request.method() === 'DELETE') {
        artist.aliases = artist.aliases.filter((alias) => alias.id !== Number(aliasMatch[2]));
        return fulfillJson(route, { status: 'success' });
      }
    }

    if (linkMatch) {
      const artist = artistDetails.find((entry) => entry.id === Number(linkMatch[1]));
      if (!artist) return fulfillJson(route, { detail: 'not found' }, 404);
      if (request.method() === 'POST') {
        const payload = JSON.parse(request.postData() || '{}');
        const urlValue = String(payload.url || '');
        const link = {
          id: nextArtistLinkId++,
          platform: String(payload.platform || ''),
          url: urlValue,
          handle: String(payload.handle || '') || urlValue.split('/').filter(Boolean).pop() || '',
          is_primary: false
        };
        artist.links.push(link);
        return fulfillJson(route, link);
      }
      if (request.method() === 'DELETE') {
        artist.links = artist.links.filter((link) => link.id !== Number(linkMatch[2]));
        return fulfillJson(route, { status: 'success' });
      }
    }

    if (detailMatch) {
      const artist = artistDetails.find((entry) => entry.id === Number(detailMatch[1]));
      if (!artist) return fulfillJson(route, { detail: 'not found' }, 404);
      if (request.method() === 'PATCH') {
        const payload = JSON.parse(request.postData() || '{}');
        artist.name = String(payload.name || artist.name);
        artist.name_norm = artist.name.toLowerCase();
        artist.kind = String(payload.kind || artist.kind);
        artist.notes = String(payload.notes ?? artist.notes);
        return fulfillJson(route, artist);
      }
      return fulfillJson(route, artist);
    }

    const q = (url.searchParams.get('q') || '').toLowerCase();
    const list = artistDetails
      .filter((artist) => !q || artist.name.toLowerCase().includes(q))
      .map((artist) => ({
        id: artist.id,
        name: artist.name,
        kind: artist.kind,
        item_count: artist.item_count,
        link_count: artist.links.length,
        alias_count: artist.aliases.length
      }));
    return fulfillJson(route, { items: list });
  });
  await page.route('**/api/platforms**', async (route) => {
    const url = new URL(route.request().url());
    const q = (url.searchParams.get('q') || '').toLowerCase();
    const list = platformDetails.filter((platform) => !q || platform.display_name.toLowerCase().includes(q));
    return fulfillJson(route, { items: list });
  });
  await page.route('**/api/logs**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/logs/ui') {
      return fulfillJson(route, { status: 'ok' });
    }
    if (url.pathname === '/api/logs/location') {
      const source = url.searchParams.get('source') || 'active';
      const mode = source === 'console' ? 'console' : source === 'startup' ? 'startup' : 'vault';
      return fulfillJson(route, {
        mode,
        label: mode === 'console' ? 'Console' : mode === 'vault' ? 'Vault logs: Default' : 'Startup logs',
        active_mode: 'vault',
        vault_available: true,
        available_sources: ['startup', 'vault', 'console']
      });
    }
    if (url.pathname === '/api/logs/clear') {
      if (options.logClearFails) {
        return fulfillJson(route, { detail: 'clear failed' }, 500);
      }
      return fulfillJson(route, { status: 'success' });
    }
    if (url.pathname === '/api/logs/open') {
      return fulfillJson(route, { status: 'success' });
    }
    if (url.pathname === '/api/logs') {
      await options.onLogStream?.(url);
      const entries = options.logEntriesForStream?.(url) || defaultMockLogs();
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: logSseBody(entries) });
      return;
    }
    return fulfillJson(route, { detail: 'not found' }, 404);
  });
  await page.route('**/api/queue/actions/retry-failed', async (route) => fulfillJson(route, {
    status: 'success',
    moved: 0,
    counts: {
      normal: manifest.queues.normal.length,
      force: manifest.queues.force.length,
      failed: manifest.queues.failed.length
    }
  }));
  await page.route('**/api/queue/actions/clear-failed', async (route) => fulfillJson(route, {
    status: 'success',
    counts: {
      normal: manifest.queues.normal.length,
      force: manifest.queues.force.length,
      failed: 0
    }
  }));
  await page.route('**/api/queue/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const match = url.pathname.match(/\/api\/queue\/([^/]+)/);
    if (!match) return fulfillJson(route, { detail: 'not found' }, 404);
    const queueName = match[1];
    if (!(queueName in queueContent)) return fulfillJson(route, { detail: 'invalid queue' }, 400);
    if (request.method() === 'GET') {
      const content = queueContent[queueName];
      const count = content.split('\n').filter((line) => line.trim().startsWith('http')).length;
      return fulfillJson(route, { content, count });
    }
    if (request.method() === 'POST') {
      const payload = JSON.parse(request.postData() || '{}');
      queueContent[queueName] = String(payload.content || '');
      const count = queueContent[queueName].split('\n').filter((line) => line.trim().startsWith('http')).length;
      return fulfillJson(route, { status: 'success', count });
    }
    return fulfillJson(route, { status: 'ok' });
  });
  await page.route('**/api/queue-stats', async (route) => fulfillJson(route, {
    normal: manifest.queues.normal.length,
    force: manifest.queues.force.length,
    failed: manifest.queues.failed.length
  }));
  await page.route('**/api/review/count', async (route) => fulfillJson(route, { count: reviewItems.length, pending: 1, cleanup: 1 }));
  await page.route('**/api/review/cleanup', async (route) => {
    await options.onMaintenanceAction?.('review');
    return fulfillJson(route, { status: 'success', cleaned: 0, failed: 0, cleaned_orphans: 0, failed_orphans: 0 });
  });
  await page.route('**/api/metadata-index/rebuild', async (route) => {
    await options.onMaintenanceAction?.('metadata');
    return fulfillJson(route, options.metadataRebuildResponse || { status: 'started' });
  });
  await page.route('**/api/metadata-index/status', async (route) => {
    const sequence = options.metadataStatusSequence || [];
    const body = sequence[Math.min(metadataStatusIndex, Math.max(0, sequence.length - 1))] || {
      ready: true,
      repair_running: false,
      items: items.length,
      indexed: items.length,
      errors: 0,
      dirty: 0,
      maintenance_rebuild: { running: false, status: 'idle' }
    };
    metadataStatusIndex += 1;
    return fulfillJson(route, body);
  });
  await page.route('**/api/review/*/action**', async (route) => {
    await options.onReviewAction?.();
    return fulfillJson(route, { status: 'success', action: 'keep', message: 'mock action ok' });
  });
  await page.route('**/api/review', async (route) => fulfillJson(route, reviewItems));
  await page.route('**/api/topics/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const payload = JSON.parse(request.postData() || '{}');
    if (url.pathname === '/api/topics/rename' && request.method() === 'POST') {
      const oldLabel = String(payload.old_label || '');
      const newLabel = String(payload.new_label || '');
      let notes = 0;
      items = items.map((item) => {
        const topics = Array.isArray(item.topics) ? item.topics as string[] : [];
        if (!topics.includes(oldLabel)) return item;
        notes += 1;
        return { ...item, topics: topics.map((topic) => topic === oldLabel ? newLabel : topic) };
      });
      return fulfillJson(route, { status: 'success', old_label: oldLabel, new_label: newLabel, vaults_touched: ['default'], notes_rewritten: notes, legacy_plain_refs_rewritten: 0, errors: [] });
    }
    if (url.pathname === '/api/topics/delete' && request.method() === 'POST') {
      const label = String(payload.label || '');
      let notes = 0;
      items = items.map((item) => {
        const topics = Array.isArray(item.topics) ? item.topics as string[] : [];
        if (!topics.includes(label)) return item;
        notes += 1;
        return { ...item, topics: topics.filter((topic) => topic !== label) };
      });
      return fulfillJson(route, { status: 'success', label, vaults_touched: ['default'], notes_rewritten: notes, errors: [] });
    }
    if (url.pathname === '/api/topics/merge' && request.method() === 'POST') {
      const sourceLabel = String(payload.source_label || '');
      const targetLabel = String(payload.target_label || '');
      let notes = 0;
      items = items.map((item) => {
        const topics = Array.isArray(item.topics) ? item.topics as string[] : [];
        if (!topics.includes(sourceLabel)) return item;
        notes += 1;
        return { ...item, topics: [...new Set(topics.map((topic) => topic === sourceLabel ? targetLabel : topic))] };
      });
      return fulfillJson(route, { status: 'success', source_label: sourceLabel, target_label: targetLabel, vaults_touched: ['default'], notes_rewritten: notes, errors: [] });
    }
    return fulfillJson(route, { detail: 'not found' }, 404);
  });
  await page.route('**/api/wd-tags/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const payload = JSON.parse(request.postData() || '{}');
    const matchType = String(payload.tag_type || '');
    const rewriteWd = (oldTag: string, newTag: string | null) => {
      let notes = 0;
      items = items.map((item) => {
        const wd = item.wd_tags as { rating?: string; characters?: string[]; general?: string[] } | undefined;
        if (!wd) return item;
        let changed = false;
        const nextWd = { ...wd };
        if ((!matchType || matchType === 'rating') && wd.rating === oldTag) {
          nextWd.rating = newTag || '';
          changed = true;
        }
        if (!matchType || matchType === 'character') {
          const characters = Array.isArray(wd.characters) ? wd.characters : [];
          if (characters.includes(oldTag)) {
            nextWd.characters = newTag ? [...new Set(characters.map((tag) => tag === oldTag ? newTag : tag))] : characters.filter((tag) => tag !== oldTag);
            changed = true;
          }
        }
        if (!matchType || matchType === 'general') {
          const general = Array.isArray(wd.general) ? wd.general : [];
          if (general.includes(oldTag)) {
            nextWd.general = newTag ? [...new Set(general.map((tag) => tag === oldTag ? newTag : tag))] : general.filter((tag) => tag !== oldTag);
            changed = true;
          }
        }
        if (!changed) return item;
        notes += 1;
        return { ...item, wd_tags: nextWd };
      });
      return notes;
    };
    if (url.pathname === '/api/wd-tags/rename' && request.method() === 'POST') {
      const oldTag = String(payload.old_tag || '');
      const newTag = String(payload.new_tag || '');
      const notes = rewriteWd(oldTag, newTag);
      return fulfillJson(route, { status: 'success', old_tag: oldTag, new_tag: newTag, tag_type: payload.tag_type || null, vaults_touched: ['default'], notes_rewritten: notes, errors: [] });
    }
    if (url.pathname === '/api/wd-tags/delete' && request.method() === 'POST') {
      const tag = String(payload.tag || '');
      const notes = rewriteWd(tag, null);
      return fulfillJson(route, { status: 'success', tag, tag_type: payload.tag_type || null, vaults_touched: ['default'], notes_rewritten: notes, errors: [] });
    }
    return fulfillJson(route, { detail: 'not found' }, 404);
  });
  await page.route('**/api/facets**', async (route) => {
    const kind = new URL(route.request().url()).searchParams.get('kind') || 'artist';
    const key = kind === 'wd_tag' ? 'wd_tag' : kind;
    const values = facetItems(items, key);
    if (kind === 'artist') values.push({ value: 'Unknown', count: 3 });
    if (kind === 'platform') {
      const pixiv = values.find((item) => item.value === 'pixiv');
      if (pixiv) pixiv.count = 3;
    }
    return fulfillJson(route, { kind, items: values });
  });
  await page.route('**/api/system/memory', async (route) => {
    if (options.memoryFails) return fulfillJson(route, { detail: 'unavailable' }, 500);
    return fulfillJson(route, { backend_mb: 42.5 });
  });
  await page.route('**/api/auth/scan', async (route) => {
    await options.onMaintenanceAction?.('auth');
    return fulfillJson(route, {
      status: 'ok',
      auth: {
        cookies: 'available',
        platforms: {
          X: { cookies: 'available', cookie_source: 'platform', cookies_path: 'C:/LMZ/secrets/auth/x/cookies.txt', token: 'not_required' },
          Instagram: { cookies: 'missing', cookie_source: 'missing', cookies_path: '', token: 'not_required' },
          Pinterest: { cookies: 'available', cookie_source: 'platform', cookies_path: 'C:/LMZ/secrets/auth/pinterest/cookies.txt', token: 'not_required' },
          Pixiv: { cookies: 'available', cookie_source: 'platform', cookies_path: 'C:/LMZ/secrets/auth/pixiv/cookies.txt', token: 'available', token_source: 'file' },
          YouTube: { cookies: 'missing', cookie_source: 'missing', cookies_path: '', token: 'not_required' }
        }
      }
    });
  });
  await page.route('**/api/local-ingest/status', async (route) => fulfillJson(route, localStatus));
  await page.route('**/api/local-ingest/start', async (route) => {
    const payload = JSON.parse(route.request().postData() || '{}');
    await options.onLocalIngestStart?.(payload);
    return fulfillJson(route, { status: 'success', run_id: 'mock-run', phase: 'scanning' });
  });
  await page.route('**/api/local-ingest/retry-failed', async (route) => fulfillJson(route, { status: 'success', queued: 0, phase: 'idle' }));
  await page.route('**/api/local-ingest/drop-intake', async (route) => fulfillJson(route, { detail: 'unused in browser mock' }, 404));
  await page.route('**/mock-vault/assets/**', async (route) => {
    const assetName = new URL(route.request().url()).pathname.split('/').pop() || 'mock.svg';
    await route.fulfill({
      status: 200,
      contentType: 'image/svg+xml',
      body: `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240"><rect width="320" height="240" fill="#1f2937"/><text x="24" y="124" fill="#fff">${assetName.slice(0, 6)}</text></svg>`
    });
  });
  await page.route('**/mock-vault/video/**', async (route) => {
    await route.fulfill({ status: 204, body: '' });
  });
  await page.route('**/review-assets/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180"><rect width="240" height="180" fill="#334155"/><text x="24" y="96" fill="#fff">Review</text></svg>'
    });
  });
  await page.exposeFunction('failNextArtistMerge', () => {
    failNextArtistMerge = true;
  });
}

async function openMockVault(
  page: Page,
  options: {
    memoryFails?: boolean;
    onReviewAction?: () => Promise<void>;
    onLocalIngestStart?: (payload?: any) => Promise<void> | void;
    onMaintenanceAction?: (action: 'auth' | 'metadata' | 'review') => Promise<void> | void;
    onItemsRequest?: (url: URL) => Promise<void> | void;
    onItemPatch?: (payload: any) => Promise<void> | void;
    onWorkspaceAction?: (action: 'add' | 'active', payload?: any) => Promise<void> | void;
    onVaultAction?: (action: 'merge-preview' | 'merge' | 'delete' | 'health' | 'repair' | 'backup' | 'export' | 'import-preview' | 'import' | 'restore-preview' | 'restore', payload?: any) => Promise<void> | void;
    logClearFails?: boolean;
    onLogStream?: (url: URL) => Promise<void> | void;
    logEntriesForStream?: (url: URL) => MockLogEntry[];
    appSettingsPutStatus?: number;
    appSettingsIncludeEtag?: boolean;
    workspaceLoadLegacy?: boolean;
  } = {}
) {
  await installMockVaultApi(page, options);
  await page.goto('/?lmz_test_page_size=100');
  await expect(page.getByTestId('vault-tile').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('.bottom-status')).toContainText('Total Items: 3');
}

test('mock vault fixture is isolated and renders grouped media paths', async ({ page }) => {
  await openMockVault(page);

  await expect(page.locator('.bottom-status')).toContainText('Showing 2 groups');
  await expect(page.getByText('Mock Solo')).toBeVisible();
  await expect(page.getByText('Mock Group B')).toBeVisible();
  await page.getByText('Mock Group B').click();
  await page.locator('aside.inspector .group-nav button').last().click();
  await expect(page.locator('aside.inspector video')).toHaveAttribute('src', /mock-vault\/video/);
});

test('gallery replaces a pending thumbnail without loading the original', async ({ page }) => {
  await installMockVaultApi(page, { testJxlThumbnail: true });
  const item = manifest.items.find((entry) => entry.artist === 'Mock Solo')!;
  let requests = 0;
  let originalRequests = 0;
  await page.route(`**${item.url}`, async (route) => {
    originalRequests += 1;
    await route.fallback();
  });
  await page.route(`**/api/thumbnails/${item.hash}`, async (route) => {
    requests += 1;
    if (requests < 3) {
      return route.fulfill({ status: 503, headers: { 'Retry-After': '0', 'Cache-Control': 'no-store' } });
    }
    return route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2"/></svg>'
    });
  });
  await page.goto('/?lmz_test_page_size=100');
  const tile = page.getByTestId('vault-tile').filter({ hasText: 'Mock Solo' });
  await expect(tile.getByRole('img')).toHaveAttribute('src', /^blob:/);
  await expect.poll(() => tile.getByRole('img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(2);
  expect(requests).toBe(3);
  expect(originalRequests).toBe(0);
});

test('artist edit refreshes tile while source URL and platform stay read-only', async ({ page }) => {
  await openMockVault(page);

  await page.getByText('Mock Solo').click();
  await expect(page.getByLabel('Artist')).toHaveValue('Mock Solo');
  await expect(page.getByLabel('Source URL')).toHaveAttribute('readonly', '');
  await expect(page.locator('aside.inspector')).toContainText('Platform');
  await expect(page.locator('aside.inspector input#inspector-platform')).toHaveCount(0);
  await page.getByLabel('Artist').fill(String(manifest.expectations.editedArtist));
  await page.getByRole('button', { name: 'Save' }).click();

  await expect(page.getByText(String(manifest.expectations.editedArtist))).toBeVisible();
  await expect(page.getByLabel('Artist')).toHaveValue(String(manifest.expectations.editedArtist));
  await expect(page.locator('.bottom-status')).toContainText(`Showing ${manifest.expectations.initialGroups} groups`);
});

test('inspector topic and WD chips show global counts', async ({ page }) => {
  await openMockVault(page);

  await page.getByText('Mock Solo').click();
  const inspector = page.locator('aside.inspector');
  await expect(inspector.locator('.tag-chip.topic').filter({ hasText: 'mock-topic' }).locator('.tag-count')).toHaveText('1');
  await expect(inspector.locator('.tag-chip.rating').filter({ hasText: 'safe' }).locator('.tag-count')).toHaveText('3');
  await expect(inspector.locator('.tag-chip.character').filter({ hasText: 'mock_character' }).locator('.tag-count')).toHaveText('1');
  await expect(inspector.locator('.tag-chip.visual').filter({ hasText: 'mock_tag' }).locator('.tag-count')).toHaveText('1');
});

test('clearing search cancels pending autocomplete request', async ({ page }) => {
  const suggestionRequests: string[] = [];
  await page.route('**/api/search/suggestions**', async (route) => {
    suggestionRequests.push(route.request().url());
    return fulfillJson(route, { suggestions: [] });
  });
  await openMockVault(page);

  await page.getByTestId('vault-search-input').fill('a:mo');
  await page.waitForTimeout(50);
  await page.getByTitle('Clear Search').click();
  await page.waitForTimeout(250);

  expect(suggestionRequests).toHaveLength(0);
});

test('inspector drafts WD promotion and removal until save', async ({ page }) => {
  const patches: any[] = [];
  await openMockVault(page, { onItemPatch: (payload) => patches.push(payload) });

  await page.getByText('Mock Solo').click();
  const inspector = page.locator('aside.inspector');
  const visualTag = inspector.locator('.tag-chip.visual').filter({ hasText: 'mock_tag' });

  await visualTag.dispatchEvent('click');
  await expect(inspector.locator('.tag-chip.topic').filter({ hasText: 'mock_tag' })).toBeVisible();
  await expect(inspector.getByRole('button', { name: 'Revert' })).toBeVisible();
  await expect(inspector.getByRole('button', { name: 'Save' })).toBeEnabled();
  expect(patches).toHaveLength(0);

  await visualTag.dispatchEvent('click');
  await expect(inspector.locator('.tag-chip.topic').filter({ hasText: 'mock_tag' })).toHaveCount(0);
  await expect(inspector.getByRole('button', { name: 'Save' })).toBeDisabled();

  await visualTag.dispatchEvent('click');
  await expect(inspector.locator('.tag-chip.topic').filter({ hasText: 'mock_tag' })).toBeVisible();
  await expect(inspector.getByRole('button', { name: 'Save' })).toBeEnabled();

  await visualTag.getByTitle('Remove WD tag').dispatchEvent('click');
  await expect(inspector.locator('.tag-chip.visual').filter({ hasText: 'mock_tag' })).toHaveCount(0);

  await inspector.getByRole('button', { name: 'Save' }).click();
  await expect.poll(() => patches.length).toBe(1);
  expect(patches[0]).toMatchObject({
    topics: ['mock-topic', 'mock_tag'],
    wd_rating: 'safe',
    wd_character_tags: ['mock_character'],
    wd_tags: []
  });
  await expect(inspector.getByRole('button', { name: 'Revert' })).toHaveCount(0);
});

test('inspector revert restores draft topic and WD tag edits', async ({ page }) => {
  const patches: any[] = [];
  await openMockVault(page, { onItemPatch: (payload) => patches.push(payload) });

  await page.getByText('Mock Solo').click();
  const inspector = page.locator('aside.inspector');
  const topic = inspector.locator('.tag-chip.topic').filter({ hasText: 'mock-topic' });
  await topic.hover();
  await topic.getByTitle('Remove topic').click();
  await expect(inspector.locator('.tag-chip.topic').filter({ hasText: 'mock-topic' })).toHaveCount(0);

  await inspector.getByRole('button', { name: 'Revert' }).click();
  await expect(inspector.locator('.tag-chip.topic').filter({ hasText: 'mock-topic' })).toBeVisible();
  await expect(inspector.getByRole('button', { name: 'Revert' })).toHaveCount(0);
  expect(patches).toHaveLength(0);
});

test('masonry keeps current data after metadata update cache reuse', async ({ page }) => {
  await openMockVault(page);

  await page.getByText('Mock Solo').click();
  await page.getByLabel('Artist').fill('Cache Fresh Artist');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.getByTestId('virtual-scroller').evaluate((node) => {
    const el = node as HTMLElement;
    el.scrollTop = 200;
    el.dispatchEvent(new Event('scroll'));
    el.scrollTop = 0;
    el.dispatchEvent(new Event('scroll'));
  });

  await expect(page.getByText('Cache Fresh Artist')).toBeVisible();
  await expect(page.getByText('Mock Solo')).toHaveCount(0);
});

test('fullscreen pan suppresses one backdrop click then closes after reset', async ({ page }) => {
  await openMockVault(page);

  await page.getByText('Mock Solo').click();
  await page.getByTitle('Fullscreen').click({ force: true });
  await expect(page.locator('.focus-overlay.fullscreen')).toBeVisible();

  const frame = page.locator('.media-frame');
  await frame.dispatchEvent('wheel', { deltaY: -180, ctrlKey: true, clientX: 400, clientY: 300 });
  const box = await frame.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + 40, box!.y + box!.height / 2 + 20);
  await page.mouse.up();
  await page.locator('.focus-overlay.fullscreen').dispatchEvent('click');
  await expect(page.locator('.focus-overlay.fullscreen')).toBeVisible();

  for (let i = 0; i < 10; i += 1) {
    await frame.dispatchEvent('wheel', { deltaY: 240, ctrlKey: true, clientX: 400, clientY: 300 });
  }
  await page.waitForTimeout(150);
  await page.locator('.focus-overlay.fullscreen').dispatchEvent('click');
  await expect(page.locator('.focus-overlay.fullscreen')).toHaveCount(0);
});

test('fullscreen uses browser fallback when native fullscreen is unavailable', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, '__TAURI_INTERNALS__', {
      configurable: true,
      get: () => undefined,
      set: () => {}
    });
    let fullscreenElement: Element | null = null;
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      get: () => fullscreenElement
    });
    Object.defineProperty(Element.prototype, 'requestFullscreen', {
      configurable: true,
      value: async () => {
        (window as any).__browserFsRequests = ((window as any).__browserFsRequests || 0) + 1;
        fullscreenElement = document.documentElement;
      }
    });
    Object.defineProperty(Document.prototype, 'exitFullscreen', {
      configurable: true,
      value: async () => {
        (window as any).__browserFsExits = ((window as any).__browserFsExits || 0) + 1;
        fullscreenElement = null;
      }
    });
  });
  await openMockVault(page);

  await page.getByText('Mock Solo').click();
  await page.getByTitle('Fullscreen').click({ force: true });
  await expect(page.locator('.focus-overlay.fullscreen')).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).__browserFsRequests || 0)).toBe(1);
  await page.keyboard.press('Escape');
  await expect(page.locator('.focus-overlay.fullscreen')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => (window as any).__browserFsExits || 0)).toBeGreaterThanOrEqual(1);
});

test('review fixture uses display names and encoded asset/action paths', async ({ page }) => {
  const reviewRequests: string[] = [];
  await openMockVault(page);
  page.on('request', (request) => {
    if (request.url().includes('/api/review/') || request.url().includes('/review-assets/')) {
      reviewRequests.push(request.url());
    }
  });

  await page.getByRole('button', { name: /Review/ }).click();
  await expect(page.getByRole('button', { name: 'Original Review Name.jpg pending' })).toBeVisible();
  await expect(page.getByRole('button', { name: /Locked Cleanup Item\.jpg/ })).toBeVisible();
  await page.getByRole('button', { name: 'Original Review Name\.jpg' }).click();
  await page.getByRole('button', { name: 'Save as Variant' }).click();

  expect(reviewRequests.some((url) => url.includes('review%2520encoded%2Bname.jpg'))).toBeTruthy();
});

test('review destructive actions unmount media before posting', async ({ page }) => {
  let mediaUnmountedBeforePost = false;
  await openMockVault(page, {
    onReviewAction: async () => {
      mediaUnmountedBeforePost = await page.locator('.review-main .pane img, .review-main .pane video').count() === 0;
    }
  });

  await page.getByRole('button', { name: /Review/ }).click();
  await page.getByRole('button', { name: /Original Review Name\.jpg/ }).click();
  await expect(page.locator('.review-main .pane img')).toHaveCount(2);
  await page.getByRole('button', { name: 'Save as Variant' }).click();

  await expect.poll(() => mediaUnmountedBeforePost).toBeTruthy();
});

test('drop request switches to local mode and appends deduped staged paths', async ({ page }) => {
  let localStartCalls = 0;
  await openMockVault(page, {
    onLocalIngestStart: () => {
      localStartCalls += 1;
    }
  });

  await page.getByRole('button', { name: /Ingestion/ }).click();
  await expect(page.getByRole('button', { name: 'Start Ingestion' })).toBeVisible();

  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('lmz:test-drop-request', {
      detail: {
        id: 'drop-1',
        session_id: 'session-1',
        source_tab: 'vault',
        accepted_paths: ['C:/drop/a.jpg', 'C:/drop/a.jpg', 'C:/drop/folder'],
        skipped: [{ path: 'C:/drop/x.txt', reason: 'unsupported_extension' }],
        summary: { received: 4, accepted: 3, skipped: 1 }
      }
    }));
  });

  await expect(page.getByRole('button', { name: 'Start Local Ingestion' })).toBeVisible();
  await expect(page.locator('.local-item')).toHaveCount(2);
  await expect(page.locator('.local-item')).toContainText(['C:/drop/a.jpg', 'C:/drop/folder']);
  expect(localStartCalls).toBe(0);
});

test('drop request does not auto-start local ingestion', async ({ page }) => {
  let localStartCalls = 0;
  await openMockVault(page, {
    onLocalIngestStart: () => {
      localStartCalls += 1;
    }
  });

  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('lmz:test-drop-request', {
      detail: {
        id: 'drop-2',
        session_id: 'session-2',
        source_tab: 'vault',
        accepted_paths: ['C:/drop/b.jpg'],
        skipped: [],
        summary: { received: 1, accepted: 1, skipped: 0 }
      }
    }));
  });

  await expect(page.getByRole('button', { name: 'Start Local Ingestion' })).toBeVisible();
  expect(localStartCalls).toBe(0);
});

test('online ingestion monitor preserves error and unknown log levels', async ({ page }) => {
  await openMockVault(page, {
    logEntriesForStream: (url) => {
      if (url.searchParams.get('filename') === 'ingest_online.jsonl') {
        return [
          {
            timestamp: '2026-06-16 11:41:45',
            level: 'ERROR',
            message: 'Download permanently failed',
            platform: 'pinterest',
            error: 'HTTP 403'
          },
          {
            timestamp: '2026-06-16 11:41:46',
            level: 'CRITICAL',
            message: 'Worker crashed',
            platform: 'pixiv',
            run_id: 'run-monitor'
          },
          {
            timestamp: '2026-06-16 11:41:47',
            level: 'NOTICE',
            message: 'Custom ingest level',
            platform: 'x'
          },
          'raw ingest monitor line'
        ];
      }
      return defaultMockLogs();
    }
  });

  await page.getByRole('button', { name: /Ingestion/ }).click();
  const monitor = page.locator('.monitor-logs');

  await expect(monitor.locator('.level.error')).toContainText('ERROR');
  await expect(monitor.locator('.level.critical')).toContainText('CRITICAL');
  await expect(monitor.locator('.level.other')).toHaveCount(2);
  await expect(monitor.locator('.platform-tag.pinterest')).toContainText('[PINTEREST]');
  await expect(monitor.locator('.platform-tag.pixiv')).toContainText('[PIXIV]');
  await expect(monitor).toContainText('original_level=NOTICE');
  await expect(monitor).toContainText('raw ingest monitor line');
  await expect(monitor).toContainText('HTTP 403');
});

test('local ingest identity controls use artist suggestions and platform dropdown', async ({ page }) => {
  let startPayload: any = null;
  await openMockVault(page, {
    onLocalIngestStart: (payload) => {
      startPayload = payload;
    }
  });

  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('lmz:test-drop-request', {
      detail: {
        id: 'drop-identity',
        session_id: 'session-identity',
        source_tab: 'vault',
        accepted_paths: ['C:/drop/identity.jpg'],
        skipped: [],
        summary: { received: 1, accepted: 1, skipped: 0 }
      }
    }));
  });

  const defaults = page.locator('.local-defaults');
  await defaults.getByLabel('Artist').fill('Mock Solo');
  await defaults.getByLabel('Platform').selectOption('Pixiv');
  await page.getByRole('button', { name: 'Start Local Ingestion' }).click();

  await expect.poll(() => startPayload).not.toBeNull();
  expect(startPayload?.defaults).toMatchObject({ artist: 'Mock Solo', platform: 'Pixiv' });
});

test('ram footer handles available and unavailable states', async ({ page }) => {
  await openMockVault(page);
  await expect(page.locator('.ram-status')).toContainText('RAM: backend 42.5 MB');

  const failingPage = await page.context().newPage();
  await openMockVault(failingPage, { memoryFails: true });
  await expect(failingPage.locator('.ram-status')).toContainText('RAM: unavailable');
  await failingPage.close();
});

test('app logs parse JSONL, raw fallback, levels, and displayed-field search', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /App Logs/ }).click();

  await expect(page.locator('.log-output')).toContainText('Disk almost full');
  await expect(page.locator('.log-output')).toContainText('CRITICAL');
  await expect(page.locator('.log-output')).toContainText('Custom level visible');
  await expect(page.locator('.log-output')).toContainText('OTHER');
  await expect(page.locator('.log-output')).toContainText('original_level=NOTICE');
  await expect(page.locator('.log-output')).toContainText('raw console line');

  await page.getByPlaceholder('Filter loaded logs...').fill('trace-42');
  await expect(page.locator('.log-output')).toContainText('Custom level visible');
  await expect(page.locator('.log-output')).not.toContainText('Disk almost full');

  await page.getByPlaceholder('Filter loaded logs...').fill('Pixiv');
  await expect(page.locator('.log-output')).toContainText('Disk almost full');

  await page.getByPlaceholder('Filter loaded logs...').fill('2026-06-16 10:00:00');
  await expect(page.locator('.log-output')).toContainText('Disk almost full');
});

test('app logs clear uses confirmation and preserves rows when backend fails', async ({ page }) => {
  page.on('dialog', async (dialog) => dialog.accept());
  await openMockVault(page, { logClearFails: true });
  await page.getByRole('button', { name: /App Logs/ }).click();

  await expect(page.locator('.log-output')).toContainText('Disk almost full');
  await page.getByRole('button', { name: 'Clear' }).click();
  await expect(page.getByRole('dialog', { name: /Clear vault logs/i })).toBeVisible();
  await expect(page.getByRole('dialog')).toContainText('vault logs');
  await page.getByRole('button', { name: 'Clear logs' }).click();
  await expect(page.locator('.log-output')).toContainText('Disk almost full');
});

test('app logs source-specific files and console source use console output', async ({ page }) => {
  const streamRequests: string[] = [];
  await openMockVault(page, {
    onLogStream: (url) => {
      streamRequests.push(`${url.searchParams.get('source')}:${url.searchParams.get('filename')}`);
    },
    logEntriesForStream: (url) => {
      if (url.searchParams.get('source') === 'console') {
        return ['console output line'];
      }
      return defaultMockLogs();
    }
  });

  await page.getByRole('button', { name: /App Logs/ }).click();
  await expect(page.getByLabel('File')).toContainText('Local ingest');

  await page.getByLabel('Source').selectOption('startup');
  await expect(page.getByLabel('File')).not.toContainText('Local ingest');
  await expect(page.getByLabel('File')).not.toContainText('Console output');

  await page.getByLabel('Source').selectOption('console');
  await expect(page.getByLabel('File')).toBeDisabled();
  await expect(page.getByLabel('File')).toContainText('Console output');
  await expect(page.locator('.log-output')).toContainText('console output line');
  expect(streamRequests).toContain('console:console.log');

  await page.getByRole('button', { name: 'Clear' }).click();
  await expect(page.getByRole('dialog', { name: /Clear console output/i })).toBeVisible();
});

test('app logs switching source and file keeps only latest stream rows', async ({ page }) => {
  let delayedInitial = false;
  await openMockVault(page, {
    onLogStream: async (url) => {
      if (!delayedInitial && url.searchParams.get('filename') === 'system.jsonl') {
        delayedInitial = true;
        await new Promise((resolve) => setTimeout(resolve, 350));
      }
    },
    logEntriesForStream: (url) => {
      const filename = url.searchParams.get('filename');
      const source = url.searchParams.get('source');
      if (filename === 'svelte.jsonl') {
        return [{ timestamp: '2026-06-16 10:10:00', level: 'INFO', module: 'svelte', message: `Frontend stream ${source}`, platform: '' }];
      }
      return [{ timestamp: '2026-06-16 10:09:00', level: 'INFO', module: 'system', message: `Backend stream ${source}`, platform: '' }];
    }
  });

  await page.getByRole('button', { name: /App Logs/ }).click();
  await page.getByLabel('File').selectOption('svelte.jsonl');
  await expect(page.locator('.log-output')).toContainText('Frontend stream vault');
  await page.waitForTimeout(500);
  await expect(page.locator('.log-output')).not.toContainText('Backend stream');

  await page.getByLabel('Source').selectOption('startup');
  await expect(page.locator('.log-output')).toContainText('Frontend stream startup');
  await expect(page.locator('.log-output')).not.toContainText('Frontend stream vault');
});

test('settings maintenance actions call existing endpoints and show compact statuses', async ({ page }) => {
  const calls: Array<'auth' | 'metadata' | 'review'> = [];
  await openMockVault(page, {
    onMaintenanceAction: (action) => {
      calls.push(action);
    }
  });

  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Maintenance' }).click();
  await page.getByRole('button', { name: 'Scan' }).click();
  await page.getByRole('button', { name: 'Rebuild' }).click();
  await page.getByRole('button', { name: 'Clean' }).click();

  await expect(page.locator('.settings-action-row-status').nth(0)).toContainText('X: available');
  await expect(page.locator('.settings-action-row-status').nth(0)).toContainText('Pixiv: OAuth');
  await expect(page.locator('.settings-action-row-status').nth(0)).toContainText('YouTube: missing');
  await expect(page.locator('.settings-action-row-status')).toContainText(['X: available, Instagram: missing, Pinterest: available, Pixiv: OAuth, YouTube: missing', 'started', 'Ready to sync workspace dictionaries from vault usage.', 'Ready to prune dictionary entries not used by any vault in the workspace.', 'cleaned 0, failed 0']);
  expect(calls).toEqual(['auth', 'metadata', 'review']);
});

test('launcher reports legacy workspace configs as an explicit adoption case', async ({ page }) => {
  await installMockVaultApi(page, { workspaceLoadLegacy: true });
  await page.goto('/');
  await page.locator('.launcher-workspace-row').first().click();
  await expect(page.getByText(/Unsupported legacy workspace config/)).toBeVisible();
  await expect(page.getByText(/explicit content adoption/)).toBeVisible();
});

test('general settings persist thumbnail format and webview controls', async ({ page }) => {
  await openMockVault(page);
  const contextMenuAllowedBefore = await page.evaluate(() =>
    window.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))
  );
  expect(contextMenuAllowedBefore).toBe(false);
  const printAllowed = await page.evaluate(() =>
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'p', ctrlKey: true, bubbles: true, cancelable: true }))
  );
  const navigationAllowed = await page.evaluate(() =>
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', altKey: true, bubbles: true, cancelable: true }))
  );
  expect(printAllowed).toBe(false);
  expect(navigationAllowed).toBe(false);

  await page.getByRole('button', { name: /Settings/ }).click();
  await expect(page.getByLabel('Thumbnail format')).toHaveValue('jpeg');
  await page.getByLabel('Thumbnail format').selectOption('webp');
  await page.getByLabel('Enable developer tools').check();
  await page.getByLabel('Enable right-click context menu').check();

  const contextMenuAllowedBeforeSave = await page.evaluate(() =>
    window.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))
  );
  expect(contextMenuAllowedBeforeSave).toBe(false);

  await page.getByRole('button', { name: 'Save Settings' }).click();
  await expect(page.getByText('All system configurations are up-to-date.')).toBeVisible();

  const contextMenuAllowedAfter = await page.evaluate(() =>
    window.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))
  );
  expect(contextMenuAllowedAfter).toBe(true);

  await page.reload();
  await expect(page.getByTestId('vault-tile').first()).toBeVisible();
  const contextMenuAllowedAfterReload = await page.evaluate(() =>
    window.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))
  );
  expect(contextMenuAllowedAfterReload).toBe(true);
  await page.getByRole('button', { name: /Settings/ }).click();
  await expect(page.getByLabel('Thumbnail format')).toHaveValue('webp');
});

test('missing app-settings ETag keeps the draft dirty and shows an actionable error', async ({ page }) => {
  await openMockVault(page, { appSettingsIncludeEtag: false });
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByLabel('Enable developer tools').check();
  await page.getByRole('button', { name: 'Save Settings' }).click();

  await expect(page.getByRole('alert')).toContainText('App settings must be loaded before saving');
  await expect(page.getByText('You have unsaved changes.')).toBeVisible();
  await expect(page.getByLabel('Enable developer tools')).toBeChecked();
});

test('failed app-settings saves retain the dirty draft and show the conflict', async ({ page }) => {
  await openMockVault(page, { appSettingsPutStatus: 412 });
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByLabel('Enable developer tools').check();
  await page.getByRole('button', { name: 'Save Settings' }).click();

  await expect(page.getByRole('alert')).toContainText('App settings changed since they were loaded');
  await expect(page.getByText('You have unsaved changes.')).toBeVisible();
  await expect(page.getByLabel('Enable developer tools')).toBeChecked();
});

test('settings shows the active workspace and vault from the runtime session', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Workspace' }).click();

  await expect(page.getByText('Active Workspace Session')).toBeVisible();
  await expect(page.getByText('C:/ObsidianVault/lmz', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('C:/ObsidianVault/lmz/data/topics')).toBeVisible();
  await expect(page.getByText('C:/ObsidianVault/lmz/data/vaults/default')).toBeVisible();
});

test('settings registers and activates workspaces for next restart', async ({ page }) => {
  const actions: Array<{ action: string; payload: any }> = [];
  await openMockVault(page, {
    onWorkspaceAction: (action, payload) => actions.push({ action, payload })
  });
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Workspace' }).click();

  await expect(page.getByText('Default').first()).toBeVisible();
  await page.getByRole('button', { name: 'Activate' }).first().click();
  await expect(page.getByText('Restart required to apply changes to the active workspace.')).toBeVisible();

  await page.getByPlaceholder('Parent folder for LMZ workspace').fill('F:/Archive/Main');
  await page.getByPlaceholder('Workspace label').fill('Main Vault');
  await page.getByRole('button', { name: 'Create Workspace' }).click();

  await expect(page.getByText('Main Vault', { exact: true })).toBeVisible();
  expect(actions).toEqual([
    { action: 'active', payload: { id: 'obsidian-main' } },
    { action: 'add', payload: { path: 'F:/Archive/Main', name: 'Main Vault' } }
  ]);
});

test('workspace deletion offers explicit modes and sends generated cleanup', async ({ page }) => {
  const actions: Array<{ action: string; payload: any }> = [];
  await openMockVault(page, {
    onWorkspaceAction: (action, payload) => actions.push({ action, payload })
  });
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Workspace' }).click();

  const row = page.locator('.workspace-row').filter({ hasText: 'Obsidian Main' });
  await row.getByRole('button', { name: 'Delete' }).click();
  const dialog = page.getByRole('dialog', { name: 'Delete Workspace' });

  await expect(dialog.getByRole('radio', { name: /Remove from LMZ/ })).toBeChecked();
  await expect(dialog).toContainText('Files remain untouched');
  await dialog.getByRole('radio', { name: /Delete LMZ data/ }).check();
  await expect(dialog).toContainText('Keeps vault assets, notes, pending media, and recovery files');
  await dialog.getByRole('button', { name: 'Delete LMZ data' }).click();

  await expect.poll(() => actions.length).toBe(1);
  expect(actions).toContainEqual({ action: 'delete', payload: { id: 'obsidian-main', mode: 'generated' } });
  await expect(page.getByText('Obsidian Main', { exact: true })).toHaveCount(0);
});

test('full workspace deletion requires ownership and exact name confirmation', async ({ page }) => {
  const actions: Array<{ action: string; payload: any }> = [];
  await openMockVault(page, {
    onWorkspaceAction: (action, payload) => actions.push({ action, payload })
  });
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Workspace' }).click();

  const legacyRow = page.locator('.workspace-row').filter({ hasText: 'Legacy Workspace' });
  await legacyRow.getByRole('button', { name: 'Delete' }).click();
  let dialog = page.getByRole('dialog', { name: 'Delete Workspace' });
  await expect(dialog.getByRole('radio', { name: /Delete entire workspace/ })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Cancel' }).click();

  const managedRow = page.locator('.workspace-row').filter({ hasText: 'Obsidian Main' });
  await managedRow.getByRole('button', { name: 'Delete' }).click();
  dialog = page.getByRole('dialog', { name: 'Delete Workspace' });
  await dialog.getByRole('radio', { name: /Delete entire workspace/ }).check();
  const confirmButton = dialog.getByRole('button', { name: 'Delete entire workspace' });
  await expect(confirmButton).toBeDisabled();
  await dialog.getByLabel('Type workspace name to confirm').fill('Obsidian Main');
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();

  expect(actions).toContainEqual({ action: 'delete', payload: { id: 'obsidian-main', mode: 'all' } });
});

test('workspace deletion reports recoverable cleanup pending state', async ({ page }) => {
  await openMockVault(page, { workspaceDeleteCleanupStatus: 'pending' });
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Workspace' }).click();

  const row = page.locator('.workspace-row').filter({ hasText: 'Obsidian Main' });
  await row.getByRole('button', { name: 'Delete' }).click();
  const dialog = page.getByRole('dialog', { name: 'Delete Workspace' });
  await dialog.getByRole('radio', { name: /Delete LMZ data/ }).check();
  await dialog.getByRole('button', { name: 'Delete LMZ data' }).click();

  await expect(page.getByText('Cleanup Pending', { exact: true })).toBeVisible();
  await expect(page.getByText(/\.lmz-delete-pending/)).toBeVisible();
});

test('settings vault delete confirmation names target before confirm=true', async ({ page }) => {
  const actions: Array<{ action: string; payload: any }> = [];
  await openMockVault(page, {
    onVaultAction: (action, payload) => actions.push({ action, payload })
  });

  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Vaults' }).click();

  const archiveRow = page.locator('.workspace-row').filter({ hasText: 'Archive' });
  await archiveRow.getByRole('button', { name: 'Delete' }).click();

  const dialog = page.getByRole('dialog', { name: 'Delete Vault' });
  await expect(dialog).toContainText('Permanently delete Archive?');
  await expect(dialog).toContainText('Vault ID: archive');
  await expect(dialog).toContainText('Items: 2');
  await expect(dialog).toContainText('Root: C:/ObsidianVault/lmz/data/vaults/archive');

  await dialog.getByRole('button', { name: 'Delete' }).click();
  await expect(page.getByText('Archive')).toHaveCount(0);
  expect(actions).toContainEqual({ action: 'delete', payload: { id: 'archive', confirm: true } });
});

test('settings previews vault merge and runs vault health package actions', async ({ page }) => {
  const actions: Array<{ action: string; payload?: any }> = [];
  await openMockVault(page, {
    onVaultAction: (action, payload) => actions.push({ action, payload })
  });
  page.on('dialog', async (dialog) => dialog.accept());
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Maintenance' }).click();

  await page.getByLabel('Merged vault name').fill('Merged Vault');
  await page.locator('.merge-vault-row').filter({ hasText: 'Default' }).getByRole('checkbox').check();
  await page.locator('.merge-vault-row').filter({ hasText: 'Archive' }).getByRole('checkbox').check();
  const mergeSection = page.locator('.settings-section').filter({ hasText: 'Merge Vaults' });
  await expect(mergeSection.getByRole('button', { name: 'Create' })).toBeDisabled();
  await mergeSection.getByRole('button', { name: 'Preview' }).click();
  await expect(page.locator('.merge-preview-box')).toContainText('Importable 4');
  await mergeSection.getByRole('button', { name: 'Create' }).click();
  await page.getByLabel('Merge Vaults').getByRole('button', { name: 'Create' }).click();
  await expect(page.getByText('Merged Vault Created')).toBeVisible();

  await page.getByRole('button', { name: 'Audit' }).click();
  await expect(page.getByText('3 issues').first()).toBeVisible();
  await page.getByRole('button', { name: 'Repair' }).click();
  await expect(page.getByRole('dialog', { name: 'Repair Vault' })).toBeVisible();
  await page.getByRole('dialog', { name: 'Repair Vault' }).getByRole('button', { name: 'Repair' }).click();
  await expect(page.getByText('Repair Complete')).toBeVisible();

  await page.getByRole('button', { name: 'Notifications', exact: true }).click();
  const notificationHistory = page.getByRole('region', { name: 'Notification history' });
  await expect(notificationHistory).toContainText('Repair Complete');
  await notificationHistory.getByRole('button', { name: 'Mark all notifications as read' }).click();
  await expect(page.getByLabel(/unread notifications/)).toHaveCount(0);
  await notificationHistory.getByRole('button', { name: 'Clear notifications' }).click();
  await expect(notificationHistory).toContainText('No notifications');
  await page.getByRole('button', { name: 'Notifications', exact: true }).click();

  await page.getByRole('button', { name: 'Backup Vault Folder' }).click();
  await page.getByRole('button', { name: 'Backup', exact: true }).click();
  await expect(page.getByText('Backup Successful')).toBeVisible();
  await page.getByRole('button', { name: 'Export Vault Package' }).click();
  await page.getByLabel('Include review state').check();
  await page.getByRole('button', { name: 'Export', exact: true }).click();
  await expect(page.getByText('Export Successful')).toBeVisible();
  await page.getByTestId('import-package-path').evaluate((element) => {
    const input = element as HTMLInputElement;
    input.value = 'C:/Exports/default.lmzvault.zip';
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.getByPlaceholder('Imported vault display name').fill('Imported');
  await page.locator('.vault-package-import-row').getByRole('button', { name: 'Preview' }).click();
  await expect(page.locator('.import-preview-box')).toContainText('Exported Vault');
  await page.getByRole('button', { name: 'Import Vault' }).click();
  await page.getByRole('button', { name: 'Import', exact: true }).click();
  await expect(page.getByText('Vault Imported')).toBeVisible();

  expect(actions).toEqual([
    { action: 'merge-preview', payload: { name: 'Merged Vault', source_vault_ids: ['default', 'archive'] } },
    { action: 'merge', payload: { name: 'Merged Vault', source_vault_ids: ['default', 'archive'] } },
    { action: 'health', payload: undefined },
    { action: 'repair', payload: { actions: ['metadata', 'thumbnails', 'wd_tagging', 'derived_cache', 'review_sidecars', 'quarantine_orphans'], confirm_destructive: true } },
    { action: 'backup', payload: { confirm: true } },
    { action: 'export', payload: { confirm: true, include_review: true } },
    { action: 'import-preview', payload: { package_path: 'C:/Exports/default.lmzvault.zip', target_name: 'Imported' } },
    { action: 'import', payload: { package_path: 'C:/Exports/default.lmzvault.zip', target_name: 'Imported', package_fingerprint: 'fingerprint-imported', confirm: true } }
  ]);
});

test('vault health rebuilds only thumbnails after confirmation', async ({ page }) => {
  const actions: Array<{ action: string; payload?: any }> = [];
  await openMockVault(page, { onVaultAction: (action, payload) => actions.push({ action, payload }) });
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Maintenance' }).click();
  await page.getByRole('button', { name: 'Rebuild thumbnails', exact: true }).click();
  expect(actions).toEqual([]);
  await page.getByRole('dialog', { name: 'Rebuild Thumbnails' }).getByRole('button', { name: /Rebuild/ }).click();
  await expect(page.getByText('Thumbnail rebuild: 2 generated, 0 skipped, 1 failed (3 checked).')).toBeVisible();
  expect(actions).toEqual([{ action: 'repair', payload: { actions: ['rebuild_thumbnails'], confirm_destructive: false } }]);
});

test('settings metadata rebuild shows progress only for maintenance job', async ({ page }) => {
  await openMockVault(page, {
    metadataRebuildResponse: {
      status: 'started',
      maintenance_rebuild: {
        running: true,
        status: 'running',
        stage: 'starting',
        items_total: 100,
        items_done: 0,
        errors: 0
      }
    },
    metadataStatusSequence: [
      {
        ready: true,
        repair_running: false,
        items: 100,
        indexed: 100,
        errors: 0,
        dirty: 0,
        maintenance_rebuild: { running: false, status: 'idle' }
      },
      {
        ready: false,
        repair_running: true,
        items: 100,
        indexed: 0,
        errors: 0,
        dirty: 0,
        maintenance_rebuild: {
          running: true,
          status: 'running',
          stage: 'reading metadata',
          items_total: 100,
          items_done: 40,
          errors: 0
        }
      },
      {
        ready: true,
        repair_running: false,
        items: 100,
        indexed: 100,
        errors: 0,
        dirty: 0,
        maintenance_rebuild: {
          running: false,
          status: 'completed',
          stage: 'completed',
          items_total: 100,
          items_done: 100,
          errors: 0,
          duration_ms: 1200
        }
      }
    ]
  });

  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByRole('button', { name: 'Maintenance' }).click();
  await expect(page.getByLabel('Metadata rebuild progress')).toHaveCount(0);

  await page.getByRole('button', { name: 'Rebuild' }).click();
  await expect(page.getByLabel('Metadata rebuild progress')).toBeVisible();
  await expect(page.locator('.settings-action-row-status').nth(1)).toContainText('40 / 100');
  await expect(page.locator('.metadata-progress-fill')).toHaveAttribute('style', /40%/);
  await expect(page.locator('.settings-action-row-status').nth(1)).toContainText('completed', { timeout: 3000 });
  await expect(page.getByLabel('Metadata rebuild progress')).toHaveCount(0);
});

test('stats artists tab shows compact detail editor and saves artist changes', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();
  await page.getByRole('button', { name: 'Artists' }).click();

  await expect(page.locator('.artist-row').first()).toBeVisible();
  await expect(page.locator('.artist-group-label')).toContainText(['Known Artists', 'Placeholders']);
  await expect(page.locator('.placeholder-row')).toContainText('Unknown');
  await expect(page.locator('.artist-detail')).toContainText('Links');

  await page.locator('#artist-name').fill('Canonical Artist');
  await page.locator('#artist-kind').selectOption('brand');
  await page.locator('#artist-notes').fill('reviewed');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.locator('.artist-detail')).toContainText('Canonical Artist');
  await expect(page.locator('.artist-detail')).toContainText('brand');

  await page.getByPlaceholder('New alias').fill('Alias One');
  await page.getByRole('button', { name: 'Add Alias' }).click();
  await expect(page.locator('.alias-chip')).toContainText('Alias One');

  const detail = page.locator('.artist-detail');
  await detail.getByLabel('Link platform').selectOption('Pixiv');
  await detail.getByPlaceholder('url').fill('https://www.pixiv.net/users/canonical_artist');
  await detail.getByRole('button', { name: 'Add link' }).click();
  await expect(page.locator('.artist-detail')).toContainText('canonical_artist');
});

test('stats artists tab previews and confirms artist merge', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();
  await page.getByRole('button', { name: 'Artists' }).click();

  await expect(page.locator('.artist-row').first()).toBeVisible();
  await page.getByRole('button', { name: 'Merge Other Artists Into This' }).click();
  await expect(page.getByRole('dialog', { name: /Merge Into/ })).toBeVisible();

  const sourceName = (await page.locator('.merge-source-row .value').first().textContent())?.trim() || '';
  await page.locator('.merge-source-row').first().click();
  await expect(page.locator('.merge-preview')).toContainText('Affected items');
  await expect(page.locator('.merge-preview')).toContainText('Aliases added/moved');

  await page.getByRole('button', { name: 'Merge into target' }).click();
  await expect(page.getByRole('dialog', { name: /Merge Into/ })).toHaveCount(0);
  await expect(page.locator('.artist-detail')).toContainText(sourceName);
});

test('stats artist merge guards empty selection and shows merge errors', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();
  await page.getByRole('button', { name: 'Artists' }).click();

  await page.getByRole('button', { name: 'Merge Other Artists Into This' }).click();
  const dialog = page.getByRole('dialog', { name: /Merge Into/ });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Merge into target' })).toBeDisabled();

  await page.locator('.merge-source-row').first().click();
  await expect(dialog.getByRole('button', { name: 'Merge into target' })).toBeEnabled();
  await page.locator('.merge-source-row').first().click();
  await expect(dialog.getByRole('button', { name: 'Merge into target' })).toBeDisabled();
  await expect(page.locator('.merge-preview')).toContainText('Select source artists');

  await page.locator('.merge-source-row').first().click();
  await page.evaluate(() => (window as any).failNextArtistMerge());
  await dialog.getByRole('button', { name: 'Merge into target' }).click();
  await expect(dialog).toBeVisible();
  await expect(page.locator('.empty-state.error')).toContainText('merge failed');
});

test('stats non-artist tabs keep facet list behavior', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();
  await page.getByRole('button', { name: 'Platforms' }).click();

  await expect(page.locator('.stats-list')).toBeVisible();
  await expect(page.locator('.stats-row').first()).toBeVisible();
});

test('stats WD tag facets include rating character and general tags', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();

  await expect(page.locator('.stat-chip')).toContainText(['safe', 'mock_character', 'mock_tag']);
});

test('stats sort toggles between popularity and alphabetical', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();
  await page.getByRole('button', { name: 'Artists' }).click();

  await expect(page.locator('.artist-row .value').first()).toHaveText('Mock Group B');
  await page.getByRole('button', { name: 'Filter stats' }).click();
  await page.getByRole('button', { name: 'Alphabetical' }).click();
  await expect(page.locator('.artist-row .value').first()).toHaveText('Mock Group B');
  await page.getByRole('button', { name: 'Platforms' }).click();
  await expect(page.locator('.stats-row .value').first()).toHaveText('Local');
  await page.getByRole('button', { name: 'Popularity' }).click();
  await expect(page.locator('.stats-row .value').first()).toHaveText('Local');
});

test('stats letter filter applies to artists and tag chips but not platforms', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();

  await page.getByRole('button', { name: 'Artists' }).click();
  await expect(page.locator('.artist-row:not(.placeholder-row)')).toHaveCount(3);
  await page.getByRole('button', { name: 'Filter stats' }).click();
  await page.getByLabel('A-Z').check();
  await page.locator('.letter-tabs').getByRole('button', { name: 'M' }).click();
  await expect(page.locator('.artist-row:not(.placeholder-row)')).toHaveCount(3);
  await expect(page.locator('.artist-row .value').first()).toContainText('Mock');

  await page.getByRole('button', { name: 'Topics' }).click();
  await expect(page.locator('.stat-chip')).toHaveCount(3);
  await page.locator('.letter-tabs').getByRole('button', { name: 'G' }).click();
  await expect(page.locator('.stat-chip')).toHaveCount(1);
  await expect(page.locator('.stat-chip .value').first()).toHaveText('group-topic');

  await page.getByRole('button', { name: 'Platforms' }).click();
  await expect(page.locator('.letter-tabs')).toHaveCount(0);
});

test('stats selected topics and wd tags filter the vault', async ({ page }) => {
  const itemRequests: URL[] = [];
  await openMockVault(page, {
    onItemsRequest: (url) => {
      itemRequests.push(new URL(url.toString()));
    }
  });
  await page.getByRole('button', { name: /Stats/ }).click();

  await page.getByRole('button', { name: 'WD Tags' }).click();
  await page.locator('.stat-chip').filter({ hasText: 'mock_tag' }).click();
  await expect(page.locator('.stats-filter-bar')).toContainText('1 selected');
  await expect(page.locator('.stat-chip-wrap.selected')).toContainText('mock_tag');

  await page.getByRole('button', { name: 'Topics' }).click();
  await page.locator('.stat-chip').filter({ hasText: 'mock-topic' }).click();
  await expect(page.locator('.stats-filter-bar')).toContainText('2 selected');
  await expect(page.locator('.stats-filter-bar')).toContainText('1 topics');
  await expect(page.locator('.stats-filter-bar')).toContainText('1 WD tags');

  await page.getByRole('button', { name: 'Filter stats' }).click();
  await page.getByRole('button', { name: 'Alphabetical' }).click();
  await page.getByLabel('A-Z').check();
  await page.locator('.letter-tabs').getByRole('button', { name: 'M' }).click();
  await expect(page.locator('.stats-filter-bar')).toContainText('2 selected');

  await page.getByRole('button', { name: 'Filter Vault' }).click();
  await expect(page.getByRole('button', { name: 'Vault', exact: true })).toHaveClass(/active/);
  await expect(page.getByTestId('vault-search-input')).toHaveValue('t:mock-topic; #mock_tag;');
  await expect(page.locator('.stats-filter-bar')).toHaveCount(0);

  const filteredRequest = itemRequests.find((url) =>
    url.searchParams.getAll('topic').includes('mock-topic') &&
    url.searchParams.getAll('wd_tag').includes('mock_tag')
  );
  expect(filteredRequest).toBeTruthy();
});

test('stats metadata actions update topic and wd facets', async ({ page }) => {
  await openMockVault(page);
  await page.getByRole('button', { name: /Stats/ }).click();

  await page.getByRole('button', { name: 'WD Tags' }).click();
  await expect(page.locator('.stat-chip').filter({ hasText: 'mock_tag' })).toBeVisible();
  await page.locator('.stat-chip-wrap').filter({ hasText: 'mock_tag' }).hover();
  await page.getByLabel('Rename mock_tag').click();
  await page.getByLabel('New').fill('renamed_tag');
  await page.getByRole('button', { name: 'Rename', exact: true }).click();
  await expect(page.locator('.stat-chip').filter({ hasText: 'renamed_tag' })).toBeVisible();
  await expect(page.locator('.stat-chip').filter({ hasText: 'mock_tag' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Close' }).click();

  await page.locator('.stat-chip-wrap').filter({ hasText: 'renamed_tag' }).hover();
  await page.getByLabel('Delete renamed_tag').click();
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(page.locator('.stat-chip').filter({ hasText: 'renamed_tag' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Close' }).click();

  await page.getByRole('button', { name: 'Topics' }).click();
  await expect(page.locator('.stat-chip').filter({ hasText: 'group-topic' })).toBeVisible();
  await page.locator('.stat-chip-wrap').filter({ hasText: 'group-topic' }).hover();
  await page.getByLabel('Merge group-topic').click();
  await page.getByLabel('Target').fill('mock-topic');
  await page.getByRole('button', { name: 'Merge', exact: true }).click();
  await expect(page.locator('.stat-chip').filter({ hasText: 'group-topic' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Close' }).click();

  await page.locator('.stat-chip-wrap').filter({ hasText: 'mock-topic' }).hover();
  await page.getByLabel('Delete mock-topic').click();
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(page.locator('.stat-chip').filter({ hasText: 'mock-topic' })).toHaveCount(0);
});



