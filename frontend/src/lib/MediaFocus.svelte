<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import { log as uiLog } from './logger';
  import { apiUrl } from './api';
  import { retryThumbnail } from './retryThumbnail';
  import { isImageMedia, isVideoMedia } from './media';
  import {
    IconChevronLeft,
    IconChevronRight,
    IconChevronUp,
    IconClose,
    IconKeyboard,
    IconMaximizeDiagonal,
    IconMinimizeDiagonal,
    IconMinus,
    IconPlus,
    IconEye
  } from './icons';

  export let item: any;
  export let group: any = null;
  export let mode: 'wide' | 'fullscreen' = 'wide';
  export let startTime: number = 0;
  
  const dispatch = createEventDispatcher();
  function currentWindowSafe() {
    try {
      return getCurrentWindow();
    } catch {
      return null;
    }
  }

  const appWindow = currentWindowSafe();
  const MIN_SCALE = 1;
  const MAX_SCALE = 6;
  const KEYBOARD_STEP = 0.25;
  const DRAG_CLICK_SUPPRESSION_MS = 120;

  let videoElement: HTMLVideoElement;
  let mediaFrameEl: HTMLElement;
  let scale = 1;
  let translateX = 0;
  let translateY = 0;
  let isDragging = false;
  let pointerMoved = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragOriginX = 0;
  let dragOriginY = 0;
  let pointerMovedResetTimer: number | null = null;
  let filmstripOpen = false;
  let appliedMode: 'wide' | 'fullscreen' | '' = '';
  let activeHash = '';
  let showShortcutsLegend = false;
  let uiHidden = false;
  let controlsVisible = true;
  let controlsTimeout: number | null = null;

  $: assetUrl = item?.url ? apiUrl(item.url) : '';
  $: currentIndex = group ? group.items.findIndex((i: any) => i.hash === item?.hash) : 0;
  $: hasGroupFilmstrip = Boolean(group && group.items?.length > 1);
  $: mediaTransform = mode === 'fullscreen' ? `translate(${translateX}px, ${translateY}px) scale(${scale})` : 'none';
  
  $: if (item?.hash && item.hash !== activeHash) {
    activeHash = item.hash;
    resetZoom();
  }
  
  $: if (mode !== appliedMode) {
    appliedMode = mode;
    if (mode !== 'fullscreen') resetZoom();
    handleModeChange(mode);
  }

  // Automatically, snappily scroll the active thumbnail into the center of the filmstrip view
  $: if (filmstripOpen && item && typeof document !== 'undefined') {
    setTimeout(() => {
      const activeEl = document.querySelector('.filmstrip-thumb.active');
      activeEl?.scrollIntoView({
        behavior: 'auto',
        block: 'nearest',
        inline: 'center'
      });
    }, 0);
  }

  let disableFilmstripClicks = false;
  function handleToggleFilmstrip() {
    if (!filmstripOpen) {
      disableFilmstripClicks = true;
      window.setTimeout(() => {
        disableFilmstripClicks = false;
      }, 150);
    }
    filmstripOpen = !filmstripOpen;
  }

  // Auto-hide controls in fullscreen mode on mouse inactivity
  function resetControlsTimeout() {
    controlsVisible = true;
    if (controlsTimeout) {
      window.clearTimeout(controlsTimeout);
    }
    // Only auto-hide in fullscreen mode when not dragging, not zoomed, and filmstrip is closed
    if (mode === 'fullscreen' && !isDragging && scale === 1 && !filmstripOpen && !showShortcutsLegend) {
      controlsTimeout = window.setTimeout(() => {
        controlsVisible = false;
      }, 3000);
    }
  }

  function clampScale(value: number) {
    return Math.max(MIN_SCALE, Math.min(MAX_SCALE, value));
  }

  function resetZoom() {
    scale = 1;
    translateX = 0;
    translateY = 0;
    isDragging = false;
    pointerMoved = false;
  }

  function zoomAt(clientX: number, clientY: number, nextScaleValue: number) {
    if (mode !== 'fullscreen' || !mediaFrameEl) return;
    const nextScale = clampScale(nextScaleValue);
    if (nextScale === scale) return;
    const rect = mediaFrameEl.getBoundingClientRect();
    const anchorX = clientX - rect.left - rect.width / 2;
    const anchorY = clientY - rect.top - rect.height / 2;
    const ratio = nextScale / scale;
    translateX = anchorX - (anchorX - translateX) * ratio;
    translateY = anchorY - (anchorY - translateY) * ratio;
    scale = nextScale;
    if (scale === 1) {
      translateX = 0;
      translateY = 0;
      pointerMoved = false;
    }
    resetControlsTimeout();
  }

  function zoomFromCenter(delta: number) {
    if (!mediaFrameEl) return;
    const rect = mediaFrameEl.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, scale + delta);
  }

  function handleWheel(event: WheelEvent) {
    if (mode !== 'fullscreen' || !event.ctrlKey) return;
    event.preventDefault();
    const nextScale = event.deltaY < 0 ? scale * 1.12 : scale / 1.12;
    zoomAt(event.clientX, event.clientY, nextScale);
  }

  function handlePointerDown(event: PointerEvent) {
    if (mode !== 'fullscreen' || scale <= 1) return;
    const target = event.target as HTMLElement;
    if (target.closest('button, input, select, textarea') || target.tagName === 'VIDEO') return;
    if (pointerMovedResetTimer !== null) {
      window.clearTimeout(pointerMovedResetTimer);
      pointerMovedResetTimer = null;
    }
    event.preventDefault();
    isDragging = true;
    pointerMoved = false;
    dragStartX = event.clientX;
    dragStartY = event.clientY;
    dragOriginX = translateX;
    dragOriginY = translateY;
    mediaFrameEl?.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(event: PointerEvent) {
    if (!isDragging) return;
    const dx = event.clientX - dragStartX;
    const dy = event.clientY - dragStartY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) pointerMoved = true;
    translateX = dragOriginX + dx;
    translateY = dragOriginY + dy;
  }

  function handlePointerUp(event: PointerEvent) {
    if (!isDragging) return;
    isDragging = false;
    mediaFrameEl?.releasePointerCapture(event.pointerId);
    pointerMovedResetTimer = window.setTimeout(() => {
      pointerMoved = false;
      pointerMovedResetTimer = null;
    }, DRAG_CLICK_SUPPRESSION_MS);
    resetControlsTimeout();
  }

  function nextItem() {
    if (!group) return;
    const nextIdx = (currentIndex + 1) % group.items.length;
    dispatch('changeItem', group.items[nextIdx]);
    resetControlsTimeout();
  }

  function prevItem() {
    if (!group) return;
    const prevIdx = (currentIndex - 1 + group.items.length) % group.items.length;
    dispatch('changeItem', group.items[prevIdx]);
    resetControlsTimeout();
  }

  function changeTo(entry: any) {
    dispatch('changeItem', entry);
    resetControlsTimeout();
  }

  async function handleModeChange(newMode: 'wide' | 'fullscreen') {
    uiLog('INFO', `MediaFocus mode changed to: ${newMode}`);
    resetControlsTimeout();
    if (newMode === 'fullscreen') {
      const tauriOk = await setTauriFullscreen(true);
      if (!tauriOk || !document.fullscreenElement) await setBrowserFullscreen(true);
      return;
    }
    await exitAllFullscreen();
  }

  async function setTauriFullscreen(enabled: boolean) {
    if (!appWindow || !(window as any).__TAURI_INTERNALS__) return false;
    try {
      await appWindow.setFullscreen(enabled);
      return true;
    } catch (error) {
      uiLog('WARNING', 'Failed to toggle native fullscreen', { enabled, error: String(error) });
      return false;
    }
  }

  async function setBrowserFullscreen(enabled: boolean) {
    try {
      if (enabled) {
        if (document.fullscreenElement) return true;
        if (!document.documentElement.requestFullscreen) return false;
        await document.documentElement.requestFullscreen();
        return true;
      }
      if (!document.fullscreenElement) return true;
      if (!document.exitFullscreen) return false;
      await document.exitFullscreen();
      return true;
    } catch (error) {
      uiLog('WARNING', 'Failed to toggle browser fullscreen', { enabled, error: String(error) });
      return false;
    }
  }

  async function exitAllFullscreen() {
    await setTauriFullscreen(false);
    await setBrowserFullscreen(false);
  }

  async function close(reason: string | Event = 'unknown') {
    const reasonStr = typeof reason === 'string' ? reason : 'overlay_or_btn_click';
    uiLog('INFO', `MediaFocus closing because: ${reasonStr}`);
    try {
      await exitAllFullscreen();
    } catch {
    } finally {
      dispatch('close');
    }
  }

  function handleOverlayClick(event: MouseEvent) {
    if (event.target !== event.currentTarget) return;
    if (mode === 'fullscreen' && scale !== 1) return;
    if (pointerMoved) return;
    close(event);
  }

  function toggleMode() {
    const targetMode = mode === 'fullscreen' ? 'wide' : 'fullscreen';
    dispatch('switchMode', targetMode);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.repeat) return;
    const target = e.target as HTMLElement;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;

    if (e.key === 'Escape') {
      e.preventDefault();
      close('Escape Key Pressed');
    } else if (mode === 'fullscreen' && (e.key === '+' || e.key === '=' || e.code === 'NumpadAdd')) {
      e.preventDefault();
      zoomFromCenter(KEYBOARD_STEP);
    } else if (mode === 'fullscreen' && (e.key === '-' || e.code === 'NumpadSubtract')) {
      e.preventDefault();
      zoomFromCenter(-KEYBOARD_STEP);
    } else if (e.key.toLowerCase() === 'w') {
      e.preventDefault();
      if (mode === 'wide') {
        close('W Key Pressed (Toggle Off Wide)');
      } else {
        dispatch('switchMode', 'wide');
      }
    } else if (e.key.toLowerCase() === 'f') {
      e.preventDefault();
      if (mode === 'fullscreen') {
        close('F Key Pressed (Toggle Off Fullscreen)');
      } else {
        dispatch('switchMode', 'fullscreen');
      }
    } else if (e.key.toLowerCase() === 's') {
      if (hasGroupFilmstrip) {
        e.preventDefault();
        handleToggleFilmstrip();
      }
    } else if (e.key.toLowerCase() === 'h') {
      e.preventDefault();
      uiHidden = !uiHidden;
    } else if (e.key.toLowerCase() === 'a') {
      e.preventDefault();
      prevItem();
    } else if (e.key.toLowerCase() === 'd') {
      e.preventDefault();
      nextItem();
    }
  }

  function handleLoaded() {
    if (videoElement && startTime > 0) {
      videoElement.currentTime = startTime;
    }
  }

  function thumbnailUrl(entry: any) {
    return apiUrl(entry.thumbnail_url || entry.url);
  }

  onMount(() => {
    resetControlsTimeout();
  });

  onDestroy(async () => {
    if (pointerMovedResetTimer !== null) window.clearTimeout(pointerMovedResetTimer);
    if (controlsTimeout !== null) window.clearTimeout(controlsTimeout);
    await exitAllFullscreen();
  });
</script>

<svelte:window on:keydown={handleKeydown}/>

{#if uiHidden}
  <button class="show-ui-btn" title="Show UI (H)" on:click={() => uiHidden = false}>
    <IconEye size={18} strokeWidth={2.2} />
  </button>
{/if}

<!-- svelte-ignore a11y-click-events-have-key-events -->
<!-- svelte-ignore a11y-no-static-element-interactions -->
<div
  class="focus-overlay"
  class:fullscreen={mode === 'fullscreen'}
  class:hide-cursor={mode === 'fullscreen' && !controlsVisible && scale === 1}
  class:filmstrip-open={filmstripOpen && hasGroupFilmstrip}
  on:click={handleOverlayClick}
  on:wheel={handleWheel}
  on:mousemove={resetControlsTimeout}
  role="button"
  tabindex="-1"
>
  <!-- Premium Header Controls Bar -->
  <header class="focus-header" class:hidden={!controlsVisible || uiHidden}>
    <div class="header-left">
      <span class="media-title truncate" title={item?.title || item?.storage_id || item?.hash}>
        {item?.title || item?.storage_id || item?.hash?.slice(0, 8)}
      </span>
      {#if group}
        <span class="group-badge">
          {currentIndex + 1} / {group.items.length}
        </span>
      {/if}
    </div>

    <div class="header-right">
      <!-- Interactive Zoom Indicator & Controls -->
      {#if mode === 'fullscreen' && isImageMedia(item)}
        <div class="zoom-controls">
          <button class="icon-btn" title="Zoom Out (-)" on:click={() => zoomFromCenter(-KEYBOARD_STEP)}>
            <IconMinus size={14} />
          </button>
          <button type="button" class="zoom-pill" class:active={scale > 1} on:click={resetZoom} title="Reset Zoom">
            {Math.round(scale * 100)}%
          </button>
          <button class="icon-btn" title="Zoom In (+)" on:click={() => zoomFromCenter(KEYBOARD_STEP)}>
            <IconPlus size={14} />
          </button>
        </div>
      {/if}

      <!-- Hide UI Toggle -->
      <button class="icon-btn" title="Hide UI (H)" on:click={() => uiHidden = true}>
        <IconEye size={18} strokeWidth={2.2} />
      </button>

      <!-- Keyboard Shortcuts HUD Toggle -->
      <button class="icon-btn" class:active={showShortcutsLegend} title="Keyboard Shortcuts" on:click={() => showShortcutsLegend = !showShortcutsLegend}>
        <IconKeyboard size={18} strokeWidth={2.2} />
      </button>

      <!-- Fullscreen / Wide Toggle -->
      <button class="icon-btn" title={mode === 'fullscreen' ? 'Exit Fullscreen (F)' : 'Enter Fullscreen (F)'} on:click={toggleMode}>
        {#if mode === 'fullscreen'}
          <IconMinimizeDiagonal size={18} />
        {:else}
          <IconMaximizeDiagonal size={18} />
        {/if}
      </button>

      <!-- Crisp Close Button -->
      <button class="close-btn-header" title="Exit (Esc)" on:click={() => close('Close Button Header Clicked')}>
        <IconClose size={18} />
      </button>
    </div>
  </header>

  <!-- Keyboard Shortcuts Floating HUD list -->
  {#if showShortcutsLegend && !uiHidden}
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div class="shortcuts-legend" on:click|stopPropagation on:keydown|stopPropagation>
      <h3>Keyboard Shortcuts</h3>
      <div class="legend-grid">
        <div class="legend-item"><kbd>Esc</kbd> <span>Close focus view</span></div>
        <div class="legend-item"><kbd>A</kbd> <span>Previous item</span></div>
        <div class="legend-item"><kbd>D</kbd> <span>Next item</span></div>
        <div class="legend-item"><kbd>W</kbd> <span>Toggle Wide mode</span></div>
        <div class="legend-item"><kbd>F</kbd> <span>Toggle Fullscreen</span></div>
        <div class="legend-item"><kbd>S</kbd> <span>Toggle Filmstrip</span></div>
        <div class="legend-item"><kbd>H</kbd> <span>Hide UI</span></div>
        <div class="legend-item">
          <div class="keys">
            <kbd>+</kbd>
            <span>/</span>
            <kbd>-</kbd>
          </div>
          <span>Zoom in / out</span>
        </div>
        <div class="legend-item"><kbd>Ctrl + Wheel</kbd> <span>Smooth zoom</span></div>
        <div class="legend-item"><kbd>Dbl Click</kbd> <span>Reset zoom</span></div>
      </div>
    </div>
  {/if}

  <!-- Beautiful Circular Glass Navigation Arrows -->
  {#if hasGroupFilmstrip}
    <button class="nav-btn-rect prev" class:hidden={!controlsVisible || uiHidden} aria-label="Previous item" on:click|stopPropagation={prevItem}>
      <IconChevronLeft size={24} strokeWidth={3} />
    </button>
    <button class="nav-btn-rect next" class:hidden={!controlsVisible || uiHidden} aria-label="Next item" on:click|stopPropagation={nextItem}>
      <IconChevronRight size={24} strokeWidth={3} />
    </button>
  {/if}

  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="media-container" class:filmstrip-open={filmstripOpen && hasGroupFilmstrip} on:click|stopPropagation on:keydown|stopPropagation>
    <div
      class="media-frame"
      class:zoomed={mode === 'fullscreen' && scale > 1}
      class:dragging={isDragging}
      bind:this={mediaFrameEl}
      style={`transform: ${mediaTransform};`}
      on:pointerdown={handlePointerDown}
      on:pointermove={handlePointerMove}
      on:pointerup={handlePointerUp}
      on:pointercancel={handlePointerUp}
      on:dblclick|preventDefault={resetZoom}
    >
      {#if isImageMedia(item)}
        <img class="focus-media" src={assetUrl} alt="Focused View" draggable="false" />
      {:else if isVideoMedia(item)}
        <!-- svelte-ignore a11y-media-has-caption -->
        <video 
          class="focus-media"
          bind:this={videoElement}
          src={assetUrl} 
          controls 
          controlslist="nofullscreen"
          autoplay 
          loop
          on:loadedmetadata={handleLoaded}
        ></video>
      {:else}
        <div class="unsupported-media">Unknown media type</div>
      {/if}
    </div>
  </div>

  <!-- Premium Filmstrip Controls -->
  {#if hasGroupFilmstrip}
    <button class="filmstrip-toggle" class:hidden={!controlsVisible || uiHidden} class:open={filmstripOpen} aria-label="Toggle filmstrip" on:click|stopPropagation={handleToggleFilmstrip}>
      <span class="toggle-text">{filmstripOpen ? 'Hide Filmstrip' : 'Show Filmstrip'}</span>
      <IconChevronUp size={14} className="chevron-icon" />
    </button>

    {#if filmstripOpen}
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div class="filmstrip" class:hidden={!controlsVisible || uiHidden} class:disable-clicks={disableFilmstripClicks} on:click|stopPropagation on:keydown|stopPropagation>
        <div class="filmstrip-row hide-scrollbar">
          {#each group.items as entry, index (entry.hash)}
            <button
              class="filmstrip-thumb"
              class:active={entry.hash === item?.hash}
              title={`${index + 1} / ${group.items.length}`}
              on:click={() => changeTo(entry)}
            >
              <img src={thumbnailUrl(entry)} use:retryThumbnail={entry.thumbnail_url ? thumbnailUrl(entry) : ''}
                   alt="" draggable="false" />
              <div class="thumb-index-badge">{index + 1}</div>
            </button>
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .focus-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    background: rgba(8, 9, 12, 0.95);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 76px 80px 64px 80px;
    overflow: hidden;
    user-select: none;
    outline: none;
  }

  .focus-overlay.filmstrip-open {
    padding-bottom: 160px;
  }

  .focus-overlay.fullscreen {
    padding: 0;
    background: #040406;
  }

  .focus-overlay.fullscreen.filmstrip-open {
    padding-bottom: 118px;
  }

  .hide-cursor {
    cursor: none !important;
  }

  .hide-cursor * {
    cursor: none !important;
  }

  .show-ui-btn {
    position: fixed;
    top: 20px;
    right: 20px;
    width: 30px;
    height: 30px;
    box-sizing: border-box;
    background: rgba(13, 17, 23, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 6px;
    color: var(--text-muted);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1070;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  }

  .show-ui-btn:hover {
    color: var(--text-bright);
    background: rgba(255, 255, 255, 0.15);
    border-color: rgba(255, 255, 255, 0.25);
  }

  /* Premium Header bar */
  .focus-header {
    position: fixed;
    top: 16px;
    left: 50%;
    transform: translateX(-50%);
    width: auto;
    min-width: 320px;
    max-width: 90%;
    height: 44px;
    background: rgba(13, 17, 23, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 8px;
    z-index: 1050;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 7px 0 16px;
    gap: 20px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
    opacity: 1;
  }

  .focus-header.hidden {
    opacity: 0;
    pointer-events: none;
    transform: translateX(-50%) translateY(-15px);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }

  .media-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-bright);
    max-width: 180px;
  }

  .group-badge {
    font-size: 11px;
    font-weight: bold;
    color: var(--text-muted);
    background: rgba(255, 255, 255, 0.05);
    height: 24px;
    padding: 0 8px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    display: flex;
    align-items: center;
    justify-content: center;
    box-sizing: border-box;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  /* Icon button styling */
  .icon-btn {
    width: 30px;
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-muted);
    border-radius: 6px;
    cursor: pointer;
    padding: 0;
    box-sizing: border-box;
  }

  .header-right > .icon-btn :global(svg),
  .close-btn-header :global(svg) {
    width: 18px !important;
    height: 18px !important;
    stroke-width: 2.2px !important;
    display: block;
  }

  .icon-btn:hover {
    color: var(--text-bright);
    background: rgba(255, 255, 255, 0.06);
    border-color: rgba(255, 255, 255, 0.08);
  }

  .icon-btn.active {
    color: var(--accent-primary);
    background: rgba(88, 166, 255, 0.15);
    border-color: rgba(88, 166, 255, 0.25);
  }

  /* Zoom controls */
  .zoom-controls {
    display: flex;
    align-items: center;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    height: 30px;
    box-sizing: border-box;
    padding: 0;
  }

  .zoom-controls .icon-btn {
    width: 28px;
    height: 28px;
    border: none;
    background: transparent;
    border-radius: 5px;
    cursor: pointer;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
  }

  .zoom-controls .icon-btn:hover {
    color: var(--text-bright);
    background: rgba(255, 255, 255, 0.06);
  }

  .zoom-controls .icon-btn :global(svg) {
    width: 14px !important;
    height: 14px !important;
    stroke-width: 2.2px !important;
    display: block;
  }

  .zoom-pill {
    font-size: 11px;
    font-weight: bold;
    color: var(--text-muted);
    padding: 0 6px;
    cursor: pointer;
    min-width: 40px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    outline: none;
  }

  .zoom-pill:hover,
  .zoom-pill.active {
    color: var(--text-bright);
    background: transparent;
    border: none;
  }

  /* Close Header button */
  .close-btn-header {
    width: 30px;
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-muted);
    border-radius: 6px;
    cursor: pointer;
    padding: 0;
    box-sizing: border-box;
  }

  .close-btn-header:hover {
    background: rgba(248, 81, 73, 0.15);
    border-color: rgba(248, 81, 73, 0.25);
    color: var(--accent-danger);
  }

  /* Keyboard HUD legend */
  .shortcuts-legend {
    position: fixed;
    top: 68px;
    left: 50%;
    transform: translateX(-50%);
    width: 270px;
    background: rgba(13, 17, 23, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 12px;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.6);
    padding: 16px;
    z-index: 1060;
    color: var(--text-main);
  }

  .shortcuts-legend h3 {
    margin: 0 0 14px 0;
    font-size: 13px;
    font-weight: bold;
    color: var(--text-bright);
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    padding-bottom: 8px;
  }

  .legend-grid {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .legend-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 11px;
  }

  .legend-item .keys {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .legend-item kbd {
    font-family: var(--font-mono, monospace);
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    padding: 2px 6px;
    box-shadow: 0 2px 0 rgba(0,0,0,0.3);
    font-weight: bold;
    color: var(--text-bright);
    font-size: 10px;
  }

  .legend-item span {
    color: var(--text-muted);
  }

  /* Centered media layout */
  .media-container {
    position: relative;
    max-width: 100%;
    max-height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    overflow: visible;
  }

  .media-container.filmstrip-open {
    max-height: calc(100vh - 236px);
  }

  .media-frame {
    display: flex;
    align-items: center;
    justify-content: center;
    transform-origin: center center;
  }

  .media-frame.zoomed {
    cursor: grab;
  }

  .media-frame.dragging {
    cursor: grabbing;
  }

  .focus-media {
    max-width: 100%;
    max-height: calc(100vh - 140px);
    box-shadow: 0 24px 70px rgba(0,0,0,0.8);
    object-fit: contain;
    user-select: none;
    border-radius: 4px;
  }

  .filmstrip-open .focus-media {
    max-height: calc(100vh - 236px);
  }

  .fullscreen .focus-media {
    max-width: 100vw;
    max-height: 100vh;
    width: 100vw;
    height: 100vh;
    object-fit: contain;
    border-radius: 0;
    box-shadow: none;
  }

  .fullscreen .media-container.filmstrip-open .focus-media {
    max-height: calc(100vh - 118px);
    height: calc(100vh - 118px);
  }

  .unsupported-media {
    min-width: 320px;
    min-height: 180px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    border: 1px solid var(--border-dim);
    border-radius: 8px;
    background: rgba(0,0,0,0.3);
  }

  /* Circular Glass Navigation controls */
  .nav-btn-rect {
    position: fixed;
    top: 50%;
    transform: translateY(-50%);
    width: 48px;
    height: 48px;
    border-radius: 8px;
    background: rgba(15, 17, 23, 0.8);
    border: 1px solid rgba(255,255,255,0.12);
    color: rgba(255, 255, 255, 0.6);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1010;
    box-shadow: 0 12px 36px rgba(0,0,0,0.5);
    opacity: 1;
    padding: 0;
  }

  .nav-btn-rect.hidden {
    opacity: 0;
    pointer-events: none;
  }

  .nav-btn-rect:hover {
    color: var(--text-bright);
    background: rgba(15, 17, 23, 0.95);
    border-color: rgba(255, 255, 255, 0.35);
    box-shadow: 0 12px 40px rgba(0,0,0,0.7);
  }

  .nav-btn-rect:active {
    background: rgba(15, 17, 23, 1.0);
  }

  .nav-btn-rect.prev {
    left: 24px;
  }

  .nav-btn-rect.next {
    right: 24px;
  }

  /* Filmstrip slider toggle */
  .filmstrip-toggle {
    position: fixed;
    left: 50%;
    bottom: 16px;
    transform: translateX(-50%);
    z-index: 1020;
    height: 30px;
    min-width: 132px;
    border-radius: 6px;
    background: rgba(13, 17, 23, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.15);
    color: var(--text-muted);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 0 16px;
    font-size: 11px;
    font-weight: bold;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
    opacity: 1;
    box-sizing: border-box;
  }

  .filmstrip-toggle.hidden {
    opacity: 0;
    pointer-events: none;
    transform: translateX(-50%) translateY(10px);
  }

  .filmstrip-toggle:hover {
    color: var(--text-bright);
    border-color: rgba(255, 255, 255, 0.3);
    box-shadow: 0 10px 35px rgba(0, 0, 0, 0.7);
    background: rgba(13, 17, 23, 0.95);
  }

  .filmstrip-toggle.open :global(.chevron-icon) {
    transform: rotate(180deg);
  }

  .filmstrip-toggle.open {
    bottom: 112px;
  }

  /* Filmstrip panel */
  .filmstrip {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    height: 96px;
    z-index: 1015;
    background: rgba(13, 17, 23, 0.95);
    border-top: 1px solid rgba(255, 255, 255, 0.1);
    display: flex;
    align-items: center;
    padding: 10px 24px;
    box-sizing: border-box;
    opacity: 1;
    transform: translateY(0);
  }

  .filmstrip.disable-clicks,
  .filmstrip.disable-clicks * {
    pointer-events: none !important;
  }

  .filmstrip.hidden {
    opacity: 0;
    pointer-events: none;
    transform: translateY(20px);
  }

  .filmstrip-row {
    display: flex;
    align-items: center;
    gap: 12px;
    overflow-x: auto;
    overflow-y: hidden;
    width: 100%;
    height: 100%;
    scroll-behavior: smooth;
    padding: 4px 0;
  }



  .filmstrip-thumb {
    position: relative;
    width: 68px;
    height: 68px;
    flex: 0 0 auto;
    padding: 0;
    border-radius: 8px;
    border: 2px solid transparent;
    background: rgba(255, 255, 255, 0.05);
    cursor: pointer;
    overflow: hidden;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  }

  .filmstrip-thumb:hover {
    border-color: rgba(255, 255, 255, 0.35);
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.4);
  }

  .filmstrip-thumb.active {
    border-color: var(--accent-primary);
    box-shadow: 0 8px 24px rgba(88, 166, 255, 0.25);
  }

  .filmstrip-thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }

  /* Thumbnail badge indicator */
  .thumb-index-badge {
    position: absolute;
    bottom: 4px;
    right: 4px;
    background: rgba(0, 0, 0, 0.7);
    color: white;
    font-size: 8px;
    font-weight: bold;
    padding: 1px 4px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.15);
    pointer-events: none;
  }
</style>
