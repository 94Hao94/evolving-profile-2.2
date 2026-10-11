import type { Edge, Node } from "@xyflow/react";
import type { FlowLane, FlowLaneSummary, FlowReceipt } from "@/lib/flow-receipt";
import { isContextBranch } from "./topology-presentation";
import { sumFlowCounts, sumObservedCounts, type FlowCounts } from "./flow-receipt";
import type { SourceNavigation } from "./tool-receipt";
import type { ReturnedContentItem } from "./returned-content-snapshot";

export type TopologyItem = { kind: string; text: string; source_navigation?: SourceNavigation; returned_item?:ReturnedContentItem };
export type TopologyNodeData = {
  label: string;
  description: string;
  lane: FlowLane | "serial" | "writeback";
  kind: "stage" | "fork" | "merge" | "packet" | "tool" | "writeback" | "receipt" | "lane";
  status: string;
  counts: FlowCounts;
  receipts: FlowReceipt[];
  items: TopologyItem[];
  sourceScope: string;
  english: boolean;
  width?: number;
  height?: number;
};
export type TopologyEdgeData = { kind: "serial" | "branch" | "merge" | "context" | "writeback"; status: string; points?: Array<{ x: number; y: number }> };
export type TopologyBranchSummary = {
  status: string;
  counts: FlowCounts;
  receipts: FlowReceipt[];
  items: TopologyItem[];
};
export type TopologyNode = Node<TopologyNodeData>;
export type TopologyEdge = Edge<TopologyEdgeData>;

export function itemText(item: { text?: string; text_preview?: string; title?: string; id?: string }) { return item.text || item.text_preview || item.title || item.id || "—"; }

function uniqueItems(items: TopologyItem[]): TopologyItem[] {
  const seen = new Set<string>();
  return items.filter((item) => { const key = `${item.kind}:${item.text}`; if (seen.has(key)) return false; seen.add(key); return true; });
}

export function buildTopologySpec(args: { english: boolean; summaries: Map<FlowLane, FlowLaneSummary>; sourceScope: string; userItems?: TopologyItem[]; guidanceItems?: TopologyItem[]; historyItems?: TopologyItem[]; branchSummaries?: Map<string, TopologyBranchSummary> }) {
  const { english, summaries, sourceScope } = args;
  const label = (en: string, zh: string) => english ? en : zh;
  const nodes: TopologyNode[] = [];
  const edges: TopologyEdge[] = [];
  const add = (id: string, data: Omit<TopologyNodeData, "english" | "sourceScope"> & Partial<Pick<TopologyNodeData, "english" | "sourceScope">>, extra: Partial<TopologyNode> = {}) => nodes.push({ id, type: data.kind, position: { x: 0, y: 0 }, data: { ...data, english, sourceScope }, ...extra });
  const edge = (id: string, source: string, target: string, kind: TopologyEdgeData["kind"] = "serial", handles?: { source?: string; target?: string }) => edges.push({ id, source, target, type: "orthogonal", ...(handles?.source ? { sourceHandle: handles.source } : {}), ...(handles?.target ? { targetHandle: handles.target } : {}), data: { kind, status: "available" } });
  const zero = (lane: FlowLane | "serial" | "writeback") => summaries.get(lane as FlowLane) ?? { lane: lane as FlowLane, status: "not_observed", receipts: [], counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 } };
  const branch = (id: string, fallback: FlowLane): TopologyBranchSummary => args.branchSummaries?.get(id) ?? { status: "not_observed", counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 }, receipts: [], items: [] };
  const branchDescription = (stableKey: string) => { const descriptions: Record<string, [string, string]> = { preference: ["Read conditional user preferences", "读取条件化用户偏好"], recall: ["Recall relevant user-memory candidates", "召回相关用户记忆候选"], research: ["Research across user history and timelines", "跨用户历史与时间线进行研究"], "scenario-summary": ["Summarize competing user contexts and Sessions", "整理用户侧竞争情境与 Session"], "read-source": ["Read back original user sources", "回读用户原始来源"], "observe-trajectory": ["Capture the agent trajectory and process state", "记录智能体轨迹与过程状态"], "capability-observation": ["Assess capabilities, tools and execution quality", "评估能力、工具与执行表现"], "failure-events": ["Collect failures and process incidents", "收集失败与过程事件"], "repair-mode": ["Identify applicable repair patterns", "识别适用的修复模式"], "reusable-process-strategy": ["Extract reusable process strategies", "提取可复用过程策略"], "process-retrieval": ["Recall relevant agent process memories", "召回相关智能体过程记忆"], "agent-research": ["Research process experience across tasks and projects", "跨任务与项目研究过程经验"], "agent-scenario-summary": ["Summarize agent-memory Sessions and contexts", "整理智能体记忆的 Session 与情境"], "migration-revalidation": ["Revalidate migrated process experience", "重新验证迁移后的过程经验"], "compatibility-gate": ["Filter incompatible or unverified process evidence", "过滤不兼容或未核验的过程证据"], "guidance-hint": ["Provide a lightweight execution hint", "提供轻量执行提示"], "guidance-recommend": ["Provide an actionable process recommendation", "提供可执行的过程建议"], "guidance-scaffold": ["Provide a structured execution scaffold", "提供结构化执行脚手架"], "guidance-guard": ["Guard against unsafe routes and scope drift", "防止错误路由与范围越界"], lexical: ["Search external documents by lexical match", "按词法匹配检索外部文档"], vector: ["Search external documents by semantic similarity", "按语义相似度检索外部文档"], "fusion-rrf": ["Fuse lexical and vector candidates", "融合词法与向量候选"], rerank: ["Rerank external candidates by relevance", "按相关性重排外部候选"], "jev-review": ["Review and verify external knowledge", "审查并核验外部知识"] }; return descriptions[stableKey] ?? ["Topology route node", "拓扑路线节点"]; };
  const packet = (lane: FlowLane, name: string, zh: string, items: TopologyItem[] = []) => { const s = zero(lane); return { label: label(name, zh), description: label("Curated context packet", "整理后的上下文包"), lane, kind: "packet" as const, status: s.status, counts: s.counts, receipts: s.receipts, items, width: 188, height: 74 }; };
  add("prompt", { label: label("Prompt Ingress", "Prompt 入口"), description: label("User request received", "收到用户请求"), lane: "serial", kind: "stage", status: "observed", counts: zero("ingress").counts, receipts: zero("ingress").receipts, items: [], width: 170, height: 72 });
  add("binding", { label: label("Host / Hook Binding", "宿主 / Hook 绑定"), description: label("Bind tools, skills, environment", "绑定工具、Skill、环境"), lane: "serial", kind: "stage", status: "observed", counts: zero("ingress").counts, receipts: zero("ingress").receipts, items: [], width: 184, height: 72 });
  add("contract", { label: label("Task Contract", "任务契约"), description: label("Parse, plan, authorize", "解析、规划、授权"), lane: "serial", kind: "stage", status: "observed", counts: zero("guidance").counts, receipts: zero("guidance").receipts, items: [], width: 170, height: 72 });
  add("fork", { label: label("FORK", "分支"), description: label("Parallel lanes", "并行泳道"), lane: "serial", kind: "fork", status: "available", counts: { candidates: 0, returned: 0, delivered: 0, tokens: 0 }, receipts: [], items: [], width: 64, height: 64 });
  add("context", { label: label("Context Assembly", "上下文组装"), description: label("Unify memory and sources", "汇总记忆与来源"), lane: "serial", kind: "stage", status: zero("model_context").status, counts: zero("model_context").counts, receipts: zero("model_context").receipts, items: [], width: 184, height: 72 });
  const deliveredToAgent = sumObservedCounts(["user_memory", "agent_process", "external_rag"].map(lane=>zero(lane as FlowLane).counts.delivered));
  add("execution", { label: label("Agent Context Delivery", "智能体上下文送达"), description: label("Observed context delivery; adoption and knowledge writeback unmeasured", "已观测上下文送达；回答采用与长期写入未测量"), lane: "serial", kind: "stage", status: (deliveredToAgent ?? 0) > 0 ? "delivered" : zero("model_context").status, counts: { candidates: deliveredToAgent, returned: deliveredToAgent, delivered: deliveredToAgent, tokens: 0 }, receipts: [], items: [], width: 174, height: 72 });
  edge("e-prompt-binding", "prompt", "binding"); edge("e-binding-contract", "binding", "contract"); edge("e-contract-fork", "contract", "fork"); edge("e-fork-context", "fork", "context", "context"); edge("e-context-execution", "context", "execution");

  const laneConfig: Array<{ lane: FlowLane; root: string; packetId: string; title: [string, string]; description: [string, string]; branches: Array<[string, string, string]> }> = [
    { lane: "user_memory", root: "user-root", packetId: "user-packet", title: ["User Memory", "用户记忆"], description: ["Preferences, history, long-term context", "偏好、历史、长期情境"], branches: [["User Preference", "用户偏好", "preference"], ["User Recall", "用户召回", "recall"], ["User Research", "用户研究", "research"], ["User Scenario Summary", "用户情景摘要", "scenario-summary"], ["User Source Readback", "用户原文回读", "read-source"]] },
    { lane: "agent_process", root: "agent-root", packetId: "agent-packet", title: ["Agent Process Memory", "智能体过程记忆"], description: ["Observation, retrieval, scenario context, repair and guidance", "观察、召回、情景上下文、修复与指导"], branches: [["Agent Observe", "智能体观察", "observe-trajectory"], ["Capability Observation", "能力观测", "capability-observation"], ["Failure Events", "失败事件", "failure-events"], ["Repair Mode", "修复模式", "repair-mode"], ["Reusable Process Strategy", "可复用过程策略", "reusable-process-strategy"], ["Agent Recall", "智能体召回", "process-retrieval"], ["Agent Research", "智能体研究", "agent-research"], ["Agent Scenario Summary", "智能体情景摘要", "agent-scenario-summary"], ["Migration / Revalidation", "迁移与再验证", "migration-revalidation"], ["Compatibility Gate", "兼容性门控", "compatibility-gate"], ["Hint", "提示", "guidance-hint"], ["Recommend", "建议", "guidance-recommend"], ["Scaffold", "脚手架", "guidance-scaffold"], ["Guard", "防护", "guidance-guard"]] },
    { lane: "external_rag", root: "rag-root", packetId: "rag-packet", title: ["External RAG", "外部 RAG"], description: ["External documents and knowledge", "外部文档与知识"], branches: [["Lexical", "词法检索", "lexical"], ["Vector", "向量检索", "vector"], ["Fusion / RRF", "融合 / RRF", "fusion-rrf"], ["Rerank", "Rerank", "rerank"], ["JEV Review", "JEV 审查", "jev-review"]] },
  ];
  for (const config of laneConfig) {
    const s = zero(config.lane); const root = config.root;
    const laneKey = config.lane === "agent_process" ? "agent-process-memory" : config.lane.replace("_", "-");
    const branchIds = config.branches.map(([, , stableKey]) => `${laneKey}-${stableKey}`);
    const childTotals = branchIds.filter(isContextBranch).map((id) => branch(id, config.lane));
    const aggregate = sumFlowCounts(childTotals.map(child=>child.counts));
    const aggregateItems = childTotals.flatMap((child) => child.items);
    const hasBranchEvidence = childTotals.some((child) => child.receipts.length || child.items.length || child.counts.candidates || child.counts.returned || child.counts.delivered);
    const rootCounts = aggregate;
    add(root, { label: label(...config.title), description: label(...config.description), lane: config.lane, kind: "tool", status: hasBranchEvidence ? (rootCounts.delivered ? "delivered" : rootCounts.returned ? "candidate_returned" : "observed") : s.status, counts: hasBranchEvidence ? rootCounts : s.counts, receipts: childTotals.flatMap((child) => child.receipts), items: uniqueItems(aggregateItems), width: 210, height: 86 });
    edge(`e-fork-${root}`, "fork", root, "branch", { source: "bottom", target: "left" });
    for (const [en, zh, stableKey] of config.branches) { const id = `${laneKey}-${stableKey}`; const projection = branch(id, config.lane); const description = branchDescription(stableKey); add(id, { label: label(en, zh), description: label(description[0], description[1]), lane: config.lane, kind: "tool", status: projection.status, counts: projection.counts, receipts: projection.receipts, items: projection.items, width: 150, height: 56 }); edge(`e-${root}-${id}`, root, id, "branch"); edge(`e-${id}-${config.packetId}`, id, config.packetId, "merge"); }
    add(config.packetId, {...packet(config.lane, config.lane === "external_rag" ? "RAG Packet" : config.lane === "agent_process" ? "Process Memory Packet" : "User Memory Packet", config.lane === "external_rag" ? "RAG 知识包" : config.lane === "agent_process" ? "过程记忆包" : "用户记忆包", uniqueItems(aggregateItems)), ...(hasBranchEvidence ? {counts:aggregate,receipts:childTotals.flatMap(c=>c.receipts)} : {})});
    edge(`e-${config.packetId}-context`, config.packetId, "context", "context", { source: "right", target: "bottom" });
  }
  if(args.branchSummaries?.size){
    const packets=nodes.filter(node=>["user-packet","agent-packet","rag-packet"].includes(node.id));
    const counts=sumFlowCounts(packets.map(node=>node.data.counts));
    const receipts=packets.flatMap(node=>node.data.receipts);
    for(const id of ["context","execution"]){
      const node=nodes.find(node=>node.id===id)!;
      node.data={...node.data,counts,receipts,status:counts.delivered ? "delivered" : counts.returned ? "candidate_returned" : "not_observed"};
    }
  }
  return { nodes, edges };
}
