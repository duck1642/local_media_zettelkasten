export type MetadataRebuildJob = {
  running?: boolean;
  status?: string;
  mode?: string;
  stage?: string;
  items_total?: number;
  items_done?: number;
  errors?: number;
  duration_ms?: number;
  message?: string;
};

export function healthSummary(report: any) {
  if (!report) return '';
  return `${Number(report.issue_count || 0).toLocaleString()} issues`;
}

export function countValues(value: any) {
  if (!value || typeof value !== 'object') return 0;
  return Object.values(value).reduce<number>((total, item: any) => {
    if (Array.isArray(item)) return total + item.length;
    if (typeof item === 'number') return total + item;
    return total;
  }, 0);
}

export function repairSummary(payload: any) {
  const fixed = Number(payload?.fixed_issue_count || 0);
  const after = Number(payload?.after_issue_count ?? payload?.after?.issue_count ?? 0);
  const manual = countValues(payload?.manual_remaining);
  const tagged = Number(payload?.wd_tagging?.tagged || 0);
  const base = payload?.message || (fixed ? `Fixed ${fixed} issues` : 'No repairable issues changed');
  const wdText = tagged ? `; tagged ${tagged.toLocaleString()} items` : '';
  if (manual) return `${base}${wdText}; ${manual.toLocaleString()} need manual review; ${after.toLocaleString()} total remain`;
  return `${base}${wdText}; ${after.toLocaleString()} total remain`;
}

export function firstValues(value: any, limit = 5) {
  if (!value || typeof value !== 'object') return [];
  const rows: Array<{ kind: string; value: string }> = [];
  for (const [kind, raw] of Object.entries(value)) {
    if (Array.isArray(raw)) {
      for (const item of raw) {
        rows.push({ kind, value: String(typeof item === 'object' && item ? (item as any).path || JSON.stringify(item) : item) });
        if (rows.length >= limit) return rows;
      }
    } else if (typeof raw === 'number' && raw) {
      rows.push({ kind, value: String(raw) });
    }
    if (rows.length >= limit) return rows;
  }
  return rows;
}

export function detailValues(value: any, limit = 100) {
  return firstValues(value, limit);
}

export function healthKindLabel(kind: string) {
  const labels: Record<string, string> = {
    asset: 'Asset',
    note: 'Note',
    wd: 'WD cache',
    thumb: 'Thumbnail',
    assets: 'Asset',
    notes: 'Note',
    wd_cache: 'WD cache',
    thumbnails: 'Thumbnail',
    thumbnail_db_rows: 'Thumbnail DB entry',
    topics: 'Topics',
    wd_tags: 'WD tags',
    metadata_files: 'Metadata rows',
    unindexed_or_changed_items: 'Unindexed or changed items'
  };
  return labels[kind] || kind.replace(/_/g, ' ');
}

export function metadataProgressPercent(job: MetadataRebuildJob | null) {
  const total = Number(job?.items_total || 0);
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((Number(job?.items_done || 0) / total) * 100)));
}

export function metadataProgressText(job: MetadataRebuildJob | null) {
  if (!job) return '';
  const total = Number(job.items_total || 0);
  const done = Number(job.items_done || 0);
  const stage = String(job.stage || job.status || 'running');
  if (total > 0) return `${stage}: ${done.toLocaleString()} / ${total.toLocaleString()}`;
  return stage;
}
