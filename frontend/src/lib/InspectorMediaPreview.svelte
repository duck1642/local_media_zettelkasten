<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { VaultItem } from './types';
  import { apiUrl } from './api';
  import { retryThumbnail } from './retryThumbnail';
  import { isImageMedia, isVideoMedia } from './media';
  import { IconChevronLeft, IconChevronRight, IconExpand, IconWide } from './icons';

  export let item: VaultItem;
  export let group: { id: string, items: VaultItem[] } | null = null;
  export let currentIndex = 0;

  const dispatch = createEventDispatcher<{
    focus: { mode: 'wide' | 'fullscreen'; startTime: number };
    prev: void;
    next: void;
    time: number;
  }>();

  let videoElement: HTMLVideoElement | undefined;

  function emitFocus(mode: 'wide' | 'fullscreen') {
    dispatch('focus', { mode, startTime: videoElement ? videoElement.currentTime : 0 });
  }

  function emitVideoTime() {
    if (videoElement) dispatch('time', videoElement.currentTime || 0);
  }
</script>

<div class="inspector-header">
  <div class="group-container media-preview">
      {#if isImageMedia(item)}
          <img src={apiUrl(item.thumbnail_url || item.url)}
               use:retryThumbnail={item.thumbnail_url ? apiUrl(item.thumbnail_url) : ''} alt="Preview" />
      {:else if isVideoMedia(item)}
          <!-- svelte-ignore a11y-media-has-caption -->
          <video
              bind:this={videoElement}
              src={apiUrl(item.url)}
              controls
              controlslist="nofullscreen"
              muted
              loop
              autoplay
              on:loadedmetadata={emitVideoTime}
              on:timeupdate={emitVideoTime}
          ></video>
      {:else}
          <div class="unsupported-media">Unknown media type</div>
      {/if}
      <div class="media-overlay">
          <button class="overlay-btn" title="Wide View" on:click={() => emitFocus('wide')}>
              <IconWide size={13} />
          </button>
          <button class="overlay-btn" title="Fullscreen" on:click={() => emitFocus('fullscreen')}>
              <IconExpand size={13} />
          </button>
      </div>
  </div>

  {#if group && group.items.length > 1}
      <div class="group-nav group-container horizontal">
          <button on:click={() => dispatch('prev')} title="Previous Item">
              <IconChevronLeft size={12} />
          </button>
          <div class="counter">
              <span class="active-index">{currentIndex + 1}</span>
              <span class="sep">/</span>
              <span class="total-count">{group.items.length}</span>
          </div>
          <button on:click={() => dispatch('next')} title="Next Item">
              <IconChevronRight size={12} />
          </button>
      </div>
  {/if}
</div>

<style>
  .inspector-header {
    display: flex;
    flex-direction: column;
    padding: 15px calc(15px + var(--inspector-scrollbar-gutter-width, 12px)) 12px 15px;
    gap: 12px;
    flex-shrink: 0;
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

  .group-container.media-preview {
    padding: 0;
    overflow: hidden;
    position: relative;
    min-height: 200px;
    max-height: 400px;
    background: #000;
  }

  .media-preview img, .media-preview video {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  .unsupported-media { min-height: 200px; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 12px; }

  .media-overlay {
      position: absolute;
      top: 10px;
      right: 10px;
      display: flex;
      gap: 5px;
      opacity: 0;
  }
  .media-preview:hover .media-overlay { opacity: 1; }

  .overlay-btn {
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
  .overlay-btn:hover { background: var(--accent-primary); border-color: var(--accent-primary); }

  .group-nav {
      align-items: center;
      justify-content: space-between;
      background: rgba(0, 0, 0, 0.15) !important;
      padding: 6px 12px !important;
      margin: 0;
  }
  .group-nav button {
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
  .group-nav button:hover:not(:disabled) {
      background: rgba(255, 255, 255, 0.1);
      border-color: rgba(255, 255, 255, 0.2);
      color: var(--text-bright);
  }
  .group-nav button:disabled {
      opacity: 0.3;
      cursor: not-allowed;
  }
  .group-nav .counter {
      font-size: 11px;
      font-weight: bold;
      color: #8b949e;
      display: flex;
      align-items: center;
      gap: 4px;
  }
  .group-nav .active-index {
      color: #8b949e;
      font-weight: bold;
  }

  .horizontal { flex-direction: row; gap: 20px; }
</style>
