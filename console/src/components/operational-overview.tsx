"use client";

import { ActionButton } from "@/components/ui/action-button";
import { MemoryRecoveryEntry } from "@/components/memory-recovery-dialog";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useBank } from "@/lib/bank-context";
import { bankRoute } from "@/lib/bank-url";
import { AlertTriangle, ArrowRight, CircleAlert, CircleCheck, CircleHelp, RefreshCw, Settings2 } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { AttemptGroup, OperationalIncident, OperationalLane, ScanCoverage } from "@/lib/operational-overview";
import { dismissOperationalDisplay, operationalDismissalStorageKey, parseOperationalDismissals, reconcileOperationalDismissals, visibleOperationalAttempts, visibleOperationalIncidents, type OperationalDismissals } from "@/lib/operational-overview";
import { acknowledgeOperationalLane, isOperationalLaneAcknowledged, operationalAcknowledgementStorageKey, parseOperationalAcknowledgements, reconcileOperationalAcknowledgements, type OperationalAcknowledgements } from "@/lib/operational-overview";

import { inlineUiText } from "@/lib/inline-i18n";
type Overview = {
  overall: "healthy" | "warning" | "critical" | "unknown";
  generatedAt: string;
  windowHours: number;
  lanes: OperationalLane[];
  incidents: OperationalIncident[];
  attemptHistory: AttemptGroup[];
  pipeline: { state: "healthy" | "warning" | "critical" | "unknown"; failedMemories: number | null; pendingMemories: number | null; processingOperations?: number | null; queuedOperations?: number | null };
  scan: { llmFailures: ScanCoverage; llmFailureGroups: { total: number | null }; failedOperations: ScanCoverage; relatedSuccess: { coverage: string; returned?: number; total?: number } };
};

export function OperationalEvidenceSummary({ scan, pipeline, showHistory=true, acknowledged=false }: Pick<Overview, "scan" | "pipeline"> & {showHistory?:boolean; acknowledged?:boolean}) {
  const t = useTranslations("operationalEvidence");
  const locale = useLocale();
  const count = (value: number) => value.toLocaleString(locale);
  return <div className="space-y-3" data-testid="operational-evidence-summary">
    <div className={`rounded border p-3 ${TONES[acknowledged ? "unknown" : pipeline.state]}`}>
      <h3 className="text-sm font-semibold">{t("currentTitle")}</h3>
      {acknowledged && <p className="mt-1 text-xs">{locale.startsWith("zh") ? "本次提醒已确认，失败源记忆仍待恢复。" : "Alert acknowledged; failed source memories still need recovery."}</p>}
      <p className="mt-1 text-xs">{pipeline.failedMemories == null || pipeline.pendingMemories == null ? t("backlogUnavailable") : `${t("failedMemories", { count: count(pipeline.failedMemories) })} · ${t("pendingMemories", { count: count(pipeline.pendingMemories) })}`}</p>
      {pipeline.processingOperations != null && pipeline.queuedOperations != null && <p className="mt-1 text-xs">{t("activeOperations", { processing: count(pipeline.processingOperations), pending: count(pipeline.queuedOperations) })}</p>}
      <p className="mt-1 text-xs text-muted-foreground">{t("currentHelp")}</p>
    </div>
    {showHistory && <div className="rounded border border-border p-3 text-xs">
      <h3 className="text-sm font-semibold">{t("historyTitle")}</h3>
      <p className="mt-1">{scan.llmFailures.total == null ? t("unknown") : t("attempts", { count: count(scan.llmFailures.total) })} · {t("inspected", { count: count(scan.llmFailures.returned) })} · {scan.llmFailureGroups.total == null ? t("unknown") : t("traceJobs", { count: count(scan.llmFailureGroups.total) })}</p>
      <p className="mt-1 text-muted-foreground">{t(scan.llmFailures.coverage)} · {t("relatedCoverage", { coverage: t(scan.relatedSuccess.coverage) })}</p>
      {scan.relatedSuccess.total != null && <p className="mt-1 text-muted-foreground">{t("relatedInspected", { returned: count(scan.relatedSuccess.returned ?? 0), total: count(scan.relatedSuccess.total) })}</p>}
      <p className="mt-1 text-muted-foreground">{t("failedOperationsCoverage", { returned: count(scan.failedOperations.returned), total: scan.failedOperations.total == null ? "?" : count(scan.failedOperations.total), coverage: t(scan.failedOperations.coverage) })}</p>
      <p className="mt-1 text-muted-foreground">{t("sourceAuditNotPerformed")}</p>
    </div>}
  </div>;
}

export function OperationalAttemptHistory({ groups, onSelect }: { groups: AttemptGroup[]; onSelect: (group: AttemptGroup) => void }) {
  const t = useTranslations("operationalEvidence");
  const locale = useLocale();
  if (!groups.length) return null;
  return <details className="rounded border border-border" data-testid="operational-attempt-history">
    <summary className="cursor-pointer p-3 text-sm font-medium hover:bg-muted/50 focus-visible:outline-2 focus-visible:outline-primary">{t("historyGroups", { count: groups.length.toLocaleString(locale) })}</summary>
    <div className="divide-y divide-border border-t border-border px-3">
      {groups.map((group) => <button key={group.id} type="button" onClick={() => onSelect(group)} className="flex w-full items-start gap-3 py-3 text-left hover:bg-muted/50 focus-visible:outline-2 focus-visible:outline-primary">
        <CircleHelp className="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1"><span className="block text-sm font-medium">{group.operation} · {t(`errors.${group.errorClass}`)} · {t("attempts", { count: group.attemptCount.toLocaleString(locale) })}</span><span className="block break-words text-xs text-muted-foreground">{t(group.recovery === "later_attempt_succeeded" ? "recovered" : group.recovery === "unresolved" ? "unresolved" : "unknownRecovery")} · {group.traceId ?? t("unknownRecovery")}</span></span>
        <ArrowRight className="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />
      </button>)}
    </div>
  </details>;
}

const TONES = {
  healthy: "text-emerald-700 dark:text-emerald-300 bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200 dark:border-emerald-900",
  warning: "text-amber-800 dark:text-amber-200 bg-amber-50 dark:bg-amber-950/40 border-amber-200 dark:border-amber-900",
  critical: "text-rose-700 dark:text-rose-300 bg-rose-50 dark:bg-rose-950/40 border-rose-200 dark:border-rose-900",
  unknown: "text-zinc-600 dark:text-zinc-300 bg-zinc-50 dark:bg-zinc-900 border-zinc-200 dark:border-zinc-700",
};
const LABELS = { healthy: "正常", warning: "需关注", critical: "故障", unknown: "未核验" };
const ICONS = { healthy: CircleCheck, warning: AlertTriangle, critical: CircleAlert, unknown: CircleHelp };
const CARD_TONE = {
  healthy: "border-emerald-300 bg-emerald-50/80 dark:border-emerald-900 dark:bg-emerald-950/30",
  warning: "border-amber-400 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/35",
  critical: "border-rose-500 bg-rose-50 dark:border-rose-900 dark:bg-rose-950/40",
  unknown: "border-zinc-300 bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900",
};
const DOT_TONE = { healthy: "bg-emerald-500", warning: "bg-amber-500", critical: "bg-rose-600", unknown: "bg-zinc-400" };

function time(value: string | null | undefined, locale = "en") {
  if (!value) return inlineUiText("时间未记录");
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString(locale.startsWith("zh") ? locale : "en-US", { hour12: false }) : inlineUiText("时间无效");
}

function operationalText(value: string | undefined, english: boolean): string {
  if (!value) return "";
  if (!english) return value
    .replaceAll("Healthy", "正常")
    .replaceAll("healthy", "正常")
    .replaceAll("current request origin", "当前请求来源")
    .replaceAll("Controller recovery lanes + runtime guidance refresh", "Controller 恢复链路 + 运行指导刷新")
    .replaceAll("Items need attention", "有项目需要关注")
    .replaceAll("Cloud mirror not verified", "云端镜像未核验");
  return value
    .replaceAll("备份与恢复", "Backup and recovery")
    .replaceAll("云端镜像未核验", "Cloud mirror not verified")
    .replaceAll("模型与 API Key", "Models and API keys")
    .replaceAll("记忆保留", "Memory retention")
    .replaceAll("地图更新", "Map update")
    .replaceAll("本地", "Local")
    .replaceAll("云端", "Cloud")
    .replaceAll("最新", "Latest")
    .replaceAll("最近成功", "Recent success")
    .replaceAll("最近完成", "Recent completion")
    .replaceAll("最近核对", "Last checked")
    .replaceAll("已滞后", "Stale")
    .replaceAll("校验完整", "Checksum verified")
    .replaceAll("已覆盖当前源版本", "Current source version covered")
    .replaceAll("结构目录", "Structure catalog")
    .replaceAll("语义目录", "Semantic catalog")
    .replaceAll("已探测服务在线", "Service detected online")
    .replace(/(\d+(?:\.\d+)?)\s*套/g, "$1 sets")
    .replace(/(\d+(?:\.\d+)?)\s*小时/g, "$1 hours")
    .replace(/(\d+(?:\.\d+)?)\s*条/g, "$1 items")
    .replace(/：/g, ": ")
    .replace(/\s+/g, " ")
    .trim();
}

export function OperationalLaneCard({ lane, incident, pipeline, acknowledged, onDetail, onToggleAcknowledgement, disabled }: {
  lane: OperationalLane; incident?: OperationalIncident; pipeline: Overview["pipeline"];
  acknowledged: boolean; onDetail: () => void; onToggleAcknowledgement: () => Promise<void>; disabled: boolean;
}) {
  const locale = useLocale();
  const english = !locale.startsWith("zh");
  const t = useTranslations("operationalEvidence");
  const tone = acknowledged ? "unknown" : lane.state;
  const Icon = acknowledged ? CircleHelp : ICONS[lane.state];
  const confirmedLabel = lane.id === "retention" && (pipeline.failedMemories ?? 0) > 0
    ? (english ? `Acknowledged · ${pipeline.failedMemories} pending recovery` : `已确认 · ${pipeline.failedMemories} 条待恢复`)
    : (english ? `Acknowledged · ${lane.incidentCount} items remain` : `已确认 · ${lane.incidentCount} 项仍待处理`);
  return <article data-testid={`operational-lane-${lane.id}`} data-alert-acknowledged={acknowledged} className={`min-h-36 rounded border-t-4 border-x border-b shadow-sm ${CARD_TONE[tone]}`}>
    <button type="button" onClick={onDetail} disabled={!incident} className="block w-full p-3 text-left disabled:cursor-default enabled:hover:brightness-[0.98] focus-visible:outline-2 focus-visible:outline-primary">
      <div className="flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground"><span>{t(`lanes.${lane.id}`)}</span><Icon className={`h-4 w-4 ${TONES[tone].split(" ")[0]}`} /></div>
      <div className={`mt-2 text-base font-bold ${TONES[tone].split(" ")[0]}`}>{acknowledged ? confirmedLabel : `${english ? ({ healthy: "Healthy", warning: "Needs attention", critical: "Failure", unknown: "Unverified" })[lane.state] : LABELS[lane.state]}${lane.incidentCount > 0 ? ` · ${lane.incidentCount}` : ""}`}</div>
      <div className="mt-1 text-xs font-medium leading-5 text-foreground">{incident ? incident.titleKey ? t(incident.titleKey, { count: incident.count ?? 0, operation: incident.operation ?? "" }) : operationalText(incident.title, english) : lane.summaryKey ? t(lane.summaryKey, { count: (lane.summaryCount ?? 0).toLocaleString(locale) }) : operationalText(lane.summary, english)}</div>
      <div className="mt-2 space-y-1.5 border-t border-current/15 pt-2">
        {lane.checks.map(check => <div key={`${lane.id}:${check.label}`} className="flex items-start gap-2 text-[11px] leading-4">
          <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${DOT_TONE[acknowledged && check.state !== "healthy" ? "unknown" : check.state]}`} />
          <span className="min-w-0"><strong className="font-semibold">{operationalText(check.label, english)}</strong><span className="text-muted-foreground"> · {check.label === "Consolidation" ? pipeline.failedMemories == null || pipeline.pendingMemories == null ? t("backlogUnavailable") : `${t("failedMemories", { count: pipeline.failedMemories.toLocaleString(locale) })} · ${t("pendingMemories", { count: pipeline.pendingMemories.toLocaleString(locale) })}` : operationalText(check.detail, english)}</span></span>
        </div>)}
      </div>
    </button>
    {incident && ["warning", "critical"].includes(lane.state) && <div className="px-3 pb-3">
      <ActionButton size="sm" variant="outline" className="h-auto min-h-8 whitespace-normal text-xs" onAction={onToggleAcknowledgement} disabled={disabled} resetKey={`${lane.id}:${acknowledged}`} successLabel={english ? "Saved" : "已保存"}>{acknowledged ? english ? "Restore this alert" : "恢复本次提醒" : english ? "Acknowledge this alert" : "解除本次提醒"}</ActionButton>
      {acknowledged && <p role="status" className="mt-1 text-[11px] text-muted-foreground">{english ? "Confirmation is not recovery; new failures will alert again." : "已解除本次强调；不代表恢复，新故障仍会提醒。"}</p>}
    </div>}
  </article>;
}

export function OperationalOverview() {
  const { currentBank: bankId } = useBank();
  const locale = useLocale();
  const t = useTranslations("operationalEvidence");
  const english = !locale.startsWith("zh");
  const router = useRouter();
  const [snapshot, setSnapshot] = useState<{ bankId: string; data: Overview } | null>(null);
  const data = snapshot?.bankId === bankId ? snapshot.data : null;
  const [dismissed, setDismissed] = useState<{ bankId: string; value: OperationalDismissals } | null>(null);
  const dismissalState = dismissed?.bankId === bankId ? dismissed.value : {};
  const [acknowledgements, setAcknowledgements] = useState<{ bankId: string; value: OperationalAcknowledgements } | null>(null);
  const acknowledgementState = acknowledgements?.bankId === bankId ? acknowledgements.value : {};
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<OperationalIncident | null>(null);
  const [selectedAttempt, setSelectedAttempt] = useState<AttemptGroup | null>(null);
  const [policyOpen, setPolicyOpen] = useState(false);
  const requestVersion = useRef(0);
  useEffect(()=>{
    if(!bankId) { setDismissed(null); return; }
    try { setDismissed({bankId,value:parseOperationalDismissals(localStorage.getItem(operationalDismissalStorageKey(bankId)))}); }
    catch { setDismissed({bankId,value:{}}); }
  },[bankId]);
  useEffect(() => {
    if (!bankId) { setAcknowledgements(null); return; }
    try { setAcknowledgements({ bankId, value: parseOperationalAcknowledgements(localStorage.getItem(operationalAcknowledgementStorageKey(bankId))) }); }
    catch { setAcknowledgements({ bankId, value: {} }); }
  }, [bankId]);

  const load = useCallback(async (throwOnError = false) => {
    if (!bankId) return false;
    const version = ++requestVersion.current;
    setLoading(true);
    try {
      const response = await fetch(`/api/evolving-profile/overview/${encodeURIComponent(bankId)}`, { cache: "no-store" });
      if (!response.ok) throw new Error("overview_unavailable");
      const next = await response.json();
      if (version === requestVersion.current) { setSnapshot({bankId,data:next}); setError(false); }
    } catch (cause) { if (version === requestVersion.current) setError(true); if (throwOnError) throw cause; }
    finally { if (version === requestVersion.current) setLoading(false); }
  }, [bankId]);

  useEffect(() => {
    setSnapshot(null); setError(false); setSelected(null); setSelectedAttempt(null);
    setLoading(Boolean(bankId));
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => { window.clearInterval(timer); requestVersion.current++; };
  }, [load, bankId]);

  useEffect(()=>{
    if(!data || !bankId || dismissed?.bankId!==bankId) return;
    const next=reconcileOperationalDismissals(dismissed.value,data.lanes.filter(l=>l.state==="healthy").map(l=>l.id));
    if(Object.keys(next).length===Object.keys(dismissed.value).length) return;
    try { localStorage.setItem(operationalDismissalStorageKey(bankId),JSON.stringify(next)); setDismissed({bankId,value:next}); } catch { /* keep the prior clear state; never change health */ }
  },[data,bankId,dismissed]);
  useEffect(() => {
    if (!data || !bankId || error || acknowledgements?.bankId !== bankId) return;
    const next = reconcileOperationalAcknowledgements(acknowledgements.value, data.lanes, data.incidents, data.attemptHistory);
    if (Object.keys(next).length === Object.keys(acknowledgements.value).length) return;
    // Reconcile in memory even if persistence fails: a new fault must never be
    // hidden just because browser storage is unavailable.
    setAcknowledgements({ bankId, value: next });
    try { localStorage.setItem(operationalAcknowledgementStorageKey(bankId), JSON.stringify(next)); } catch { /* render live evidence */ }
  }, [data, bankId, error, acknowledgements]);
  const laneAcknowledged = (lane: OperationalLane) => Boolean(data && !error && isOperationalLaneAcknowledged(lane, data.incidents, data.attemptHistory, acknowledgementState));
  const toggleAcknowledgement = async (lane: OperationalLane) => {
    if (!bankId || !data || error || acknowledgements?.bankId !== bankId) throw new Error(english ? "Wait for current evidence to load." : "请等待当前运行证据加载完成。");
    const next = { ...acknowledgementState };
    if (laneAcknowledged(lane)) delete next[lane.id];
    else Object.assign(next, acknowledgeOperationalLane(next, lane, data.incidents, data.attemptHistory));
    try { localStorage.setItem(operationalAcknowledgementStorageKey(bankId), JSON.stringify(next)); }
    catch { throw new Error(english ? "Could not save confirmation. The alert remains active." : "确认状态保存失败，提醒未解除。"); }
    setAcknowledgements({ bankId, value: next });
  };
  const visibleIncidents=visibleOperationalIncidents(data?.incidents??[],dismissalState);
  const visibleAttempts=visibleOperationalAttempts(data?.attemptHistory??[],dismissalState);
  const hiddenCount=(data?.incidents.length??0)+(data?.attemptHistory.length??0)-visibleIncidents.length-visibleAttempts.length;
  const clearDisplay=async()=>{
    if(!bankId || !data || dismissed?.bankId!==bankId) throw new Error(english?"Wait for this bank to load.":"请等待当前记忆库加载完成。");
    const next=dismissOperationalDisplay(dismissalState,visibleIncidents,visibleAttempts);
    try { localStorage.setItem(operationalDismissalStorageKey(bankId),JSON.stringify(next)); }
    catch { throw new Error(english?"Could not save the cleared list. Check browser storage access.":"清空状态保存失败，请检查浏览器存储权限。"); }
    setDismissed({bankId,value:next});setSelected(null);setSelectedAttempt(null);
  };
  const restoreDisplay=async()=>{
    if(!bankId) return;
    localStorage.removeItem(operationalDismissalStorageKey(bankId));setDismissed({bankId,value:{}});
  };

  const openDetail = (incident: OperationalIncident) => setSelected(incident);
  const navigate = (incident: OperationalIncident) => {
    if (!bankId) return false;
    const suffix = incident.action === "flow" ? "?view=flow" : incident.action === "llm-requests" ? "?view=profile&bankConfigTab=llm-requests" : incident.action === "operations" ? "?view=profile&bankConfigTab=general#bank-operations" : "?view=profile&bankConfigTab=configuration";
    setSelected(null);
    router.push(bankRoute(bankId, suffix));
  };

  const overall = data?.overall ?? "unknown";
  const displayOverall = !data ? overall : data.lanes.some(lane => lane.state === "critical" && !laneAcknowledged(lane)) ? "critical" : data.lanes.some(lane => lane.state === "warning" && !laneAcknowledged(lane)) ? "warning" : overall === "healthy" ? "healthy" : "unknown";
  const allAlertsAcknowledged = Boolean(data && data.incidents.length && data.lanes.filter(lane => ["critical", "warning"].includes(lane.state)).every(laneAcknowledged));
  const retentionAcknowledged = Boolean(data?.lanes.some(lane => lane.id === "retention" && laneAcknowledged(lane)));
  const incidentTitle = (incident: OperationalIncident) => incident.titleKey ? t(incident.titleKey, { count: incident.count ?? 0, operation: incident.operation ?? "" }) : operationalText(incident.title, english);
  const incidentDetail = (incident: OperationalIncident) => incident.detailKey ? t(incident.detailKey) : operationalText(incident.detail, english);
  const OverallIcon = ICONS[displayOverall];
  return (
    <section className="mb-7 space-y-4" aria-label={inlineUiText("运行总览")}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
        <div>
          <h2 className="text-lg font-semibold">{inlineUiText("运行总览")}</h2>
          <p className="text-xs text-muted-foreground">{data ? (english ? `Last 24 hours · Updated ${time(data.generatedAt, locale)}` : `最近 24 小时 · 更新于 ${time(data.generatedAt, locale)}`) : error ? t("unavailable") : loading ? t("loading") : t("unknown")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ActionButton variant="outline" type="button" aria-label={english?"Clear issue list":"清空列表"} title={english?"Clear displayed issues and failure groups; audit records and health status are unchanged.":"清空已显示的问题与失败分组，不删除审计、不改变健康状态。"} successLabel={english?"Cleared":"已清空"} onAction={clearDisplay} disabled={!data || error || dismissed?.bankId!==bankId || !visibleIncidents.length&&!visibleAttempts.length} resetKey={bankId}>{english?"Clear issue list":"清空列表"}</ActionButton>
          <MemoryRecoveryEntry bankId={bankId} location="overview" />
          <button type="button" className="inline-flex h-9 w-9 items-center justify-center rounded border hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary" title={inlineUiText("预警口径")} aria-label={inlineUiText("预警口径")} onClick={() => setPolicyOpen(true)}><Settings2 className="h-4 w-4" /></button>
          <ActionButton variant="outline" type="button" size="icon" className="inline-flex h-9 w-9 items-center justify-center rounded border hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary" title={inlineUiText("刷新运行总览")} aria-label={inlineUiText("刷新运行总览")} onAction={() => load(true)}><RefreshCw className="h-4 w-4" /></ActionButton>
        </div>
      </div>
      {error && <p role="alert" className="text-sm text-rose-700">{inlineUiText("运行状态读取失败，现有数据可能已过期。")}</p>}
      {hiddenCount>0 && <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-border bg-muted/30 px-3 py-2" data-testid="operational-cleared-notice"><p role="status" className="text-xs text-muted-foreground">{english?`${hiddenCount} old items cleared from this browser. Audit records and health status are unchanged; new issues still appear.`:`已清空本浏览器中的 ${hiddenCount} 项旧提示。审计记录与健康状态保留，新问题仍会显示。`}</p><ActionButton variant="ghost" size="sm" onAction={restoreDisplay}>{english?"Show cleared items":"显示已清空项"}</ActionButton></div>}
      <div className={`flex items-center gap-3 rounded border p-3 ${TONES[displayOverall]}`}>
        <OverallIcon className="h-5 w-5 shrink-0" />
        <div className="min-w-0"><div className="text-sm font-semibold">{!data && error ? t("unavailable") : loading && !data ? t("loading") : allAlertsAcknowledged ? english ? "Alerts acknowledged; unresolved items remain" : "提醒已确认，仍有待处理项目" : overall === "healthy" ? inlineUiText("已核验的链路正常") : displayOverall === "critical" ? inlineUiText("有故障需要处理") : displayOverall === "warning" ? inlineUiText("有项目需要关注") : inlineUiText("有项目尚未核验")}</div>
          <div className="text-xs">{data ? (english ? `${data.incidents.length} issues or unverified items · ${data.lanes.filter((lane) => lane.state === "healthy").length}/${data.lanes.length} lanes verified healthy` : `${data.incidents.length} 条问题与未核验项 · ${data.lanes.filter((lane) => lane.state === "healthy").length}/${data.lanes.length} 条链路已核验正常`) : error ? t("unavailable") : loading ? t("loading") : t("unknown")}</div></div>
      </div>
      {data && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {data.lanes.map((lane) => {
          const incident = data.incidents.find((item) => item.category === lane.id);
          return <OperationalLaneCard key={lane.id} lane={lane} incident={incident} pipeline={data.pipeline} acknowledged={laneAcknowledged(lane)} onDetail={() => incident && openDetail(incident)} onToggleAcknowledgement={() => toggleAcknowledgement(lane)} disabled={error || acknowledgements?.bankId !== bankId} />;
        })}
      </div>}

      {data && <OperationalEvidenceSummary scan={data.scan} pipeline={data.pipeline} showHistory={hiddenCount===0} acknowledged={retentionAcknowledged && data.pipeline.state === "critical"} />}
      {data && <OperationalAttemptHistory groups={visibleAttempts} onSelect={setSelectedAttempt} />}

      {data && <div className="border-t border-border pt-4">
        <div className="flex items-baseline justify-between gap-3"><h3 className="text-sm font-semibold">{t("currentTitle")}</h3><span className="text-xs text-muted-foreground">{visibleIncidents.length} {english ? (visibleIncidents.length === 1 ? "item" : "items") : "项"}</span></div>
        <div className="mt-2 divide-y divide-border border-y border-border" data-testid="operational-issue-list">
          {visibleIncidents.length ? visibleIncidents.map((incident) => {
            const confirmed = data.lanes.some(lane => lane.id === incident.category && laneAcknowledged(lane));
            const Icon = confirmed ? CircleHelp : ICONS[incident.severity];
            return <button type="button" key={incident.id} onClick={() => openDetail(incident)} className="flex w-full items-center gap-3 py-3 text-left hover:bg-muted/50 focus-visible:outline-2 focus-visible:outline-primary">
              <Icon className={`h-4 w-4 shrink-0 ${TONES[confirmed ? "unknown" : incident.severity].split(" ")[0]}`} />
              <span className="min-w-0 flex-1"><span className="block text-sm font-medium">{confirmed ? english ? "Acknowledged · " : "已确认 · " : ""}{incidentTitle(incident)}</span><span className="block truncate text-xs text-muted-foreground">{incidentDetail(incident)}</span></span>
              <span className="hidden shrink-0 text-xs text-muted-foreground sm:block">{time(incident.at, locale)}</span><ArrowRight className="h-4 w-4 shrink-0 text-muted-foreground" />
            </button>;
          }) : <p className="py-4 text-sm text-muted-foreground">{hiddenCount>0 ? (english?"List cleared. This does not mean issues are fixed; see the health status above.":"列表已清空，不代表故障已修复；真实状态请查看上方。") : inlineUiText("最近 24 小时没有观测到失败回执。未核验链路仍显示在上方，不等于全部正常。")}</p>}
        </div>
        {((data.scan?.llmFailures.total ?? 0) > (data.scan?.llmFailures.returned ?? 0) || (data.scan?.failedOperations.total ?? 0) > (data.scan?.failedOperations.returned ?? 0)) &&
          <p className="mt-2 text-xs text-amber-800">{inlineUiText("列表按数据源分页展示；还有较早记录未展开，请到操作或模型请求页继续查看。")}</p>}
      </div>}

      <Dialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader><DialogTitle>{selected ? incidentTitle(selected) : ""}</DialogTitle><DialogDescription>{selected ? time(selected.at, locale) : ""}</DialogDescription></DialogHeader>
          {selected && <div className="space-y-3 text-sm">
            <p className="break-words leading-6">{incidentDetail(selected)}</p>
            <dl className="grid grid-cols-[80px_1fr] gap-2 border-t border-border pt-3 text-xs"><dt className="text-muted-foreground">{inlineUiText("证据来源")}</dt><dd className="break-all">{selected.source}</dd><dt className="text-muted-foreground">{inlineUiText("记录 ID")}</dt><dd className="break-all font-mono">{selected.sourceId ?? inlineUiText("未提供")}</dd><dt className="text-muted-foreground">{inlineUiText("级别")}</dt><dd>{english ? ({ healthy: "Healthy", warning: "Needs attention", critical: "Failure", unknown: "Unverified" } as Record<string, string>)[selected.severity] : LABELS[selected.severity]}</dd></dl>
            <button type="button" className="inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline" onClick={() => navigate(selected)}>{inlineUiText("查看对应页面")} <ArrowRight className="h-4 w-4" /></button>
          </div>}
        </DialogContent>
      </Dialog>
      <Dialog open={Boolean(selectedAttempt)} onOpenChange={(open) => !open && setSelectedAttempt(null)}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader><DialogTitle>{t("historyTitle")}</DialogTitle><DialogDescription>{selectedAttempt ? time(selectedAttempt.at, locale) : ""}</DialogDescription></DialogHeader>
          {selectedAttempt && <div className="space-y-3 text-sm">
            <p>{t(`advice.${selectedAttempt.errorClass}`)}</p>
            <p className="rounded border bg-muted/50 p-3 text-xs leading-5">{t("completionBoundary")}</p>
            <dl className="grid grid-cols-[100px_minmax(0,1fr)] gap-2 text-xs">
              <dt>{t("trace")}</dt><dd className="break-all font-mono">{selectedAttempt.traceId ?? t("unknownRecovery")}</dd>
              <dt>{t("operation")}</dt><dd>{selectedAttempt.operation}</dd><dt>{t("scope")}</dt><dd>{selectedAttempt.scope ?? t("unknown")}</dd>
              <dt>{t("errorClass")}</dt><dd>{t(`errors.${selectedAttempt.errorClass}`)}</dd>
              <dt>{t("requestIds")}</dt><dd className="space-y-1 break-all font-mono">{selectedAttempt.requestIds.map((id) => <div key={id}>{id}</div>)}</dd>
              <dt>{t("laterSuccess")}</dt><dd className="break-all font-mono">{selectedAttempt.laterSuccessId ?? t("unknownRecovery")}</dd>
            </dl>
            <button type="button" className="inline-flex items-center gap-2 text-primary hover:underline" onClick={() => { setSelectedAttempt(null); if (bankId) router.push(bankRoute(bankId, "?view=profile&bankConfigTab=llm-requests")); }}>{inlineUiText("查看对应页面")}<ArrowRight className="h-4 w-4" /></button>
          </div>}
        </DialogContent>
      </Dialog>
      <Dialog open={policyOpen} onOpenChange={setPolicyOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader><DialogTitle>{inlineUiText("预警口径")}</DialogTitle><DialogDescription>{inlineUiText("概览只根据可回读证据判断，不以配置存在代替真实可用。")}</DialogDescription></DialogHeader>
          <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-3 text-sm">
            <dt className="text-muted-foreground">{inlineUiText("观察窗口")}</dt><dd>{inlineUiText("最近 24 小时")}</dd>
            <dt className="text-muted-foreground">{inlineUiText("备份")}</dt><dd>{inlineUiText("计划退出失败、最新备份超过 24 小时、缺少数据库/配置/校验清单均提示；云端未回读标为未核验。")}</dd>
            <dt className="text-muted-foreground">{inlineUiText("模型连接")}</dt><dd>{t("currentHelp")}</dd>
            <dt className="text-muted-foreground">{inlineUiText("记忆保留")}</dt><dd>{t("completionBoundary")}</dd>
            <dt className="text-muted-foreground">{inlineUiText("地图")}</dt><dd>{inlineUiText("结构目录超过 3 分钟未核对提示过期；模型语义更新超过 20 分钟仍未完成才提示。")}</dd>
          </dl>
          <button type="button" className="inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline" onClick={() => { setPolicyOpen(false); if (bankId) router.push(bankRoute(bankId, "?view=profile&bankConfigTab=configuration")); }}>{inlineUiText("打开运行配置")} <ArrowRight className="h-4 w-4" /></button>
        </DialogContent>
      </Dialog>
    </section>
  );
}
