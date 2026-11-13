<script lang="ts">
  import type { VaultItem } from './types';
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { apiUrl } from './api';
  import { isImageMedia, isVideoMedia } from './media';
  import { IconChevronLeft, IconChevronRight } from './icons';
  
  export let group: { id: string, items: VaultItem[] };
  export let selectedHash: string | undefined = '';
  export let selectedHashes: Set<string> = new Set();
  export let layout: 'masonry' | 'grid' = 'masonry';
  export let activeIndex = 0;
  export let eagerImages = false;
  
  const dispatch = createEventDispatcher();

  $: index = Math.min(Math.max(activeIndex || 0, 0), Math.max(0, group.items.length - 1));
  $: current = group.items[index];
  $: thumbnailUrl = current ? apiUrl(current.thumbnail_url) : '';
  $: fullUrl = current ? apiUrl(current.url) : '';
  let thumbnailObjectUrl = '';
  let thumbnailReady = false;
  let thumbFailed = false;
  let activeThumbnailUrl = '';
  let thumbnailController: AbortController | null = null;
  let thumbnailRequestId = 0;

  const MAX_THUMBNAIL_ATTEMPTS = 5;
  const MAX_RETRY_DELAY_MS = 5000;

  function retryDelay(response: Response) {
    const retryAfter = response.headers.get('Retry-After');
    if (!retryAfter) return 1000;
    const seconds = Number(retryAfter);
    const delayMs = Number.isFinite(seconds)
      ? seconds * 1000
      : Date.parse(retryAfter) - Date.now();
    return Math.max(0, Math.min(MAX_RETRY_DELAY_MS, Number.isFinite(delayMs) ? delayMs : 1000));
  }

  function waitForRetry(ms: number, signal: AbortSignal) {
    return new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        signal.removeEventListener('abort', onAbort);
        resolve();
      }, ms);
      const onAbort = () => {
        window.clearTimeout(timer);
        reject(new DOMException('The operation was aborted.', 'AbortError'));
      };
      signal.addEventListener('abort', onAbort, { once: true });
    });
  }

  function resetThumbnail(url: string) {
    thumbnailController?.abort();
    thumbnailController = null;
    if (thumbnailObjectUrl) URL.revokeObjectURL(thumbnailObjectUrl);
    thumbnailObjectUrl = '';
    thumbnailReady = false;
    thumbFailed = false;
    activeThumbnailUrl = url;
    thumbnailRequestId += 1;
  }

  async function handleThumbError() {
    thumbFailed = true;
    const url = thumbnailUrl;
    const requestId = ++thumbnailRequestId;
    const controller = new AbortController();
    thumbnailController?.abort();
    thumbnailController = controller;

    try {
      for (let attempt = 0; attempt < MAX_THUMBNAIL_ATTEMPTS; attempt += 1) {
        const response = await fetch(url, { signal: controller.signal, cache: 'no-store' });
        if (response.status === 503 && attempt < MAX_THUMBNAIL_ATTEMPTS - 1) {
          await waitForRetry(retryDelay(response), controller.signal);
          continue;
        }
        if (!response.ok) return;
        const objectUrl = URL.createObjectURL(await response.blob());
        if (requestId !== thumbnailRequestId || controller.signal.aborted) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        thumbnailObjectUrl = objectUrl;
        thumbnailReady = true;
        thumbFailed = false;
        return;
      }
    } catch {
      // A cancelled or failed request does not replace the placeholder.
    }
  }

  function handleBlobThumbError() {
    if (thumbnailObjectUrl) URL.revokeObjectURL(thumbnailObjectUrl);
    thumbnailObjectUrl = '';
    thumbnailReady = false;
    thumbFailed = true;
  }

  $: if (thumbnailUrl !== activeThumbnailUrl) resetThumbnail(thumbnailUrl);
  onDestroy(() => {
    thumbnailController?.abort();
    if (thumbnailObjectUrl) URL.revokeObjectURL(thumbnailObjectUrl);
  });
  $: isSelected = group.items.some(item => item.hash === selectedHash || selectedHashes.has(item.hash));
  $: aspectStyle = (current?.width && current?.height)
    ? `aspect-ratio: ${current.width} / ${current.height}`
    : '';

  function next(e: MouseEvent) {
    e.stopPropagation();
    const nextIndex = (index + 1) % group.items.length;
    dispatch('indexChange', { groupId: group.id, index: nextIndex });
  }

  function prev(e: MouseEvent) {
    e.stopPropagation();
    const nextIndex = (index - 1 + group.items.length) % group.items.length;
    dispatch('indexChange', { groupId: group.id, index: nextIndex });
  }

  function select(event: MouseEvent) {
    if (current) dispatch('select', { item: current, event });
  }

  function playVideo(e: MouseEvent & { currentTarget: HTMLVideoElement }) {
    e.currentTarget.play().catch(() => {});
  }

  function pauseVideo(e: MouseEvent & { currentTarget: HTMLVideoElement }) {
    const v = e.currentTarget;
    v.pause();
    v.currentTime = 0;
  }
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="tile-group {layout}" data-testid="vault-tile" class:selected={isSelected} on:click={select} on:keydown={(e) => { if (e.key === 'Enter' && current) dispatch('select', { item: current }); }} role="button" tabindex="0">
    <div class="media-stack" style={layout !== 'grid' ? aspectStyle : ''}>
        {#if current && isImageMedia(current)}
            {#if thumbnailReady}
                <img src={thumbnailObjectUrl} alt="Vault Item"
                     width={current.width || undefined} height={current.height || undefined}
                     on:error={handleBlobThumbError} />
            {:else if !thumbFailed}
                <img src={thumbnailUrl} alt="Vault Item" loading={eagerImages ? 'eager' : 'lazy'}
                     width={current.width || undefined} height={current.height || undefined}
                     on:error={handleThumbError} />
            {:else}
                <div class="thumbnail-placeholder" aria-label="Thumbnail unavailable">No Preview</div>
            {/if}
        {:else if current && isVideoMedia(current)}
            <!-- svelte-ignore a11y-media-has-caption -->
            <video src={fullUrl} poster={thumbnailUrl} preload="none" muted loop
                   on:mouseenter={playVideo} on:mouseleave={pauseVideo}></video>
        {:else}
            <div class="unsupported-media">Unknown media</div>
        {/if}

        {#if group.items.length > 1}
            <div class="controls">
                <button class="nav-btn" on:click={prev} title="Previous Item">
                    <IconChevronLeft size={11} strokeWidth={3} />
                </button>
                <div class="counter">{index + 1} / {group.items.length}</div>
                <button class="nav-btn" on:click={next} title="Next Item">
                    <IconChevronRight size={11} strokeWidth={3} />
                </button>
            </div>
        {/if}
    </div>

    <div class="info">
        <span class="hash">{current ? current.hash.substring(0, 12) : ''}</span>
        <span class="artist">{current?.artist || 'Unknown'}</span>
    </div>
</div>

<style>
  .tile-group {
    background: var(--bg-panel);
    border: 2px solid transparent;
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 12px;
    break-inside: avoid;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    content-visibility: auto;
  }

  .tile-group.grid {
    margin-bottom: 0;
    height: 100%;
  }

  .tile-group:hover { border-color: var(--border-hover); }
  .tile-group.selected {
    border-color: #58a6ff;
  }

  .media-stack { position: relative; width: 100%; background: #000; min-height: 100px; display: flex; align-items: center; justify-content: center; }

  .thumbnail-placeholder { width: 100%; min-height: 100px; display: flex; align-items: center; justify-content: center; color: #555; font: 10px sans-serif; }
  
  .tile-group.grid .media-stack {
    flex-grow: 1;
    aspect-ratio: 1 / 1;
  }

  img, video { width: 100%; display: block; height: auto; }
  
  .tile-group.grid img, .tile-group.grid video {
    height: 100%;
    width: 100%;
    object-fit: cover;
  }

  .controls {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    background: rgba(0,0,0,0.7);
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 2px;
    opacity: 0;
  }

  .tile-group:hover .controls { opacity: 1; }

  .nav-btn {
    background: #161b22;
    border: 1px solid #30363d;
    color: white;
    width: 24px;
    height: 20px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    cursor: pointer;
    border-radius: 4px;
  }
  .nav-btn:hover { border-color: var(--accent-primary); }

  .nav-btn :global(svg) {
    display: block;
  }

  .counter { font-size: 10px; color: #8b949e; font-weight: bold; }
  .unsupported-media { min-height: 100px; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 11px; }

  .info {
    padding: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11px;
    flex-shrink: 0;
  }

  .hash { color: var(--text-muted); font-family: monospace; }
  .artist { color: var(--accent-purple); font-weight: bold; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60%; text-align: right; }

  .tile-group.selected .info {
    background: rgba(31, 111, 235, 0.15);
  }
  .tile-group.selected .hash {
    color: #79c0ff;
  }
  .tile-group.selected .artist {
    color: #58a6ff;
  }
</style>
