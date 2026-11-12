<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import InspectorTagChip from './InspectorTagChip.svelte';

  export let rating = '';
  export let characters: string[] = [];
  export let general: string[] = [];
  export let groups: Record<string, string[]> = {};
  export let wdTagCounts: Record<string, number> | undefined = undefined;
  export let draftTopics: string[] = [];
  export let savedTopics: string[] = [];
  export let isAlreadyTopic: (value: string, saved: string[]) => boolean = () => false;
  export let isTagPromoted: (value: string, draft: string[], saved: string[]) => boolean = () => false;
  export let promoteHandler: (value: string) => void = () => {};
  export let removeTagHandler: (kind: string, value: string) => void = () => {};

  const dispatch = createEventDispatcher();

  function countFor(value: string) {
    const count = wdTagCounts?.[value];
    return typeof count === 'number' && count > 0 ? count : null;
  }

  function promote(value: string) {
    promoteHandler(value);
    dispatch('promote', value);
  }

  function remove(kind: string, value: string) {
    removeTagHandler(kind, value);
    dispatch('remove', { kind, value });
  }
</script>

<div class="group-container">
  <!-- svelte-ignore a11y-label-has-associated-control -->
  <label class="section-label">AI Tag Suggestions</label>

  <div class="tags-list suggestions-wrap">
    {#if rating}
      <InspectorTagChip
        label={rating}
        kind="rating"
        count={countFor(rating)}
        clickable={!isAlreadyTopic(rating, savedTopics)}
        promoted={isTagPromoted(rating, draftTopics, savedTopics)}
        interactive
        title={isAlreadyTopic(rating, savedTopics) ? "Already a topic" : "Promote to topic"}
        removeTitle="Remove WD tag"
        activateHandler={() => promote(rating)}
        removeHandler={() => remove('rating', rating)}
      />
    {/if}

    {#each (characters || []) as tag}
      <InspectorTagChip
        label={tag}
        kind="character"
        count={countFor(tag)}
        clickable={!isAlreadyTopic(tag, savedTopics)}
        promoted={isTagPromoted(tag, draftTopics, savedTopics)}
        interactive
        title={isAlreadyTopic(tag, savedTopics) ? "Already a topic" : "Promote to topic"}
        removeTitle="Remove WD tag"
        activateHandler={() => promote(tag)}
        removeHandler={() => remove('character', tag)}
      />
    {/each}

    {#each (general || []) as tag}
      <InspectorTagChip
        label={tag}
        kind="visual"
        count={countFor(tag)}
        clickable={!isAlreadyTopic(tag, savedTopics)}
        promoted={isTagPromoted(tag, draftTopics, savedTopics)}
        interactive
        title={isAlreadyTopic(tag, savedTopics) ? "Already a topic" : "Promote to topic"}
        removeTitle="Remove WD tag"
        activateHandler={() => promote(tag)}
        removeHandler={() => remove('general', tag)}
      />
    {/each}
  </div>

  {#each Object.entries(groups) as [category, tags]}
    {#if tags.length}
      <div class="section-label">{category}</div>
      <div class="tags-list suggestions-wrap">
        {#each tags as tag}
          <InspectorTagChip label={tag} kind="visual" count={countFor(tag)} interactive
            clickable={!isAlreadyTopic(tag, savedTopics)} promoted={isTagPromoted(tag, draftTopics, savedTopics)}
            title={isAlreadyTopic(tag, savedTopics) ? 'Already a topic' : 'Promote to topic'}
            removeTitle="Remove AI tag" activateHandler={() => promote(tag)} removeHandler={() => remove(category, tag)} />
        {/each}
      </div>
    {/if}
  {/each}

  {#if !rating && (!characters || characters.length === 0) && (!general || general.length === 0) && !Object.values(groups).some((tags) => tags.length)}
    <div class="value-text">No suggestions</div>
  {/if}
</div>

<style>
  .group-container {
    background: var(--bg-panel);
    border: 1px solid var(--border-dim);
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .section-label { font-size: 11px; color: var(--text-muted); font-weight: 500; }

  .tags-list { display: flex; flex-wrap: wrap; gap: 6px; }

  .value-text {
    color: #6a737d;
    font-style: italic;
    font-weight: normal;
    font-size: 13px;
    padding: 2px 0;
  }
</style>
