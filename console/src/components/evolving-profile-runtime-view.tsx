"use client";

import { ActionButton } from "@/components/ui/action-button";

import { useCallback, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Activity, Bot, Cloud, Database, HardDrive, RefreshCw, Route, ShieldCheck } from "lucide-react";
import { AgentProcessView } from "./agent-process-view";
import { ContextMemoryView } from "./context-memory-view";
import { GraphWindowControls } from "./graph-window-controls";
import { useWindowedGraph } from "@/lib/use-windowed-graph";
import { RecallPolicySettings, type RecallPolicyValue, updateRecallPolicy } from "./recall-policy-settings";
import { normalizeRecallPolicy } from "@/lib/recall-policy";

import { inlineUiText } from "@/lib/inline-i18n";
type RuntimeState = {
  model: { provider: string; model: string; baseUrl: string; apiKey: string | null };
  services: Array<{ name: string; url: string; status: "healthy" | "attention" | "unavailable"; detail: string }>;
  hostIntegration: { hosts: string[]; hookStages: string[]; mcpServer: string; entry: string };
  behavior: { guidance: string; retrieval: string; controller: string; background: string; backup: string };
  backup: {
    settings?: any;
    local: { status: string; setCount: number; fileCount: number; totalBytes: number; latestSet: string | null; latestAt: string | null; latestBytes: number; latestVerified: boolean; artifacts: { database: boolean; config: boolean; capture: boolean }; retentionDays: number; location: string };
    cloud: { status: string; mirrorSetCount: number; totalBytes: number; latestSet: string | null; latestAt: string | null; ageHours: number | null; encrypted: boolean; plaintextFiles: number; wpsCache: { present: boolean; fileCount: number; timedOut?: boolean; probeSkipped?: boolean }; readback?: { observed: boolean; fileName?: string; fileId?: string; fileSize?: number; completeSize?: number; errorCode?: number; completedAt?: string | null }; expectedRetentionSets: number; location: string; encryption: string };
    job: { loaded: boolean; running: boolean; lastExitCode: number | null; schedule: string };
  };
  guidanceSettings?: { max_candidates: number; adaptive_budget: boolean; auto_probe: boolean; probe_max_tokens: number };
  runtimeSettings: {
    recall_policy?: RecallPolicyValue;
    modules: Record<string, { record: boolean; retrieve: boolean; inject: boolean }>;
    routing: { mode: string; ep_enabled: boolean; external_rag_enabled: boolean; allow_parallel?: boolean; conflict_policy?: string };
    budgets: Record<string, number>;
    rag: { enabled: boolean; root_path: string; collection?: string; lexical_enabled?: boolean; vector_enabled?: boolean; fusion?: string; rerank_enabled?: boolean; rerank_provider?: string; rerank_model?: string; top_k?: number; score_threshold?: number; max_chunks?: number; auto_index?: boolean };
    providers: { primary?: { name?: string; base_url?: string; model?: string; api_key?: string | null }; fallbacks?: Array<{ name?: string; base_url?: string; model?: string; api_key?: string | null }> };
  };
  processMemory?: { status: string; record_count: number; by_kind: Record<string, number>; profiles: number; revalidation_queue?: number; updated_at?: string | null; recent?: Array<{ id: string; kind: string; phase: string; maturity: string; outcome: string; task_archetype?: string[]; dimensions?: string[]; model_family?: string | null; at?: string | null; source_count: number }>; graph?: { nodes: Array<{ id: string; type: string; label: string; phase?: string; maturity?: string; status?: string; at?: string | null }>; edges: Array<{ source: string; target: string; type: string }>; timeline: Array<{ id: string; type: string; at?: string | null; label: string; status?: string }> } };
  context: { bankId?: string; version?: string; graphStats?: { nodes:number;edges:number;timeline:number }; status: string; schema: string; sessionCount: number; projectCount: number; pendingReview: number; qualityStatus?: string; pipeline: string; executionOwner?: string; externalEpModel?: string; sourceOfTruth: string; evidenceRole: string; updatedAt: string | null; progress?: { status: string; total: number; queued: number; running: number; retrying: number; succeeded: number; failed: number; review_pending?: number; updated_at?: string }; audit?: { status: string; error_count?: number | null; warning_count?: number | null; audited_at?: string; semantic_sample?: { method?: string } }; graph?: { nodes: Array<{ id: string; type: string; label: string; projectKey?: string; sessionCount?: number; status?: string }>; edges: Array<{ source: string; target: string; type: string }>; timeline: Array<{ id: string; type: string; at?: string; label: string; status?: string }>; bankRecordLinks: { available: boolean; linked: number; sampled?: number; scanned?: number; reason: string } } };
};

const STATUS_STYLE: Record<string, string> = {
  healthy: "bg-emerald-500",
  attention: "bg-amber-500",
  unavailable: "bg-rose-500",
};

function formatBytes(value: number) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, value); let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

function formatDate(value: string | null, locale: string) {
  return value ? new Date(value).toLocaleString(locale.startsWith("zh") ? locale : "en-US", { hour12: false }) : "—";
}

const CLOUD_LABEL: Record<string, string> = { observed: "已观测", unverified: "待云端回读", stale: "已滞后", missing: "未发现" };

export function EvolvingProfileRuntimeView() {
  const locale = useLocale();
  const releaseText = useTranslations("releaseUi");
  const english = !locale.startsWith("zh");
  const [state, setState] = useState<RuntimeState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingBackup, setSavingBackup] = useState(false);
  const [backupMessage, setBackupMessage] = useState<string | null>(null);
  const [guidanceMessage, setGuidanceMessage] = useState<string | null>(null);
  const [runtimeSettingsMessage, setRuntimeSettingsMessage] = useState<string | null>(null);
  const [processGraphEnabled,setProcessGraphEnabled] = useState(false);
  const [scenarioGraphEnabled,setScenarioGraphEnabled] = useState(false);
  const processWindow = useWindowedGraph("/api/evolving-profile/process-memory/graph","",processGraphEnabled);
  const [runtimeTab, setRuntimeTab] = useState<"memory" | "rag" | "providers" | "scenario" | "backup">("memory");
  const [directoryMessage, setDirectoryMessage] = useState<string | null>(null);
  const [editVersions, setEditVersions] = useState({ runtime: 0, guidance: 0, backup: 0 });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/evolving-profile/runtime", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || inlineUiText("读取失败"));
      setState(body); setLoadError(null);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : inlineUiText("读取失败"));
      throw error;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load().catch(() => undefined);
  }, [load]);

  const saveBackupSettings = useCallback(async () => {
    if (!state?.backup.settings) return false;
    setSavingBackup(true); setBackupMessage(null);
    try {
      const response = await fetch("/api/evolving-profile/backup-settings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(state.backup.settings) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || inlineUiText("保存失败"));
      setBackupMessage(inlineUiText("备份设置已保存并应用"));
    } catch (error) { setBackupMessage(error instanceof Error ? error.message : inlineUiText("保存失败")); throw error; }
    finally { setSavingBackup(false); }
  }, [load, state]);

  const saveGuidanceSettings = useCallback(async () => {
    if (!state?.guidanceSettings) return false;
    setGuidanceMessage(null);
    try {
      const response = await fetch("/api/evolving-profile/guidance-settings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(state.guidanceSettings) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || inlineUiText("保存失败"));
      setGuidanceMessage(inlineUiText("检索设置已保存，下次入口读取生效"));
    } catch (error) { setGuidanceMessage(error instanceof Error ? error.message : inlineUiText("保存失败")); throw error; }
  }, [load, state]);

  const saveRuntimeSettings = useCallback(async () => {
    if (!state?.runtimeSettings) return false;
    setRuntimeSettingsMessage(null);
    try {
      const response = await fetch("/api/evolving-profile/runtime-settings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(state.runtimeSettings) });
      const body = await response.json();
      if (!response.ok || body.saved === false) throw new Error(body.error || inlineUiText("运行配置保存失败"));
      setRuntimeSettingsMessage(inlineUiText("运行配置已保存；新一轮入口读取时生效"));
    } catch (error) { setRuntimeSettingsMessage(error instanceof Error ? error.message : inlineUiText("运行配置保存失败")); throw error; }
  }, [load, state]);

  const saveRecallPolicy = useCallback(async () => {
    if (!state?.runtimeSettings) return false;
    const submitted = normalizeRecallPolicy(state.runtimeSettings.recall_policy);
    setRuntimeSettingsMessage(null);
    try {
      const response = await fetch("/api/evolving-profile/runtime-settings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ recall_policy: submitted }) });
      const body = await response.json();
      if (!response.ok || body.saved === false) throw new Error(body.error || inlineUiText("运行配置保存失败"));
      const saved = normalizeRecallPolicy(body.recall_policy ?? submitted);
      setState((previous) => {
        if (!previous || JSON.stringify(normalizeRecallPolicy(previous.runtimeSettings.recall_policy)) !== JSON.stringify(submitted)) return previous;
        return { ...previous, runtimeSettings: { ...previous.runtimeSettings, recall_policy: saved } };
      });
      setRuntimeSettingsMessage(inlineUiText("运行配置已保存；新一轮入口读取时生效"));
    } catch (error) { setRuntimeSettingsMessage(error instanceof Error ? error.message : inlineUiText("运行配置保存失败")); throw error; }
  }, [state]);

  const chooseRagDirectory = useCallback(async () => {
    setDirectoryMessage(inlineUiText("正在打开系统目录选择器…"));
    try {
      const response = await fetch("/api/evolving-profile/select-directory", { method: "POST" });
      const body = await response.json();
      if (body.canceled) { setDirectoryMessage(inlineUiText("已取消选择")); return false; }
      if (!response.ok) throw new Error(body.error || inlineUiText("目录选择失败"));
      setState((previous) => previous ? { ...previous, runtimeSettings: { ...previous.runtimeSettings, routing: { ...previous.runtimeSettings.routing, external_rag_enabled: true }, rag: { ...previous.runtimeSettings.rag, root_path: body.path, enabled: true } } } : previous);
      setDirectoryMessage(inlineUiText("目录已选择，保存后生效"));
    } catch (error) { setDirectoryMessage(error instanceof Error ? error.message : inlineUiText("目录选择失败")); throw error; }
  }, []);

  if (!state) {
    return <div className="rounded-lg border p-6 text-sm text-muted-foreground">{loadError ? <><p role="alert">{loadError}</p><ActionButton onAction={load}>{english ? "Retry" : "重试"}</ActionButton></> : english ? "Loading Evolving Profile runtime configuration..." : inlineUiText("正在读取 Evolving Profile 运行配置…")}</div>;
  }

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">{english ? "Evolving Profile 5.1 Runtime Configuration" : inlineUiText("Evolving Profile 5.1 运行配置")}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{inlineUiText("只显示当前系统实际使用的链路；密钥永不在界面显示明文。")}</p>
        </div>
        <ActionButton variant="outline" size="icon" aria-label={inlineUiText("刷新运行状态")} onAction={() => load()} className="inline-flex h-9 w-9 items-center justify-center rounded-md border hover:bg-muted" title={inlineUiText("刷新运行状态")}>
          <RefreshCw className="h-4 w-4" />
        </ActionButton>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <article className="rounded-lg border p-4">
          <div className="flex items-center gap-2 text-sm font-semibold"><Bot className="h-4 w-4 text-primary" />{inlineUiText("后台加工模型")}</div>
          <dl className="mt-4 grid grid-cols-[110px_1fr] gap-y-3 text-sm">
            <dt className="text-muted-foreground">{inlineUiText("提供商")}</dt><dd>{state.model.provider}</dd>
            <dt className="text-muted-foreground">{inlineUiText("模型")}</dt><dd>{state.model.model}</dd>
            <dt className="text-muted-foreground">{inlineUiText("API 密钥")}</dt><dd>{state.model.apiKey ?? inlineUiText("未配置")}</dd>
            <dt className="text-muted-foreground">{inlineUiText("服务地址")}</dt><dd className="truncate font-mono text-xs">{state.model.baseUrl}</dd>
          </dl>
        </article>
        <article className="rounded-lg border p-4">
          <div className="flex items-center gap-2 text-sm font-semibold"><Route className="h-4 w-4 text-primary" />{inlineUiText("宿主接入")}</div>
          <p className="mt-3 text-sm"><span className="text-muted-foreground">MCP：</span>{state.hostIntegration.mcpServer}</p>
          <p className="mt-2 text-sm"><span className="text-muted-foreground">{inlineUiText("入口：")}</span>{state.hostIntegration.entry}</p>
          <p className="mt-2 text-sm"><span className="text-muted-foreground">{inlineUiText("宿主：")}</span>{state.hostIntegration.hosts.join(" · ")}</p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {state.hostIntegration.hookStages.map((stage) => <span key={stage} className="rounded bg-muted px-2 py-1 font-mono text-[11px]">{stage}</span>)}
          </div>
        </article>
      </div>

      <div className="rounded-lg border p-4">
        <div className="flex items-center gap-2 text-sm font-semibold"><Activity className="h-4 w-4 text-primary" />{inlineUiText("本机服务")}</div>
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          {state.services.map((service) => (
            <div key={service.name} className="rounded-md border p-3">
              <div className="flex items-center gap-2 text-sm font-medium"><span className={`h-2 w-2 rounded-full ${STATUS_STYLE[service.status]}`} />{service.name}</div>
              <p className="mt-2 truncate font-mono text-[11px] text-muted-foreground">{service.url}</p>
              <p className="mt-1 text-xs text-muted-foreground">{service.detail}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="rounded-lg border p-4">
        <div className="flex items-center justify-between gap-3"><div><div className="flex items-center gap-2 text-sm font-semibold"><Bot className="h-4 w-4 text-primary" />{english ? "Agent Process Memory · EP5.1" : inlineUiText("Agent 过程记忆 · EP5.1")}</div><p className="mt-1 text-xs text-muted-foreground">{english ? "Trajectories, failures, repair patterns, capability profiles, and reusable process strategies are isolated; candidates are gated by maturity and compatibility, and the Agent explicitly requests a hint packet before using them." : inlineUiText("轨迹、失败事件、修复模式、能力画像和可复用过程策略独立保存；候选必须经过成熟度和兼容性门控，过程提示由 Agent 显式请求后再决定是否采用。")}</p></div><span className="text-xs text-muted-foreground">{state.processMemory?.status === "ready" ? (english ? "Ready" : inlineUiText("已就绪")) : (english ? "No records" : inlineUiText("尚无记录"))}</span></div>
        <div className="mt-3 grid grid-cols-2 gap-3 text-sm md:grid-cols-5"><div className="rounded border p-3"><div className="text-xs text-muted-foreground">{inlineUiText("过程记录")}</div><div className="mt-1 text-lg font-semibold">{state.processMemory?.record_count ?? 0}</div></div><div className="rounded border p-3"><div className="text-xs text-muted-foreground">{inlineUiText("失败事件")}</div><div className="mt-1 text-lg font-semibold">{state.processMemory?.by_kind?.episode ?? 0}</div></div><div className="rounded border p-3"><div className="text-xs text-muted-foreground">{inlineUiText("修复模式")}</div><div className="mt-1 text-lg font-semibold">{state.processMemory?.by_kind?.pattern ?? 0}</div></div><div className="rounded border p-3"><div className="text-xs text-muted-foreground">{inlineUiText("可复用过程策略")}</div><div className="mt-1 text-lg font-semibold">{state.processMemory?.by_kind?.skill ?? 0}</div></div><div className="rounded border p-3"><div className="text-xs text-muted-foreground">{inlineUiText("待再验证")}</div><div className="mt-1 text-lg font-semibold">{state.processMemory?.revalidation_queue ?? 0}</div></div></div>
        <div className="mt-4 overflow-x-auto rounded border"><table className="w-full min-w-[720px] text-left text-xs"><thead className="bg-muted/50 text-muted-foreground"><tr><th className="px-3 py-2">{inlineUiText("时间")}</th><th className="px-3 py-2">{inlineUiText("类型")}</th><th className="px-3 py-2">{inlineUiText("阶段")}</th><th className="px-3 py-2">{inlineUiText("任务族")}</th><th className="px-3 py-2">{inlineUiText("成熟度")}</th><th className="px-3 py-2">{inlineUiText("来源")}</th></tr></thead><tbody>{(state.processMemory?.recent ?? []).slice(0, 12).map((row) => <tr key={row.id} className="border-t"><td className="px-3 py-2 text-muted-foreground">{row.at ? new Date(row.at).toLocaleString(english ? "en-US" : locale, { hour12: false }) : "—"}</td><td className="px-3 py-2 font-medium">{({ trace: inlineUiText("轨迹"), episode: inlineUiText("失败事件"), pattern: inlineUiText("修复模式"), skill: inlineUiText("可复用过程策略"), capability_observation: inlineUiText("能力观测") } as Record<string, string>)[row.kind] ?? row.kind}</td><td className="px-3 py-2">{({ understand: inlineUiText("理解"), plan: inlineUiText("规划"), retrieve: inlineUiText("检索"), act: inlineUiText("执行"), observe: inlineUiText("观察"), verify: inlineUiText("验证"), recover: inlineUiText("恢复"), deliver: inlineUiText("交付"), reflect: inlineUiText("反思") } as Record<string, string>)[row.phase] ?? row.phase}</td><td className="px-3 py-2">{(row.task_archetype ?? []).join("、") || inlineUiText("其他")}</td><td className="px-3 py-2">{({ observed: inlineUiText("已观察"), diagnosed: inlineUiText("已诊断"), repaired: inlineUiText("已修复"), verified: inlineUiText("已验证"), replicated: inlineUiText("已复现"), generalized: inlineUiText("已泛化"), deprecated: inlineUiText("已弃用") } as Record<string, string>)[row.maturity] ?? row.maturity}</td><td className="px-3 py-2">{row.source_count}</td></tr>)}</tbody></table>{!(state.processMemory?.recent ?? []).length && <div className="px-3 py-4 text-xs text-muted-foreground">{inlineUiText("还没有过程轨迹记录。启用记录后，工具回执会逐步出现在这里。")}</div>}</div>
        <div className="mt-4 rounded border p-3">
          <button type="button" className="rounded border px-3 py-2 text-xs" onClick={()=>setProcessGraphEnabled(value=>!value)}>{english ? "Agent process graph and timeline" : inlineUiText("Agent 过程图谱与时间线")}</button>
          {processGraphEnabled ? <div className="mt-3"><GraphWindowControls windowed={processWindow} />{processWindow.data ? <AgentProcessView key={`${processWindow.data.version}:${processWindow.data.page.offset}`} version={processWindow.data.version} onRefresh={processWindow.refresh} graph={processWindow.data.graph} english={english} /> : null}</div> : null}
          <p className="mt-2 text-[11px] text-muted-foreground">{english ? "Process memory remains separate from Facts, Experiences, and Preferences; edges show derivation only, not fact replacement." : inlineUiText("过程经验与 Facts、Experiences、Preferences 保持分层；连线只表示派生关系，不代表事实覆盖。")}</p>
        </div>
      </div>

      <RecallPolicySettings value={state.runtimeSettings.recall_policy} onChange={(patch) => setState((previous) => previous ? { ...previous, runtimeSettings: updateRecallPolicy(previous.runtimeSettings, patch) } : previous)} onSave={saveRecallPolicy} />
      <article id="ep-memory-settings" className="rounded-lg border p-4">
        <div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold">{inlineUiText("模块与外部 RAG")}</h3><p className="mt-1 text-xs text-muted-foreground">{inlineUiText("EP 内部记忆和外部文件 RAG 使用独立来源与预算；关闭模块后，本轮不会调用对应能力。")}</p></div>{runtimeSettingsMessage && <span className="text-xs text-emerald-700">{runtimeSettingsMessage}</span>}</div>
        <nav className="mt-4 flex flex-wrap gap-1 border-b pb-2" aria-label={inlineUiText("EP5.1 配置分区")}><a href="#ep-memory-settings" className="rounded px-3 py-2 text-xs text-primary hover:bg-muted">{inlineUiText("EP 记忆")}</a><a href="#ep-rag-settings" className="rounded px-3 py-2 text-xs text-primary hover:bg-muted">{inlineUiText("外部 RAG")}</a><a href="#ep-provider-settings" className="rounded px-3 py-2 text-xs text-primary hover:bg-muted">{inlineUiText("Provider 与 Fallback")}</a><a href="#ep-scenario-settings" className="rounded px-3 py-2 text-xs text-primary hover:bg-muted">{inlineUiText("情景摘要")}</a><a href="#ep-backup-settings" className="rounded px-3 py-2 text-xs text-primary hover:bg-muted">{inlineUiText("备份")}</a></nav>
        <form onSubmit={(event) => event.preventDefault()} onChangeCapture={() => setEditVersions((previous) => ({ ...previous, runtime: previous.runtime + 1 }))} className="mt-4 space-y-5 text-sm">
          <div><div className="mb-2 text-xs font-semibold text-muted-foreground">{english ? "EP modules: record / retrieve / inject" : inlineUiText("EP 内部模块：记录 / 检索 / 注入")}</div><div className="grid gap-2 md:grid-cols-2">{Object.entries(state.runtimeSettings.modules).map(([name, settings]) => <div key={name} className="rounded border p-3"><div className="mb-2 flex items-center justify-between gap-2 font-medium"><span>{english ? name : ({ facts: inlineUiText("事实"), experiences: inlineUiText("经历"), entities: inlineUiText("实体与关系"), preferences: inlineUiText("多维度偏好"), scenario_summary: inlineUiText("情景摘要"), mental_models: inlineUiText("融合心智模型"), source_readback: inlineUiText("原文回读"), background_reflection: inlineUiText("后台记录与反思"), agent_process_memory: inlineUiText("Agent 过程记忆") } as Record<string, string>)[name] ?? name}</span>{name === "agent_process_memory" && <span className="text-[11px] text-muted-foreground">{english ? "Explicit hint packet + evidence gates" : inlineUiText("显式提示包 + 证据门控")}</span>}</div><div className="flex flex-wrap gap-3 text-xs">{(["record", "retrieve", "inject"] as const).map((action) => <label key={action}><input type="checkbox" checked={settings[action]} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, modules: { ...state.runtimeSettings.modules, [name]: { ...settings, [action]: e.target.checked } } } })} /> {english ? action : ({ record: inlineUiText("记录"), retrieve: inlineUiText("检索"), inject: inlineUiText("注入") } as Record<string, string>)[action]}</label>)}</div></div>)}</div></div>
          <div className="grid gap-4 md:grid-cols-2"><label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("来源路由")}</span><select className="h-9 w-full rounded border bg-background px-2" value={state.runtimeSettings.routing.mode} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, routing: { ...state.runtimeSettings.routing, mode: e.target.value } } })}><option value="auto">{inlineUiText("自动判断")}</option><option value="ep">{inlineUiText("只用 EP 内部记忆")}</option><option value="external_rag">{inlineUiText("只用外部 RAG")}</option><option value="both_isolated">{inlineUiText("两边隔离并行")}</option></select></label><label className="flex items-end gap-2 pb-2 text-xs"><input type="checkbox" checked={state.runtimeSettings.routing.external_rag_enabled} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, routing: { ...state.runtimeSettings.routing, external_rag_enabled: e.target.checked } } })} /> {inlineUiText("允许外部 RAG 路由")}</label></div>
          <div className="rounded border p-3"><div className="mb-3 font-medium">{inlineUiText("外部 RAG")}</div><div className="grid gap-4 md:grid-cols-2"><label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("启用")}</span><input type="checkbox" checked={state.runtimeSettings.rag.enabled} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, rag: { ...state.runtimeSettings.rag, enabled: e.target.checked } } })} /></label><label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("资料目录")}</span><input className="h-9 w-full rounded border bg-background px-2 font-mono text-xs" value={state.runtimeSettings.rag.root_path} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, rag: { ...state.runtimeSettings.rag, root_path: e.target.value } } })} /></label><label className="flex items-end gap-2 text-xs"><input type="checkbox" checked={state.runtimeSettings.rag.lexical_enabled ?? true} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, rag: { ...state.runtimeSettings.rag, lexical_enabled: e.target.checked } } })} /> {inlineUiText("词法检索")}</label><label className="flex items-end gap-2 text-xs"><input type="checkbox" checked={state.runtimeSettings.rag.vector_enabled ?? true} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, rag: { ...state.runtimeSettings.rag, vector_enabled: e.target.checked } } })} /> {inlineUiText("向量检索")}</label><label className="flex items-end gap-2 text-xs"><input type="checkbox" checked={state.runtimeSettings.rag.rerank_enabled ?? true} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, rag: { ...state.runtimeSettings.rag, rerank_enabled: e.target.checked } } })} /> Re-rank</label><label className="space-y-1"><span className="text-xs text-muted-foreground">Top-K</span><input type="number" min="1" max="100" className="h-9 w-24 rounded border bg-background px-2" value={state.runtimeSettings.rag.top_k ?? 20} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, rag: { ...state.runtimeSettings.rag, top_k: Number(e.target.value) } } })} /></label></div></div>
          <div className="grid gap-4 md:grid-cols-3"><label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("EP Token 预算")}</span><input type="number" min="500" max="20000" className="h-9 w-28 rounded border bg-background px-2" value={state.runtimeSettings.budgets.ep_total_tokens ?? 4000} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, budgets: { ...state.runtimeSettings.budgets, ep_total_tokens: Number(e.target.value) } } })} /></label><label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("RAG Token 预算")}</span><input type="number" min="0" max="20000" className="h-9 w-28 rounded border bg-background px-2" value={state.runtimeSettings.budgets.rag_total_tokens ?? 4000} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, budgets: { ...state.runtimeSettings.budgets, rag_total_tokens: Number(e.target.value) } } })} /></label><label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("总 Token 上限")}</span><input type="number" min="500" max="30000" className="h-9 w-28 rounded border bg-background px-2" value={state.runtimeSettings.budgets.total_tokens ?? 6000} onChange={(e) => setState({ ...state, runtimeSettings: { ...state.runtimeSettings, budgets: { ...state.runtimeSettings.budgets, total_tokens: Number(e.target.value) } } })} /></label></div>
          <ActionButton type="submit" resetKey={editVersions.runtime} onAction={saveRuntimeSettings} className="rounded bg-primary px-4 py-2 text-primary-foreground">{inlineUiText("保存配置")}</ActionButton>
        </form>
      </article>

      <article id="ep-scenario-settings" className="rounded-lg border p-4">
        <div className="flex items-center justify-between gap-3">
          <div><div className="flex items-center gap-2 text-sm font-semibold"><Route className="h-4 w-4 text-primary" />{inlineUiText("Scenario Summary 情景摘要")}</div><p className="mt-1 text-xs text-muted-foreground">{inlineUiText("Recall/Research 候选关联后，由 Agent 判断是否调用 read_scenario_summary 下钻。")}</p></div>
          <span className={`rounded-full px-2.5 py-1 text-xs ${state.context.qualityStatus === "reviewed" ? "bg-emerald-100 text-emerald-800" : "bg-amber-100 text-amber-800"}`}>{state.context.qualityStatus === "reviewed" ? inlineUiText("已复核") : inlineUiText("未完成语义复核")}</span>
        </div>
<dl className="mt-4 grid grid-cols-[110px_1fr] gap-y-2 text-xs"><dt className="text-muted-foreground">Session</dt><dd>{state.context.sessionCount}</dd><dt className="text-muted-foreground">{inlineUiText("工作目录候选")}</dt><dd>{state.context.projectCount}</dd><dt className="text-muted-foreground">{inlineUiText("队列状态")}</dt><dd>{state.context.progress?.status ?? inlineUiText("未知")}</dd><dt className="text-muted-foreground">{inlineUiText("处理进度")}</dt><dd>{inlineUiText("总计")} {state.context.progress?.total ?? 0} {inlineUiText("· 排队")} {state.context.progress?.queued ?? 0} {inlineUiText("· 运行")} {state.context.progress?.running ?? 0} {inlineUiText("· 待复核")} {state.context.pendingReview} {inlineUiText("· 失败")} {state.context.progress?.failed ?? 0}</dd><dt className="text-muted-foreground">{inlineUiText("结构检查")}</dt><dd>{state.context.audit?.status ?? inlineUiText("未执行")} {inlineUiText("· 错误")} {state.context.audit?.error_count ?? "—"} {inlineUiText("· 警告")} {state.context.audit?.warning_count ?? "—"}{inlineUiText("；不等于语义审核")}</dd><dt className="text-muted-foreground">{inlineUiText("处理方式")}</dt><dd>{state.context.pipeline}</dd><dt className="text-muted-foreground">{inlineUiText("EP外部检索")}</dt><dd>{state.context.externalEpModel ?? "Coding Plan / Qwen 3.7 Plus"}</dd><dt className="text-muted-foreground">{inlineUiText("证据边界")}</dt><dd>{state.context.evidenceRole}</dd><dt className="text-muted-foreground">{inlineUiText("索引更新")}</dt><dd>{formatDate(state.context.updatedAt, locale)}</dd></dl>
      </article>

      <article className="rounded-lg border p-4">
        <div className="flex items-center justify-between gap-3"><div><div className="flex items-center gap-2 text-sm font-semibold"><Route className="h-4 w-4 text-primary" />{inlineUiText("情景摘要星座图与时间线")}</div><p className="mt-1 text-xs text-muted-foreground">{inlineUiText("工作目录—Session 及部分 Bank 事实/经历是只读关联快照；实体、偏好关联尚未覆盖。")}</p></div><span className="text-xs text-muted-foreground">{inlineUiText("节点")} {state.context.graphStats?.nodes ?? 0} {inlineUiText("· 边")} {state.context.graphStats?.edges ?? 0}</span></div>
        <button type="button" className="mt-3 rounded border px-3 py-2 text-xs" onClick={()=>setScenarioGraphEnabled(value=>!value)}>{releaseText("scenarioGraphLoad")}</button>
        {scenarioGraphEnabled ? <div className="mt-4"><ContextMemoryView bankId={state.context.bankId || null} /></div> : null}
      </article>

      <article className="rounded-lg border p-4">
        <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex items-center gap-2 text-sm font-semibold"><Database className="h-4 w-4 text-primary" />{inlineUiText("数据与备份")}</div><p className="mt-2 text-sm leading-6 text-muted-foreground">{state.behavior.background}</p></div><span className={`rounded-full px-2.5 py-1 text-xs font-medium ${state.backup.job.loaded && state.backup.job.lastExitCode === 0 ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200" : "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200"}`}>{state.backup.job.schedule} {inlineUiText("· 最近退出")} {state.backup.job.lastExitCode ?? inlineUiText("未知")}</span></div>
        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          <div className="rounded-md border p-3">
            <div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-sm font-medium"><HardDrive className="h-4 w-4 text-emerald-600" />{inlineUiText("本地备份")}</div><span className="text-xs text-emerald-700">{state.backup.local.status === "healthy" ? inlineUiText("正常") : inlineUiText("缺失")}</span></div>
            <dl className="mt-3 grid grid-cols-[90px_1fr] gap-y-2 text-xs"><dt className="text-muted-foreground">{inlineUiText("最新时间")}</dt><dd>{formatDate(state.backup.local.latestAt, locale)}</dd><dt className="text-muted-foreground">{inlineUiText("备份集合")}</dt><dd>{state.backup.local.setCount} {inlineUiText("套 ·")} {state.backup.local.fileCount} {inlineUiText("个文件")}</dd><dt className="text-muted-foreground">{inlineUiText("占用空间")}</dt><dd>{formatBytes(state.backup.local.totalBytes)} {inlineUiText("· 最近一套")} {formatBytes(state.backup.local.latestBytes)}</dd><dt className="text-muted-foreground">{inlineUiText("内容")}</dt><dd>{state.backup.local.artifacts.database ? inlineUiText("数据库") : inlineUiText("缺数据库")} · {state.backup.local.artifacts.config ? inlineUiText("加密配置") : inlineUiText("缺配置")} · {state.backup.local.artifacts.capture ? inlineUiText("加密回执") : inlineUiText("无回执快照")}</dd><dt className="text-muted-foreground">{inlineUiText("校验")}</dt><dd>{state.backup.local.latestVerified ? inlineUiText("SHA-256 清单存在") : inlineUiText("未发现校验清单")}</dd><dt className="text-muted-foreground">{inlineUiText("保留策略")}</dt><dd>{state.backup.local.retentionDays} 天</dd></dl>
            <p className="mt-3 break-all font-mono text-[10px] text-muted-foreground">{state.backup.local.location}</p>
          </div>
          <div className="rounded-md border p-3">
            <div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-sm font-medium"><Cloud className="h-4 w-4 text-sky-600" />{inlineUiText("云备份镜像")}</div><span className={`text-xs ${state.backup.cloud.status === "observed" ? "text-emerald-700" : "text-amber-700"}`}>{english ? ({ observed: "Observed", unverified: "Cloud readback pending", stale: "Stale", missing: "Missing" } as Record<string, string>)[state.backup.cloud.status] ?? state.backup.cloud.status : inlineUiText(CLOUD_LABEL[state.backup.cloud.status] ?? state.backup.cloud.status)}</span></div>
            <dl className="mt-3 grid grid-cols-[90px_minmax(0,1fr)] gap-y-2 text-xs"><dt className="text-muted-foreground">{inlineUiText("最新镜像")}</dt><dd>{formatDate(state.backup.cloud.latestAt, locale)}{state.backup.cloud.ageHours != null ? ` · ${state.backup.cloud.ageHours} 小时前` : ""}</dd><dt className="text-muted-foreground">{inlineUiText("备份集合")}</dt><dd>{state.backup.cloud.mirrorSetCount} {inlineUiText("套 / 预期保留")} {state.backup.cloud.expectedRetentionSets} 套</dd><dt className="text-muted-foreground">{inlineUiText("占用空间")}</dt><dd>{formatBytes(state.backup.cloud.totalBytes)}</dd><dt className="text-muted-foreground">{inlineUiText("加密")}</dt><dd>{state.backup.cloud.encrypted ? state.backup.cloud.encryption : inlineUiText("未完整核实")} {inlineUiText("· 明文制品")} {state.backup.cloud.plaintextFiles}</dd><dt className="text-muted-foreground">{inlineUiText("WPS 文件")}</dt><dd className="break-all">{state.backup.cloud.readback?.observed ? state.backup.cloud.readback.fileName : inlineUiText("未取得上传完成回执")}</dd><dt className="text-muted-foreground">{inlineUiText("上传回执")}</dt><dd>{state.backup.cloud.readback?.observed ? `完整 ${formatBytes(state.backup.cloud.readback.completeSize ?? 0)} · 错误码 0` : inlineUiText("未知")}</dd><dt className="text-muted-foreground">{inlineUiText("WPS 文件 ID")}</dt><dd className="break-all font-mono">{state.backup.cloud.readback?.fileId ?? inlineUiText("未知")}</dd><dt className="text-muted-foreground">{inlineUiText("缓存扫描")}</dt><dd>{state.backup.cloud.wpsCache.probeSkipped ? inlineUiText("已跳过 · 以云端上传回执为准") : state.backup.cloud.wpsCache.timedOut ? inlineUiText("读取超时") : state.backup.cloud.wpsCache.present ? `${state.backup.cloud.wpsCache.fileCount} 个可见入口` : inlineUiText("目录不可读")}</dd></dl>
            <p className="mt-3 text-xs leading-5 text-muted-foreground">{inlineUiText("云端正常必须同时满足：本地加密集合完整、WPS 上传完成字节等于文件大小、错误码为 0，并取得云端文件 ID。")}</p>
            <p className="mt-2 break-all font-mono text-[10px] text-muted-foreground">{state.backup.cloud.location}</p>
          </div>
        </div>
      </article>

      <article className="rounded-lg border p-4">
        <div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold">{inlineUiText("记忆检索设置")}</h3><p className="mt-1 text-xs text-muted-foreground">{inlineUiText("每轮先做有界的 Get Preference；涉及历史依赖时先做窄 Recall 探测，再由 Agent 决定是否继续下钻。")}</p></div>{guidanceMessage && <span className="text-xs text-emerald-700">{guidanceMessage}</span>}</div>
        {state.guidanceSettings && <form onSubmit={(event) => event.preventDefault()} onChangeCapture={() => setEditVersions((previous) => ({ ...previous, guidance: previous.guidance + 1 }))} className="mt-4 grid gap-4 md:grid-cols-3 text-sm">
          <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("Get Preference 候选上限（1-20）")}</span><input type="number" min="1" max="20" className="h-9 w-28 rounded border bg-background px-2" value={state.guidanceSettings.max_candidates} onChange={(e) => setState({...state, guidanceSettings:{...state.guidanceSettings!, max_candidates:Number(e.target.value)}})} /></label>
          <label className="flex items-end gap-2 pb-2 text-xs"><input type="checkbox" checked={state.guidanceSettings.adaptive_budget} onChange={(e) => setState({...state, guidanceSettings:{...state.guidanceSettings!, adaptive_budget:e.target.checked}})} /> {inlineUiText("按任务阶段自适应候选数量")}</label>
          <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("窄探测 Token 上限（300-1200）")}</span><input type="number" min="300" max="1200" className="h-9 w-32 rounded border bg-background px-2" value={state.guidanceSettings.probe_max_tokens} onChange={(e) => setState({...state, guidanceSettings:{...state.guidanceSettings!, probe_max_tokens:Number(e.target.value)}})} /></label>
          <label className="flex items-end gap-2 pb-2 text-xs"><input type="checkbox" checked={state.guidanceSettings.auto_probe} onChange={(e) => setState({...state, guidanceSettings:{...state.guidanceSettings!, auto_probe:e.target.checked}})} /> {inlineUiText("历史依赖时自动窄探测")}</label>
          <div className="md:col-span-3"><ActionButton type="submit" resetKey={editVersions.guidance} onAction={saveGuidanceSettings} className="rounded bg-primary px-4 py-2 text-primary-foreground">{inlineUiText("保存检索设置")}</ActionButton></div>
        </form>}
      </article>

      <article className="rounded-lg border p-4">
        <div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold">{inlineUiText("备份设置")}</h3><p className="mt-1 text-xs text-muted-foreground">{inlineUiText("本地计划和保留策略可编辑；云端镜像单独管理，不自动覆盖本地规则。")}</p></div>{backupMessage && <span className="text-xs text-emerald-700">{backupMessage}</span>}</div>
        {state.backup.settings && <form onSubmit={(event) => event.preventDefault()} onChangeCapture={() => setEditVersions((previous) => ({ ...previous, backup: previous.backup + 1 }))} className="mt-4 grid gap-4 md:grid-cols-2 text-sm">
          <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("本地备份位置")}</span><input className="h-9 w-full rounded border bg-background px-2 font-mono text-xs" value={state.backup.settings.local?.root ?? ""} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, local:{...state.backup.settings.local, root:e.target.value}}}})} /></label>
          <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("周期")}</span><select className="h-9 w-full rounded border bg-background px-2" value={state.backup.settings.schedule?.mode ?? "daily"} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, schedule:{...state.backup.settings.schedule, mode:e.target.value}}}})}><option value="daily">{inlineUiText("每日")}</option><option value="weekly">{inlineUiText("每周")}</option><option value="monthly">{inlineUiText("每月")}</option></select></label>
          <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("执行时间（小时 / 分钟）")}</span><div className="flex gap-2"><input type="number" min="0" max="23" className="h-9 w-20 rounded border bg-background px-2" value={state.backup.settings.schedule?.hour ?? 3} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, schedule:{...state.backup.settings.schedule, hour:Number(e.target.value)}}}})} /><input type="number" min="0" max="59" className="h-9 w-20 rounded border bg-background px-2" value={state.backup.settings.schedule?.minute ?? 25} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, schedule:{...state.backup.settings.schedule, minute:Number(e.target.value)}}}})} /></div></label>
          {state.backup.settings.schedule?.mode === "weekly" && <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("每周执行日")}</span><select className="h-9 w-full rounded border bg-background px-2" value={state.backup.settings.schedule?.weekday ?? 1} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, schedule:{...state.backup.settings.schedule, weekday:Number(e.target.value)}}}})}>{[inlineUiText("周日"),inlineUiText("周一"),inlineUiText("周二"),inlineUiText("周三"),inlineUiText("周四"),inlineUiText("周五"),inlineUiText("周六")].map((label, value) => <option key={label} value={value}>{label}</option>)}</select></label>}
          {state.backup.settings.schedule?.mode === "monthly" && <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("每月执行日")}</span><input type="number" min="1" max="28" className="h-9 w-28 rounded border bg-background px-2" value={state.backup.settings.schedule?.day ?? 1} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, schedule:{...state.backup.settings.schedule, day:Number(e.target.value)}}}})} /></label>}
          <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("本地保留天数 / 最大套数")}</span><div className="flex gap-2"><input type="number" min="1" max="3650" className="h-9 w-28 rounded border bg-background px-2" value={state.backup.settings.local?.retention_days ?? 14} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, local:{...state.backup.settings.local, retention_days:Number(e.target.value)}}}})} /><input type="number" min="1" max="1000" className="h-9 w-28 rounded border bg-background px-2" value={state.backup.settings.local?.max_sets ?? 14} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, local:{...state.backup.settings.local, max_sets:Number(e.target.value)}}}})} /></div></label>
          <div className="flex flex-wrap gap-4 text-xs md:col-span-2"><label><input type="checkbox" checked={state.backup.settings.local?.database ?? true} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, local:{...state.backup.settings.local, database:e.target.checked}}}})} /> {inlineUiText("数据库")}</label><label><input type="checkbox" checked={state.backup.settings.local?.config ?? true} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, local:{...state.backup.settings.local, config:e.target.checked}}}})} /> {inlineUiText("加密配置")}</label><label><input type="checkbox" checked={state.backup.settings.local?.capture ?? true} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, local:{...state.backup.settings.local, capture:e.target.checked}}}})} /> {inlineUiText("加密回执")}</label><label><input type="checkbox" checked={state.backup.settings.local?.verify_checksum ?? true} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, local:{...state.backup.settings.local, verify_checksum:e.target.checked}}}})} /> {inlineUiText("SHA-256 校验")}</label></div>
          <label className="space-y-1"><span className="text-xs text-muted-foreground">{inlineUiText("云端预期保留套数（独立策略）")}</span><input type="number" min="0" max="1000" className="h-9 w-32 rounded border bg-background px-2" value={state.backup.settings.cloud?.retention_sets ?? 2} onChange={(e) => setState({...state, backup:{...state.backup, settings:{...state.backup.settings, cloud:{...state.backup.settings.cloud, retention_sets:Number(e.target.value)}}}})} /></label>
          <div className="flex items-end"><ActionButton type="submit" resetKey={editVersions.backup} onAction={saveBackupSettings} disabled={savingBackup} className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50">{savingBackup ? inlineUiText("保存中…") : inlineUiText("保存并应用")}</ActionButton></div>
        </form>}
      </article>

      <div className="grid gap-4 lg:grid-cols-2">
        <article className="rounded-lg border p-4">
          <div className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="h-4 w-4 text-primary" />{inlineUiText("前台边界")}</div>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">{state.behavior.guidance}</p>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">{state.behavior.retrieval}</p>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">{state.behavior.controller}</p>
        </article>
        <article className="rounded-lg border p-4">
          <div className="flex items-center gap-2 text-sm font-semibold"><Database className="h-4 w-4 text-primary" />{inlineUiText("备份说明")}</div>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">{state.behavior.backup}</p>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">{inlineUiText("份数按同一时间戳的一组数据库、配置、回执和校验清单计算，不把每个文件误算成一份备份。")}</p>
        </article>
      </div>
    </section>
  );
}
