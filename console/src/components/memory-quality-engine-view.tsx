"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, RefreshCw, ShieldCheck, Timer, Zap } from "lucide-react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { projectMemoryQualityEvents, summarizeMemoryQualityEvents, type MemoryQualityEvent, type MemoryQualityMode } from "@/lib/memory-quality-event";
import { evaluateMemoryQuality } from "@/lib/memory-quality-evaluator";
import { runQualityRegression } from "@/lib/memory-quality-regression";
import type { FlowPrompt } from "@/lib/flow-projection";
import { useTranslations } from "next-intl";
import { JevReviewCard } from "@/components/jev-review-card";
import { ActionButton } from "@/components/ui/action-button";
import { enumUiText } from "@/lib/inline-i18n";

const LABELS = {
  en: { title: "Memory Quality Engine", subtitle: "Read-only observability for capture, retrieval, delivery, and process-memory quality.", refresh: "Refresh", mode: "Observation mode", lightweight: "Lightweight", diagnostic: "Diagnostic", deep_audit: "Deep audit", prompts: "Prompt samples", events: "Observed events", returned: "Returned", delivered: "Delivered", llm: "Extra LLM calls", noData: "No prompt-bound receipt is available in the current sample; this is not proof of zero calls.", modeHint: "Lightweight adds no LLM call and does not change memory truth.", status: "Status", route: "Route", counts: "Candidates / returned / delivered" },
  zh: { title: "记忆质量引擎", subtitle: "只读观测写入、检索、送达和智能体过程记忆质量。", refresh: "刷新", mode: "观测模式", lightweight: "轻量观测", diagnostic: "诊断观测", deep_audit: "深度审计", prompts: "Prompt 样本", events: "观测事件", returned: "返回", delivered: "送达", llm: "额外模型调用", noData: "当前样本没有可绑定的回执，不代表调用次数为 0。", modeHint: "轻量观测不增加模型调用，也不改变记忆真相。", status: "状态", route: "路线", counts: "候选 / 返回 / 送达" },
};

export function MemoryQualityEngineView() {
  const jevText = useTranslations("jevScopes");
  const [judgeReview, setJudgeReview] = useState<any>(null);
  const [judgeRunning, setJudgeRunning] = useState(false);
  const [judgeReviews, setJudgeReviews] = useState<any[]>([]);
  const [locale, setLocale] = useState("en");
  const [mode, setMode] = useState<MemoryQualityMode>("lightweight");
  const [rows, setRows] = useState<FlowPrompt[]>([]);
  const [ledgerEvents, setLedgerEvents] = useState<MemoryQualityEvent[]>([]);
  const [coverage,setCoverage]=useState<{runtime_stages?:string[];ledger_stages?:string[];source_readback?:string}>({});
  const [bindingSummary, setBindingSummary] = useState<{ prompt_bound_events: number; ledger_events: number; unattributed_activity: number; prompt_samples: number }>({ prompt_bound_events: 0, ledger_events: 0, unattributed_activity: 0, prompt_samples: 0 });
  const [unattributedActivity, setUnattributedActivity] = useState<{ event_count?: number; by_tool?: Record<string, { calls?: number; returned?: number }> }>({});
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [selected, setSelected] = useState<MemoryQualityEvent | null>(null);
  const [regressionResults, setRegressionResults] = useState<ReturnType<typeof runQualityRegression> | null>(null);
  const [auditRunning, setAuditRunning] = useState(false);
  const [tab, setTab] = useState<"overview" | "dimensions" | "retrieval" | "cost" | "regression" | "settings">("overview");
  const english = locale !== "zh";
  const copy = english ? LABELS.en : LABELS.zh;

  useEffect(() => {
    const browserLocale = typeof document !== "undefined" ? document.documentElement.lang : "en";
    setLocale(browserLocale.startsWith("zh") ? "zh" : "en");
    setEnabled(window.localStorage.getItem("ep51-quality-enabled") !== "false");
    const storedMode = window.localStorage.getItem("ep51-quality-mode") as MemoryQualityMode | null;
    if (storedMode === "lightweight" || storedMode === "diagnostic" || storedMode === "deep_audit") setMode(storedMode);
  }, []);

  const load = async () => {
    setLoading(true);
    try {
      const read = async (url: string) => {
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) throw new Error(`${copy.refresh}: HTTP ${response.status}`);
        return response.json();
      };
      const [promptPayload, ledgerPayload] = await Promise.all([
        read("/api/evolving-profile/guidance/prompts?limit=100&cursor=0&host=all"),
        read("/api/evolving-profile/quality/events?limit=500"),
      ]);
      setRows(Array.isArray(promptPayload?.items) ? promptPayload.items : []); setLedgerEvents(Array.isArray(ledgerPayload?.events) ? ledgerPayload.events : []); setBindingSummary(ledgerPayload?.binding_summary ?? { prompt_bound_events: 0, ledger_events: 0, unattributed_activity: 0, prompt_samples: 0 }); setUnattributedActivity(ledgerPayload?.unattributed_activity ?? {}); setJudgeReviews(ledgerPayload?.judge_reviews || []);
      setLastRefresh(new Date().toLocaleTimeString());
      setCoverage(ledgerPayload?.coverage || {});
    } finally { setLoading(false); }
  };
  useEffect(() => { void load().catch(() => undefined); }, []);

  const events = useMemo<MemoryQualityEvent[]>(() => [...ledgerEvents, ...rows.flatMap((row) => projectMemoryQualityEvents(row, mode))].filter((event, index, all) => all.findIndex((candidate) => candidate.event_id === event.event_id) === index), [ledgerEvents, rows, mode]);
  const summary = useMemo(() => summarizeMemoryQualityEvents(events), [events]);
  const label=(value:string)=>enumUiText(value,english ? "en" : "zh-CN");
  const findings = useMemo(() => evaluateMemoryQuality(events), [events]);
  const recent = events.slice(-30).reverse();
  const activity = useMemo(() => {
    const buckets = new Map<string, { bucket: string; user: number; agent: number; scenario: number; rag: number }>();
    for (const event of events) {
      const date = new Date(event.created_at);
      const bucket = Number.isNaN(date.getTime()) ? "unknown" : `${String(date.getHours()).padStart(2, "0")}:00`;
      const row = buckets.get(bucket) ?? { bucket, user: 0, agent: 0, scenario: 0, rag: 0 };
      if (event.plane === "user_memory") row.user += 1;
      else if (event.plane === "agent_process") row.agent += 1;
      else if (event.plane === "scenario") row.scenario += 1;
      else if (event.plane === "external_rag") row.rag += 1;
      buckets.set(bucket, row);
    }
    return [...buckets.values()].slice(-24);
  }, [events]);
  const dimensions = useMemo(() => Object.entries(Object.groupBy(events, (event) => `${event.plane}:${event.dimension}`)).map(([name, items]) => ({ name, events: items ?? [] })), [events]);
  const tabs = english ? { overview: "Overview", dimensions: "Dimension quality", retrieval: "Retrieval & delivery", cost: "Cost & performance", regression: "Regression lab", settings: "Settings" } : { overview: "总览", dimensions: "维度质量", retrieval: "召回与送达", cost: "成本与性能", regression: "回归实验室", settings: "设置" };

  const setObservationMode = (value: MemoryQualityMode) => { setMode(value); window.localStorage.setItem("ep51-quality-mode", value); };
  const setEngineEnabled = (value: boolean) => { setEnabled(value); window.localStorage.setItem("ep51-quality-enabled", String(value)); };
  const runDeepAudit = async () => {
    setAuditRunning(true);
    try {
      await new Promise((resolve) => window.setTimeout(resolve, 80));
      setRegressionResults(runQualityRegression("deep_audit"));
    } finally { setAuditRunning(false); }
  };
  const runJudgeAudit = async () => {
    setJudgeRunning(true);
    try {
      const response = await fetch("/api/evolving-profile/quality/judge", { method: "POST", signal: AbortSignal.timeout(13000) });
      const review = await response.json();
      setJudgeReview(review);
      if (!response.ok || review.status === "unavailable") throw new Error(review.error || (english ? "Quality audit unavailable" : "质量审计不可用"));
    } catch (error) { setJudgeReview({ status: "unavailable", calls: 0, fallback: "rules" }); throw error; }
    finally { setJudgeRunning(false); }
  };
  return <div className="space-y-6">
    <header className="flex flex-col gap-4 rounded-2xl border bg-card p-6 shadow-sm md:flex-row md:items-start md:justify-between">
      <div><div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase tracking-[0.18em] text-primary"><Activity className="h-4 w-4" /> EP 5.1</div><h1 className="text-2xl font-semibold tracking-tight">{copy.title}</h1><p className="mt-2 max-w-3xl text-sm text-muted-foreground">{copy.subtitle}</p></div>
      <ActionButton variant="outline" type="button" onAction={load} className="inline-flex items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium hover:bg-accent"><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> {copy.refresh}</ActionButton>
    </header>
    <section className="rounded-xl border bg-card p-4" data-testid="quality-jev-review">
      <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-sm font-semibold">{jevText("auditTitle")}</h2><p className="mt-1 text-xs text-muted-foreground">{jevText("quality_diagnosisHint")}</p></div>
        <ActionButton variant="outline" type="button" onAction={() => runJudgeAudit()} disabled={judgeRunning} className="rounded-lg border px-3 py-2 text-xs disabled:opacity-50">{judgeRunning ? jevText("running") : jevText("runAudit")}</ActionButton>
      </div>
      {judgeReview ? <JevReviewCard review={judgeReview} /> : judgeReviews.length ? <JevReviewCard review={judgeReviews[judgeReviews.length - 1]} /> : <p className="mt-3 text-xs text-muted-foreground">{jevText("noReview")}</p>}
    </section>
    <section className="grid gap-3 md:grid-cols-2 lg:grid-cols-6">
      {[{ icon: Zap, label: copy.prompts, value: rows.length }, { icon: Activity, label: copy.events, value: summary.measuredEventCount }, { icon: Timer, label: copy.returned, value: summary.totals.returned }, { icon: ShieldCheck, label: copy.delivered, value: summary.deliveryMeasuredEventCount ? summary.totals.delivered : "—" }, { icon: Zap, label: copy.llm, value: summary.totals.llmCalls }, { icon: ShieldCheck, label: english ? "Attention" : "需关注", value: findings.length }].map(({ icon: Icon, label, value }) => <div className="rounded-xl border bg-card p-4" key={label}><Icon className="h-4 w-4 text-primary" /><div className="mt-3 text-2xl font-semibold">{loading ? "—" : value}</div><div className="text-xs text-muted-foreground">{loading ? (english ? "Loading…" : "加载中…") : label}</div></div>)}
    </section>
    <section className="rounded-xl border bg-card p-4 text-sm" data-testid="quality-source-coverage">
      <p>{english ? "Return and delivery totals count occurrences and may overlap. Unique source IDs: " : "返回与送达按累计次数计数，内容可能重复。去重来源 ID："}{summary.uniqueSourceCount}</p>
      <p className="mt-2 text-xs text-muted-foreground">{english ? "Persisted ledger events: " : "持久质量账本事件："}{bindingSummary.ledger_events} · {english ? "Observed runtime stages: " : "已观测运行阶段："}{(coverage.runtime_stages || []).map(label).join(" · ") || label("not_measured")}</p>
      <p className="mt-2 text-xs text-muted-foreground">{english ? "Capture, extraction, verification, promotion and revalidation coverage remains unknown without corresponding ledger receipts. Runtime retrieval evidence does not establish all-stage completion." : "缺少相应账本回执时，捕获、提炼、核验、晋升和再验证的覆盖仍为未知。运行检索回执不能证明全阶段闭环。"}</p>
      <p className="mt-2 text-xs text-muted-foreground">{english ? "Evidence levels: " : "证据来源等级："}{Object.entries(Object.groupBy(events,event=>event.evidence)).map(([evidence,items])=>`${label(evidence)} ${items?.length || 0}`).join(" · ")}</p>
    </section>
    <section className="grid gap-3 md:grid-cols-3">
      <div className="rounded-xl border bg-card p-4"><div className="text-xs text-muted-foreground">{english ? "Prompt-bound receipts" : "已绑定 Prompt 回执"}</div><div className="mt-2 text-2xl font-semibold">{loading ? "—" : bindingSummary.prompt_bound_events}</div><p className="mt-1 text-xs text-muted-foreground">{english ? "Strong evidence used for quality metrics" : "用于质量统计的强证据"}</p></div>
      <div className="rounded-xl border bg-card p-4"><div className="text-xs text-muted-foreground">{english ? "Unattributed runtime activity" : "未归因运行活动"}</div><div className="mt-2 text-2xl font-semibold">{loading ? "—" : (unattributedActivity.event_count ?? bindingSummary.unattributed_activity)}</div><p className="mt-1 text-xs text-muted-foreground">{english ? "Visible for diagnosis, never counted as Prompt delivery" : "仅用于诊断，不计入 Prompt 送达"}</p></div>
      <div className="rounded-xl border bg-card p-4"><div className="text-xs text-muted-foreground">{english ? "Binding status" : "绑定状态"}</div><div className="mt-2 text-lg font-semibold">{loading ? "—" : bindingSummary.prompt_bound_events ? (english ? "Measured" : "已测量") : (english ? "Waiting for bound receipts" : "等待绑定回执")}</div><p className="mt-1 text-xs text-muted-foreground">{english ? `Planned/no-call records: ${summary.plannedEventCount}` : `计划/未调用记录：${summary.plannedEventCount}`}</p></div>
    </section>
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-4 md:flex-row md:items-center md:justify-between"><div><div className="flex items-center gap-3 text-sm font-semibold"><span>{copy.mode}</span><button type="button" role="switch" aria-checked={enabled} onClick={() => setEngineEnabled(!enabled)} className={`relative h-5 w-9 rounded-full transition-colors ${enabled ? "bg-primary" : "bg-muted"}`}><span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${enabled ? "translate-x-4" : "translate-x-0.5"}`} /></button><span className="text-xs font-normal text-muted-foreground">{enabled ? (english ? "Enabled" : "已开启") : (english ? "Disabled" : "已关闭")}</span></div><div className="mt-1 text-xs text-muted-foreground">{copy.modeHint}{lastRefresh ? ` · ${lastRefresh}` : ""}</div></div><div className="flex flex-wrap gap-2">{(["lightweight", "diagnostic", "deep_audit"] as MemoryQualityMode[]).map((value) => <button key={value} type="button" disabled={!enabled} onClick={() => setObservationMode(value)} className={`rounded-full border px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50 ${mode === value ? "border-primary bg-primary/10 text-primary" : "hover:bg-accent"}`}>{copy[value]}</button>)}</div></section>
    <nav className="flex gap-1 overflow-x-auto rounded-xl border bg-card p-1" aria-label={english ? "Quality engine sections" : "质量引擎分区"}>{(Object.keys(tabs) as Array<keyof typeof tabs>).map((key) => <button key={key} type="button" onClick={() => setTab(key)} className={`whitespace-nowrap rounded-lg px-3 py-2 text-xs font-medium ${tab === key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent"}`}>{tabs[key]}</button>)}</nav>
    {tab === "dimensions" && <section className="rounded-xl border bg-card p-4"><h2 className="text-sm font-semibold">{tabs.dimensions}</h2><div className="mt-4 grid gap-3 md:grid-cols-2">{dimensions.length ? dimensions.map((dimension) => { const returned = dimension.events.reduce((sum, event) => sum + (event.returned_count ?? 0), 0); const delivered = dimension.events.reduce((sum, event) => sum + (event.delivered_count ?? 0), 0); return <article key={dimension.name} className="rounded-lg border p-4"><div className="font-mono text-xs">{dimension.name}</div><div className="mt-3 grid grid-cols-3 gap-2 text-xs"><span>{english ? "Events" : "事件"}<b className="block text-lg">{dimension.events.length}</b></span><span>{english ? "Returned" : "返回"}<b className="block text-lg">{returned}</b></span><span>{english ? "Delivered" : "送达"}<b className="block text-lg">{delivered}</b></span></div></article>; }) : <p className="text-sm text-muted-foreground">{copy.noData}</p>}</div></section>}
    {tab === "retrieval" && <section className="rounded-xl border bg-card p-4"><h2 className="text-sm font-semibold">{tabs.retrieval}</h2><p className="mt-2 text-xs text-muted-foreground">{english ? "Route status remains receipt-derived; not_called, returned_zero, returned, and delivered are never collapsed." : "路线状态来自真实回执；未调用、调用为空、返回和送达不会被合并。"}</p><div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{Object.entries(summary.byStatus).map(([status, items]) => <div key={status} className="rounded-lg border p-3"><div className="text-xs text-muted-foreground">{label(status)}</div><div className="mt-1 text-2xl font-semibold">{items?.length ?? 0}</div></div>)}</div></section>}
    {tab === "cost" && <section className="rounded-xl border bg-card p-4"><h2 className="text-sm font-semibold">{tabs.cost}</h2><div className="mt-4 grid gap-3 md:grid-cols-3"><div className="rounded-lg border p-4"><div className="text-xs text-muted-foreground">{english ? "Extra tokens" : "额外 Token"}</div><div className="mt-1 text-2xl font-semibold">{summary.totals.extraTokens}</div><p className="mt-1 text-xs text-muted-foreground">{english ? "Lightweight projection only" : "仅轻量投影"}</p></div><div className="rounded-lg border p-4"><div className="text-xs text-muted-foreground">{english ? "Extra LLM calls" : "额外模型调用"}</div><div className="mt-1 text-2xl font-semibold">{summary.totals.llmCalls}</div></div><div className="rounded-lg border p-4"><div className="text-xs text-muted-foreground">{english ? "Measured storage" : "已测量存储"}</div><div className="mt-1 text-2xl font-semibold">{summary.eventCount}</div><p className="mt-1 text-xs text-muted-foreground">{english ? "events in current sample" : "当前样本事件数"}</p></div></div></section>}
    {tab === "regression" && <section className="rounded-xl border bg-card p-4"><div className="flex items-start justify-between gap-3"><div><h2 className="text-sm font-semibold">{tabs.regression}</h2><p className="mt-2 text-xs text-muted-foreground">{english ? "Fixture evidence and real historical evidence are labelled separately. No regression action mutates production memory." : "夹具证据与真实历史证据分开标记；回归实验不会改写生产记忆。"}</p></div><ActionButton variant="outline" type="button" onAction={runDeepAudit} disabled={auditRunning} className="rounded-lg border px-3 py-2 text-xs font-medium hover:bg-accent disabled:opacity-50">{auditRunning ? (english ? "Running…" : "运行中…") : (english ? "Run fixture regression" : "运行夹具回归")}</ActionButton></div>{regressionResults ? <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[640px] text-left text-xs"><thead className="border-b text-muted-foreground"><tr><th className="px-3 py-2">{english ? "Case" : "案例"}</th><th className="px-3 py-2">{english ? "Expected" : "预期"}</th><th className="px-3 py-2">{english ? "Actual" : "实际"}</th><th className="px-3 py-2">{english ? "Result" : "结果"}</th></tr></thead><tbody className="divide-y">{regressionResults.map((result) => <tr key={result.id}><td className="px-3 py-2">{result.label}</td><td className="px-3 py-2 font-mono">{result.expected.join(", ")}</td><td className="px-3 py-2 font-mono">{result.actual.join(", ")}</td><td className="px-3 py-2"><span className={`rounded-full px-2 py-1 ${result.passed ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800"}`}>{result.passed ? (english ? "Pass" : "通过") : (english ? "Fail" : "失败")}</span></td></tr>)}</tbody></table><p className="mt-3 text-xs text-muted-foreground">{english ? "Deterministic fixture run; extra LLM calls: 0." : "确定性夹具运行；额外模型调用：0。"}</p></div> : <div className="mt-4 rounded-lg border border-dashed p-5 text-sm text-muted-foreground">{english ? "Run the bounded fixture set to verify no-call, empty, preference-only, history-only, and mixed routes." : "运行受控夹具，验证未调用、调用为空、仅偏好、仅历史和混合路线。"}</div>}</section>}
    {tab === "settings" && <section className="rounded-xl border bg-card p-4"><h2 className="text-sm font-semibold">{tabs.settings}</h2><div className="mt-4 space-y-3 text-sm"><label className="flex items-center justify-between gap-4 rounded-lg border p-3"><span>{english ? "Enable quality observation" : "启用质量观测"}</span><input type="checkbox" checked={enabled} onChange={(event) => setEngineEnabled(event.target.checked)} /></label><label className="flex items-center justify-between gap-4 rounded-lg border p-3"><span>{english ? "Allow source text sampling" : "允许原文采样"}</span><input type="checkbox" defaultChecked={false} /></label><ActionButton variant="outline" type="button" onAction={runDeepAudit} disabled={auditRunning} preserveLabel className="block h-auto w-full whitespace-normal rounded-lg border p-3 text-left text-foreground hover:bg-accent disabled:opacity-50"><span className="font-medium">{auditRunning ? (english ? "Running deterministic audit…" : "正在运行确定性审计…") : (english ? "Run deterministic deep audit" : "运行确定性深度审计")}</span><span className="mt-1 block text-xs text-muted-foreground">{english ? "No LLM call; writes no memory truth." : "不调用模型；不改写记忆真相。"}</span></ActionButton><div className="rounded-lg border p-3 text-xs text-muted-foreground">{english ? "Retention, sampling ratio, dimension/stage switches, and evaluator calls remain explicit settings; no hidden LLM call is made by this page." : "保留周期、采样比例、维度/阶段开关和评估器调用都必须显式设置；本页面不会隐藏调用模型。"}</div></div></section>}
    {tab === "overview" && <section className="grid gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(18rem,.8fr)]"><div className="rounded-xl border bg-card p-4"><div className="flex items-start justify-between gap-3"><div><h2 className="text-sm font-semibold">{english ? "Memory activity over time" : "记忆活动趋势"}</h2><p className="mt-1 text-xs text-muted-foreground">{english ? "Measured events only; no event is synthesized when the receipt is absent." : "只显示已测量事件；没有回执时不会合成数据。"}</p></div><span className="rounded-full border px-2 py-1 text-[10px] text-muted-foreground">{english ? "Measured" : "已测量"}</span></div><div className="mt-4 h-64">{activity.length ? <ResponsiveContainer width="100%" height="100%"><LineChart data={activity}><CartesianGrid strokeDasharray="3 3" opacity={.25}/><XAxis dataKey="bucket" tick={{fontSize:10}}/><YAxis allowDecimals={false} tick={{fontSize:10}}/><Tooltip/><Line type="monotone" dataKey="user" name={english ? "User memory" : "用户记忆"} stroke="#22a7f0" strokeWidth={2}/><Line type="monotone" dataKey="agent" name={english ? "Agent process" : "智能体过程"} stroke="#8b5cf6" strokeWidth={2}/><Line type="monotone" dataKey="scenario" name={english ? "Scenario" : "情景摘要"} stroke="#10b981" strokeWidth={2}/><Line type="monotone" dataKey="rag" name="External RAG" stroke="#6366f1" strokeWidth={2}/></LineChart></ResponsiveContainer> : <div className="flex h-full items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">{loading ? (english ? "Loading measured activity…" : "正在加载已测量活动…") : copy.noData}</div>}</div></div><div className="rounded-xl border bg-card p-4"><div className="flex items-center justify-between"><h2 className="text-sm font-semibold">{english ? "Alerts" : "告警"}</h2><span className="text-xs text-muted-foreground">{findings.length}</span></div><div className="mt-4 space-y-2">{findings.slice(0,4).map((finding) => <button type="button" key={finding.event_id+finding.code} onClick={() => { const event = events.find((candidate) => candidate.event_id === finding.event_id); if (event) setSelected(event); }} className="w-full rounded-lg border p-3 text-left hover:bg-accent"><div className="flex items-center justify-between gap-2"><span className={`rounded-full px-2 py-1 text-[10px] ${finding.severity === "warning" ? "bg-amber-100 text-amber-800" : "bg-blue-100 text-blue-800"}`}>{finding.severity}</span><span className="font-mono text-[10px] text-muted-foreground">{finding.code}</span></div><p className="mt-2 text-xs leading-5">{finding.message}</p></button>)}{!findings.length && <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">{loading ? (english ? "Loading alerts…" : "正在加载告警…") : (english ? "No quality findings in the measured sample." : "已测量样本中没有质量告警。")}</div>}</div></div></section>}
    {tab === "overview" && <section className="rounded-xl border bg-card p-4"><div className="flex items-center justify-between"><div><h2 className="text-sm font-semibold">{english ? "Recent activity" : "最近活动"}</h2><p className="mt-1 text-xs text-muted-foreground">{english ? "Click an event to inspect its source and counts." : "点击事件查看来源和计数。"}</p></div><span className="text-xs text-muted-foreground">{recent.length}</span></div>{recent.length ? <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[720px] text-left text-xs"><thead className="border-b text-muted-foreground"><tr><th className="px-3 py-2">{english ? "Time" : "时间"}</th><th className="px-3 py-2">{english ? "Route" : "路线"}</th><th className="px-3 py-2">{english ? "Memory plane" : "记忆平面"}</th><th className="px-3 py-2">{english ? "Status" : "状态"}</th><th className="px-3 py-2">{english ? "Counts" : "计数"}</th></tr></thead><tbody className="divide-y">{recent.slice(0,8).map((event) => <tr key={`recent-${event.event_id}`} onClick={() => setSelected(event)} className="cursor-pointer hover:bg-accent"><td className="px-3 py-2 font-mono">{event.created_at}</td><td className="px-3 py-2">{event.route}</td><td className="px-3 py-2">{label(event.plane)}</td><td className="px-3 py-2">{label(event.status)}</td><td className="px-3 py-2 font-mono">{event.candidate_count ?? "—"} / {event.returned_count ?? "—"} / {event.delivered_count ?? "—"}</td></tr>)}</tbody></table></div> : <div className="mt-4 rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">{loading ? (english ? "Loading recent activity…" : "正在加载最近活动…") : (english ? "No prompt-bound activity to display." : "当前没有可显示的 Prompt 绑定活动。")}</div>}</section>}
    <section className="overflow-hidden rounded-xl border bg-card"><div className="border-b px-4 py-3 text-sm font-semibold">{english ? "Canonical receipt projection" : "规范回执投影"}</div>{!recent.length ? <div className="px-4 py-12 text-center text-sm text-muted-foreground">{copy.noData}</div> : <div className="overflow-x-auto"><table className="w-full min-w-[760px] text-left text-sm"><thead className="bg-muted/40 text-xs text-muted-foreground"><tr><th className="px-4 py-3">{copy.status}</th><th className="px-4 py-3">{copy.route}</th><th className="px-4 py-3">{english ? "Plane" : "平面"}</th><th className="px-4 py-3">{english ? "Dimension" : "维度"}</th><th className="px-4 py-3">{copy.counts}</th><th className="px-4 py-3">{english ? "Evidence" : "证据"}</th></tr></thead><tbody className="divide-y">{recent.map((event) => <tr key={event.event_id} tabIndex={0} role="button" onClick={() => setSelected(event)} onKeyDown={(e) => e.key === "Enter" && setSelected(event)} className="cursor-pointer hover:bg-accent/50"><td className="px-4 py-3"><span className={`rounded-full px-2 py-1 text-xs ${event.status === "delivered" ? "bg-emerald-100 text-emerald-800" : event.status === "returned_zero" || event.status === "not_called" ? "bg-amber-100 text-amber-800" : "bg-muted"}`}>{event.status}</span></td><td className="px-4 py-3 font-mono text-xs">{event.route}</td><td className="px-4 py-3">{label(event.plane)}</td><td className="px-4 py-3">{event.dimension}</td><td className="px-4 py-3 font-mono text-xs">{event.candidate_count ?? "—"} / {event.returned_count ?? "—"} / {event.delivered_count ?? "—"}</td><td className="px-4 py-3 text-xs text-muted-foreground">{event.evidence}</td></tr>)}</tbody></table></div>}</section>
    {selected && <div role="dialog" aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4" onClick={() => setSelected(null)}><div className="max-h-[80vh] w-full max-w-xl overflow-y-auto rounded-2xl border bg-card p-6 shadow-2xl" onClick={(event) => event.stopPropagation()}><div className="flex items-start justify-between gap-4"><div><div className="text-xs font-bold uppercase tracking-wider text-primary">{english ? "Event detail" : "事件详情"}</div><h2 className="mt-1 text-lg font-semibold">{selected.route}</h2></div><button type="button" onClick={() => setSelected(null)} className="rounded-md border px-2 py-1 text-sm">×</button></div><p className="mt-4 text-xs text-muted-foreground">{label(selected.evidence)} · {label(selected.stage)} · {selected.purpose ? label(selected.purpose) : label("not_measured")}</p><dl className="mt-5 grid grid-cols-2 gap-3 text-sm"><div><dt className="text-xs text-muted-foreground">{english ? "Status" : "状态"}</dt><dd className="font-medium">{label(selected.status)}</dd></div><div><dt className="text-xs text-muted-foreground">{english ? "Plane / dimension" : "平面 / 维度"}</dt><dd className="font-medium">{label(selected.plane)} / {label(selected.dimension)}</dd></div><div><dt className="text-xs text-muted-foreground">{english ? "Prompt" : "Prompt"}</dt><dd className="break-all font-mono text-xs">{selected.prompt_id}</dd></div><div><dt className="text-xs text-muted-foreground">{english ? "Trace" : "Trace"}</dt><dd className="break-all font-mono text-xs">{selected.trace_id}</dd></div><div className="col-span-2"><dt className="text-xs text-muted-foreground">{copy.counts}</dt><dd className="font-mono">{selected.candidate_count ?? "—"} / {selected.returned_count ?? "—"} / {selected.delivered_count ?? "—"}</dd></div></dl></div></div>}
  </div>;
}
