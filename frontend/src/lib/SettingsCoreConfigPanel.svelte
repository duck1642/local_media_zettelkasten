<script lang="ts">
  import { TILE_MIN_WIDTH_CEILING, TILE_MIN_WIDTH_FLOOR } from './layout';
  import { appSettings, appSettingsDirty, appSettingsError, appSettingsSaving, saveCurrentAppSettings, updateAppSettings } from './appSettingsStore';
  import { IconSparkles, IconCheckCircle, IconAlertTriangle } from './icons';
  import SettingsTaggingPanel from './SettingsTaggingPanel.svelte';

  function setConfig(mutator: (draft: any) => void) {
    updateAppSettings(mutator, false);
  }

  function textValue(event: Event) {
    return (event.currentTarget as HTMLInputElement | HTMLSelectElement).value;
  }

  function numberValue(event: Event) {
    return Number((event.currentTarget as HTMLInputElement).value);
  }

  function checkedValue(event: Event) {
    return (event.currentTarget as HTMLInputElement).checked;
  }

  async function saveSettings() {
    try {
      await saveCurrentAppSettings();
    } catch {
      // The store keeps the draft dirty and exposes the actionable API error below.
    }
  }
</script>

{#if $appSettings}
<div class="section-card">
  <h4 class="settings-section-title">Vault Display Settings</h4>
  <div class="form-grid">
    <label for="settings-layout-mode">
      Vault Layout Mode
      <div class="micro-desc">Choose between standard grid alignment or staggered masonry.</div>
    </label>
    <select id="settings-layout-mode" value={$appSettings.ui.vault_layout_mode} on:change={(event) => setConfig((draft) => draft.ui.vault_layout_mode = textValue(event))}>
      <option value="masonry">Masonry</option>
      <option value="grid">Grid</option>
    </select>

    <label for="settings-tile-min-width">
      Vault Min Tile Width
      <div class="micro-desc">Controls the base sizing of media cards in your library.</div>
    </label>
    <div class="slider-container">
      <input
        id="settings-tile-min-width"
        type="range"
        min={TILE_MIN_WIDTH_FLOOR}
        max={TILE_MIN_WIDTH_CEILING}
        step="10"
        value={$appSettings.ui.vault_tile_min_width}
        on:input={(event) => setConfig((draft) => draft.ui.vault_tile_min_width = numberValue(event))}
      />
      <span class="slider-value">{$appSettings.ui.vault_tile_min_width}px</span>
    </div>

    <label for="settings-privacy-blur">
      Privacy blur
      <div class="micro-desc">Blur media previews for screenshots. Local to this app window.</div>
    </label>
    <div class="checkbox-group">
      <label class="check-label" id="settings-privacy-blur">
        <input type="checkbox" checked={$appSettings.ui.privacy_blur} on:change={(event) => setConfig((draft) => draft.ui.privacy_blur = checkedValue(event))} />
        Blur media previews
      </label>
    </div>

    <label for="settings-devtools-enabled">
      Developer tools
      <div class="micro-desc">Allow Ctrl+Shift+I or F12 to open the native web inspector.</div>
    </label>
    <div class="checkbox-group">
      <label class="check-label" id="settings-devtools-enabled">
        <input type="checkbox" checked={$appSettings.webview.devtools_enabled} on:change={(event) => setConfig((draft) => draft.webview.devtools_enabled = checkedValue(event))} />
        Enable developer tools
      </label>
    </div>

    <label for="settings-context-menu-enabled">
      Webview context menu
      <div class="micro-desc">Allow the browser-style right-click menu inside the app.</div>
    </label>
    <div class="checkbox-group">
      <label class="check-label" id="settings-context-menu-enabled">
        <input type="checkbox" checked={$appSettings.webview.context_menu_enabled} on:change={(event) => setConfig((draft) => draft.webview.context_menu_enabled = checkedValue(event))} />
        Enable right-click context menu
      </label>
    </div>
  </div>
</div>

<div class="section-card">
  <h4 class="settings-section-title">
    <span class="settings-title-icon">
      <IconSparkles size={14} />
    </span>
    AI Tagging Engine
  </h4>
  <div class="form-grid">
    <label for="settings-flatten-transparency">
      Image Transcoding
      <div class="micro-desc">Handling transparent PNG/WEBP layers.</div>
    </label>
    <div class="checkbox-group">
      <label class="check-label" id="settings-flatten-transparency">
        <input type="checkbox" checked={$appSettings.ingestion.processing.flatten_transparency} on:change={(event) => setConfig((draft) => draft.ingestion.processing.flatten_transparency = checkedValue(event))} />
        Flatten Transparency
      </label>
    </div>

    <SettingsTaggingPanel />
  </div>
</div>

<div class="save-bar" class:dirty={$appSettingsDirty}>
  <div class="save-info settings-title-inline">
    {#if $appSettingsDirty}
      <span class="settings-status-icon warning">
        <IconAlertTriangle size={13} />
      </span>
      <span class="status-label unsaved">You have unsaved changes.</span>
    {:else}
      <span class="settings-status-icon success">
        <IconCheckCircle size={13} />
      </span>
      <span class="micro-desc">All system configurations are up-to-date.</span>
    {/if}
  </div>
  {#if $appSettingsError}
    <span class="status-label unsaved" role="alert">{$appSettingsError}</span>
  {/if}
  <button class="primary" on:click={saveSettings} disabled={!$appSettingsDirty || $appSettingsSaving}>
    {$appSettingsSaving ? 'Saving...' : 'Save Settings'}
  </button>
</div>
{/if}
