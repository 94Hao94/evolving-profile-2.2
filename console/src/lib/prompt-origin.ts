export const promptOriginKinds = ['human', 'subagent', 'automation', 'memory-maintenance', 'unknown'] as const;
export type PromptOriginKind = typeof promptOriginKinds[number];
const memorySources = new Set(['memory_consolidation', 'memory_maintenance', 'memory-maintenance']);
const automationSources = new Set(['automation', 'heartbeat', 'scheduled', 'background']);
const automationOrigins = new Set(['automatic', 'automation', 'system', 'agent_generated', 'agent_tool_call', 'test_probe']);

function occurrenceTime(value: unknown): bigint | null {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
  if (!match) return null;
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const offsetHour = Number(match[10] || 0), offsetMinute = Number(match[11] || 0);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1] || hour > 23 || minute > 59 || second > 59 || offsetHour > 23 || offsetMinute > 59) return null;
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day); date.setUTCHours(hour, minute, second, 0);
  const offset = (offsetHour * 60 + offsetMinute) * (match[9] === '-' ? -1 : 1);
  return BigInt(date.getTime() - offset * 60000) * BigInt(1000) + BigInt((match[7] || '').slice(0, 6).padEnd(6, '0'));
}

export function selectPromptOccurrences(rawRows: any[], allowedSources = ['codex-userpromptsubmit']) {
  // Same policy as Python: newest native recorded time, then last append;
  // representative selection precedes metadata, host/q/source filters.
  const selected: any[] = [], positions = new Map<string, number>();
  for (const raw of rawRows) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) || !allowedSources.includes(raw.source)) continue;
    const prompt = String(raw.prompt_preview || '').replace(/\s+/g, ' ').trim();
    if (!prompt) continue;
    const row = { ...raw, prompt_preview: prompt };
    const identity = [row.session_id, row.turn_id, row.hook_invocation_id];
    const complete = identity.every(value => typeof value === 'string' && value.trim());
    const key = complete ? JSON.stringify(identity) : undefined;
    const position = key === undefined ? undefined : positions.get(key);
    if (position === undefined) {
      if (key !== undefined) positions.set(key, selected.length);
      selected.push(row);
    } else {
      const previousTime = occurrenceTime(selected[position].at), currentTime = occurrenceTime(row.at);
      if (previousTime === null || currentTime !== null && currentTime >= previousTime) selected[position] = row;
    }
  }
  return selected.sort((a, b) => {
    const at = occurrenceTime(a.at), bt = occurrenceTime(b.at);
    if (at === bt) return 0;
    if (at === null) return 1;
    if (bt === null) return -1;
    return at > bt ? -1 : 1;
  });
}

// Keep native metadata cases aligned with guidance/prompt-origin-fixtures.json.
// Prompt wording and the Hook's default user_direct never prove a human origin.
export function classifyPromptOrigin(row: any, metadata: any, evidence: { source_path?: string; source_revision?: string; error?: string } = {}) {
  const nativeId = String(metadata?.id || '');
  const expected = String(row.session_id || '');
  const source = metadata?.source ?? null;
  const threadSource = String(metadata?.thread_source || '').toLowerCase();
  const subagent = source && typeof source === 'object' ? source.subagent : undefined;
  const spawn = subagent && typeof subagent === 'object' ? subagent.thread_spawn : undefined;
  const parentMatch = Boolean(spawn && expected && nativeId && spawn.parent_thread_id === expected && metadata?.parent_thread_id === expected && metadata?.session_id === expected);
  let kind: PromptOriginKind = 'unknown';
  let reason = evidence.error || 'native_source_not_classified';
  if (metadata && (!expected || (nativeId !== expected && !parentMatch))) {
    reason = 'session_identity_mismatch';
  } else if (metadata) {
    const memoryKind = typeof subagent === 'string' ? subagent : subagent?.other;
    if (memorySources.has(memoryKind) || memorySources.has(threadSource)) {
      kind = 'memory-maintenance'; reason = 'native_memory_maintenance';
    } else if (subagent !== undefined && subagent !== null || ['subagent', 'guardian_review'].includes(threadSource)) {
      kind = 'subagent'; reason = 'native_subagent';
    } else if (automationSources.has(threadSource) || threadSource.startsWith('hermes-background') || source === 'exec' || automationOrigins.has(String(row.prompt_origin || '').toLowerCase())) {
      kind = 'automation'; reason = 'native_automation';
    } else if (['user', 'realtime_voice', 'composer_link'].includes(threadSource) && ['vscode', 'cli', 'app'].includes(source)) {
      kind = 'human'; reason = 'native_human_session';
    }
  }
  return { origin_kind: kind, origin_status: kind === 'unknown' ? 'unknown' : 'verified',
    origin_evidence: { reason, source_path: evidence.source_path ?? null, source_revision: evidence.source_revision ?? null,
      native_session_id: nativeId || null, native_session_source: source, thread_source: threadSource || null,
      boundary: 'native_session_metadata_not_prompt_text' } };
}

export function promptPopulationProjection(rows: any[], requestedSource = 'natural') {
  const requested = String(requestedSource || 'natural').toLowerCase();
  const valid = ['natural', 'all', ...promptOriginKinds].includes(requested);
  const sourceFilter = valid ? requested : 'natural';
  const counts: Record<PromptOriginKind, number> = { human: 0, subagent: 0, automation: 0, 'memory-maintenance': 0, unknown: 0 };
  for (const row of rows) counts[promptOriginKinds.includes(row.origin_kind) ? row.origin_kind as PromptOriginKind : 'unknown']++;
  const selected = sourceFilter === 'all' ? rows : rows.filter(row => (row.origin_kind || 'unknown') === (sourceFilter === 'natural' ? 'human' : sourceFilter));
  return { rows: selected, source_filter: sourceFilter, source_filter_status: valid ? 'valid' : 'invalid_defaulted',
    source_counts: counts, natural_total: counts.human, audit_total: rows.length, statistics_denominator: selected.length,
    classification_scope: 'bounded_prompt_ingress_window',source_verification_status:counts.unknown?'partial':'complete',
    source_verification_pending_total:rows.filter(row=>row.origin_kind==='unknown' && row.origin_evidence?.reason==='native_prompt_source_scan_incomplete').length,
    statistics_denominator_scope:['natural','human'].includes(sourceFilter)?'verified_natural_user_occurrences_only':'selected_origin_filter_in_audit_window' };
}
