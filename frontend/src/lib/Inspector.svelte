<script lang="ts">
  import type { VaultItem } from './types';
  import { safeConfirm } from './windowLayout';
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { log as uiLog } from './logger';
  import { apiFetch } from './api';
  import { runtimeSessionKey } from './runtimeStore';
  import InspectorMediaPreview from './InspectorMediaPreview.svelte';
  import InspectorMetadataGrid from './InspectorMetadataGrid.svelte';
  import InspectorTopicEditor from './InspectorTopicEditor.svelte';
  import InspectorWdSuggestions from './InspectorWdSuggestions.svelte';
  import { IconCopy, IconFileText, IconFolder, IconSparkles, IconTrash } from './icons';
  import MetadataActionModal from './stats/MetadataActionModal.svelte';
  import { renameTopic } from './stats/statsApi';

  export let item: VaultItem | null = null;
  export let group: { id: string, items: VaultItem[] } | null = null;
  export let focusMode: 'normal' | 'wide' | 'fullscreen' = 'normal';
  export let width = 400;
  const dispatch = createEventDispatcher();

  let fullItem: any = null;
  let artist = '';
  let savedArtist = '';
  let sourceUrl = '';
  let platform = '';
  let savedTopics: string[] = [];
  let draftTopics: string[] = [];
  let topicInputOpen = false;
  let topicInputValue = '';
  let topicInputElement: HTMLInputElement | undefined;
  let topicSuggestions: { value: string; count?: number }[] = [];
  let topicSuggestionsOpen = false;
  let topicSuggestionsLoading = false;
  let activeTopicSuggestionIndex = -1;
  let topicSuggestionTimer: number | null = null;
  let topicSuggestionAbortController: AbortController | null = null;
  let savedWdRating = '';
  let draftWdRating = '';
  let savedWdCharacters: string[] = [];
  let draftWdCharacters: string[] = [];
  let savedWdGeneral: string[] = [];
  let draftWdGeneral: string[] = [];
  let savedWdGroups: Record<string, string[]> = {};
  let draftWdGroups: Record<string, string[]> = {};
  let isDirty = false;
  let loadingTimeout: any = null;
  let showLoadingIndicator = false;
  let tagging = false;
  let abortController: AbortController | null = null;
  let lastLoadedHash: string | null = null;
  let currentRuntimeSessionKey = '';

  // Topic Rename Modal State
  let renameModalOpen = false;
  let renameModalValue = '';
  let renameModalNewValue = '';
  let renameModalBusy = false;
  let renameModalResult = '';
  let renameModalError = '';

  function openRenameTopicModal(topic: string) {
    renameModalOpen = true;
    renameModalValue = topic;
    renameModalNewValue = topic;
    renameModalBusy = false;
    renameModalResult = '';
    renameModalError = '';
  }

  function closeRenameTopicModal() {
    if (renameModalBusy) return;
    renameModalOpen = false;
    renameModalValue = '';
    renameModalNewValue = '';
    renameModalResult = '';
    renameModalError = '';
  }

  async function confirmRenameTopic() {
    if (!renameModalValue || !renameModalNewValue.trim()) return;
    renameModalBusy = true;
    renameModalResult = '';
    renameModalError = '';
    try {
      const payload = await renameTopic(renameModalValue, renameModalNewValue);
      const newLabel = String(payload.new_label || renameModalNewValue.trim());
      
      // Update draftTopics and savedTopics in local state
      draftTopics = draftTopics.map((t) => t === renameModalValue ? newLabel : t);
      savedTopics = savedTopics.map((t) => t === renameModalValue ? newLabel : t);
      
      // If fullItem holds details, refresh counts or update fullItem.topics
      if (fullItem && Array.isArray(fullItem.topics)) {
        fullItem.topics = fullItem.topics.map((t: string) => t === renameModalValue ? newLabel : t);
        if (fullItem.topic_counts) {
          const count = fullItem.topic_counts[renameModalValue];
          delete fullItem.topic_counts[renameModalValue];
          if (count !== undefined) {
            fullItem.topic_counts[newLabel] = count;
          }
        }
      }
      
      const vaultCount = Array.isArray(payload?.vaults_touched) ? payload.vaults_touched.length : 0;
      renameModalResult = `Renamed topic across ${payload.notes_rewritten || 0} notes in ${vaultCount} vaults.`;
      
      // Log maintenance action
      uiLog('INFO', 'Topic renamed from inspector', {
        oldValue: renameModalValue,
        newValue: newLabel,
        notes: payload.notes_rewritten
      });
      
      // Dispatch global refresh events
      window.dispatchEvent(new CustomEvent('lmz:refresh', { detail: { tab: 'stats' } }));
      window.dispatchEvent(new CustomEvent('lmz:refresh', { detail: { tab: 'vault' } }));
    } catch (err) {
      renameModalError = `Failed to rename topic: ${String(err)}`;
      uiLog('ERROR', 'Failed to rename topic from inspector', {
        value: renameModalValue,
        error: String(err)
      });
    } finally {
      renameModalBusy = false;
    }
  }

  let previewVideoTime = 0;

  $: if (item) {
      if (item.hash !== lastLoadedHash) loadFullDetails(item.hash);
  } else {
      clearDetails();
  }
  $: if ($runtimeSessionKey) {
      if (currentRuntimeSessionKey && currentRuntimeSessionKey !== $runtimeSessionKey) clearDetails(true);
      currentRuntimeSessionKey = $runtimeSessionKey;
  }

  $: currentIndex = group ? group.items.findIndex(i => i.hash === item?.hash) : 0;
  $: metadataDirty = !sameStringList(draftTopics, savedTopics)
    || draftWdRating !== savedWdRating
    || !sameStringList(draftWdCharacters, savedWdCharacters)
    || !sameStringList(draftWdGeneral, savedWdGeneral)
    || JSON.stringify(draftWdGroups) !== JSON.stringify(savedWdGroups);
  $: isDirty = Boolean(fullItem && (artist !== savedArtist || metadataDirty));

  async function loadFullDetails(hash: string) {
    if (abortController) abortController.abort();
    abortController = new AbortController();
    const signal = abortController.signal;
    
    if (loadingTimeout) {
      clearTimeout(loadingTimeout);
      loadingTimeout = null;
    }
    
    showLoadingIndicator = false;
    
    // Only trigger loading overlay and clear details if it takes longer than 200ms
    loadingTimeout = setTimeout(() => {
      showLoadingIndicator = true;
      fullItem = null;
    }, 200);

    try {
        const res = await apiFetch(`/api/items/${hash}`, { signal });
        if (!res.ok) throw new Error('API error');
        const detail = await res.json();

        if (loadingTimeout) {
          clearTimeout(loadingTimeout);
          loadingTimeout = null;
        }
        showLoadingIndicator = false;

        applyLoadedDetails(detail);
        lastLoadedHash = hash;
    } catch (e: any) {
        if (e.name !== 'AbortError') {
            if (loadingTimeout) {
              clearTimeout(loadingTimeout);
              loadingTimeout = null;
            }
            showLoadingIndicator = false;
            fullItem = null;
            uiLog('ERROR', 'Failed to load item details', { hash, error: String(e) });
        }
    }
  }

  function normalizeList(values: unknown): string[] {
    if (!Array.isArray(values)) return [];
    const seen = new Set<string>();
    const result: string[] = [];
    for (const value of values) {
      const clean = String(value || '').trim();
      const key = clean.toLocaleLowerCase();
      if (!clean || seen.has(key)) continue;
      seen.add(key);
      result.push(clean);
    }
    return result;
  }

  function clearDetails(abort = false) {
    if (abort) abortController?.abort();
    if (loadingTimeout) {
      clearTimeout(loadingTimeout);
      loadingTimeout = null;
    }
    showLoadingIndicator = false;
    fullItem = null;
    artist = '';
    savedArtist = '';
    sourceUrl = '';
    platform = '';
    savedTopics = [];
    draftTopics = [];
    topicInputOpen = false;
    topicInputValue = '';
    clearTopicSuggestions();
    savedWdRating = '';
    draftWdRating = '';
    savedWdCharacters = [];
    draftWdCharacters = [];
    savedWdGeneral = [];
    draftWdGeneral = [];
    savedWdGroups = {};
    draftWdGroups = {};
    tagging = false;
    lastLoadedHash = null;
  }

  function sameStringList(a: string[], b: string[]) {
    if (a.length !== b.length) return false;
    return a.every((value, index) => value === b[index]);
  }

  function applyLoadedDetails(detail: any) {
    fullItem = detail;
    savedArtist = detail.artist || '';
    artist = savedArtist;
    sourceUrl = detail.source_url || '';
    platform = detail.platform || '';
    savedTopics = normalizeList(detail.topics || []);
    draftTopics = [...savedTopics];
    savedWdRating = detail.wd_tags?.rating && detail.wd_tags.rating !== 'None' ? String(detail.wd_tags.rating) : '';
    draftWdRating = savedWdRating;
    savedWdCharacters = normalizeList(detail.wd_tags?.characters || []);
    draftWdCharacters = [...savedWdCharacters];
    savedWdGeneral = normalizeList(detail.wd_tags?.general || []);
    draftWdGeneral = [...savedWdGeneral];
    savedWdGroups = Object.fromEntries(Object.entries(detail.wd_tags?.groups || {}).map(([category, tags]) => [category, normalizeList(tags)]));
    draftWdGroups = JSON.parse(JSON.stringify(savedWdGroups));
  }

  function countFor(map: Record<string, number> | undefined, value: string) {
    const count = map?.[value];
    return typeof count === 'number' && count > 0 ? count : null;
  }

  function isTagPromoted(value: string, draft = draftTopics, saved = savedTopics) {
    if (!value) return false;
    const clean = normalizeTopicLabel(value);
    const key = clean.toLocaleLowerCase();
    return draft.some((topic) => topic.toLocaleLowerCase() === key) &&
           !saved.some((topic) => topic.toLocaleLowerCase() === key);
  }

  function normalizeTopicLabel(value: string) {
    const cleaned = String(value || '')
      .trim()
      .toLocaleLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .replace(/_+/g, '_');
    return cleaned || 'topic';
  }

  function isAlreadyTopic(value: string, saved = savedTopics) {
    if (!value) return false;
    const clean = normalizeTopicLabel(value);
    const key = clean.toLocaleLowerCase();
    return saved.some((topic) => topic.toLocaleLowerCase() === key);
  }

  function promoteWdToTopic(value: string) {
    const clean = normalizeTopicLabel(value);
    if (!clean) return;
    const key = clean.toLocaleLowerCase();
    
    // Guard: If this normalized name is already a saved topic, do nothing.
    if (savedTopics.some((topic) => topic.toLocaleLowerCase() === key)) {
      return;
    }
    
    if (draftTopics.some((topic) => topic.toLocaleLowerCase() === key)) {
      removeDraftTopic(clean);
      return;
    }
    draftTopics = [...draftTopics, clean];
  }

  function openTopicInput() {
    topicInputOpen = true;
    fetchTopicSuggestions('');
  }

  function clearTopicSuggestions() {
    if (topicSuggestionTimer !== null) {
      window.clearTimeout(topicSuggestionTimer);
      topicSuggestionTimer = null;
    }
    topicSuggestionAbortController?.abort();
    topicSuggestionAbortController = null;
    topicSuggestions = [];
    topicSuggestionsOpen = false;
    topicSuggestionsLoading = false;
    activeTopicSuggestionIndex = -1;
  }

  function normalizeTopicKey(value: string) {
    return normalizeTopicLabel(value).toLocaleLowerCase();
  }

  function availableTopicSuggestions(items: { value: string; count?: number }[]) {
    const existing = new Set(draftTopics.map((topic) => topic.toLocaleLowerCase()));
    return items
      .filter((item) => item.value && !existing.has(normalizeTopicKey(item.value)))
      .slice(0, 8);
  }

  async function fetchTopicSuggestions(query: string) {
    topicSuggestionAbortController?.abort();
    topicSuggestionAbortController = new AbortController();
    topicSuggestionsLoading = true;
    try {
      const params = new URLSearchParams({
        kind: 'topic',
        q: query.trim(),
        scope: 'all',
        limit: '8'
      });
      const response = await apiFetch(`/api/facets?${params.toString()}`, {
        signal: topicSuggestionAbortController.signal
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const items = Array.isArray(data.items) ? data.items : [];
      topicSuggestions = availableTopicSuggestions(items.map((item: any) => ({
        value: String(item.value || ''),
        count: Number(item.count || 0)
      })));
      topicSuggestionsOpen = topicInputOpen && topicSuggestions.length > 0;
      activeTopicSuggestionIndex = topicSuggestions.length > 0 ? 0 : -1;
    } catch (error: any) {
      if (error?.name !== 'AbortError') {
        topicSuggestions = [];
        topicSuggestionsOpen = false;
        uiLog('ERROR', 'Failed to load topic suggestions', { error: String(error) });
      }
    } finally {
      topicSuggestionsLoading = false;
    }
  }

  function queueTopicSuggestions() {
    if (topicSuggestionTimer !== null) window.clearTimeout(topicSuggestionTimer);
    topicSuggestionTimer = window.setTimeout(() => {
      topicSuggestionTimer = null;
      fetchTopicSuggestions(topicInputValue);
    }, 150);
  }

  function addTopicValue(value: string) {
    const clean = normalizeTopicLabel(value);
    if (!clean) return;
    const key = clean.toLocaleLowerCase();
    if (draftTopics.some((topic) => topic.toLocaleLowerCase() === key)) return;
    draftTopics = [...draftTopics, clean];
  }

  function addDraftTopic() {
    const clean = topicSuggestionsOpen && activeTopicSuggestionIndex >= 0
      ? topicSuggestions[activeTopicSuggestionIndex]?.value || topicInputValue
      : topicInputValue;
    topicInputValue = '';
    topicInputOpen = false;
    clearTopicSuggestions();
    addTopicValue(clean);
  }

  function handleTopicInputKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      addDraftTopic();
    } else if (event.key === 'ArrowDown' && topicSuggestionsOpen && topicSuggestions.length > 0) {
      event.preventDefault();
      activeTopicSuggestionIndex = (activeTopicSuggestionIndex + 1) % topicSuggestions.length;
    } else if (event.key === 'ArrowUp' && topicSuggestionsOpen && topicSuggestions.length > 0) {
      event.preventDefault();
      activeTopicSuggestionIndex = (activeTopicSuggestionIndex - 1 + topicSuggestions.length) % topicSuggestions.length;
    } else if (event.key === 'Escape') {
      event.preventDefault();
      topicInputValue = '';
      topicInputOpen = false;
      clearTopicSuggestions();
    }
  }

  function selectTopicSuggestion(value: string) {
    topicInputValue = '';
    topicInputOpen = false;
    clearTopicSuggestions();
    addTopicValue(value);
  }

  function handleTopicInputBlur() {
    if (!topicInputValue.trim()) {
      topicInputOpen = false;
      clearTopicSuggestions();
    }
  }

  function removeDraftTopic(value: string) {
    const key = String(value || '').trim().toLocaleLowerCase();
    draftTopics = draftTopics.filter((topic) => topic.toLocaleLowerCase() !== key);
  }

  function removeDraftWdTag(kind: string, value: string) {
    const key = String(value || '').trim().toLocaleLowerCase();
    if (kind === 'rating' && draftWdRating.toLocaleLowerCase() === key) {
      draftWdRating = '';
    } else if (kind === 'character') {
      draftWdCharacters = draftWdCharacters.filter((tag) => tag.toLocaleLowerCase() !== key);
    } else if (kind === 'general') {
      draftWdGeneral = draftWdGeneral.filter((tag) => tag.toLocaleLowerCase() !== key);
    } else if (draftWdGroups[kind]) {
      draftWdGroups = { ...draftWdGroups, [kind]: draftWdGroups[kind].filter((tag) => tag.toLocaleLowerCase() !== key) };
    }
  }

  async function revertChanges() {
    if (!item) return;
    await loadFullDetails(item.hash);
  }

  async function responseErrorText(response: Response, fallback: string) {
    try {
      const data = await response.json();
      return data?.detail || data?.message || fallback;
    } catch {
      return fallback;
    }
  }

  function reportActionFailure(action: string, error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    uiLog('ERROR', `${action} failed`, { error: message });
    alert(`${action} failed: ${message}`);
  }

  async function save() {
    if (!item) return;
    const savedSnapshot = {
      artist,
      topics: [...draftTopics],
      wdRating: draftWdRating,
      wdCharacters: [...draftWdCharacters],
      wdGeneral: [...draftWdGeneral],
      wdGroups: JSON.parse(JSON.stringify(draftWdGroups)) as Record<string, string[]>,
    };
    try {
      const res = await apiFetch(`/api/items/${item.hash}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          artist: savedSnapshot.artist,
          topics: savedSnapshot.topics,
          wd_rating: savedSnapshot.wdRating,
          wd_character_tags: savedSnapshot.wdCharacters,
          wd_tags: savedSnapshot.wdGeneral,
          tag_groups: savedSnapshot.wdGroups,
        })
      });
      if (!res.ok) throw new Error('Failed to save');
      const detail = await res.json();
      applyLoadedDetails(detail);
      savedArtist = savedSnapshot.artist;
      artist = savedSnapshot.artist;
      savedTopics = [...savedSnapshot.topics];
      draftTopics = [...savedSnapshot.topics];
      savedWdRating = savedSnapshot.wdRating;
      draftWdRating = savedSnapshot.wdRating;
      savedWdCharacters = [...savedSnapshot.wdCharacters];
      draftWdCharacters = [...savedSnapshot.wdCharacters];
      savedWdGeneral = [...savedSnapshot.wdGeneral];
      draftWdGeneral = [...savedSnapshot.wdGeneral];
      savedWdGroups = savedSnapshot.wdGroups;
      draftWdGroups = JSON.parse(JSON.stringify(savedWdGroups));
      if (fullItem) {
        fullItem = {
          ...fullItem,
          artist: savedSnapshot.artist,
          topics: [...savedSnapshot.topics],
          wd_tags: {
            ...(fullItem.wd_tags || {}),
            rating: savedSnapshot.wdRating || 'None',
            characters: [...savedSnapshot.wdCharacters],
            general: [...savedSnapshot.wdGeneral],
            groups: savedSnapshot.wdGroups,
          }
        };
      }
      dispatch('updated', { hash: item.hash, artist, source_url: sourceUrl, platform });
      uiLog('INFO', `Metadata saved for ${item.hash.substring(0, 12)}`);
      
      // Refresh stats facets without resetting vault selection/scroll.
      window.dispatchEvent(new CustomEvent('lmz:refresh', { detail: { tab: 'stats' } }));
    } catch (e) {
        uiLog('ERROR', 'Save failed', { error: String(e) });
        alert('Failed to save changes.');
    }
  }

  async function runTagging() {
      if (!item || tagging) return;
      const hash = item.hash;
      const session = currentRuntimeSessionKey;
      tagging = true;
      uiLog('INFO', `Manual tagging started for ${hash.substring(0, 12)}`);
      try {
          const res = await apiFetch(`/api/items/${hash}/tag`, { method: 'POST' });
          if (!res.ok) {
              const err = await res.json();
              throw new Error(err.detail || 'Tagging failed');
          }
          const detail = await res.json();
          uiLog('INFO', `Tagging complete for ${hash.substring(0, 12)}`);
          if (currentRuntimeSessionKey !== session) return;
          if (item?.hash === hash) applyLoadedDetails(detail);

          // Refresh the list without clearing the selection that shows the new tags.
          dispatch('tagged');
          window.dispatchEvent(new CustomEvent('lmz:refresh', { detail: { tab: 'stats' } }));
      } catch (e) {
          uiLog('ERROR', 'Tagging failed', { error: String(e) });
          alert(`Tagging failed: ${e}`);
      } finally {
          tagging = false;
      }
  }

  async function openFolder() {
    if (!item) return;
    try {
        const res = await apiFetch(`/api/items/${item.hash}/open_folder`, { method: 'POST' });
        if (!res.ok) throw new Error(await responseErrorText(res, `HTTP ${res.status}`));
        uiLog('INFO', `Opened folder and selected ${item.hash.substring(0, 12)}`);
    } catch (e) { reportActionFailure('Open folder', e); }
  }

  async function openMarkdown() {
    if (!item) return;
    try {
        const res = await apiFetch(`/api/items/${item.hash}/open_note`, { method: 'POST' });
        if (!res.ok) throw new Error(await responseErrorText(res, `HTTP ${res.status}`));
        uiLog('INFO', `Opened markdown note for ${item.hash.substring(0, 12)}`);
    } catch (e) { reportActionFailure('Open note', e); }
  }

  async function copyFile() {
      if (!item) return;
      try {
          const res = await apiFetch(`/api/items/${item.hash}/path`);
          if (!res.ok) throw new Error(await responseErrorText(res, `HTTP ${res.status}`));
          const data = await res.json();
          await invoke('copy_file_to_clipboard', { path: data.absolute_path });
          uiLog('INFO', `File copied to clipboard: ${item.hash.substring(0, 12)}`);
      } catch (e) { reportActionFailure('Copy file', e); }
  }

  async function deleteData() {
      if (!item) return;
      if (!await safeConfirm("Are you sure you want to permanently delete this item? This will delete the file, note, and database entry.")) return;
      try {
          const res = await apiFetch(`/api/items/${item.hash}`, { method: 'DELETE' });
          if (res.ok) {
              const result = await res.json().catch(() => ({}));
              const cleanupErrors = Array.isArray(result.cleanup_errors) ? result.cleanup_errors : [];
              if (result.status === 'warning' && result.deleted === false) {
                  uiLog('WARNING', 'Item delete did not complete', { hash: item.hash, cleanup_errors: cleanupErrors, message: result.message || '' });
                  alert(result.message || 'Item was not deleted. Check App Logs.');
                  return;
              }
              if (cleanupErrors.length > 0) {
                  uiLog('WARNING', 'Item deleted with cleanup failures', { hash: item.hash, cleanup_errors: cleanupErrors });
                  alert(`Item deleted, but ${cleanupErrors.length} cleanup operation(s) failed. Check App Logs.`);
              }
              uiLog('INFO', `Item deleted: ${item.hash}`);
              dispatch('deleted', item.hash);
              
              // Dispatch global refresh events to update sibling panels (StatsView)
              window.dispatchEvent(new CustomEvent('lmz:refresh', { detail: { tab: 'stats' } }));
              window.dispatchEvent(new CustomEvent('lmz:refresh', { detail: { tab: 'vault' } }));
          } else {
              throw new Error("Failed to delete");
          }
      } catch (e) { uiLog('ERROR', 'Delete failed', { error: String(e) }); }
  }

  function copyHash() {
    if (!item) return;
    navigator.clipboard.writeText(item.hash)
        .then(() => uiLog('DEBUG', 'Hash copied to clipboard'))
        .catch(e => uiLog('ERROR', 'Failed to copy hash', { error: String(e) }));
  }

  function toggleFocus(mode: 'wide' | 'fullscreen', startTime = previewVideoTime) {
      dispatch('focus', { mode, hash: item?.hash, startTime });
  }

  function handleKeydown(e: KeyboardEvent) {
      if (!item) return;
      if (e.repeat) return;
      const target = e.target as HTMLElement;
      if (['INPUT', 'TEXTAREA'].includes(target.tagName)) return;

      if (focusMode !== 'normal') return;

      const key = e.key.toLowerCase();
      if (key === 'a') {
          e.preventDefault();
          prevItem();
      } else if (key === 'd') {
          e.preventDefault();
          nextItem();
      } else if (key === 'w') {
          e.preventDefault();
          toggleFocus('wide');
      } else if (key === 'f') {
          e.preventDefault();
          toggleFocus('fullscreen');
      }
  }

  function nextItem() {
      if (!group) return;
      const nextIdx = (currentIndex + 1) % group.items.length;
      dispatch('changeItem', group.items[nextIdx]);
  }

  function prevItem() {
      if (!group) return;
      const prevIdx = (currentIndex - 1 + group.items.length) % group.items.length;
      dispatch('changeItem', group.items[prevIdx]);
  }

  onDestroy(() => {
      abortController?.abort();
      clearTopicSuggestions();
  });
</script>

<svelte:window on:keydown={handleKeydown} />

<aside class="inspector" style={`width: ${width}px; min-width: ${width}px;`}>
  {#if !item}
    <div class="empty-panel">
        <p>No item selected</p>
    </div>
  {:else}
    {#if showLoadingIndicator}
      <div class="loading-overlay">
          <p>Loading details...</p>
      </div>
    {/if}
    {#if fullItem}
      <InspectorMediaPreview
        {item}
        {group}
        {currentIndex}
        on:focus={(event) => toggleFocus(event.detail.mode, event.detail.startTime)}
        on:prev={prevItem}
        on:next={nextItem}
        on:time={(event) => previewVideoTime = event.detail}
      />

      <!-- Scrollable Body -->
      <div class="inspector-body">
        <InspectorMetadataGrid
          {item}
          {artist}
          {platform}
          {sourceUrl}
          on:artistChange={(event) => artist = event.detail}
          on:copyHash={copyHash}
        />

        <div class="group-container horizontal action-toolbar">
          <button class="toolbar-btn" on:click={openFolder} title="Open Folder (Reveal in File Explorer)">
            <IconFolder size={14} />
          </button>
          <button class="toolbar-btn" on:click={openMarkdown} title="Open Note (Open Markdown in Obsidian)">
            <IconFileText size={14} />
          </button>
          <button class="toolbar-btn" on:click={copyFile} title="Copy File to Clipboard">
            <IconCopy size={14} />
          </button>
          <button class="toolbar-btn delete-btn" on:click={deleteData} title="Permanently Delete Media, Note, and Database Record">
            <IconTrash size={14} />
          </button>
        </div>

        <InspectorTopicEditor
          {draftTopics}
          {savedTopics}
          topicCounts={fullItem.topic_counts}
          inputOpen={topicInputOpen}
          inputValue={topicInputValue}
          suggestions={topicSuggestions}
          suggestionsOpen={topicSuggestionsOpen}
          suggestionsLoading={topicSuggestionsLoading}
          activeSuggestionIndex={activeTopicSuggestionIndex}
          normalizeLabel={normalizeTopicLabel}
          on:openInput={openTopicInput}
          on:inputChange={(event) => topicInputValue = event.detail}
          on:queueSuggestions={queueTopicSuggestions}
          on:fetchSuggestions={() => fetchTopicSuggestions(topicInputValue)}
          on:inputKeydown={(event) => handleTopicInputKeydown(event.detail)}
          on:inputBlur={handleTopicInputBlur}
          on:add={addDraftTopic}
          on:suggestionHover={(event) => activeTopicSuggestionIndex = event.detail}
          on:selectSuggestion={(event) => selectTopicSuggestion(event.detail)}
          on:removeTopic={(event) => removeDraftTopic(event.detail)}
          on:renameTopic={(event) => openRenameTopicModal(event.detail)}
        />

        <InspectorWdSuggestions
          rating={draftWdRating}
          characters={draftWdCharacters}
          general={draftWdGeneral}
          groups={draftWdGroups}
          wdTagCounts={fullItem.wd_tag_counts}
          {draftTopics}
          {savedTopics}
          {isAlreadyTopic}
          {isTagPromoted}
          promoteHandler={promoteWdToTopic}
          removeTagHandler={removeDraftWdTag}
        />

        <div class="action-footer">
            <button class="tag-btn" on:click={runTagging} disabled={tagging}>
                <IconSparkles size={12} className="btn-icon" />
                {tagging ? 'Tagging...' : 'Tag Media'}
            </button>
            
            <div class="save-group">
                {#if isDirty}
                <button class="revert-btn" on:click={revertChanges}>
                    Revert
                </button>
                {/if}
                <button class="save-btn" on:click={save} disabled={!isDirty}>
                    Save
                </button>
            </div>
        </div>
      </div>
    {/if}
  {/if}

  <MetadataActionModal
    open={renameModalOpen}
    kind="topic"
    action="rename"
    value={renameModalValue}
    bind:newValue={renameModalNewValue}
    busy={renameModalBusy}
    result={renameModalResult}
    error={renameModalError}
    onClose={closeRenameTopicModal}
    onConfirm={confirmRenameTopic}
  />
</aside>

<style>
  .inspector {
    --inspector-scrollbar-gutter-width: var(--scrollbar-size);
    background: var(--bg-main);
    display: flex;
    flex-direction: column;
    padding: 0;
    gap: 0;
    overflow: hidden;
    position: relative;
    height: 100%;
  }



  .inspector-body {
    flex-grow: 1;
    overflow-y: auto;
    scrollbar-gutter: stable;
    padding: 0 15px 12px 15px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .group-container {
    background: var(--bg-panel);
    border: 1px solid var(--border-dim);
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  :global(.group-container.media-preview) {
    padding: 0;
    overflow: hidden;
    position: relative;
    min-height: 200px;
    max-height: 400px;
    background: #000;
  }

  :global(.media-preview img), :global(.media-preview video) {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  :global(.unsupported-media) { min-height: 200px; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 12px; }

  :global(.media-overlay) {
      position: absolute;
      top: 10px;
      right: 10px;
      display: flex;
      gap: 5px;
      opacity: 0;
  }
  :global(.media-preview:hover .media-overlay) { opacity: 1; }

  :global(.overlay-btn) {
      width: 30px;
      height: 30px;
      padding: 0;
      background: rgba(0,0,0,0.6);
      border: 1px solid rgba(255,255,255,0.2);
      color: white;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      border-radius: 6px;
  }
  :global(.overlay-btn:hover) { background: var(--accent-primary); border-color: var(--accent-primary); }

  :global(.group-nav) {
      align-items: center;
      justify-content: space-between;
      background: rgba(0, 0, 0, 0.15) !important;
      padding: 6px 12px !important;
      margin: 0;
  }
  :global(.group-nav button) {
      width: 32px;
      height: 28px;
      padding: 0;
      display: inline-grid;
      place-items: center;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.08);
      color: var(--text-main);
      border-radius: 6px;
      cursor: pointer;
  }
  :global(.group-nav button:hover:not(:disabled)) {
      background: rgba(255, 255, 255, 0.1);
      border-color: rgba(255, 255, 255, 0.2);
      color: var(--text-bright);
  }
  :global(.group-nav button:disabled) {
      opacity: 0.3;
      cursor: not-allowed;
  }
  :global(.group-nav .counter) {
      font-size: 11px;
      font-weight: bold;
      color: #8b949e;
      display: flex;
      align-items: center;
      gap: 4px;
  }
  :global(.group-nav .active-index) {
      color: #8b949e;
      font-weight: bold;
  }

  .horizontal { flex-direction: row; gap: 20px; }

  /* Metadata Grid Styling */
  :global(.metadata-grid) {
    display: grid;
    grid-template-columns: 80px 1fr;
    row-gap: 6px;
    column-gap: 12px;
    align-items: center;
    background: var(--bg-panel);
    border: 1px solid var(--border-dim);
    border-radius: 8px;
    padding: 10px 12px;
  }

  :global(.grid-label) {
    font-size: 10px;
    color: var(--text-muted);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    user-select: none;
  }

  :global(.grid-value) {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  :global(input.inline-input) {
    width: 100%;
    height: 24px;
    padding: 2px 6px;
    margin-left: -6px; /* Offset padding exactly so text left-aligns with non-input text */
    background: transparent !important;
    border: 1px solid transparent !important;
    border-radius: 4px;
    color: var(--text-main);
    font-size: 12px;
    font-weight: 500;
    transition: none !important;
    box-shadow: none !important;
  }

  :global(input.inline-input:hover) {
    background: rgba(255, 255, 255, 0.03) !important;
    border-color: rgba(255, 255, 255, 0.08) !important;
  }

  :global(input.inline-input:focus) {
    background: var(--bg-input) !important;
    border-color: var(--accent-purple) !important;
    outline: none !important;
  }

  :global(input.inline-input.read-only-input) {
    cursor: default;
    color: var(--text-muted);
  }

  :global(input.inline-input.read-only-input:hover) {
    background: transparent !important;
    border-color: transparent !important;
  }

  :global(input.inline-input.read-only-input:focus) {
    background: transparent !important;
    border-color: transparent !important;
  }

  :global(.platform-row) {
    display: flex;
    align-items: center;
  }

  :global(.platform-text) {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-main);
  }

  :global(.source-row) {
    display: flex;
    align-items: center;
    width: 100%;
    gap: 4px;
    min-width: 0;
  }

  :global(.link-icon-btn) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--accent-primary);
    opacity: 0.8;
    cursor: pointer;
    padding: 4px;
    border-radius: 4px;
    flex-shrink: 0;
  }

  :global(.link-icon-btn:hover) {
    opacity: 1;
    background: rgba(31, 111, 235, 0.1);
    color: var(--text-bright);
  }

  :global(.hash-row) {
    display: flex;
    align-items: center;
    width: 100%;
    gap: 6px;
    min-width: 0;
  }

  :global(.hash-text) {
    font-family: monospace;
    font-size: 11px;
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex-grow: 1;
  }

  :global(.icon-btn-compact) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    padding: 0;
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    border-radius: 3px;
    flex-shrink: 0;
  }

  :global(.icon-btn-compact:hover) {
    background: rgba(255, 255, 255, 0.08);
    color: var(--text-bright);
  }

  .action-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    gap: 12px;
    margin-top: 4px;
  }

  .save-group {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .tag-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 28px;
    padding: 0 10px;
    font-size: 11px;
    font-weight: 600;
    background: rgba(163, 113, 247, 0.08);
    border: 1px solid rgba(163, 113, 247, 0.3);
    border-radius: 6px;
    color: var(--accent-purple);
    cursor: pointer;
    transition: none !important;
  }

  .tag-btn:hover:not(:disabled) {
    background: rgba(163, 113, 247, 0.15);
    border-color: var(--accent-purple);
    color: var(--text-bright);
  }

  .tag-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .revert-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 28px;
    padding: 0 10px;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    color: var(--text-muted);
    cursor: pointer;
    transition: none !important;
  }

  .revert-btn:hover {
    color: var(--text-bright);
    background: rgba(255, 255, 255, 0.05);
  }

  .save-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 28px;
    padding: 0 14px;
    font-size: 11px;
    font-weight: 700;
    background: var(--accent-purple);
    border: 1px solid var(--accent-purple);
    border-radius: 6px;
    color: #ffffff;
    cursor: pointer;
    transition: none !important;
  }

  .save-btn:hover:not(:disabled) {
    background: #b085ff;
    border-color: #b085ff;
  }

  .save-btn:disabled {
    background: rgba(255, 255, 255, 0.05);
    border-color: var(--border-dim);
    color: var(--text-muted);
    cursor: not-allowed;
    font-weight: 600;
  }

  :global(.btn-icon) {
    margin-right: 4px;
    flex-shrink: 0;
  }

  .loading-overlay {
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10;
    border-radius: 8px;
    color: var(--text-muted);
  }

  .empty-panel {
    flex-grow: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
  }

  /* Sleek Horizontal Action Toolbar */
  .action-toolbar {
    flex-direction: row !important;
    gap: 6px !important;
    padding: 6px 8px !important;
    align-items: center;
  }

  .toolbar-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-grow: 1;
    height: 30px;
    padding: 0;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--border-dim);
    border-radius: 6px;
    color: var(--text-main);
    cursor: pointer;
    transition: none !important;
  }

  .toolbar-btn:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.2);
    color: var(--text-bright);
  }

  .toolbar-btn.delete-btn {
    color: var(--text-muted);
  }

  .toolbar-btn.delete-btn:hover {
    background: rgba(248, 81, 73, 0.15);
    border-color: rgba(248, 81, 73, 0.3);
    color: var(--accent-danger);
  }
</style>

