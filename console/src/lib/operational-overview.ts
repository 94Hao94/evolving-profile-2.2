export type HealthState = "healthy" | "warning" | "critical" | "unknown";
export type OperationalIncident = {
  id: string;
  category: "backup" | "model" | "retention" | "map" | "service";
  severity: Exclude<HealthState, "healthy">;
  title: string;
  detail: string;
  at: string | null;
  source: string;
  sourceId?: string;
  action: "runtime" | "operations" | "llm-requests" | "flow";
  titleKey?: string;
  detailKey?: string;
  count?: number;
  operation?: string;
};
export type ErrorClass = "schema_validation" | "invalid_json" | "output_truncated" | "authentication" | "rate_limit" | "timeout" | "request_failed";
export type AttemptGroup = {
  id: string; traceId: string | null; operation: string; scope: string | null;
  errorClass: ErrorClass; attemptCount: number; requestIds: string[]; at: string;
  recovery: "later_attempt_succeeded" | "unresolved" | "unknown";
  traceComplete: false; laterSuccessId: string | null;
};
export type ScanCoverage = { returned: number; total: number | null; coverage: "complete" | "partial" | "unknown" | "unavailable" };
export type LlmEvidence = { id: string; operation: string; status: string; started_at: string; trace_id?: string | null; scope?: string | null; error?: unknown; finish_reason?: string | null };
export type OperationalLane = {
  id: OperationalIncident["category"];
  label: string;
  state: HealthState;
  summary: string;
  summaryKey?: string;
  summaryCount?: number;
  detail: string;
  lastObservedAt: string | null;
  incidentCount: number;
  checks: Array<{ label: string; state: HealthState; detail: string; at: string | null }>;
};

export type OperationalDismissals = Record<string, { kind: "state" | "receipt" | "attempt"; category?: OperationalIncident["category"]; clearedAt: number }>;
export const operationalDismissalStorageKey = (bankId: string) => `ep-operational-dismissals-v1:${encodeURIComponent(bankId)}`;
const receiptIncident = (row: OperationalIncident) => /^(operation:|model:|backup:\d)/.test(row.id);
// Poll timestamps for stateful checks are not new incidents. Receipt identity
// and substantive state changes are; hiding must never change health evidence.
function incidentDisplayKey(row: OperationalIncident) {
  return JSON.stringify(["issue",row.id,row.category,row.severity,row.sourceId??null,row.count??null,row.operation??null,
    row.titleKey??row.title,row.detailKey??row.detail,receiptIncident(row)?row.at:null]);
}
function attemptDisplayKey(row: AttemptGroup) {
  return JSON.stringify(["attempt",row.traceId??row.id,row.operation,row.scope,row.errorClass,[...row.requestIds].sort()]);
}
export function parseOperationalDismissals(raw: string | null): OperationalDismissals {
  try {
    const value=JSON.parse(raw || "{}");
    if(!value || typeof value!=="object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([,entry])=>entry && typeof entry==="object"
      && ["state","receipt","attempt"].includes((entry as any).kind) && Number.isFinite((entry as any).clearedAt))) as OperationalDismissals;
  } catch { return {}; }
}
export function dismissOperationalDisplay(previous: OperationalDismissals, incidents: readonly OperationalIncident[], attempts: readonly AttemptGroup[]): OperationalDismissals {
  const now=Date.now();
  const next=Object.fromEntries(Object.entries(previous).filter(([,v])=>v.kind==="state" || now-v.clearedAt<48*60*60*1000));
  for(const row of incidents) next[incidentDisplayKey(row)]={kind:receiptIncident(row)?"receipt":"state",category:row.category,clearedAt:now};
  for(const row of attempts) next[attemptDisplayKey(row)]={kind:"attempt",clearedAt:now};
  return next;
}
export function visibleOperationalIncidents(rows: readonly OperationalIncident[], state: OperationalDismissals) {
  return rows.filter(row=>!state[incidentDisplayKey(row)]);
}
export function visibleOperationalAttempts(rows: readonly AttemptGroup[], state: OperationalDismissals) {
  return rows.filter(row=>!state[attemptDisplayKey(row)]);
}
export function reconcileOperationalDismissals(state: OperationalDismissals, recoveredCategories: readonly OperationalIncident["category"][]) {
  return Object.fromEntries(Object.entries(state).filter(([,entry])=>entry.kind!=="state" || !entry.category || !recoveredCategories.includes(entry.category)));
}

// Acknowledging presentation is deliberately separate from clearing a list or
// recovering source memories. Only this exact observed alert may be quieted.
export type OperationalAcknowledgements = Partial<Record<OperationalLane["id"], {
  signature: string; failureRequestIds: string[]; confirmedAt: number;
}>>;
export const operationalAcknowledgementStorageKey = (bankId: string) => `ep-operational-acknowledgements-v1:${encodeURIComponent(bankId)}`;
function laneAlertSignature(lane: OperationalLane, incidents: readonly OperationalIncident[]) {
  return JSON.stringify([lane.state,
    incidents.filter(row => row.category === lane.id).map(incidentDisplayKey).sort(),
    lane.checks.filter(check => check.state !== "healthy").map(check => [check.label, check.state]).sort(),
  ]);
}
function laneFailureRequests(lane: OperationalLane, attempts: readonly AttemptGroup[]) {
  // Consolidation failures can change source identity while /stats stays at 1.
  // New failure receipts must break confirmation even with an unchanged count.
  return lane.id === "retention" ? [...new Set(attempts.filter(row => /retain|consolidat|observation/i.test(row.operation)).flatMap(row => row.requestIds))] : [];
}
export function parseOperationalAcknowledgements(raw: string | null): OperationalAcknowledgements {
  try {
    const value: unknown = JSON.parse(raw || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([category, entry]) => {
      if (!["backup", "model", "retention", "map", "service"].includes(category) || !entry || typeof entry !== "object") return false;
      const row = entry as Record<string, unknown>;
      return typeof row.signature === "string" && typeof row.confirmedAt === "number" && Number.isFinite(row.confirmedAt)
        && Array.isArray(row.failureRequestIds) && row.failureRequestIds.every(id => typeof id === "string");
    })) as OperationalAcknowledgements;
  } catch { return {}; }
}
export function acknowledgeOperationalLane(previous: OperationalAcknowledgements, lane: OperationalLane, incidents: readonly OperationalIncident[], attempts: readonly AttemptGroup[]): OperationalAcknowledgements {
  if (!["warning", "critical"].includes(lane.state) || !incidents.some(row => row.category === lane.id)) return previous;
  return { ...previous, [lane.id]: { signature: laneAlertSignature(lane, incidents), failureRequestIds: laneFailureRequests(lane, attempts), confirmedAt: Date.now() } };
}
export function isOperationalLaneAcknowledged(lane: OperationalLane, incidents: readonly OperationalIncident[], attempts: readonly AttemptGroup[], state: OperationalAcknowledgements): boolean {
  const saved = state[lane.id];
  return Boolean(saved && ["warning", "critical"].includes(lane.state) && incidents.some(row => row.category === lane.id)
    && saved.signature === laneAlertSignature(lane, incidents)
    && laneFailureRequests(lane, attempts).every(id => saved.failureRequestIds.includes(id)));
}
export function reconcileOperationalAcknowledgements(state: OperationalAcknowledgements, lanes: readonly OperationalLane[], incidents: readonly OperationalIncident[], attempts: readonly AttemptGroup[] = []): OperationalAcknowledgements {
  return Object.fromEntries(Object.entries(state).filter(([category]) => {
    const lane = lanes.find(row => row.id === category);
    // Missing observations are not evidence of recovery. Keep saved state but
    // never display that state as confirmed until observations return.
    return !lane || lane.state === "unknown"
      || isOperationalLaneAcknowledged(lane, incidents, attempts, state);
  }));
}

type Input = {
  now: number;
  runtime: {
    model?: { provider?: string; model?: string; apiKey?: string | null };
    services?: Array<{ name: string; status: string; detail?: string; url?: string }>;
    backup?: {
      local?: { status: string; setCount?: number; latestAt: string | null; latestBytes?: number; latestVerified: boolean; artifacts?: { database?: boolean; config?: boolean; capture?: boolean } };
      cloud?: { status: string; mirrorSetCount?: number; latestAt?: string | null; ageHours?: number | null };
      job?: { loaded: boolean; lastExitCode: number | null; schedule?: string };
    };
  };
  backupEvents: Array<{ at: string; status: string; code: string; detail: string }>;
  llm: { status: "observed" | "unavailable"; currentStatus?: "observed" | "unavailable"; items: LlmEvidence[]; total?: number | null; groupTotal?: number | null; relatedSuccessStatus?: "observed" | "partial" | "unavailable"; relatedTracesChecked?: string[] };
  operations: { status: "observed" | "unavailable"; items: Array<{ id: string; task_type: string; status: string; created_at: string; error_message?: string | null }>; total?: number | null; returned?: number };
  bankStats?: { status: "observed" | "unavailable"; failed_consolidation?: number; pending_consolidation?: number; operations_by_status?: Record<string, number> };
  map: { status: string; checked_at?: string; error_type?: string; last_success_at?: string; stale_after_seconds?: number; semantic_status?: string; semantic_worker_status?: string; semantic_error_type?: string };
};

export function classifyOperationalError(error: unknown, finishReason?: string | null): ErrorClass {
  const text = typeof error === "string" ? error : error && typeof error === "object" ? Object.values(error).filter((value) => typeof value === "string").join(" ") : "";
  if (finishReason === "length" || /finish_reason\s*[=:]\s*["']?length|truncat|max(?:imum)?[_ ](?:completion[_ ])?tokens|token (?:cap|limit)|lengthfinishreason/i.test(text)) return "output_truncated";
  if (/jsondecodeerror|invalid json|json.*(?:parse|decode)|expecting.*delimiter|unterminated string|json_invalid/i.test(text)) return "invalid_json";
  if (/validationerror|validation errors?|field required|type=missing|schema[_ -]?(?:validation|mismatch|error)|observation_id[\s\S]*missing/i.test(text)) return "schema_validation";
  if (/authentication|auth_failed|unauthorized|incorrect api key|invalid api[_ -]?key|\b401\b|\b403\b/i.test(text)) return "authentication";
  if (/rate.?limit|\b429\b|quota|capacity/i.test(text)) return "rate_limit";
  if (/timeout|timed out/i.test(text)) return "timeout";
  return "request_failed";
}

function coverage(returned: number, total: number | null | undefined, status: string): ScanCoverage {
  return { returned, total: total ?? null, coverage: status === "unavailable" ? "unavailable" : total == null ? "unknown" : returned >= total ? "complete" : "partial" };
}

function attemptGroups(input: Input): AttemptGroup[] {
  const groups = new Map<string, { group: AttemptGroup; latest: LlmEvidence }>();
  for (const entry of input.llm.items.filter((row) => row.status === "error" && recent(row.started_at, input.now))) {
    const errorClass = classifyOperationalError(entry.error, entry.finish_reason);
    const key = JSON.stringify([entry.trace_id || `request:${entry.id}`, entry.operation, entry.scope ?? null, errorClass]);
    const existing = groups.get(key);
    if (existing) {
      if (!existing.group.requestIds.includes(entry.id)) { existing.group.requestIds.push(entry.id); existing.group.attemptCount++; }
      if (Date.parse(entry.started_at) > Date.parse(existing.latest.started_at)) { existing.latest = entry; existing.group.at = entry.started_at; }
    } else groups.set(key, { latest: entry, group: { id: `attempt:${entry.id}`, traceId: entry.trace_id || null, operation: entry.operation, scope: entry.scope ?? null, errorClass,
      attemptCount: 1, requestIds: [entry.id], at: entry.started_at, recovery: "unknown", traceComplete: false, laterSuccessId: null } });
  }
  for (const { group, latest } of groups.values()) {
    if (!group.traceId) continue;
    const success = input.llm.items.find((row) => row.status === "success" && row.trace_id === group.traceId && row.operation === group.operation && (row.scope ?? null) === group.scope && Date.parse(row.started_at) > Date.parse(latest.started_at) && recent(row.started_at, input.now));
    if (success) { group.recovery = "later_attempt_succeeded"; group.laterSuccessId = success.id; }
    else if ((input.llm.relatedSuccessStatus ?? "observed") === "observed" || input.llm.relatedTracesChecked?.includes(group.traceId)) group.recovery = "unresolved";
  }
  return [...groups.values()].map(({ group }) => group).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

const WINDOW_MS = 24 * 60 * 60 * 1000;
function recent(value: string | null | undefined, now: number) {
  const time = Date.parse(value ?? "");
  return Number.isFinite(time) && time <= now && now - time <= WINDOW_MS;
}
function stateRank(state: HealthState) { return { healthy: 0, unknown: 1, warning: 2, critical: 3 }[state]; }
function maxState(states: HealthState[]): HealthState {
  return states.reduce<HealthState>((result, state) => stateRank(state) > stateRank(result) ? state : result, "healthy");
}
function compactTime(value: string | null | undefined) {
  if (!value || !Number.isFinite(Date.parse(value))) return "时间未记录";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)).replaceAll("/", "-");
}
function bytes(value: number | null | undefined) {
  if (value == null) return "大小未知";
  if (value >= 1_073_741_824) return `${(value / 1_073_741_824).toFixed(1)} GB`;
  if (value >= 1_048_576) return `${(value / 1_048_576).toFixed(0)} MB`;
  return `${value} B`;
}

export function buildOperationalOverview(input: Input) {
  const { now, runtime } = input;
  const incidents: OperationalIncident[] = [];
  const history = attemptGroups(input);
  const latestLlm = (input.llm.currentStatus ?? input.llm.status) === "observed" ? [...input.llm.items].filter((row) => recent(row.started_at, now)).sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))[0] : undefined;
  const failedMemories = input.bankStats?.status === "observed" ? input.bankStats.failed_consolidation ?? null : null;
  const pendingMemories = input.bankStats?.status === "observed" ? input.bankStats.pending_consolidation ?? null : null;
  const pipelineState: HealthState = failedMemories == null || pendingMemories == null ? "unknown" : failedMemories > 0 ? "critical" : pendingMemories > 0 ? "warning" : "healthy";
  const add = (incident: OperationalIncident) => incidents.push(incident);
  for (const event of input.backupEvents.filter((row) => recent(row.at, now) && row.status === "failed")) {
    add({ id: `backup:${event.at}:${event.code}`, category: "backup", severity: "critical", title: "备份运行失败", detail: event.detail.slice(0, 300), at: event.at, source: "本地备份回执", sourceId: event.code, action: "runtime" });
  }
  const backup = runtime.backup;
  const backupAt = backup?.local?.latestAt ?? null;
  if (backup?.job?.loaded === false || (backup?.job?.lastExitCode != null && backup.job.lastExitCode !== 0)) {
    add({ id: "backup:job", category: "backup", severity: "critical", title: "备份计划异常", detail: backup.job.loaded ? `最近退出码 ${backup.job.lastExitCode}` : "备份计划未加载", at: null, source: "launchd", action: "runtime" });
  }
  if (!backupAt || !recent(backupAt, now) || !backup?.local?.latestVerified || !backup.local.artifacts?.database || !backup.local.artifacts?.config) {
    add({ id: "backup:latest", category: "backup", severity: "warning", title: "最近备份未完整核验", detail: !backupAt ? "未发现本地备份" : `最近一套 ${backupAt}；请核对数据库、加密配置和校验清单`, at: backupAt, source: "本地备份文件", action: "runtime" });
  }
  if (backup?.cloud && backup.cloud.status !== "observed") {
    const missing=backup.cloud.status === "missing";
    add({ id: "backup:cloud", category: "backup", severity: missing ? "critical" : "warning", title: missing ? "未发现云端备份" : "云端镜像未核验", detail: missing ? "云端镜像 0 套；本机 WPS 缓存为空，当前没有可证明云端同步或恢复的证据" : "本机镜像或 WPS 缓存不能证明云端已经完成同步与恢复", at: backup.cloud.latestAt ?? null, source: "云镜像观测", action: "runtime" });
  }

  if (latestLlm?.status === "error") {
    const code = classifyOperationalError(latestLlm.error, latestLlm.finish_reason);
    add({ id: `model:${latestLlm.id}`, category: "model", severity: code === "authentication" ? "critical" : "warning", title: `最近模型请求失败 · ${code}`, titleKey: "latestFailure", detailKey: `advice.${code}`, detail: `错误类别：${code}。请查看相应请求诊断；历史失败尝试不等于当前连接不可用。`, at: latestLlm.started_at, source: "模型请求回执", sourceId: latestLlm.id, action: "llm-requests" });
  }
  if (failedMemories != null && failedMemories > 0) add({ id: "retention:failed-memories", category: "retention", severity: "critical", title: `当前 ${failedMemories} 条源记忆加工失败`, titleKey: "failedMemories", count: failedMemories, detailKey: "failedMemoryHelp", detail: "Bank 当前失败源记忆仍需恢复；其他请求成功不会清除这些失败。", at: null, source: "Bank /stats", action: "operations" });
  for (const operation of input.operations.items.filter((row) => recent(row.created_at, now) && row.status === "failed")) {
    const isRetain = /retain/i.test(operation.task_type);
    const code = classifyOperationalError(operation.error_message);
    add({ id: `operation:${operation.id}`, category: "retention", severity: isRetain ? "critical" : "warning", title: `${operation.task_type} 操作失败`, titleKey: "operationFailed", operation: operation.task_type, detail: `错误类别：${code}；请打开操作详情核对`, detailKey: `advice.${code}`, at: operation.created_at, source: "Bank 操作回执", sourceId: operation.id, action: "operations" });
  }
  const mapAge = now - Date.parse(input.map.checked_at ?? "");
  const mapStale = Number.isFinite(mapAge) && mapAge > (input.map.stale_after_seconds ?? 180) * 1000;
  if (input.map.status !== "ready" || mapStale) {
    add({ id: "map:refresh", category: "map", severity: input.map.status === "failed" ? "warning" : "unknown", title: "记忆地图更新未确认", detail: `状态 ${mapStale ? "核对过期" : input.map.status}${input.map.error_type ? ` · ${input.map.error_type}` : ""}；旧地图不能用来排除新资料`, at: input.map.checked_at ?? null, source: "地图更新回执", action: "flow" });
  }
  if (input.map.semantic_status && !["ready", "fresh_with_pending_changes"].includes(input.map.semantic_status)) {
    add({ id: "map:semantic", category: "map", severity: "warning", title: "语义目录待更新", detail: "结构目录已可查询，但模型摘要尚未覆盖当前源版本；有缺口时直接检索 Bank", at: input.map.checked_at ?? null, source: "语义目录回执", action: "flow" });
  }
  if (input.map.semantic_worker_status === "failed") {
    add({ id: "map:semantic-worker", category: "map", severity: "warning", title: "语义地图加工失败", detail: `本轮模型输出未通过发布检查（${input.map.semantic_error_type || "未知错误"}）；结构目录仍可用，后台会自动重试`, at: input.map.checked_at ?? null, source: "语义地图任务回执", action: "flow" });
  }
  for (const service of runtime.services ?? []) {
    if (service.status !== "healthy") add({ id: `service:${service.name}`, category: "service", severity: "critical", title: `${service.name} 不可用`, detail: service.detail || "服务探测失败", at: null, source: service.url || "本机健康探测", action: "runtime" });
  }

  const specs: Array<{ id: OperationalLane["id"]; label: string; observed: boolean; at: string | null; ok: string; unknown: string }> = [
    { id: "backup", label: "备份与恢复", observed: Boolean(backup), at: backupAt, ok: "最近备份已核验", unknown: "备份状态未核验" },
    { id: "model", label: "模型与 API Key", observed: (input.llm.currentStatus ?? input.llm.status) === "observed" && latestLlm?.status === "success", at: latestLlm?.started_at ?? null, ok: "最近模型调用成功", unknown: runtime.model?.apiKey ? "已配置密钥，近期连通性未核验" : "密钥未配置或连通性未核验" },
    { id: "retention", label: "记忆保留", observed: input.operations.status === "observed" && input.operations.items.some((row) => /retain/i.test(row.task_type) && row.status === "completed" && recent(row.created_at, now)), at: input.operations.items[0]?.created_at ?? null, ok: "最近保留已完成", unknown: "近期保留回执未核验" },
    { id: "map", label: "地图更新", observed: input.map.status === "ready" && !mapStale && recent(input.map.checked_at, now), at: input.map.checked_at ?? null, ok: "源版本已核对", unknown: "目录同步状态未核验" },
    { id: "service", label: "本机服务", observed: Boolean(runtime.services?.length), at: null, ok: "已探测服务在线", unknown: "服务状态未探测" },
  ];
  const successLlm = latestLlm?.status === "success" ? latestLlm : undefined;
  const successRetain = input.operations.items.find((row) => /retain/i.test(row.task_type) && row.status === "completed" && recent(row.created_at, now));
  const localComplete = Boolean(backupAt && recent(backupAt, now) && backup?.local?.latestVerified && backup.local.artifacts?.database && backup.local.artifacts?.config);
  const cloudState: HealthState = backup?.cloud?.status === "observed" ? "healthy" : backup?.cloud?.status === "missing" ? "critical" : "warning";
  const checks: Record<OperationalLane["id"], OperationalLane["checks"]> = {
    backup: [
      { label: "本地", state: localComplete ? "healthy" : "warning", at: backupAt,
        detail: localComplete ? `${backup?.local?.setCount ?? "—"} 套 · 最新 ${compactTime(backupAt)} · ${bytes(backup?.local?.latestBytes)} · 校验完整` : "最新备份缺失、过期或制品不完整" },
      { label: "云端", state: cloudState, at: backup?.cloud?.latestAt ?? null,
        detail: backup?.cloud?.status === "observed" ? `${backup.cloud.mirrorSetCount ?? "—"} 套 · 最新 ${compactTime(backup.cloud.latestAt)}` : backup?.cloud?.status === "missing" ? "0 套 · 未发现云端镜像" : backup?.cloud?.status === "stale" ? `${backup.cloud.mirrorSetCount ?? "—"} 套 · 已滞后 ${backup.cloud.ageHours ?? "—"} 小时` : "有本机线索，但未完成云端回读" },
    ],
    model: [{ label: runtime.model?.model || "后台模型", state: successLlm ? "healthy" : "unknown", at: successLlm?.started_at ?? null,
      detail: successLlm ? `最近成功 ${compactTime(successLlm.started_at)} · ${successLlm.operation}` : runtime.model?.apiKey ? "API Key 已配置，近期调用未核验" : "API Key 未配置" }],
    retention: [{ label: "Retain", state: successRetain ? "healthy" : input.operations.status === "observed" ? "warning" : "unknown", at: successRetain?.created_at ?? null,
      detail: successRetain ? `最近完成 ${compactTime(successRetain.created_at)} · ${successRetain.id.slice(0, 12)}` : "最近 24 小时未找到完成回执" }, ...(input.bankStats ? [{ label: "Consolidation", state: pipelineState, at: null,
      detail: failedMemories == null || pendingMemories == null ? "当前源记忆统计不可用" : `失败 ${failedMemories} 条 · 待处理 ${pendingMemories} 条` }] : [])],
    map: [
      { label: "结构目录", state: input.map.status === "ready" && !mapStale ? "healthy" : input.map.status === "failed" ? "warning" : "unknown", at: input.map.checked_at ?? null,
        detail: input.map.status === "ready" && !mapStale ? `最近核对 ${compactTime(input.map.checked_at)}` : mapStale ? "核对超过 3 分钟" : `状态 ${input.map.status}` },
      { label: "语义目录", state: input.map.semantic_worker_status === "failed" ? "warning" : ["ready", "fresh_with_pending_changes"].includes(input.map.semantic_status || "") ? "healthy" : "unknown", at: input.map.checked_at ?? null,
        detail: input.map.semantic_worker_status === "failed" ? `本轮未发布 · ${input.map.semantic_error_type || "质量检查未通过"}` : input.map.semantic_status === "fresh_with_pending_changes" ? "已发布版本可用 · 新增资料排队整理" : input.map.semantic_status === "ready" ? "已覆盖当前源版本" : "尚未发布有效摘要" },
    ],
    service: (runtime.services ?? []).map((service) => ({ label: service.name, state: service.status === "healthy" ? "healthy" : "critical", detail: service.detail || service.status, at: null })),
  };
  const lanes: OperationalLane[] = specs.map((spec) => {
    const own = incidents.filter((row) => row.category === spec.id);
    const itemChecks=checks[spec.id];
    const state: HealthState = own.length ? maxState(own.map((row) => row.severity)) : itemChecks.length ? maxState(itemChecks.map((row) => row.state)) : spec.observed ? "healthy" : "unknown";
    const pending = spec.id === "retention" && !own.length && pendingMemories != null && pendingMemories > 0;
    return { id: spec.id, label: spec.label, state, summary: own[0]?.title || (pending ? `待处理 ${pendingMemories} 条源记忆` : state === "healthy" ? spec.ok : spec.unknown),
      ...(pending ? { summaryKey: "pendingMemories", summaryCount: pendingMemories } : {}),
      detail: itemChecks.map((row) => `${row.label}：${row.detail}`).join("；"), lastObservedAt: spec.at, incidentCount: own.length, checks:itemChecks };
  });
  incidents.sort((a, b) => stateRank(b.severity) - stateRank(a.severity) || Date.parse(b.at ?? "") - Date.parse(a.at ?? ""));
  return { schema: "evolving-profile.operational-overview.v1", generatedAt: new Date(now).toISOString(), windowHours: 24,
    overall: lanes.some((row) => row.state === "critical") ? "critical" : lanes.some((row) => row.state === "warning") ? "warning" : lanes.some((row) => row.state === "unknown") ? "unknown" : "healthy",
    lanes, incidents, attemptHistory: history,
    pipeline: { state: pipelineState, failedMemories, pendingMemories, processingOperations: input.bankStats?.operations_by_status?.processing ?? null, queuedOperations: input.bankStats?.operations_by_status?.pending ?? null },
    scan: { llmFailures: coverage(history.reduce((sum, group) => sum + group.attemptCount, 0), input.llm.total, input.llm.status),
      llmFailureGroups: { total: input.llm.groupTotal ?? null },
      failedOperations: coverage(input.operations.returned ?? input.operations.items.filter((row) => row.status === "failed").length, input.operations.total, input.operations.status),
      relatedSuccess: { returned: input.llm.relatedTracesChecked?.length ?? 0, total: new Set(history.flatMap((group) => group.traceId ? [group.traceId] : [])).size,
        coverage: input.llm.relatedSuccessStatus === "unavailable" ? "unavailable" : input.llm.relatedSuccessStatus === "partial" ? "partial" : "complete" },
    },
    sourceCoverage: { backup: Boolean(backup), model: input.llm.currentStatus ?? input.llm.status, retention: input.operations.status, map: input.map.status, bankStats: input.bankStats?.status ?? "unavailable", sourceAudit: "not_performed" } };
}
