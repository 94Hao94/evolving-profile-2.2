"use client";

import { useEffect, useMemo, useState } from "react";
import { useLocale } from "next-intl";
import { ActionButton } from "@/components/ui/action-button";
import { Constellation } from "@/components/constellation";
import { Graph2D, type GraphNode } from "@/components/graph-2d";
import {
  buildPreferenceGraph,
  preferenceDimensionColor,
  preferenceDimensionLabel,
  FUSION_MODEL_COLOR,
  type FusionModel,
  type PreferenceDimension,
  type PreferenceLocale,
  type PreferenceUnit,
} from "@/lib/preference-graph";
import {
  preferenceTableRows,
  preferenceTimelineEvents,
  visiblePreferenceUnits,
  type PreferenceSelection,
  type PreferenceLifecycle,
  type PreferenceTableRow,
} from "@/lib/preference-view-model";

import { inlineUiText } from "@/lib/inline-i18n";
const DIMENSIONS: PreferenceDimension[] = [
  "communication",
  "learning",
  "reasoning",
  "collaboration",
  "delivery",
];

type Selection = PreferenceSelection;
type ViewMode = "constellation" | "graph" | "table" | "timeline" | "review";

const FUSION_COLOR = FUSION_MODEL_COLOR;

const COPY = {
  zh: {
    title: inlineUiText("多维度偏好"), intro: inlineUiText("当前 Prompt 优先。以下内容只在适用范围内作为参考，不自动扩张为执行授权。"),
    approved: inlineUiText("已审核"), pending: inlineUiText("待审核"), superseded: inlineUiText("已失效"), allRecords: inlineUiText("全部记录"), all: inlineUiText("全部显示"),
    fusion: inlineUiText("融合心智模型"), scope: inlineUiText("显示范围："), constellation: inlineUiText("星座图"), graph: inlineUiText("图谱"), table: inlineUiText("表格"), timeline: inlineUiText("时间线"),
    loading: inlineUiText("正在读取多维度偏好…"), unavailable: inlineUiText("多维度偏好注册表暂时不可读取。"), kind: inlineUiText("类型"), content: inlineUiText("内容"), status: inlineUiText("状态"), time: inlineUiText("时间"),
    active: inlineUiText("正式可用"), noRows: inlineUiText("当前筛选没有可显示的条目。"), noTimeline: inlineUiText("当前筛选没有可显示的时间事件。"),
    colors: inlineUiText("颜色与数量"), currentView: inlineUiText("当前视图："), details: inlineUiText("节点详情"), detailHint: inlineUiText("点击星座中的偏好或心智模型节点查看适用范围、机制和来源引用。"),
    applies: inlineUiText("适用："), exceptions: inlineUiText("例外："), action: inlineUiText("行动影响："), evidence: inlineUiText("证据"), model: inlineUiText("融合心智模型"),
    noMechanism: inlineUiText("该模型尚未登记机制正文。"), dynamicFusion: inlineUiText("融合心智模型不是固定五个领域，而是随着多维度偏好证据持续新增、修订、合并与归档的动态机制集合。"),
    pendingExplanation: inlineUiText("待审核条目保留用于核对和后续加工，当前不会进入 Agent 的多维度偏好指导包。它们通常还缺少独立原文证据、适用范围确认，或需要先处理与现有条目的重复和冲突。"),
    supersededExplanation: inlineUiText("已失效条目保留历史来源，但不会作为当前指导返回给 Agent。"),
    reviewRecommendations: inlineUiText("建议复核"),
    manualCorrection: inlineUiText("手动修正"),
    otherRecords: inlineUiText("其他条目"),
    editNow: inlineUiText("点击修改"),
    reviewRecommendationIntro: inlineUiText("系统只列出疑点较强的条目，不代表已经判错；复核不会阻塞日常运行。"),
    missingEvidence: inlineUiText("来源证据不足"),
    missingScope: inlineUiText("适用条件或例外不完整"),
    pendingReason: inlineUiText("条目仍处于待审核状态"),
    recommendationImpact: inlineUiText("影响：可能造成过度泛化或错误指导"),
    noRecommendations: inlineUiText("当前没有高优先级复核建议。"),
  },
  en: {
    title: "Multi-dimensional Preferences", intro: "The current prompt takes priority. These entries are conditional references and do not extend execution authority.",
    approved: "Reviewed", pending: "Pending review", superseded: "Superseded", allRecords: "All records", all: "All",
    fusion: "Fused mental models", scope: "Display:", constellation: "Constellation", graph: "Graph", table: "Table", timeline: "Timeline",
    loading: "Loading preferences...", unavailable: "The preference registry is temporarily unavailable.", kind: "Type", content: "Content", status: "Status", time: "Time",
    active: "Active", noRows: "No entries match the current filter.", noTimeline: "No timeline events match the current filter.",
    colors: "Colors and counts", currentView: "Current view:", details: "Details", detailHint: "Select a preference or mental-model node to inspect scope, mechanism, and sources.",
    applies: "Applies:", exceptions: "Exceptions:", action: "Action impact:", evidence: "evidence", model: "Fused mental model",
    noMechanism: "No mechanism body is registered for this model.", dynamicFusion: "Fused mental models are a dynamic set of mechanisms. They are not a fixed set of five domains.",
    pendingExplanation: "Pending entries are retained for review and follow-up processing. They are not included in the Agent's current preference packet because evidence, scope, duplication, or conflicts still need review.",
    supersededExplanation: "Superseded entries retain their historical sources but are not returned as current Agent guidance.",
    reviewRecommendations: "Review suggestions",
    manualCorrection: "Manual correction",
    otherRecords: "Other entries",
    editNow: "Edit",
    reviewRecommendationIntro: "These are stronger doubts, not confirmed errors; review does not block normal operation.",
    missingEvidence: "Insufficient source evidence",
    missingScope: "Scope or exceptions are incomplete",
    pendingReason: "The entry is still pending review",
    recommendationImpact: "Impact: possible over-generalization or misleading guidance",
    noRecommendations: "No high-priority review suggestions.",
  },
} as const;

export function PreferenceView() {
  const locale = useLocale();
  const language: PreferenceLocale = locale.startsWith("zh") ? "zh" : "en";
  const copy = COPY[language];
  const [units, setUnits] = useState<PreferenceUnit[]>([]);
  const [models, setModels] = useState<FusionModel[]>([]);
  const [selection, setSelection] = useState<Selection>("all");
  const [lifecycle, setLifecycle] = useState<PreferenceLifecycle>("approved");
  const [viewMode, setViewMode] = useState<ViewMode>("constellation");
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editingUnit, setEditingUnit] = useState<PreferenceUnit | null>(null);
  const [editText, setEditText] = useState("");
  const [editApplies, setEditApplies] = useState("");
  const [editExceptions, setEditExceptions] = useState("");
  const [editReason, setEditReason] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch("/api/evolving-profile/guidance/units?limit=500&cursor=0", {
        signal: controller.signal,
        cache: "no-store",
      }).then((response) => response.json()),
      fetch("/api/evolving-profile/guidance/models", {
        signal: controller.signal,
        cache: "no-store",
      }).then((response) => response.json()),
    ])
      .then(([unitResponse, modelResponse]) => {
        setUnits(unitResponse.items ?? []);
        setModels(modelResponse.items ?? []);
      })
      .catch((cause) => {
        if (cause.name !== "AbortError") setError(copy.unavailable);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  const filteredUnits = useMemo(() => visiblePreferenceUnits(units, selection, lifecycle), [lifecycle, selection, units]);
  const visibleAllUnits = useMemo(() => visiblePreferenceUnits(units, "all", lifecycle), [lifecycle, units]);
  const visibleModelCount = lifecycle === "needs_review" || lifecycle === "superseded" ? 0 : models.length;
  const graph = useMemo(() => buildPreferenceGraph(filteredUnits, models, selection, language), [filteredUnits, language, models, selection]);
  const tableRows = useMemo(() => preferenceTableRows(units, models, selection, lifecycle), [lifecycle, models, selection, units]);
  const timelineEvents = useMemo(() => preferenceTimelineEvents(units, models, selection, lifecycle), [lifecycle, models, selection, units]);
  const countByDimension = useMemo(
    () =>
      Object.fromEntries(
        DIMENSIONS.map((dimension) => [
          dimension,
          visibleAllUnits.filter((unit) => unit.primary_category === dimension).length,
        ])
      ) as Record<PreferenceDimension, number>,
    [visibleAllUnits]
  );
  const lifecycleCounts = useMemo(
    () => Object.fromEntries(["approved", "needs_review", "superseded"].map((state) => [state, units.filter((unit) => unit.preference_audit?.state === state).length])) as Record<Exclude<PreferenceLifecycle, "all">, number>,
    [units]
  );
  const reviewRecommendations = useMemo(() => units.map((unit) => {
    const reasons: string[] = [];
    if (unit.preference_audit?.state === "needs_review") reasons.push(copy.pendingReason);
    if (!(unit.evidence_refs?.length)) reasons.push(copy.missingEvidence);
    if (!(unit.applies_when?.length) || !(unit.exceptions?.length)) reasons.push(copy.missingScope);
    if (!reasons.length) return null;
    const severity = unit.preference_audit?.state === "needs_review" || !(unit.evidence_refs?.length) ? inlineUiText("高") : inlineUiText("中");
    return { unit, reasons, severity };
  }).filter(Boolean).sort((a: any, b: any) => (a.severity === inlineUiText("高") ? 0 : 1) - (b.severity === inlineUiText("高") ? 0 : 1)).slice(0, 12), [copy, units]);
  const reviewIds = useMemo(() => new Set(reviewRecommendations.map((item: any) => item.unit.id)), [reviewRecommendations]);
  const otherReviewUnits = useMemo(() => units.filter((unit) => !reviewIds.has(unit.id)).slice(0, 30), [reviewIds, units]);

  const selectedDetail = selectedNode?.metadata?.unit as PreferenceUnit | undefined;
  const selectedModel = selectedNode?.metadata?.model as FusionModel | undefined;
  const selectionLabel = selection === "all" ? copy.all : selection === "fusion" ? copy.fusion : preferenceDimensionLabel(selection, language);
  const legend = [
    ...DIMENSIONS.map((dimension) => ({
      id: dimension,
          label: preferenceDimensionLabel(dimension, language),
      color: preferenceDimensionColor(dimension),
      count: countByDimension[dimension],
    })),
    { id: "fusion", label: copy.fusion, color: FUSION_COLOR, count: visibleModelCount },
  ];
  const selectRow = (row: PreferenceTableRow) => {
    setSelectedNode({
      id: row.kind === "model" ? `model:${row.id}` : row.id,
      metadata: row.kind === "model" ? { kind: "model", model: row.model } : { kind: "preference", unit: row.unit },
    });
  };
  const startEdit = (unit: PreferenceUnit) => {
    setEditingUnit(unit);
    setEditText(unit.text);
    setEditApplies((unit.applies_when || []).join("\n"));
    setEditExceptions((unit.exceptions || []).join("\n"));
    setEditReason("");
    setEditError(null);
  };
  const saveEdit = async () => {
    if (!editingUnit || !editText.trim()) return false;
    setEditSaving(true); setEditError(null);
    try {
      const response = await fetch("/api/evolving-profile/guidance/manual-correction", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ unit_id: editingUnit.id, text: editText.trim(), applies_when: editApplies.split("\n").map((item) => item.trim()).filter(Boolean), exceptions: editExceptions.split("\n").map((item) => item.trim()).filter(Boolean), reason: editReason.trim() || inlineUiText("用户手动修正") }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || inlineUiText("修正保存失败"));
      const [unitResponse, modelResponse] = await Promise.all([
        fetch("/api/evolving-profile/guidance/units?limit=500&cursor=0", { cache: "no-store" }),
        fetch("/api/evolving-profile/guidance/models", { cache: "no-store" }),
      ]);
      if (!unitResponse.ok || !modelResponse.ok) throw new Error(copy.unavailable);
      const [unitBody, modelBody] = await Promise.all([unitResponse.json(), modelResponse.json()]);
      setUnits(unitBody.items ?? []);
      setModels(modelBody.items ?? []);
    } catch (cause) {
      setEditError(cause instanceof Error ? cause.message : inlineUiText("修正保存失败"));
      throw cause;
    }
    finally { setEditSaving(false); }
  };
  const eventTime = (at: number) => at ? new Date(at).toLocaleString(language === "zh" ? "zh-CN" : "en-US", { hour12: false }) : "—";

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">{copy.title}</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {copy.intro}
          </p>
        </div>
        <div className="text-xs text-muted-foreground">
          {copy.approved} {lifecycleCounts.approved} · {copy.pending} {lifecycleCounts.needs_review} · {copy.superseded} {lifecycleCounts.superseded} · {copy.fusion} {models.length}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1 border-b border-border pb-3 text-sm">
        <span className="mr-1 text-muted-foreground">{copy.scope}</span>
        {([
          ["approved", copy.approved + " " + lifecycleCounts.approved],
          ["needs_review", copy.pending + " " + lifecycleCounts.needs_review],
          ["superseded", copy.superseded + " " + lifecycleCounts.superseded],
          ["all", copy.allRecords + " " + units.length],
        ] as const).map(([state, label]) => (
          <button key={state} type="button" onClick={() => { setLifecycle(state); setSelectedNode(null); }} className={"rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors " + (lifecycle === state ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground")}>{label}</button>
        ))}
      </div>
      {lifecycle === "needs_review" && (
        <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm leading-6 text-amber-950 dark:bg-amber-950/20 dark:text-amber-100">
          {copy.pendingExplanation}
        </p>
      )}
      {lifecycle === "superseded" && (
        <p className="rounded-md border border-slate-300 bg-muted/50 px-3 py-2 text-sm leading-6 text-muted-foreground">
          {copy.supersededExplanation}
        </p>
      )}

      <div className="flex flex-wrap gap-2 border-b border-border pb-3">
        <button
          onClick={() => {
            setSelection("all");
            setSelectedNode(null);
          }}
          className={`rounded-md border px-3 py-2 text-sm font-semibold transition-colors ${
            selection === "all" ? "bg-muted" : "hover:bg-muted/60"
          }`}
          style={{ borderColor: "#475569" }}
        >
          {copy.all} <span className="ml-1 opacity-70">{visibleAllUnits.length + visibleModelCount}</span>
        </button>
        {DIMENSIONS.map((dimension) => (
          <button
            key={dimension}
            onClick={() => {
              setSelection(dimension);
              setSelectedNode(null);
            }}
            className={`rounded-md border px-3 py-2 text-sm font-medium transition-colors ${
              selection === dimension ? "bg-muted" : "hover:bg-muted/60"
            }`}
            style={{ borderColor: preferenceDimensionColor(dimension) }}
          >
            {preferenceDimensionLabel(dimension, language)} <span className="ml-1 opacity-70">{countByDimension[dimension]}</span>
          </button>
        ))}
        <button
          onClick={() => {
            setSelection("fusion");
            setLifecycle("approved");
            setSelectedNode(null);
          }}
          className={`rounded-md border-2 px-3 py-2 text-sm font-semibold transition-colors ${
            selection === "fusion" ? "bg-amber-50 text-amber-950 dark:bg-amber-950/30 dark:text-amber-50" : "hover:bg-amber-50/60"
          }`}
          style={{ borderColor: FUSION_COLOR }}
        >
          {copy.fusion} <span className="ml-1 opacity-70">{models.length}</span>
        </button>
      </div>

      <div className="flex flex-wrap gap-1" role="tablist" aria-label={copy.title}>
        {([
          ["constellation", copy.constellation],
          ["graph", copy.graph],
          ["table", copy.table],
          ["timeline", copy.timeline],
          ["review", `${copy.manualCorrection}${reviewRecommendations.length ? ` ${reviewRecommendations.length}` : ""}`],
        ] as const).map(([mode, label]) => (
          <button
            key={mode}
            type="button"
            role="tab"
            aria-selected={viewMode === mode}
            onClick={() => setViewMode(mode)}
            className={`rounded-md px-3 py-2 text-sm font-medium transition-colors ${viewMode === mode ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"}`}
          >
            {label}
          </button>
        ))}
      </div>

      {loading && <div className="rounded-lg border p-6 text-sm text-muted-foreground">{copy.loading}</div>}
      {error && <div className="rounded-lg border border-destructive/40 p-6 text-sm text-destructive">{error}</div>}
      {!loading && !error && (
        <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0 overflow-hidden rounded-lg border bg-background">
            {viewMode === "review" && (
              <div className="grid min-h-[680px] gap-4 p-4 lg:grid-cols-2">
                <section className="min-w-0 rounded-lg border border-amber-300 bg-amber-50/50 p-4 dark:bg-amber-950/20" aria-label={copy.reviewRecommendations}>
                  <div className="flex items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-amber-950 dark:text-amber-100">{copy.reviewRecommendations}</h3><p className="mt-1 text-xs leading-5 text-amber-900/80 dark:text-amber-100/80">{copy.reviewRecommendationIntro}</p></div><span className="rounded-full border border-amber-300 px-2 py-1 text-xs font-medium text-amber-900 dark:text-amber-100">{reviewRecommendations.length}</span></div>
                  <div className="mt-3 max-h-[560px] space-y-2 overflow-y-auto pr-1">{reviewRecommendations.map((item: any) => <button key={item.unit.id} type="button" className="w-full rounded-md border border-amber-200 bg-background/80 p-3 text-left hover:border-amber-400" onClick={() => startEdit(item.unit)}><div className="flex items-start justify-between gap-3"><span className="line-clamp-3 text-sm font-medium">{item.unit.text}</span><span className={`shrink-0 rounded px-2 py-0.5 text-[10px] ${item.severity === inlineUiText("高") ? "bg-rose-100 text-rose-800" : "bg-amber-100 text-amber-800"}`}>{item.severity} · {copy.editNow}</span></div><p className="mt-1 text-xs text-muted-foreground">{item.reasons.join(" · ")} · {copy.recommendationImpact}</p></button>)}</div>
                  {!reviewRecommendations.length && <p className="mt-4 text-sm text-muted-foreground">{copy.noRecommendations}</p>}
                </section>
                <section className="min-w-0 rounded-lg border p-4" aria-label={copy.otherRecords}>
                  <div className="flex items-start justify-between gap-3"><div><h3 className="text-sm font-semibold">{copy.otherRecords}</h3><p className="mt-1 text-xs leading-5 text-muted-foreground">{inlineUiText("点击任意条目后，在右侧详情中进入手动修正。")}</p></div><span className="rounded-full border px-2 py-1 text-xs">{units.length - reviewRecommendations.length}</span></div>
                  <div className="mt-3 max-h-[560px] space-y-2 overflow-y-auto pr-1">{otherReviewUnits.map((unit) => <button key={unit.id} type="button" className="w-full rounded-md border p-3 text-left hover:border-primary" onClick={() => startEdit(unit)}><div className="flex items-start justify-between gap-3"><span className="line-clamp-2 text-sm">{unit.text}</span><span className="shrink-0 rounded border px-2 py-0.5 text-[10px]">{copy.editNow}</span></div><p className="mt-1 text-xs text-muted-foreground">{preferenceDimensionLabel(unit.primary_category, language)} · {unit.preference_audit?.state === "approved" ? copy.approved : unit.preference_audit?.state ?? "—"}</p></button>)}</div>
                </section>
              </div>
            )}
            {viewMode === "constellation" && (
              <Constellation
                data={graph}
                height={680}
                nodeColorFn={(node) => node.color ?? "#64748b"}
                nodeSizeFn={(node) => {
                  if (node.id === "fusion-center") return 16;
                  if (node.metadata?.kind === "model") return 10;
                  return 5;
                }}
                preserveNodeColor
                linkColorFn={(link) => link.color ?? "#64748b"}
                linkOpacity={0.18}
                linkWidth={0.7}
                clusterKeyFn={(node) => (selection === "all" || selection === "fusion" ? node.group ?? null : null)}
                clusterColorFn={(group) => group === "fusion" ? FUSION_COLOR : preferenceDimensionColor(group as PreferenceDimension)}
                clusterLabelFn={(group) => group === "fusion" ? copy.fusion + " " + models.length : preferenceDimensionLabel(group as PreferenceDimension, language) + " " + countByDimension[group as PreferenceDimension]}
                centerClusterKey={selection === "all" ? "fusion" : undefined}
                onNodeClick={setSelectedNode}
              />
            )}
            {viewMode === "graph" && (
              <Graph2D
                data={graph}
                height={680}
                showLabels
                nodeColorFn={(node) => node.color ?? "#64748b"}
                nodeSizeFn={(node) => node.metadata?.kind === "model" ? 30 : node.id === "fusion-center" ? 38 : 18}
                linkColorFn={(link) => link.color ?? "#64748b"}
                linkWidthFn={(link) => link.type === "reference" ? 2 : 1}
                onNodeClick={setSelectedNode}
              />
            )}
            {viewMode === "table" && (
              <div className="max-h-[680px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="sticky top-0 bg-muted text-xs text-muted-foreground">
                    <tr><th className="px-4 py-3 font-medium">{copy.kind}</th><th className="px-4 py-3 font-medium">{copy.content}</th><th className="px-4 py-3 font-medium">{copy.status}</th><th className="px-4 py-3 font-medium">{copy.time}</th></tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {tableRows.map((row) => (
                      <tr key={`${row.kind}:${row.id}`} className="cursor-pointer hover:bg-muted/60" onClick={() => selectRow(row)}>
                        <td className="whitespace-nowrap px-4 py-3 text-xs text-muted-foreground">{row.kind === "model" ? copy.model : preferenceDimensionLabel(row.unit.primary_category, language)}</td>
                        <td className="max-w-[480px] px-4 py-3 leading-6"><span data-i18n-ignore="true" className="line-clamp-2">{row.title}</span></td>
                        <td className="px-4 py-3 text-xs"><span className="rounded border px-2 py-1">{row.lifecycle === "approved" ? copy.approved : row.lifecycle === "active" ? copy.active : row.lifecycle === "needs_review" ? copy.pending : row.lifecycle === "superseded" ? copy.superseded : row.lifecycle}</span></td>
                        <td className="whitespace-nowrap px-4 py-3 text-xs text-muted-foreground">{eventTime(row.at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!tableRows.length && <p className="p-6 text-sm text-muted-foreground">{copy.noRows}</p>}
              </div>
            )}
            {viewMode === "timeline" && (
              <ol className="max-h-[680px] divide-y divide-border overflow-auto">
                {timelineEvents.map((event) => (
                  <li key={`${event.kind}:${event.id}`} className="cursor-pointer px-5 py-4 hover:bg-muted/60" onClick={() => selectRow(event)}>
                    <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-muted-foreground">{eventTime(event.at)}</span><span className="rounded border px-2 py-1 text-xs">{event.kind === "model" ? copy.model : event.lifecycle === "approved" ? copy.approved : copy.pending}</span></div>
                    <p className="mt-2 leading-6">{event.title}</p>
                  </li>
                ))}
                {!timelineEvents.length && <li className="p-6 text-sm text-muted-foreground">{copy.noTimeline}</li>}
              </ol>
            )}
          </div>
          <aside className="min-w-0 rounded-lg border bg-background p-4">
            <section className="border-b border-border pb-3">
              <h3 className="text-sm font-semibold">{copy.colors}</h3>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{copy.currentView} {selectionLabel}</p>
              <div className="mt-3 space-y-2">
                {legend.map((item) => (
                  <div key={item.id} className="flex items-center justify-between gap-3 text-xs">
                    <span className="flex min-w-0 items-center gap-2">
                      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
                      <span className="truncate">{item.label}</span>
                    </span>
                    <span className="font-medium tabular-nums">{item.count}</span>
                  </div>
                ))}
              </div>
            </section>
            <section className="pt-4">
            <h3 className="text-sm font-semibold">{copy.details}</h3>
            {!selectedNode && <p className="mt-2 text-sm leading-6 text-muted-foreground">{copy.detailHint}</p>}
            {selectedDetail && (
              <div className="mt-3 space-y-3 text-sm">
                <div className="font-medium" style={{ color: preferenceDimensionColor(selectedDetail.primary_category) }}>
                  {preferenceDimensionLabel(selectedDetail.primary_category, language)}
                </div>
                <p data-i18n-ignore="true" className="leading-6">{selectedDetail.text}</p>
                {selectedDetail.applies_when?.length ? <p className="border-l-2 pl-3 text-xs leading-5 text-muted-foreground" style={{ borderColor: preferenceDimensionColor(selectedDetail.primary_category) }}>{copy.applies} {selectedDetail.applies_when.join("；")}</p> : null}
                {selectedDetail.exceptions?.length ? <p className="border-l-2 pl-3 text-xs leading-5 text-muted-foreground" style={{ borderColor: preferenceDimensionColor(selectedDetail.primary_category) }}>{copy.exceptions} {selectedDetail.exceptions.join("；")}</p> : null}
                {selectedDetail.effect_on_action ? <p className="text-xs leading-5 text-muted-foreground">{copy.action} {selectedDetail.effect_on_action}</p> : null}
                <p className="text-xs leading-5 text-muted-foreground">{copy.status}: {selectedDetail.preference_audit?.state === "approved" ? copy.approved : selectedDetail.preference_audit?.state === "needs_review" ? copy.pending : selectedDetail.preference_audit?.state ?? "—"} · {copy.evidence} {selectedDetail.evidence_refs?.length ?? 0}</p>
              </div>
            )}
            {selectedModel && (
              <div className="mt-3 space-y-3 text-sm">
                <div className="font-medium" style={{ color: FUSION_COLOR }}>{selectedModel.title}</div>
                {selectedModel.purpose && <p className="leading-6">{selectedModel.purpose}</p>}
                <p className="leading-6">{selectedModel.mechanism ?? selectedModel.sections?.[0]?.text ?? copy.noMechanism}</p>
                <div className="flex flex-wrap gap-1.5 text-xs">
                  {selectedModel.dimensions.map((dimension) => <span key={dimension} className="rounded border px-2 py-1" style={{ borderColor: preferenceDimensionColor(dimension), color: preferenceDimensionColor(dimension) }}>{preferenceDimensionLabel(dimension, language)}</span>)}
                </div>
                {selectedModel.sections?.map((section, index) => (
                  <div key={index} className="space-y-1 border-l-2 pl-3" style={{ borderColor: FUSION_COLOR }}>
                    {section.applies_when?.length ? <p className="text-xs leading-5 text-muted-foreground">{copy.applies} {section.applies_when.join("；")}</p> : null}
                    {section.exceptions?.length ? <p className="text-xs leading-5 text-muted-foreground">{copy.exceptions} {section.exceptions.join("；")}</p> : null}
                    {section.counterevidence?.length ? <p className="text-xs leading-5 text-muted-foreground">Counterevidence: {section.counterevidence.join("；")}</p> : null}
                  </div>
                ))}
                <p className="text-xs leading-5 text-muted-foreground">{copy.evidence} {selectedModel.sections?.flatMap((section) => section.guidance_refs ?? []).length ?? 0}{selectedModel.confidence != null ? " · " + selectedModel.confidence.toFixed(2) : ""}</p>
              </div>
            )}
            {selectedNode?.metadata?.kind === "fusion" && (
              <p className="mt-3 text-sm leading-6 text-muted-foreground">{copy.dynamicFusion}</p>
            )}
            </section>
          </aside>
        </div>
      )}
      {editingUnit && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label={inlineUiText("手动修正偏好")}><div className="w-full max-w-2xl rounded-xl border bg-background p-5 shadow-2xl"><div className="flex items-start justify-between gap-3"><div><h3 className="text-lg font-semibold">{inlineUiText("手动修正")}</h3><p className="mt-1 text-xs text-muted-foreground">{inlineUiText("保存后会生成新版本，保留原始来源和历史记录，不直接覆盖原文。")}</p></div><button type="button" className="rounded border px-2 py-1 text-sm" onClick={() => setEditingUnit(null)}>{inlineUiText("关闭")}</button></div><div className="mt-4 space-y-3"><label className="block text-sm font-medium">{inlineUiText("偏好内容")}<textarea className="mt-1 min-h-28 w-full rounded-md border bg-background p-3 text-sm" value={editText} onChange={(event) => setEditText(event.target.value)} /></label><div className="grid gap-3 md:grid-cols-2"><label className="block text-sm font-medium">{inlineUiText("适用条件（每行一条）")}<textarea className="mt-1 min-h-24 w-full rounded-md border bg-background p-3 text-sm" value={editApplies} onChange={(event) => setEditApplies(event.target.value)} /></label><label className="block text-sm font-medium">{inlineUiText("例外条件（每行一条）")}<textarea className="mt-1 min-h-24 w-full rounded-md border bg-background p-3 text-sm" value={editExceptions} onChange={(event) => setEditExceptions(event.target.value)} /></label></div><label className="block text-sm font-medium">{inlineUiText("修正说明")}<input className="mt-1 w-full rounded-md border bg-background p-2 text-sm" value={editReason} onChange={(event) => setEditReason(event.target.value)} placeholder={inlineUiText("例如：原条目把一次性项目要求误提炼成全局偏好")} /></label>{editError && <p className="rounded border border-destructive/40 bg-destructive/5 p-2 text-sm text-destructive">{editError}</p>}</div><div className="mt-5 flex justify-end gap-2"><button type="button" className="rounded border px-3 py-2 text-sm" onClick={() => setEditingUnit(null)}>{inlineUiText("取消")}</button><ActionButton resetKey={JSON.stringify([editingUnit.id, editText, editApplies, editExceptions, editReason])} disabled={editSaving || !editText.trim()} className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50" onAction={saveEdit}>{inlineUiText("保存修正")}</ActionButton></div></div></div>}
    </section>
  );
}
