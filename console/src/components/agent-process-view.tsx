"use client";

import { ActionButton } from "@/components/ui/action-button";

import { useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import type { GraphData } from "./graph-2d";

import { inlineUiText, enumUiText } from "@/lib/inline-i18n";
const Graph = dynamic(() => import("./graph-2d").then(m => m.Graph2D), { ssr: false });
const Stars = dynamic(() => import("./constellation").then(m => m.Constellation), { ssr: false });
type Node = { id: string; type: string; label: string;nodeVersion?:string; phase?: string; maturity?: string; status?: string; at?: string | null };
type ProcessGraph = { nodes: Node[]; edges: { source: string; target: string; type: string }[]; timeline: { id: string; label: string; at?: string | null }[] };

const labels: Record<string, string> = { trace: inlineUiText("原始轨迹"), event: inlineUiText("过程事件"), episode: inlineUiText("失败事件"), pattern: inlineUiText("修复模式"), skill: inlineUiText("可复用过程策略"), process_draft: inlineUiText("过程草稿"), capability_observation: inlineUiText("能力观测"), verified: inlineUiText("已验证"), observed: inlineUiText("已观察"), diagnosed: inlineUiText("候选/已诊断"), replicated: inlineUiText("已复现"), generalized: inlineUiText("已泛化"), stable: inlineUiText("稳定"), revalidation_required: inlineUiText("待再验证"), deprecated: inlineUiText("已弃用") };
const color = (type: string) => type.includes("episode") ? "#ef4444" : /pattern|skill/.test(type) ? "#22c55e" : "#3b82f6";

export function AgentProcessView({ graph, english, emptyMessage,version,onRefresh }: { graph: ProcessGraph; english: boolean; emptyMessage?: string;version?:string;onRefresh?:()=>void }) {
  const t = useTranslations("releaseUi");
  const requestRef = useRef<AbortController|null>(null);
  useEffect(()=>()=>requestRef.current?.abort(),[]);
  const [view, setView] = useState("constellation");
  const [selected, setSelected] = useState<Node | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [message, setMessage] = useState("");
  const [stale,setStale] = useState(false);
  const data = useMemo<GraphData>(() => ({ nodes: graph.nodes.map(n => ({ ...n, color: color(n.type), group: n.type })), links: graph.edges.map(e => ({ ...e, color: "#64748b" })) }), [graph]);
  const name = (value: string) => enumUiText(value,english ? "en" : "zh-CN");
  const title = (node: {label:string;label_generated?:boolean}) => node.label_generated ? node.label.split(" · ").map(name).join(" · ") : node.label;
  async function select(id: string,sourceVersion?:string) {
    const node = graph.nodes.find(n => n.id === id) || {id,type:"agent_record",label:id};
    requestRef.current?.abort();
    const controller = new AbortController(); requestRef.current=controller;
    setSelected(node); setDetail(null); setStale(false); setMessage(english ? "Loading source…" : inlineUiText("正在读取来源…"));
    try {
      const params=new URLSearchParams();if(version)params.set("version",version);
      const nodeVersion=sourceVersion||node.nodeVersion;if(nodeVersion)params.set("nodeVersion",nodeVersion);
      const r = await fetch(`/api/evolving-profile/process-memory/${encodeURIComponent(id.replace(/^agent:/, ""))}${params.size ? `?${params}` : ""}`, { cache: "no-store",signal:controller.signal });
      const body = await r.json();
      if (controller.signal.aborted) return false;
      if (r.status === 409) { setStale(true); throw new Error(t("scenarioStaleVersion")); }
      if (!r.ok) throw new Error(body.error || inlineUiText("读取失败"));
      setDetail(body); setMessage("");
      setSelected({...node,nodeVersion:body.nodeVersion,type:`agent_${body.record.kind}`,label:String(body.record.text||body.record.kind).slice(0,160),phase:body.record.phase,maturity:body.record.maturity,status:body.record.drift_status||"stable",at:body.record.updated_at||body.record.created_at});
      return true;
    } catch (e) { if (controller.signal.aborted) return false; setMessage(e instanceof Error ? e.message : inlineUiText("读取失败")); throw e; }
  }
  return <div className="mt-4 rounded-lg border p-4">
    <div className="flex flex-wrap gap-2" role="tablist" aria-label={english ? "Process memory views" : inlineUiText("过程记忆视图")}>{["constellation", "graph", "table", "timeline"].map(v => <button type="button" role="tab" aria-selected={v === view} className={`rounded border px-3 py-2 text-xs ${view === v ? "bg-primary text-primary-foreground" : "hover:bg-muted"}`} key={v} onClick={() => setView(v)}>{english ? v : ({ constellation: inlineUiText("星座图"), graph: inlineUiText("图谱"), table: inlineUiText("表格"), timeline: inlineUiText("时间线") } as Record<string, string>)[v]}</button>)}</div>
    <div className="mt-3 grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
      <div className="min-w-0">
        {view === "constellation" && <Stars data={data} height={480} preserveNodeColor nodeColorFn={n => color(n.group || "")} nodeHeatFn={() => 1} nodeOpacity={0.9} labelOpacity={0.85} nodeSizeFn={n => /episode/.test(n.group || "") ? 10 : /pattern|skill/.test(n.group || "") ? 9 : 7} onNodeClick={n => { void select(n.id).catch(() => undefined); }} linkOpacity={0.55} linkWidth={1.4} />}
        {view === "graph" && <Graph data={data} height={480} showLabels nodeColorFn={n => color(n.group || "")} nodeSizeFn={n => /episode/.test(n.group || "") ? 26 : /pattern|skill/.test(n.group || "") ? 22 : 18} linkColorFn={() => "#64748b"} linkWidthFn={() => 2} onNodeClick={n => { void select(n.id).catch(() => undefined); }} />}
        {view === "table" && <div className="max-h-[480px] overflow-auto">{graph.nodes.map(n => <ActionButton preserveLabel variant="ghost" type="button" key={n.id} onAction={() => select(n.id)} className="flex h-auto w-full justify-start gap-3 whitespace-normal border-b p-3 text-left text-xs text-foreground hover:bg-muted"><span style={{ color: color(n.type) }}>{name(n.type)}</span><span className="truncate">{title(n)}</span></ActionButton>)}</div>}
        {view === "timeline" && <ol className="max-h-[480px] overflow-auto">{graph.timeline.slice().reverse().map(n => <li key={n.id}><ActionButton preserveLabel variant="ghost" type="button" onAction={() => select(n.id)} className="h-auto w-full justify-start whitespace-normal border-b p-3 text-left text-xs text-foreground hover:bg-muted"><time className="mr-3 text-muted-foreground">{n.at ? new Date(n.at).toLocaleString(english ? "en-US" : "zh-CN") : "—"}</time>{title(n)}</ActionButton></li>)}</ol>}
        {!graph.nodes.length && <p className="p-4 text-sm text-muted-foreground">{emptyMessage || (english ? "No process records" : inlineUiText("暂无过程记录"))}</p>}
        {graph.nodes.length > 0 && graph.edges.length === 0 && <p className="mt-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">{english ? "No relationship links are available in the current filtered view. Select Process Overview to inspect derivation links." : inlineUiText("当前筛选视图没有可显示的关系线；切回“过程总览”可以查看失败事件到修复模式的派生关系。")}</p>}
      </div>
      <aside className="min-w-0 rounded border p-3 text-xs" aria-live="polite">
        <h4 className="font-semibold">{english ? "Process details and sources" : inlineUiText("过程详情与来源")}</h4>
        {!selected ? <p className="mt-3 text-muted-foreground">{english ? "Select a node to inspect evidence." : inlineUiText("点击节点、表格或时间线查看完整记录及来源。")}</p> : <>
          <p className="mt-3 font-semibold" style={{ color: color(selected.type) }}>{name(selected.type)} · {name(selected.maturity || "")}</p>
          <p className="mt-2">{name(selected.status || "")}</p><p className="mt-2 break-all font-mono text-[10px]">{selected.id}</p>
          {message && <p className="mt-2 text-muted-foreground">{message}</p>}
          {stale && onRefresh ? <button type="button" className="mt-2 rounded border px-2 py-1" onClick={onRefresh}>{t("scenarioRefresh")}</button> : null}
          {detail && <><p data-i18n-ignore="true" className="mt-3 whitespace-pre-wrap break-words">{detail.record?.text || selected.label}</p>
            <h5 className="mt-4 font-semibold">{english ? "Source chain" : inlineUiText("来源链 · 可点击下钻")}</h5>
            {(detail.sources || []).map((source: any) => <ActionButton preserveLabel variant="ghost" type="button" key={source.process_memory_id} className="mt-2 block h-auto w-full whitespace-normal rounded border p-2 text-left text-foreground hover:bg-muted" onAction={() => select(`agent:${source.process_memory_id}`,source.nodeVersion)}>{name(source.kind)} · {source.text}</ActionButton>)}
            {(detail.unresolved_source_ids || []).map((id: string) => <p key={id} className="mt-2 break-all text-amber-700">{english ? "External source locator (not verified): " : inlineUiText("外部来源定位（未核验）：")}{id}</p>)}
            <h5 className="mt-4 font-semibold">{english ? "Verification evidence" : inlineUiText("验证证据")}</h5><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all text-[10px]">{JSON.stringify(detail.record?.verification_evidence || [], null, 2)}</pre>
          </>}
        </>}
      </aside>
    </div>
  </div>;
}
