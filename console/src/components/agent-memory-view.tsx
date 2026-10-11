"use client";

import { useState } from "react";
import { useLocale } from "next-intl";
import { AgentProcessView } from "./agent-process-view";
import { useWindowedGraph } from "@/lib/use-windowed-graph";
import { GraphWindowControls } from "./graph-window-controls";

import { inlineUiText } from "@/lib/inline-i18n";
import { needsProcessRevalidation } from "@/lib/process-presentation";
export function AgentMemoryView() {
  const english = !useLocale().startsWith("zh");
  const [filter, setFilter] = useState("all");
  const windowed = useWindowedGraph("/api/evolving-profile/process-memory/graph", new URLSearchParams({kind:filter}).toString());
  if (!windowed.data) return <div className="rounded-lg border p-6"><GraphWindowControls windowed={windowed} /></div>;
  const memory = windowed.data?.processMemory || {};
  const graph = windowed.data?.graph || { nodes: [], edges: [], timeline: [] };
  const matches = (node: any) => filter === "all" || filter === "rollout" ? (filter === "all" || needsProcessRevalidation(node)) : node.type === `agent_${filter}`;
  const filteredNodes = graph.nodes.filter(matches);
  const ids = new Set(filteredNodes.map((n: any) => n.id));
  const filteredGraph = { nodes: filteredNodes, edges: graph.edges.filter((e: any) => ids.has(e.source) && ids.has(e.target)), timeline: graph.timeline.filter((n: any) => ids.has(n.id)) };
  const labels = english ? { title: "Agent Memory", intro: "How the Agent executes tasks, fails, repairs, and verifies reusable process strategies.", candidate: "historical process strategy candidates", candidateHint: "They are evidence previews only and are not injected automatically until independently verified.", stats: [["Process records", memory.record_count || 0], ["Process observations", memory.by_kind?.process_observation || 0], ["Failure episodes", memory.by_kind?.episode || 0], ["Repair patterns", memory.by_kind?.pattern || 0], ["Reusable strategies", memory.by_kind?.skill || 0], ["Candidates", memory.skill_candidates || 0], ["Revalidation", memory.revalidation_queue || 0]] as Array<[string, number]> } : { title: inlineUiText("智能体记忆"), intro: inlineUiText("记录智能体如何执行任务、哪里失败、如何修复，以及哪些流程已经被验证。"), candidate: inlineUiText("候选过程策略"), candidateHint: inlineUiText("它们仅用于展示来源和待验证形状，完成独立验证后才会进入可复用策略层。"), stats: [[inlineUiText("过程记录"), memory.record_count || 0], [inlineUiText("过程观察"), memory.by_kind?.process_observation || 0], [inlineUiText("失败事件"), memory.by_kind?.episode || 0], [inlineUiText("修复模式"), memory.by_kind?.pattern || 0], [inlineUiText("可复用过程策略"), memory.by_kind?.skill || 0], [inlineUiText("其中候选"), memory.skill_candidates || 0], [inlineUiText("待再验证"), memory.revalidation_queue || 0]] as Array<[string, number]> };
  return <div>
    <h1 className="mb-2 text-3xl font-bold text-foreground">{labels.title}</h1>
    <p className="mb-6 text-muted-foreground">{labels.intro}</p>
    {memory.skill_candidates > 0 && <p className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">{memory.skill_candidates} {labels.candidate}{english ? ". " : "。"}{labels.candidateHint}</p>}
    <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
      {labels.stats.map(([label, value]) => <div key={String(label)} className="rounded-lg border bg-card p-4"><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 text-2xl font-semibold">{value}</div></div>)}
    </div>
    <div className="mb-4 flex flex-wrap gap-2 border-b pb-3">{(english ? [["all", "Overview"], ["process_observation", "Observations"], ["trace", "Raw trajectories"], ["episode", "Failure episodes"], ["pattern", "Repair patterns"], ["skill", "Reusable strategies"], ["capability_observation", "Capability"], ["rollout", "Migration & revalidation"]] : [["all", inlineUiText("过程总览")], ["process_observation", inlineUiText("过程观察")], ["trace", inlineUiText("原始轨迹")], ["episode", inlineUiText("失败事件")], ["pattern", inlineUiText("修复模式")], ["skill", inlineUiText("可复用过程策略")], ["capability_observation", inlineUiText("能力观测")], ["rollout", inlineUiText("迁移与再验证")]]).map(([id, label]) => <button key={id} type="button" onClick={() => setFilter(id)} className={`rounded-md border px-3 py-2 text-sm font-medium ${filter === id ? "bg-primary text-primary-foreground" : "hover:bg-muted"}`}>{label}</button>)}</div>
    <GraphWindowControls windowed={windowed} />
    {windowed.data?.graph && <AgentProcessView key={`${windowed.data.version}:${windowed.data.page.offset}:${filter}`} version={windowed.data.version} onRefresh={windowed.refresh} graph={filteredGraph} english={english} emptyMessage={(english ? {all:"No process records",process_observation:"No process observations",trace:"No raw trajectories",episode:"No failure episodes",pattern:"No repair patterns",skill:"No publishable reusable strategies; candidates remain shadow-only until independent verification.",capability_observation:"No capability observations",rollout:"No migration or revalidation items."} : {all:inlineUiText("暂无过程记录"),process_observation:inlineUiText("暂无过程观察"),trace:inlineUiText("暂无原始轨迹"),episode:inlineUiText("暂无失败事件候选"),pattern:inlineUiText("暂无修复模式候选"),skill:inlineUiText("暂无可发布过程策略；当前候选处于影子状态，需独立任务验证后才会进入默认检索。"),capability_observation:inlineUiText("尚无能力观测。需要独立验证器记录模型在具体任务族和阶段上的实际表现。"),rollout:inlineUiText("当前没有迁移或再验证队列。模型、工具链或验证器变化后，相关记录会进入这里。")})[filter]} />}
  </div>;
}
