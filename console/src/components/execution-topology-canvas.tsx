"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, Handle, Position, ReactFlow, type EdgeProps, type NodeProps } from "@xyflow/react";
import type { FlowLane, FlowLaneSummary } from "@/lib/flow-receipt";
import type { projectFlowAudit } from "@/lib/flow-projection";
import { buildTopologySpec, itemText, type TopologyBranchSummary, type TopologyEdge, type TopologyEdgeData, type TopologyNode, type TopologyNodeData } from "@/lib/topology-spec";
import type { FlowReceipt } from "@/lib/flow-receipt";
import { sumFlowCounts } from "@/lib/flow-receipt";
import { TopologyEngineLab } from "@/components/topology-engine-lab";
import { projectToolBranches } from "@/lib/topology-mapping";
import { contextChildren } from "@/lib/topology-presentation";

type Audit = ReturnType<typeof projectFlowAudit>;
const STATUS: Record<string, { en: string; zh: string }> = { not_observed: { en: "Not observed", zh: "未观测" }, observed: { en: "Observed", zh: "已观测" }, delivered: { en: "Delivered", zh: "已送达" }, candidate_returned: { en: "Candidates returned", zh: "已返回候选" }, unknown: { en: "Unknown", zh: "未知" }, failed: { en: "Failed", zh: "失败" } };
const statusText = (status: string, english: boolean) => STATUS[status]?.[english ? "en" : "zh"] ?? status;

function emptyBranch(): TopologyBranchSummary { return { status: "not_observed", counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 }, receipts: [], items: [] }; }
function summarizeReceipts(receipts: FlowReceipt[], english: boolean): TopologyBranchSummary {
  if (!receipts.length) return emptyBranch();
  const counts = sumFlowCounts(receipts.map(receipt=>({candidates:receipt.candidate_count ?? null,returned:receipt.returned_count ?? null,delivered:receipt.delivered_count ?? null,tokens:receipt.token_count ?? null})));
  return { status: counts.delivered ? "delivered" : counts.returned ? "candidate_returned" : "observed", counts, receipts, items: receipts.map((receipt) => ({ kind: english ? "Receipt" : "回执", text: receipt.summary || receipt.trace_id })) };
}
function summarizeToolEvents(events: Array<any>, names: string[], history: any, english: boolean): TopologyBranchSummary {
  const known=(value:unknown)=>typeof value==="number"&&Number.isInteger(value)&&value>=0?value:null;
  const matched = events.filter((event) => names.includes(String(event.tool || "")));
  if (!matched.length) {
    const route = String(history?.value ?? history?.route ?? "");
    const routeMatches = names.some((name) => route === name || route.includes(name));
    if (!routeMatches || !history || history.state === "not_observed" || history.state === "unknown") return emptyBranch();
    // Keep the topology on host-visible counts. Backend discovery candidates
    // are not the same as candidates returned to the Agent and belong only in
    // the audit detail, never in the branch total.
    const counts = { candidates: known(history.metrics?.candidates ?? history.candidate_count), returned: known(history.metrics?.returned ?? history.returned_to_host_count), delivered: known(history.delivered), tokens: 0 };
    return { status: counts.delivered ? "delivered" : counts.returned ? "candidate_returned" : "observed", counts, receipts: [], items: counts.returned || counts.delivered ? (history.items ?? []).map((item: any) => ({ kind: english ? "History" : "历史", text: itemText(item) })) : [] };
  }
  const counts = sumFlowCounts(matched.map(event=>({candidates:known(event.candidate_count),returned:known(event.returned_count),delivered:known(event.delivered_count) ?? (["observed","received"].includes(event.delivery?.host_visibility)?known(event.returned_count):null),tokens:known(event.token_count)})));
  const items = matched.flatMap((event) => (event.memory_ids || event.scenario_ids || []).map((id: string) => ({ kind: english ? String(event.tool) : "工具", text: id })));
  return { status: counts.delivered ? "delivered" : counts.returned ? "candidate_returned" : "observed", counts, receipts: [], items };
}

export function ExecutionTopologyCanvas({ promptId, promptText, english, audit, guidanceReceipt }: { promptId?: string | null; promptText?: string; english: boolean; audit?: Audit | null; guidanceReceipt?: any }) {
  const [apiLanes, setApiLanes] = useState<FlowLaneSummary[]>([]); const [sourceScope, setSourceScope] = useState("no_bound_process_receipt"); const [detailGuidance, setDetailGuidance] = useState<any>(null); const [nodes, setNodes] = useState<TopologyNode[]>([]); const [edges, setEdges] = useState<TopologyEdge[]>([]); const [selected, setSelected] = useState<TopologyNodeData | null>(null); const [selectedEdge, setSelectedEdge] = useState<TopologyEdge | null>(null); const flowApi = useRef<any>(null);
  useEffect(() => { if (!promptId) { setApiLanes([]); setSourceScope("no_prompt"); return; } const controller = new AbortController(); fetch(`/api/evolving-profile/flow/${encodeURIComponent(promptId)}`, { cache: "no-store", signal: controller.signal }).then((r) => r.ok ? r.json() : null).then((payload) => { setApiLanes(payload?.lanes ?? []); setSourceScope(payload?.source_scope ?? "unknown"); }).catch((error) => { if (error?.name !== "AbortError") { setApiLanes([]); setSourceScope("unavailable"); } }); return () => controller.abort(); }, [promptId]);
  useEffect(() => { if (!promptId) { setDetailGuidance(null); return; } const controller = new AbortController(); fetch(`/api/evolving-profile/guidance/prompts/${encodeURIComponent(promptId)}`, { cache: "no-store", signal: controller.signal }).then((r) => r.ok ? r.json() : null).then((payload) => setDetailGuidance(payload?.guidance_receipt ?? null)).catch((error) => { if (error?.name !== "AbortError") setDetailGuidance(null); }); return () => controller.abort(); }, [promptId]);
  const guidanceSource = guidanceReceipt ?? detailGuidance;
  const guidanceHostObserved=["observed","received"].includes(guidanceSource?.host_state || guidanceSource?.delivery?.host_visibility);
  const guidanceFallback = useMemo(() => guidanceSource ? { count: guidanceSource.guidance_count ?? guidanceSource.guidance_items?.length ?? guidanceSource.stable_profile_count ?? 0, candidateCount: guidanceSource.preference_candidate_count ?? guidanceSource.guidance_items?.length ?? guidanceSource.stable_profile_count ?? 0, deferred: guidanceSource.deferred_count ?? 0, items: guidanceSource.guidance_items ?? guidanceSource.stable_profile ?? [] } : null, [guidanceSource]);
  const summaries = useMemo(() => { const map = new Map<FlowLane, FlowLaneSummary>(apiLanes.map((lane) => [lane.lane, lane])); const history = audit?.history; const guidance = audit?.guidance ?? guidanceFallback; const preferenceCandidates = Math.max(guidance?.candidateCount ?? 0, guidance?.count ?? 0); const preferenceReturned = guidance?.count ?? 0; const historyCandidates = history && (history.calls ?? 0) > 0 ? history.metrics.candidates ?? 0 : 0; const historyReturned = history && (history.calls ?? 0) > 0 ? history.metrics.returned ?? 0 : 0; const user = map.get("user_memory") ?? { lane: "user_memory", status: "not_observed", receipts: [], counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 } }; map.set("user_memory", { ...user, status: historyReturned > 0 ? "candidate_returned" : preferenceReturned > 0 ? (guidanceHostObserved ? "delivered" : "candidate_returned") : history?.calls ? "observed" : "not_observed", counts: { candidates: preferenceCandidates + historyCandidates, returned: preferenceReturned + historyReturned, delivered: guidanceHostObserved ? preferenceReturned + (history?.delivered ?? 0) : null, tokens: 0 } }); if (guidance) map.set("guidance", { lane: "guidance", status: preferenceReturned ? (guidanceHostObserved ? "delivered" : "candidate_returned") : "not_observed", receipts: [], counts: { candidates: preferenceCandidates, returned: preferenceReturned, delivered: guidanceHostObserved ? preferenceReturned : null, tokens: 0 } }); return map; }, [apiLanes, audit, guidanceFallback,guidanceHostObserved]);
  const historyItems = useMemo(() => audit?.history && (audit.history.calls ?? 0) > 0 ? audit.history.items?.map((item) => ({ kind: english ? "History" : "历史", text: itemText(item) })) ?? [] : [], [audit, english]);
  const guidanceItems = useMemo(() => { const seen = new Set<string>(); return (audit?.guidance.items ?? guidanceFallback?.items ?? []).map((item: any) => ({ kind: english ? "Preference" : "偏好", text: itemText(item) })).filter((item: { kind: string; text: string }) => { const key = `${item.kind}:${item.text}`; if (seen.has(key)) return false; seen.add(key); return true; }); }, [audit, guidanceFallback, english]);
  const branchSummaries = useMemo(() => {
    const branches = new Map<string, TopologyBranchSummary>();
    const events = audit?.history?.routeReceipt?.tool_events ?? [];
    branches.set("user-memory-preference", audit?.guidance || guidanceFallback ? { status: guidanceItems.length ? (guidanceHostObserved ? "delivered" : "candidate_returned") : "not_observed", counts: { candidates: audit?.guidance?.candidateCount ?? guidanceFallback?.candidateCount ?? guidanceItems.length, returned: guidanceItems.length, delivered: guidanceHostObserved ? guidanceItems.length : null, tokens: 0 }, receipts: [], items: guidanceItems } : emptyBranch());
    branches.set("user-memory-recall", summarizeToolEvents(events, ["user_recall", "recall"], audit?.history, english));
    branches.set("user-memory-research", summarizeToolEvents(events, ["user_research", "research", "read_research"], audit?.history, english));
    branches.set("user-memory-scenario-summary", summarizeToolEvents(events, ["search_scenario_summary", "search_scenario_contexts", "read_scenario_summary", "read_context_summary", "scenario_gate"], audit?.history, english));
    branches.set("user-memory-read-source", summarizeToolEvents(events, ["read_source", "find_sources"], audit?.history, english));
    const agentReceipts = apiLanes.find((lane) => lane.lane === "agent_process")?.receipts ?? [];
    const groups: Record<string, FlowReceipt[]> = { observe: [], capability: [], failure: [], repair: [], strategy: [], recall: [], research: [], scenario: [], migration: [], compatibility: [], guidance: [] };
    for (const receipt of agentReceipts) {
      const stage = String(receipt.stage || "").toLowerCase();
      if (/capability|ability|competenc/.test(stage)) groups.capability.push(receipt);
      else if (/failure|error|incident|failed/.test(stage)) groups.failure.push(receipt);
      else if (/repair|recovery|fix/.test(stage)) groups.repair.push(receipt);
      else if (/strategy|reusable|playbook|pattern/.test(stage)) groups.strategy.push(receipt);
      else if (/scenario|context.?summary|session.?summary/.test(stage)) groups.scenario.push(receipt);
      else if (/migration|revalid|transfer/.test(stage)) groups.migration.push(receipt);
      else if (/observe|trace|event|process_observation/.test(stage)) groups.observe.push(receipt);
      else if (/agent.?research|cross.?project|cross.?task|research/.test(stage)) groups.research.push(receipt);
      else if (/retrieve|retrieval|recall|search|episode|skill/.test(stage)) groups.recall.push(receipt);
      else if (/compat|gate|evaluat/.test(stage)) groups.compatibility.push(receipt);
      else if (/guidance|context|hint|recommend|scaffold|guard/.test(stage)) groups.guidance.push(receipt);
    }
    branches.set("agent-process-memory-observe-trajectory", summarizeReceipts(groups.observe, english));
    branches.set("agent-process-memory-capability-observation", summarizeReceipts(groups.capability, english));
    branches.set("agent-process-memory-failure-events", summarizeReceipts(groups.failure, english));
    branches.set("agent-process-memory-repair-mode", summarizeReceipts(groups.repair, english));
    branches.set("agent-process-memory-reusable-process-strategy", summarizeReceipts(groups.strategy, english));
    branches.set("agent-process-memory-process-retrieval", summarizeReceipts(groups.recall, english));
    branches.set("agent-process-memory-agent-research", summarizeReceipts(groups.research, english));
    branches.set("agent-process-memory-agent-scenario-summary", summarizeReceipts(groups.scenario, english));
    branches.set("agent-process-memory-migration-revalidation", summarizeReceipts(groups.migration, english));
    branches.set("agent-process-memory-compatibility-gate", summarizeReceipts(groups.compatibility, english));
    for (const key of ["guidance-hint", "guidance-recommend", "guidance-scaffold", "guidance-guard"]) branches.set(`agent-process-memory-${key}`, summarizeReceipts(groups.guidance.filter(r=>String(r.stage).includes(key.slice(9))), english));
    const processScope = `${promptText ?? ""} ${events.map((event) => event.query ?? "").join(" ")}`;
    const agentScope = /agent|智能体|过程记忆|过程经验|链路图|修复经验|process memory/i.test(processScope);
    const agentEvents = agentScope ? events : [];
    const agentRecall = summarizeToolEvents(agentEvents, ["agent_recall", "agent_memory_recall", "read_agent_process_memory"], audit?.history, english);
    const agentResearch = summarizeToolEvents(agentEvents, ["agent_research"], audit?.history, english);
    const agentScenario = summarizeToolEvents(agentEvents, ["search_scenario_summary", "read_scenario_summary", "search_scenario_contexts", "read_context_summary"], audit?.history, english);
    if (agentRecall.receipts.length || agentRecall.items.length || agentRecall.counts.returned || agentRecall.counts.candidates) branches.set("agent-process-memory-process-retrieval", agentRecall);
    if (agentResearch.receipts.length || agentResearch.items.length || agentResearch.counts.returned || agentResearch.counts.candidates) branches.set("agent-process-memory-agent-research", agentResearch);
    if (agentScenario.receipts.length || agentScenario.items.length || agentScenario.counts.returned || agentScenario.counts.candidates) branches.set("agent-process-memory-agent-scenario-summary", agentScenario);
    // Single mapping source: actual tool identity and source-bearing call receipts.
    branches.set("user-memory-scenario-summary",emptyBranch());
    branches.set("agent-process-memory-agent-scenario-summary",emptyBranch());
    for(const [id, mapped] of projectToolBranches(events, english)) branches.set(id,mapped);
    return branches;
  }, [apiLanes, audit, english, promptText,guidanceHostObserved,guidanceItems,guidanceFallback]);
  const spec = useMemo(() => {
    const result=buildTopologySpec({english,summaries,sourceScope:audit?.history?.routeReceipt?.tool_events?.length ? "同一 Prompt / 宿主原始工具结果回放（不代表回答采用）" : sourceScope,historyItems,guidanceItems,branchSummaries});
    const packets=["user-packet","agent-packet","rag-packet"];
    for(const [lane,packetId,root] of [["user_memory","user-packet","user-root"],["agent_process","agent-packet","agent-root"],["external_rag","rag-packet","rag-root"]] as const) {
      const children=contextChildren(result.nodes,lane,root,packetId);
      const counts=sumFlowCounts(children.map(n=>n.data.counts));
      const receipts=children.flatMap(n=>n.data.receipts);
      const items=children.flatMap(n=>n.data.items);
      for(const n of result.nodes.filter(n=>n.id===packetId || n.id===root)) Object.assign(n.data,{counts,receipts,items,status:receipts.length?counts.delivered?"delivered":counts.returned?"candidate_returned":"observed":n.data.status});
      // An umbrella tool owns its result; do not lose it in child-stage sums
      // or add both the umbrella result and its stages a second time.
      const direct=branchSummaries.get(packetId) || branchSummaries.get(root);
      if(direct?.receipts.length && ((direct.counts.returned ?? 0)>0 || !receipts.length)) for(const n of result.nodes.filter(n=>n.id===packetId || n.id===root)) Object.assign(n.data,direct);
    }
    const contextPackets=result.nodes.filter(n=>packets.includes(n.id));
    const counts=sumFlowCounts(contextPackets.map(n=>n.data.counts));
    const contextReceipts=contextPackets.flatMap(n=>n.data.receipts);
    for(const n of result.nodes.filter(n=>n.id==="context" || n.id==="execution")) Object.assign(n.data,{counts,receipts:contextReceipts,items:contextPackets.flatMap(n=>n.data.items),status:counts.delivered?"delivered":counts.returned?"candidate_returned":contextReceipts.length?"observed":"not_observed"});
    return result;
  }, [english,summaries,sourceScope,historyItems,guidanceItems,branchSummaries]);
  useEffect(() => { const positions: Record<string, { x: number; y: number }> = { prompt: { x: 40, y: 42 }, binding: { x: 250, y: 42 }, contract: { x: 470, y: 42 }, fork: { x: 690, y: 47 }, context: { x: 920, y: 42 }, execution: { x: 1150, y: 42 }, "user-root": { x: 40, y: 226 }, "user-memory-preference": { x: 300, y: 200 }, "user-memory-recall": { x: 490, y: 200 }, "user-memory-research": { x: 490, y: 270 }, "user-memory-scenario-summary": { x: 680, y: 200 }, "user-memory-read-source": { x: 680, y: 270 }, "user-packet": { x: 900, y: 226 }, "agent-root": { x: 40, y: 400 }, "agent-process-memory-observe-trajectory": { x: 330, y: 374 }, "agent-process-memory-process-retrieval": { x: 330, y: 444 }, "agent-process-memory-agent-research": { x: 330, y: 514 }, "agent-process-memory-agent-scenario-summary": { x: 560, y: 444 }, "agent-process-memory-compatibility-gate": { x: 700, y: 444 }, "agent-process-memory-guidance": { x: 820, y: 444 }, "agent-packet": { x: 1040, y: 400 }, "rag-root": { x: 40, y: 574 }, "external-rag-lexical": { x: 330, y: 548 }, "external-rag-vector": { x: 330, y: 618 }, "external-rag-fusion-rrf": { x: 560, y: 548 }, "external-rag-rerank": { x: 560, y: 618 }, "external-rag-jev-review": { x: 560, y: 688 }, "rag-packet": { x: 820, y: 574 } };
    const backgrounds: TopologyNode[] = [{ id: "lane-user", type: "lane", position: { x: 18, y: 164 }, zIndex: -1, selectable: false, draggable: false, data: { label: english ? "A  User Memory" : "A  用户记忆", description: english ? "Preferences, history, long-term context" : "偏好、历史、长期情境", lane: "user_memory", kind: "lane", status: "available", counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 }, receipts: [], items: [], sourceScope, english, width: 1240, height: 142 } }, { id: "lane-agent", type: "lane", position: { x: 18, y: 338 }, zIndex: -1, selectable: false, draggable: false, data: { label: english ? "B  Agent Process Memory" : "B  智能体过程记忆", description: english ? "Trajectory, retrieval, intervention" : "轨迹、检索、干预", lane: "agent_process", kind: "lane", status: "available", counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 }, receipts: [], items: [], sourceScope, english, width: 1240, height: 142 } }, { id: "lane-rag", type: "lane", position: { x: 18, y: 512 }, zIndex: -1, selectable: false, draggable: false, data: { label: english ? "C  External RAG" : "C  外部 RAG", description: english ? "External documents and knowledge" : "外部文档与知识", lane: "external_rag", kind: "lane", status: "available", counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 }, receipts: [], items: [], sourceScope, english, width: 1240, height: 214 } }];
    setNodes([...backgrounds, ...spec.nodes.map((node) => ({ ...node, position: positions[node.id] ?? { x: 0, y: 0 } }))]);
    setEdges(spec.edges.map((edge) => ({ ...edge, data: { kind: edge.data?.kind ?? "serial", status: edge.data?.status ?? "available" } })));
  }, [spec, english, sourceScope]);
  useEffect(() => { if (!nodes.length || !flowApi.current) return; const timer = window.requestAnimationFrame(() => flowApi.current?.fitView({ padding: 0.08, duration: 0 })); return () => window.cancelAnimationFrame(timer); }, [nodes.length, promptId, english]);
  useEffect(() => { if (spec.edges.length) setEdges(spec.edges.map((edge) => ({ ...edge, data: { kind: edge.data?.kind ?? "serial", status: edge.data?.status ?? "available" } }))); }, [spec]);
  const close = () => { setSelected(null); setSelectedEdge(null); };
  return <TopologyEngineLab key={promptId || "unbound"} english={english} nodes={spec.nodes} edges={spec.edges} />;
}

function TopologyNodeCard({ data, selected }: NodeProps<TopologyNode>) { const metrics = `${data.english ? "Candidates" : "候选"} ${data.counts.candidates} · ${data.english ? "Delivered" : "送达"} ${data.counts.delivered}`; const tone = data.lane === "user_memory" ? "teal" : data.lane === "agent_process" ? "amber" : data.lane === "external_rag" ? "cyan" : data.lane === "writeback" ? "emerald" : "blue"; return <div className={`execution-node execution-node-${tone} ${selected ? "is-selected" : ""}`}><Handle type="target" position={Position.Left} id="left" /><Handle type="target" position={Position.Top} id="top" /><strong>{data.label}</strong><small>{data.description}</small><span className="execution-node-status">{statusText(data.status, data.english)}</span><em>{metrics}</em><Handle type="source" position={Position.Right} id="right" /><Handle type="source" position={Position.Bottom} id="bottom" /></div>; }

function LaneBackground({ data }: NodeProps<TopologyNode>) { return <div className={`execution-lane-background execution-lane-${data.lane}`}><b>{data.label}</b><small>{data.description}</small></div>; }

function OrthogonalEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps<TopologyEdge>) {
  const branchOrder: Record<string, number> = {
    "user-memory-preference": 0, "user-memory-recall": 1, "user-memory-research": 2, "user-memory-scenario-summary": 3, "user-memory-read-source": 4,
    "agent-process-memory-observe-trajectory": 0, "agent-process-memory-process-retrieval": 1, "agent-process-memory-agent-research": 2, "agent-process-memory-agent-scenario-summary": 3, "agent-process-memory-migration-revalidation": 4, "agent-process-memory-compatibility-gate": 5, "agent-process-memory-guidance": 6,
    "external-rag-lexical": 0, "external-rag-vector": 1, "external-rag-fusion-rrf": 2, "external-rag-rerank": 3, "external-rag-jev-review": 4,
  };
  const targetBranch = Object.keys(branchOrder).find((key) => id.endsWith(`-${key}`));
  const sourceBranch = Object.keys(branchOrder).find((key) => id.startsWith(`e-${key}-`));
  const branchIndex = branchOrder[targetBranch ?? sourceBranch ?? ""] ?? 0;
  const isForkBranch = Boolean(targetBranch);
  const isMergeBranch = Boolean(sourceBranch);
  const isLaneIngress = /^e-fork-(user-root|agent-root|rag-root)$/.test(id);
  const isPacketContext = /^e-(user-packet|agent-packet|rag-packet)-context$/.test(id);
  const packetOffsets: Record<string, number> = { "e-user-packet-context": 120, "e-agent-packet-context": 160, "e-rag-packet-context": 200 };
  const points = data?.points?.length ? data.points : isLaneIngress
    ? [{ x: sourceX, y: sourceY }, { x: sourceX, y: targetY - 30 }, { x: targetX - 24, y: targetY - 30 }, { x: targetX - 24, y: targetY }, { x: targetX, y: targetY }]
    : isPacketContext
      ? [{ x: sourceX, y: sourceY }, { x: sourceX + (packetOffsets[id] ?? 28), y: sourceY }, { x: sourceX + (packetOffsets[id] ?? 28), y: targetY + 28 }, { x: targetX, y: targetY + 28 }, { x: targetX, y: targetY }]
      : isForkBranch
    ? [{ x: sourceX, y: sourceY }, { x: sourceX + 28, y: sourceY }, { x: sourceX + 28, y: targetY }, { x: targetX, y: targetY }]
    : isMergeBranch
      ? [{ x: sourceX, y: sourceY }, { x: targetX - 28, y: sourceY }, { x: targetX - 28, y: targetY }, { x: targetX, y: targetY }]
      : packetOffsets[id] != null
        ? [{ x: sourceX, y: sourceY }, { x: sourceX + packetOffsets[id], y: sourceY }, { x: sourceX + packetOffsets[id], y: targetY }, { x: targetX, y: targetY }]
        : [{ x: sourceX, y: sourceY }, { x: (sourceX + targetX) / 2, y: sourceY }, { x: (sourceX + targetX) / 2, y: targetY }, { x: targetX, y: targetY }];
  const path = `M ${points.map((point) => `${point.x} ${point.y}`).join(" L ")}`;
  const end = points[points.length - 1]; const prev = points[Math.max(0, points.length - 2)]; const angle = Math.atan2(end.y - prev.y, end.x - prev.x);
  const arrowEnd = { x: end.x - 10 * Math.cos(angle), y: end.y - 10 * Math.sin(angle) }; const size = 9;
  const left = { x: arrowEnd.x - size * Math.cos(angle - Math.PI / 6), y: arrowEnd.y - size * Math.sin(angle - Math.PI / 6) }; const right = { x: arrowEnd.x - size * Math.cos(angle + Math.PI / 6), y: arrowEnd.y - size * Math.sin(angle + Math.PI / 6) };
  const stroke = data?.kind === "writeback" ? "#16835b" : "#315f93"; const markerId = `execution-arrow-${id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  return <g className="execution-orthogonal-edge"><defs><marker id={markerId} markerWidth="14" markerHeight="14" refX="10" refY="0" orient="auto" markerUnits="userSpaceOnUse"><path d="M 0 -5 L 10 0 L 0 5 Z" fill={stroke} /></marker></defs><path className="execution-edge-marker-path" d={path} fill="none" stroke={stroke} strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round" markerEnd={`url(#${markerId})`} /><path className="execution-edge-arrow-fallback" d={`M ${arrowEnd.x} ${arrowEnd.y} L ${left.x} ${left.y} L ${right.x} ${right.y} Z`} fill={stroke} stroke={stroke} strokeWidth="1" /></g>;
}
