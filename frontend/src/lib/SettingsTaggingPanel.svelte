<script lang="ts">
  import { onMount } from 'svelte';
  import { apiFetch } from './api';
  import { appSettings, updateAppSettings } from './appSettingsStore';

  type Model = { id: 'pixai' | 'wd'; name: string; recommended: boolean; default_repo: string; categories: string[]; thresholds: Record<string, number> };
  let models: Model[] = [];
  let error = '';
  $: selected = models.find((model) => model.id === $appSettings?.tagging.backend);
  $: profile = $appSettings?.tagging.profiles[$appSettings.tagging.backend];

  onMount(() => {
    const controller = new AbortController();
    apiFetch('/api/tagging/models', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('Could not load supported tagging models');
        models = await response.json();
      })
      .catch((reason) => { if (!controller.signal.aborted) error = String(reason); });
    return () => controller.abort();
  });

  function value(event: Event) {
    return (event.currentTarget as HTMLInputElement).value;
  }

  function selectBackend(event: Event) {
    const backend = value(event) as 'pixai' | 'wd';
    updateAppSettings((draft) => {
      draft.tagging.backend = backend;
      draft.tagging.model_repo = '';
      draft.tagging.model_path = '';
    });
  }

  function editProfile(edit: (next: NonNullable<typeof profile>) => void) {
    updateAppSettings((draft) => {
      const next = draft.tagging.profiles[draft.tagging.backend] ??= { thresholds: {}, category_map: {}, label_map: {} };
      edit(next);
    });
  }

  function threshold(category: string) {
    return profile?.thresholds[category] ?? (selected?.id === 'wd' && category !== 'rating'
      ? $appSettings?.tagging.threshold : selected?.thresholds[category]) ?? 0.35;
  }
</script>

{#if $appSettings}
  <label for="settings-enable-tagging">Auto Tagging<div class="micro-desc">Generate local AI tag suggestions for imported media.</div></label>
  <label class="check-label"><input id="settings-enable-tagging" type="checkbox" checked={$appSettings.tagging.enabled} on:change={(event) => updateAppSettings((draft) => { draft.tagging.enabled = event.currentTarget.checked; })} />Enable AI Tagging</label>

  <label for="settings-tag-backend">Tagging Model</label>
  <select id="settings-tag-backend" value={$appSettings.tagging.backend} on:change={selectBackend} disabled={!models.length}>
    {#each models as model}<option value={model.id}>{model.name}{model.recommended ? ' (Recommended)' : ' (Alternative)'}</option>{/each}
  </select>
  {#if error}<span role="alert">{error}</span>{/if}

  <label for="settings-model-repo">Model Repository<div class="micro-desc">Leave empty to use {selected?.default_repo || 'the official model'}.</div></label>
  <input id="settings-model-repo" value={$appSettings.tagging.model_repo} placeholder={selected?.default_repo} on:input={(event) => updateAppSettings((draft) => { draft.tagging.model_repo = value(event); })} />

  <label for="settings-model-path">Local Model Folder<div class="micro-desc">Optional. Use already downloaded model files.</div></label>
  <input id="settings-model-path" value={$appSettings.tagging.model_path} placeholder="Use the model repository" on:input={(event) => updateAppSettings((draft) => { draft.tagging.model_path = value(event); })} />

  <label for="settings-tag-device">Device</label>
  <select id="settings-tag-device" value={$appSettings.tagging.device} on:change={(event) => updateAppSettings((draft) => { draft.tagging.device = value(event) as 'auto' | 'cpu' | 'cuda'; })}>
    <option value="auto">Auto</option><option value="cuda">CUDA</option><option value="cpu">CPU</option>
  </select>

  <label for="settings-max-tags">Maximum Tags per Category</label>
  <input id="settings-max-tags" type="number" min="1" max="1000" value={$appSettings.tagging.max_tags} on:input={(event) => updateAppSettings((draft) => { draft.tagging.max_tags = Number(value(event)); })} />

  {#if selected}
    <div class="output-profile">
      <p>Output categories and thresholds for {selected.name}. Leave a destination empty to hide that category. Changes apply when tagging media again.</p>
      <table>
        <thead><tr><th>Model Category</th><th>Threshold</th><th>LMZ Category</th></tr></thead>
        <tbody>
          {#each selected.categories as category}
            <tr>
              <td>{category}</td>
              <td><input aria-label={`${category} threshold`} type="number" min="0" max="1" step="0.01" value={threshold(category)} on:input={(event) => editProfile((next) => { next.thresholds[category] = Number(value(event)); })} /></td>
              <td><input aria-label={`${category} destination`} placeholder="Hidden" value={profile?.category_map[category] === null ? '' : profile?.category_map[category] ?? category} on:input={(event) => editProfile((next) => { next.category_map[category] = value(event).trim() || null; })} /></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
{/if}

<style>
  .output-profile { grid-column: 1 / -1; min-width: 0; }
  p { color: var(--text-muted); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 6px; text-align: left; font-size: 12px; }
  input, select { width: 100%; min-width: 0; box-sizing: border-box; }
  input[type='number'] { max-width: 100px; }
  input[type='checkbox'] { width: auto; }
  .micro-desc { color: var(--text-muted); font-size: 11px; margin-top: 4px; }
  .check-label { display: flex; gap: 8px; align-items: center; }
</style>
