<script lang="ts">
  import { createEventDispatcher, onMount, tick } from 'svelte';
  import type { SearchFilters, VaultGroup, VaultItem } from './types';
  import { apiFetch } from './api';
  import {
    appSettings,
    loadAppSettings,
    saveCurrentAppSettings,
    updateAppSettings,
    setVaultLayoutMode,
    setVaultTileMinWidthLocal,
    setInspectorWidth,
    normalizeInspectorWidth,
    DEFAULT_INSPECTOR_WIDTH
  } from './appSettingsStore';
  import { log as uiLog } from './logger';
  import type { VaultLayoutMode } from './layout';
  import { DEFAULT_TILE_MIN_WIDTH, normalizeLayoutMode, normalizeTileMinWidth } from './layout';
  import { buildItemQueryParams, emptyFilters } from './search';
  import { updateSelection } from './selection';
  import { watchIntersection, watchResize, type ObserverCleanup } from './observers';
  import { toggleRamTracking } from './ramStore';
  import { runtimeSessionKey } from './runtimeStore';
  import GridRenderer from './renderers/grid/GridRenderer.svelte';
  import MasonryRenderer from './renderers/masonry/MasonryRenderer.svelte';
  import Inspector from './Inspector.svelte';
  import MediaFocus from './MediaFocus.svelte';
  import SearchBar from './SearchBar.svelte';

  const dispatch = createEventDispatcher();
  export let filterRequest: { id: string; query: string } | null = null;
  export let active = false;
  let isDirty = false;

  let items: VaultItem[] = [];
  let groupedItems: VaultGroup[] = [];
  let stats = { total_items: 0 };
  let isSearching = true;
  let isLoadingMore = false;
  let selectedItem: VaultItem | null = null;
  let selectedGroup: VaultGroup | null = null;
  let selectedHashes = new Set<string>();
  let lastSelectedHash: string | null = null;
  let bulkDeleting = false;
  let focusMode: 'normal' | 'wide' | 'fullscreen' = 'normal';
  let focusStartTime = 0;
  let currentLayoutMode: VaultLayoutMode = 'masonry';
  let currentSort = 'newest';
  let currentMediaType = 'all';
  let activeFilters: SearchFilters = emptyFilters();
  let nextCursor: string | null = null;
  let hasMore = false;
  let sentinelEl: HTMLElement | null = null;
  let layoutHostEl: HTMLElement | null = null;
  let intersectionCleanup: ObserverCleanup | null = null;
  let observedSentinel: HTMLElement | null = null;
  let observedLayout = '';
  let resizeCleanup: ObserverCleanup | null = null;
  let observedLayoutHost: HTMLElement | null = null;
  let vaultWidth = 0;
  let tileMinWidth = DEFAULT_TILE_MIN_WIDTH;
  let tileSizeSaveTimer: number | null = null;
  let groupIndexes: Record<string, number> = {};
  let inspectorVisible = true;
  let inspectorWidth = DEFAULT_INSPECTOR_WIDTH;
  let isResizingInspector = false;
  let resizeStartX = 0;
  let resizeStartWidth = DEFAULT_INSPECTOR_WIDTH;

  let groupsById = new Map<string, VaultGroup>();
  let groupOrder: string[] = [];
  let hashIndex = new Map<string, { item: VaultItem; group: VaultGroup }>();
  let lastStatus = { totalItems: -1, groups: -1, hasMore: false, layoutMode: '' as string };

  $: emitStatus(stats.total_items, groupedItems.length, hasMore, currentLayoutMode);
  $: loadedHashOrder = items.map((item) => item.hash);
  $: attachInfiniteScroll(sentinelEl, currentLayoutMode);
  $: observeLayoutHost(layoutHostEl);
  $: if (active && isDirty) {
    isDirty = false;
    refreshFromTop();
  }
  $: if ($appSettings) {
    const nextMode = normalizeLayoutMode($appSettings);
    const nextWidth = normalizeTileMinWidth($appSettings?.ui?.vault_tile_min_width);
    if (currentLayoutMode !== nextMode) {
      currentLayoutMode = nextMode;
    }
    if (tileMinWidth !== nextWidth) {
      tileMinWidth = nextWidth;
    }
    const nextInspectorWidth = normalizeInspectorWidth($appSettings?.ui?.inspector_width);
    if (!isResizingInspector && inspectorWidth !== nextInspectorWidth) {
      inspectorWidth = nextInspectorWidth;
    }
  }

  let currentRuntimeSessionKey = '';
  $: if ($runtimeSessionKey) {
    if (currentRuntimeSessionKey && currentRuntimeSessionKey !== $runtimeSessionKey) {
      resetForRuntimeSwitch($runtimeSessionKey);
    }
    currentRuntimeSessionKey = $runtimeSessionKey;
  }

  function resetGroupsState() {
    groupsById = new Map();
    groupOrder = [];
    hashIndex = new Map();
  }

  function resetForRuntimeSwitch(sessionKey: string) {
    uiLog('INFO', 'Runtime switch detected in VaultView; resetting view state', { sessionKey });
    clearSelection();
    activeFilters = emptyFilters();
    currentSort = 'newest';
    currentMediaType = 'all';
    focusMode = 'normal';
    focusStartTime = 0;
    groupIndexes = {};
    items = [];
    groupedItems = [];
    stats = { total_items: 0 };
    resetGroupsState();
    nextCursor = null;
    hasMore = false;
    if (layoutHostEl) layoutHostEl.scrollTop = 0;
    fetchItems(false);
  }

  function groupKeyForItem(item: VaultItem) {
    return item.source_url && item.source_url.trim() !== '' ? item.source_url : `single-${item.hash}`;
  }

  function appendToGroups(newItems: VaultItem[], reset: boolean): VaultGroup[] {
    if (reset) resetGroupsState();
    if (!newItems.length) return groupOrder.map((id) => groupsById.get(id)!);

    const additionsByKey = new Map<string, VaultItem[]>();
    for (const item of newItems) {
      const key = groupKeyForItem(item);
      const list = additionsByKey.get(key);
      if (list) list.push(item);
      else additionsByKey.set(key, [item]);
    }

    for (const [key, addedItems] of additionsByKey) {
      const existing = groupsById.get(key);
      if (existing) {
        const updated: VaultGroup = { id: key, items: [...existing.items, ...addedItems] };
        groupsById.set(key, updated);
        for (const it of updated.items) hashIndex.set(it.hash, { item: it, group: updated });
      } else {
        const created: VaultGroup = { id: key, items: [...addedItems] };
        groupsById.set(key, created);
        groupOrder.push(key);
        for (const it of addedItems) hashIndex.set(it.hash, { item: it, group: created });
      }
    }

    return groupOrder.map((id) => groupsById.get(id)!);
  }

  function rebuildGroupsFromItems(nextItems: VaultItem[]): VaultGroup[] {
    return appendToGroups(nextItems, true);
  }

  function emitStatus(totalItems: number, groups: number, moreAvailable: boolean, layoutMode: VaultLayoutMode) {
    if (
      lastStatus.totalItems === totalItems &&
      lastStatus.groups === groups &&
      lastStatus.hasMore === moreAvailable &&
      lastStatus.layoutMode === layoutMode
    ) return;
    lastStatus = { totalItems, groups, hasMore: moreAvailable, layoutMode };
    dispatch('status', { totalItems, groups, hasMore: moreAvailable, layoutMode });
  }

  async function fetchConfig() {
    try {
      const loaded = await loadAppSettings();
      currentLayoutMode = normalizeLayoutMode(loaded);
      tileMinWidth = normalizeTileMinWidth(loaded?.ui?.vault_tile_min_width);
      inspectorVisible = loaded?.ui?.inspector_visible !== false;
      inspectorWidth = normalizeInspectorWidth(loaded?.ui?.inspector_width);
    } catch (error) {
      uiLog('ERROR', 'Failed to fetch config', { error });
    }
  }

  async function saveLayoutMode(mode: VaultLayoutMode) {
    currentLayoutMode = mode;
    try {
      await setVaultLayoutMode(mode);
    } catch (error) {
      uiLog('ERROR', 'Failed to save layout mode command', { error });
    }
  }

  function saveTileSizeDebounced() {
    if (tileSizeSaveTimer !== null) window.clearTimeout(tileSizeSaveTimer);
    tileSizeSaveTimer = window.setTimeout(async () => {
      tileSizeSaveTimer = null;
      try {
        await saveCurrentAppSettings();
      } catch (error) {
        uiLog('ERROR', 'Failed to save vault zoom', { error });
      }
    }, 500);
  }

  function zoomTileSize(delta: number) {
    const next = normalizeTileMinWidth(tileMinWidth + delta);
    if (next === tileMinWidth) return;
    tileMinWidth = next;
    setVaultTileMinWidthLocal(next);
    saveTileSizeDebounced();
  }

  function zoomIn() {
    zoomTileSize(20);
  }

  function zoomOut() {
    zoomTileSize(-20);
  }

  function itemPageLimit() {
    if (!import.meta.env.DEV) return '50';
    const value = new URLSearchParams(window.location.search).get('lmz_test_page_size');
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 1) return '50';
    return String(Math.min(100000, Math.round(numeric)));
  }

  async function fetchItems(append = false) {
    if (!append) {
      nextCursor = null;
      isSearching = true;
    } else {
      isLoadingMore = true;
    }
    try {
      const params = buildItemQueryParams(activeFilters, currentSort, currentMediaType, itemPageLimit(), append ? nextCursor : null);
      const response = await apiFetch(`/api/items?${params.toString()}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const newItems: VaultItem[] = Array.isArray(data.items) ? data.items : [];
      if (append) {
        items = [...items, ...newItems];
        groupedItems = appendToGroups(newItems, false);
      } else {
        items = newItems;
        groupedItems = rebuildGroupsFromItems(newItems);
      }
      nextCursor = data.next_cursor || null;
      hasMore = data.has_more || false;
    } catch (error) {
      uiLog('ERROR', 'Failed to fetch items', { error: String(error) });
    } finally {
      if (!append) isSearching = false;
      else isLoadingMore = false;
    }

    if (!append) {
      try {
        const statsRes = await apiFetch('/api/stats');
        if (!statsRes.ok) throw new Error(`HTTP ${statsRes.status}`);
        stats = await statsRes.json();
      } catch (error) {
        uiLog('ERROR', 'Failed to fetch vault stats', { error: String(error) });
      }
    }
  }

  function loadMore() {
    if (hasMore && nextCursor && !isSearching && !isLoadingMore) fetchItems(true);
  }

  function handleFiltersChanged(event: CustomEvent) {
    activeFilters = event.detail.filters;
    fetchItems();
  }

  function handleCommand(event: CustomEvent) {
    const command = event.detail.command;
    if (command === 'masonry' || command === 'grid') {
      saveLayoutMode(command);
    } else if (command === 'zoom-in') {
      zoomIn();
    } else if (command === 'zoom-out') {
      zoomOut();
    } else if (command === 'toggle-inspector') {
      toggleInspector();
    } else if (command === 'ram-track') {
      toggleRamTracking().catch((error) => uiLog('ERROR', 'Failed to toggle RAM tracker', { error: String(error) }));
    } else if (command === 'scan-auth') {
      scanAuthStatus();
    } else if (command === 'cleanup-review') {
      cleanupReview();
    } else if (command === 'sort-newest') {
      setSortMode('newest');
    } else if (command === 'sort-oldest') {
      setSortMode('oldest');
    } else if (command === 'sort-artist') {
      setSortMode('artist');
    } else if (command === 'media-all') {
      setMediaType('all');
    } else if (command === 'media-image') {
      setMediaType('image');
    } else if (command === 'media-video') {
      setMediaType('video');
    }
  }

  async function scanAuthStatus() {
    try {
      const response = await apiFetch('/api/auth/scan', { method: 'POST' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      uiLog('INFO', 'Auth scan requested from command');
    } catch (error) {
      uiLog('ERROR', 'Auth scan command failed', { error: String(error) });
    }
  }

  async function cleanupReview() {
    try {
      const response = await apiFetch('/api/review/cleanup', { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
      uiLog('INFO', 'Review cleanup requested from command', {
        cleaned: payload?.cleaned ?? 0,
        failed: payload?.failed ?? 0
      });
    } catch (error) {
      uiLog('ERROR', 'Review cleanup command failed', { error: String(error) });
    }
  }

  function setSortMode(sort: string) {
    if (currentSort === sort) return;
    currentSort = sort;
    fetchItems();
  }

  function setMediaType(mediaType: string) {
    if (currentMediaType === mediaType) return;
    currentMediaType = mediaType;
    fetchItems();
  }

  function handleSelectItem(item: VaultItem, group: VaultGroup, event?: MouseEvent) {
    const next = updateSelection(selectedHashes, loadedHashOrder, item.hash, lastSelectedHash, event);
    selectedHashes = next.selectedHashes;
    lastSelectedHash = next.lastSelectedHash;
    if (selectedHashes.has(item.hash)) {
      selectedItem = item;
      selectedGroup = group;
    } else if (selectedItem && selectedHashes.has(selectedItem.hash)) {
      // current selectedItem still selected — keep it
    } else if (selectedHashes.size > 0) {
      const firstHash = selectedHashes.values().next().value as string;
      const entry = hashIndex.get(firstHash);
      selectedItem = entry?.item ?? null;
      selectedGroup = entry?.group ?? null;
    } else {
      selectedItem = null;
      selectedGroup = null;
    }
    uiLog('DEBUG', `Selected item: ${item.hash.substring(0, 12)}`);
  }

  function handleGroupIndexChange(groupId: string, index: number) {
    groupIndexes = { ...groupIndexes, [groupId]: index };
  }

  function setSingleSelection(item: VaultItem) {
    selectedItem = item;
    const nextGroup = hashIndex.get(item.hash)?.group ?? null;
    if (nextGroup) {
      selectedGroup = nextGroup;
      const idx = nextGroup.items.findIndex((i) => i.hash === item.hash);
      if (idx !== -1) {
        groupIndexes = { ...groupIndexes, [nextGroup.id]: idx };
      }
    } else if (!selectedGroup || !selectedGroup.items.some((i) => i.hash === item.hash)) {
      selectedGroup = null;
    }
    selectedHashes = new Set([item.hash]);
    lastSelectedHash = item.hash;
    focusStartTime = 0;
  }

  function clearSelection() {
    selectedHashes = new Set();
    lastSelectedHash = null;
    selectedItem = null;
    selectedGroup = null;
  }

  async function deleteSelected() {
    const hashes = Array.from(selectedHashes);
    if (!hashes.length || bulkDeleting) return;
    const confirmed = confirm(`Delete ${hashes.length} selected item${hashes.length === 1 ? '' : 's'}? This removes DB rows, vault files, notes, and WD tag cache.`);
    if (!confirmed) return;
    bulkDeleting = true;
    try {
      const response = await apiFetch('/api/items/bulk_delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hashes })
      });
      if (!response.ok) throw new Error(await responseErrorText(response, `HTTP ${response.status}`));
      const result = await response.json();
      if (result.failed_cleanup_count > 0) {
        uiLog('WARNING', 'Bulk delete completed with cleanup failures', result);
        alert(`Deleted ${result.deleted_count} item(s), but ${result.failed_cleanup_count} cleanup operation(s) failed. Check App Logs.`);
      } else {
        uiLog('INFO', 'Bulk delete completed', result);
      }
      clearSelection();
    } catch (error) {
      uiLog('ERROR', 'Bulk delete failed', { error: String(error) });
      alert('Delete failed. Check App Logs for details.');
    } finally {
      bulkDeleting = false;
      await fetchItems();
    }
  }

  async function responseErrorText(response: Response, fallback: string) {
    try {
      const data = await response.json();
      return data?.detail || data?.message || fallback;
    } catch {
      return fallback;
    }
  }

  function handleUpdate(event: CustomEvent) {
    const { hash, artist, source_url, platform } = event.detail;
    uiLog('INFO', `Item updated: ${hash.substring(0, 12)}`, { artist, platform });
    const nextItems = items.map((item) => (item.hash === hash ? { ...item, artist, source_url, platform } : item));
    items = nextItems;
    groupedItems = rebuildGroupsFromItems(nextItems);
    if (selectedItem) {
      const entry = hashIndex.get(selectedItem.hash);
      selectedItem = entry?.item ?? (selectedItem.hash === hash ? { ...selectedItem, artist, source_url, platform } : selectedItem);
      selectedGroup = entry?.group ?? null;
    }
  }

  function handleFocusMode(event: CustomEvent) {
    focusMode = event.detail.mode;
    focusStartTime = event.detail.startTime || 0;
    uiLog('INFO', `Switched to ${focusMode} view from time ${focusStartTime}`);
  }

  function handleGlobalKeydown(event: KeyboardEvent) {
    const target = event.target instanceof Element ? event.target : null;
    const editing = target?.closest('input, textarea, select, [contenteditable="true"]');
    if (!editing && (event.code === 'NumpadAdd' || event.key === '+')) {
      event.preventDefault();
      zoomIn();
      return;
    }
    if (!editing && (event.code === 'NumpadSubtract' || event.key === '-')) {
      event.preventDefault();
      zoomOut();
      return;
    }
    if (!editing && event.key === 'i' && !event.ctrlKey && !event.altKey && !event.metaKey && focusMode === 'normal') {
      event.preventDefault();
      toggleInspector();
      return;
    }
  }

  function toggleInspector() {
    inspectorVisible = !inspectorVisible;
    updateAppSettings((draft) => {
      draft.ui.inspector_visible = inspectorVisible;
    }, true);
  }

  function startInspectorResize(event: PointerEvent) {
    event.preventDefault();
    isResizingInspector = true;
    resizeStartX = event.clientX;
    resizeStartWidth = inspectorWidth;
    document.body.classList.add('inspector-resizing');
    window.addEventListener('pointermove', handleInspectorResize);
    window.addEventListener('pointerup', stopInspectorResize);
    window.addEventListener('pointercancel', stopInspectorResize);
  }

  function handleInspectorResize(event: PointerEvent) {
    if (!isResizingInspector) return;
    inspectorWidth = normalizeInspectorWidth(resizeStartWidth - (event.clientX - resizeStartX));
  }

  function stopInspectorResize() {
    if (!isResizingInspector) return;
    isResizingInspector = false;
    document.body.classList.remove('inspector-resizing');
    window.removeEventListener('pointermove', handleInspectorResize);
    window.removeEventListener('pointerup', stopInspectorResize);
    window.removeEventListener('pointercancel', stopInspectorResize);
    setInspectorWidth(inspectorWidth).catch((error) => uiLog('ERROR', 'Failed to save inspector width', { error: String(error) }));
  }

  function handleResizeHandleKeydown(event: KeyboardEvent) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const delta = event.key === 'ArrowLeft' ? 20 : -20;
    inspectorWidth = normalizeInspectorWidth(inspectorWidth + delta);
    setInspectorWidth(inspectorWidth).catch((error) => uiLog('ERROR', 'Failed to save inspector width', { error: String(error) }));
  }

  async function refreshFromTop() {
    uiLog('INFO', 'Vault refresh from top requested', { layout: currentLayoutMode });
    if (layoutHostEl) layoutHostEl.scrollTop = 0;
    clearSelection();
    focusMode = 'normal';
    nextCursor = null;
    hasMore = false;
    items = [];
    await fetchItems(false);
    await tick();
    if (layoutHostEl) layoutHostEl.scrollTop = 0;
  }

  function handleGlobalRefresh(event: Event) {
    const detail = (event as CustomEvent).detail || {};
    if (detail.tab !== 'vault') return;
    refreshFromTop();
  }

  function handleVaultChanged() {
    if (active) {
      refreshFromTop();
    } else {
      isDirty = true;
    }
  }

  function handleVaultWheel(event: WheelEvent) {
    if (!event.ctrlKey) return;
    event.preventDefault();
    if (event.deltaY < 0) zoomIn();
    else if (event.deltaY > 0) zoomOut();
  }

  function attachInfiniteScroll(node: HTMLElement | null, layout: string) {
    if (observedSentinel === node && observedLayout === layout) return;
    intersectionCleanup?.();
    intersectionCleanup = null;
    observedSentinel = node;
    observedLayout = layout;
    if (!node) return;
    intersectionCleanup = watchIntersection(node, {
      rootMargin: '400px',
      onEnter: loadMore
    });
  }

  function observeLayoutHost(node: HTMLElement | null) {
    if (observedLayoutHost === node) return;
    resizeCleanup?.();
    resizeCleanup = null;
    observedLayoutHost = node;
    if (!node) {
      vaultWidth = 0;
      return;
    }
    resizeCleanup = watchResize(node, (width) => {
      vaultWidth = width;
    });
  }

  onMount(() => {
    window.addEventListener('lmz:refresh', handleGlobalRefresh);
    window.addEventListener('lmz:vault-changed', handleVaultChanged);
    fetchConfig();
    fetchItems();
    return () => {
      window.removeEventListener('lmz:refresh', handleGlobalRefresh);
      window.removeEventListener('lmz:vault-changed', handleVaultChanged);
      intersectionCleanup?.();
      resizeCleanup?.();
      stopInspectorResize();
      if (tileSizeSaveTimer !== null) window.clearTimeout(tileSizeSaveTimer);
    };
  });
</script>

<svelte:window on:keydown={handleGlobalKeydown} />

<div class="vault-shell">
  <div class="vault-main">
    <header class="top-header">
      <SearchBar externalQuery={filterRequest} on:filtersChanged={handleFiltersChanged} on:command={handleCommand} />
    </header>

    <div class="viewport" on:wheel={handleVaultWheel}>
      {#if selectedHashes.size > 1}
        <div class="bulk-action-bar">
          <span class="selection-count">{selectedHashes.size} selected</span>
          <button on:click={clearSelection}>Clear Selection</button>
          <button class="danger" on:click={deleteSelected} disabled={bulkDeleting}>
            {bulkDeleting ? 'Deleting...' : 'Delete Selected'}
          </button>
        </div>
      {/if}
      {#if isSearching && items.length === 0}
        <div class="initial-loading"></div>
      {:else if currentLayoutMode === 'masonry'}
        <MasonryRenderer
          groups={groupedItems}
          viewportWidth={vaultWidth}
          {tileMinWidth}
          selectedHash={selectedItem?.hash}
          {selectedHashes}
          activeIndexes={groupIndexes}
          bind:sentinelEl
          bind:hostEl={layoutHostEl}
          {isLoadingMore}
          onSelectItem={handleSelectItem}
          onIndexChange={handleGroupIndexChange}
        />
      {:else}
        <GridRenderer
          groups={groupedItems}
          viewportWidth={vaultWidth}
          {tileMinWidth}
          selectedHash={selectedItem?.hash}
          {selectedHashes}
          activeIndexes={groupIndexes}
          bind:sentinelEl
          bind:hostEl={layoutHostEl}
          {isLoadingMore}
          onSelectItem={handleSelectItem}
          onIndexChange={handleGroupIndexChange}
        />
      {/if}
    </div>
  </div>

  {#if inspectorVisible}
    <button
      type="button"
      class="inspector-resize-handle"
      class:active={isResizingInspector}
      aria-label="Resize inspector"
      on:pointerdown={startInspectorResize}
      on:keydown={handleResizeHandleKeydown}
    ></button>
    <Inspector
      item={selectedItem}
      group={selectedGroup}
      {focusMode}
      width={inspectorWidth}
      on:close={clearSelection}
      on:updated={handleUpdate}
      on:tagged={() => fetchItems()}
      on:focus={handleFocusMode}
      on:changeItem={(event) => setSingleSelection(event.detail)}
      on:deleted={() => { clearSelection(); fetchItems(); }}
    />
  {/if}
</div>

{#if focusMode !== 'normal' && selectedItem}
  <MediaFocus
    item={selectedItem}
    group={selectedGroup}
    mode={focusMode}
    startTime={focusStartTime}
    on:close={() => focusMode = 'normal'}
    on:switchMode={(event) => { focusMode = event.detail; }}
    on:changeItem={(event) => setSingleSelection(event.detail)}
  />
{/if}

<style>
  :global(body.inspector-resizing) { cursor: col-resize; user-select: none; }
  :global(body.inspector-resizing *) { cursor: col-resize !important; user-select: none; }
  .vault-shell { flex-grow: 1; display: flex; overflow: hidden; min-width: 0; }
  .vault-main { flex-grow: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
  .top-header { height: var(--header-height); display: flex; align-items: center; padding: 0 calc(var(--vault-content-padding) + var(--scrollbar-size)) 0 var(--vault-content-padding); gap: 15px; border-bottom: 1px solid var(--border-dim); flex-shrink: 0; z-index: 100; }
  .viewport { flex-grow: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; position: relative; }
  .inspector-resize-handle { width: 7px; flex: 0 0 7px; cursor: col-resize; border: 0; border-left: 1px solid var(--border-dim); border-right: 1px solid transparent; border-radius: 0; background: var(--bg-main); position: relative; z-index: 20; padding: 0; }
  .inspector-resize-handle:hover, .inspector-resize-handle.active { border-left-color: var(--accent-primary); }
  .bulk-action-bar { position: absolute; left: 50%; bottom: 18px; transform: translateX(-50%); z-index: 60; display: flex; align-items: center; gap: 10px; padding: 9px 12px; border: 1px solid var(--border-dim); border-radius: 8px; background: rgba(13, 17, 23, 0.96); box-shadow: 0 10px 30px rgba(0,0,0,0.35); }
  .selection-count { color: var(--text-bright); font-weight: 600; white-space: nowrap; }
  .initial-loading { flex-grow: 1; }
</style>
