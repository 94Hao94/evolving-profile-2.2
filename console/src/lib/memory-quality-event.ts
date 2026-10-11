import type { FlowPrompt } from "@/lib/flow-projection";
import { mergeToolReceipts } from "./tool-receipt";

export type MemoryQualityPlane = "user_memory" | "agent_process" | "scenario" | "external_rag" | "audit";
export type MemoryQualityStage = "capture" | "extract" | "associate" | "retrieve" | "deliver" | "readback" | "verify" | "promote" | "revalidate";
export type MemoryQualityStatus = "not_called" | "returned_zero" | "source_navigation_returned" | "returned" | "delivered" | "readback" | "verified" | "unavailable" | "failed" | "not_measured";
export type MemoryQualityMode = "lightweight" | "diagnostic" | "deep_audit";

export type MemoryQualityEvent = {
  schema: "evolving-profile.memory-quality-event.v1";
  event_id: string;
  trace_id: string;
  prompt_id: string;
  session_id: string | null;
  project_id: string | null;
  plane: MemoryQualityPlane;
  dimension: string;
  stage: MemoryQualityStage;
  route: string;
  status: MemoryQualityStatus;
  candidate_count: number | null;
  returned_count: number | null;
  source_navigation_returned_count?: number;
  source_navigation_returned_ids?: string[];
  delivered_count: number | null;
  readback_count: number | null;
  verified_count: number | null;
  latency_ms: number | null;
  extra_tokens: number | null;
  llm_calls: number;
  source_ids: string[];
  source_revision: string | null;
  build_revision: string | null;
  runtime_revision: string | null;
  config_generation: string | null;
  sampling_mode: MemoryQualityMode;
  created_at: string;
  evidence: "prompt_bound_receipt" | "host_retained_result" | "canonical_activity" | "bounded_activity" | "route_plan" | "not_observed";
  purpose?: string | null;
};

type ToolEvent = NonNullable<NonNullable<FlowPrompt["memory_route_receipt"]>["tool_events"]>[number];

function safeCount(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function routeName(tool: string): string {
  const map: Record<string, string> = {
    recall: "user_recall",
    user_recall: "user_recall",
    research: "user_research",
    user_research: "user_research",
    get_preference: "user_preference",
    user_preference: "user_preference",
    read_source: "user_read_source",
    read_research: "user_read_research",
    read_scenario_summary: "user_scenario_summary",
    search_scenario_summary: "user_scenario_summary_search",
    search_scenario_contexts: "user_scenario_summary_search",
    read_context_summary: "user_scenario_summary",
    scenario_gate: "user_scenario_gate",
    find_sources: "user_find_sources",
    agent_recall: "agent_recall",
    agent_research: "agent_research",
  };
  return map[tool] ?? tool;
}

function classify(tool: string): { plane: MemoryQualityPlane; dimension: string; stage: MemoryQualityStage } {
  if (/^(agent_|read_agent_|search_agent_|prepare_agent_|write_agent_|record_agent_|promote_agent_|revalidate_agent_|evaluate_agent_|manage_agent_)/.test(tool)) {
    const stage:MemoryQualityStage=tool.startsWith("read_") ? "readback" : tool==="record_agent_process_draft" ? "extract" : tool.startsWith("record_agent_") ? "capture" : tool.startsWith("promote_agent_") ? "promote" : tool.startsWith("evaluate_agent_") ? "verify" : /^(revalidate|manage)_agent_/.test(tool) ? "revalidate" : tool.startsWith("write_agent_") ? "associate" : "retrieve";
    return {plane:"agent_process",dimension:"process_memory",stage};
  }
  if (tool==="rag_search") return {plane:"external_rag",dimension:"external_documents",stage:"retrieve"};
  if (tool === "read_agent_process_memory") return { plane: "agent_process", dimension: "process_memory", stage: "readback" };
  if (tool === "read_preference_unit") return { plane: "user_memory", dimension: "preferences", stage: "readback" };
  if (tool.startsWith("agent_")) return { plane: "agent_process", dimension: "process_memory", stage: "retrieve" };
  if (tool === "get_preference" || tool === "user_preference") return { plane: "user_memory", dimension: "preferences", stage: "deliver" };
  if (["search_scenario_summary", "search_scenario_contexts"].includes(tool)) return { plane: "scenario", dimension: "scenario_summary", stage: "retrieve" };
  if (tool === "scenario_gate") return { plane: "scenario", dimension: "scenario_summary", stage: "associate" };
  if (["read_scenario_summary", "read_context_summary"].includes(tool)) return { plane: "scenario", dimension: "scenario_summary", stage: "readback" };
  if (tool.includes("source")) return { plane: "user_memory", dimension: "facts_and_experiences", stage: "readback" };
  return { plane: "user_memory", dimension: "facts_experiences_entities", stage: "retrieve" };
}

function canonicalClassification(event:ToolEvent, fallback:ReturnType<typeof classify>) {
  const metadata=event as ToolEvent & {plane?:unknown;stage?:unknown;purpose?:string;dimension?:string};
  const planes:Record<string,MemoryQualityPlane>={user_memory:"user_memory",user_preference:"user_memory",agent_process:"agent_process",scenario_context:"scenario",scenario:"scenario",external_rag:"external_rag",audit:"audit"};
  const stages:MemoryQualityStage[]=["capture","extract","associate","retrieve","deliver","readback","verify","promote","revalidate"];
  const plane=typeof metadata.plane==="string" && Object.hasOwn(planes,metadata.plane) ? planes[metadata.plane] : ["agent_process","user_memory"].includes(metadata.purpose || "") && fallback.plane==="scenario" ? metadata.purpose as MemoryQualityPlane : fallback.plane;
  const stage=typeof metadata.stage==="string" && stages.includes(metadata.stage as MemoryQualityStage) ? metadata.stage as MemoryQualityStage : fallback.stage;
  const dimension=metadata.plane==="user_preference" ? "preferences" : fallback.dimension;
  return {plane,stage,dimension};
}

function statusFor(event: ToolEvent): MemoryQualityStatus {
  const returned = safeCount(event.returned_count);
  const delivery = event.delivery?.host_visibility ?? "";
  if ((event as ToolEvent & { error_type?: string; failed?: boolean }).error_type || (event as any).failed) return "failed";
  if (["unavailable", "not_found", "unknown"].includes((event as any).result_status)) return "unavailable";
  if (returned === 0) return (safeCount(event.source_navigation_returned_count) ?? 0) > 0 ? "source_navigation_returned" : "returned_zero";
  if (delivery === "observed" || event.delivery?.answer_use === "observed") return "delivered";
  if (returned !== null && returned > 0) return "returned";
  return "not_measured";
}

function eventFromTool(prompt: FlowPrompt, toolEvent: ToolEvent, index: number, mode: MemoryQualityMode): MemoryQualityEvent {
  const judge = (toolEvent as ToolEvent & { jev_review?: { calls?: number; usage?: { input_tokens?: number } } }).jev_review;
  const tool = String(toolEvent.tool ?? "unknown");
  const kind = canonicalClassification(toolEvent,classify(tool));
  const legacyRoute = routeName(tool);
  const route = classify(tool).plane==="scenario" && kind.plane==="agent_process" ? legacyRoute.replace(/^user_/,"agent_") : legacyRoute;
  const returned = safeCount(toolEvent.returned_count);
  const candidate = safeCount(toolEvent.candidate_count);
  const sourceIds = [...new Set([...(toolEvent.memory_ids ?? []), ...(toolEvent.scenario_ids ?? []), ...(toolEvent.memory_id ? [toolEvent.memory_id] : [])].filter(Boolean))];
  return {
    schema: "evolving-profile.memory-quality-event.v1",
    event_id: `mqe:${prompt.prompt_id}:${(toolEvent as any).tool_call_id || (toolEvent as any).call_id || `${tool}:${index}`}`,
    trace_id: String(toolEvent.hook_invocation_id ?? toolEvent.turn_id ?? prompt.prompt_id),
    prompt_id: prompt.prompt_id,
    session_id: toolEvent.session_id ?? null,
    project_id: null,
    plane: kind.plane,
    dimension: kind.dimension,
    stage: kind.stage,
    route,
    status: statusFor(toolEvent),
    candidate_count: candidate,
    returned_count: returned,
    ...(safeCount(toolEvent.source_navigation_returned_count)!==null ? {source_navigation_returned_count:safeCount(toolEvent.source_navigation_returned_count)!,source_navigation_returned_ids:[...new Set(toolEvent.source_navigation_returned_ids || [])]} : {}),
    delivered_count: toolEvent.delivery?.host_visibility === "observed" ? returned : null,
    readback_count: kind.stage === "readback" ? returned : null,
    verified_count: null,
    latency_ms: null,
    extra_tokens: judge?.calls ? safeCount(judge?.usage?.input_tokens) : 0,
    llm_calls: safeCount(judge?.calls) ?? 0,
    source_ids: sourceIds,
    source_revision: null,
    build_revision: null,
    runtime_revision: null,
    config_generation: null,
    sampling_mode: mode,
    created_at: toolEvent.at ?? prompt.at,
    evidence: (toolEvent as any).source_type === "codex_host_tool_result" ? "host_retained_result" : (toolEvent as any).source_type === "mcp_tool_activity" ? "canonical_activity" : "prompt_bound_receipt",
    purpose: (toolEvent as any).purpose || (toolEvent as any).memory_plane || null,
  };
}

function plannedNoCall(prompt: FlowPrompt, mode: MemoryQualityMode): MemoryQualityEvent[] {
  const planned = prompt.history_plan?.recommended_route ?? prompt.memory_route_receipt?.recommended_route;
  const required = Boolean(prompt.history_plan?.minimum_action === "recall_probe" || prompt.history_plan?.history_dependency === "likely" || planned === "recall" || planned === "research");
  if (!required || prompt.memory_route_receipt?.tool_events?.length) return [];
  const route = planned === "research" ? "user_research" : "user_recall";
  const complete=prompt.memory_route_receipt?.call_coverage === "exact_host_turn_complete";
  const kind = classify(route === "user_research" ? "research" : "recall");
  return [{
    schema: "evolving-profile.memory-quality-event.v1",
    event_id: `mqe:${prompt.prompt_id}:${route}:not-called`,
    trace_id: prompt.prompt_id,
    prompt_id: prompt.prompt_id,
    session_id: null,
    project_id: null,
    plane: kind.plane,
    dimension: kind.dimension,
    stage: kind.stage,
    route,
    status: complete ? "not_called" : "not_measured",
    candidate_count: null,
    returned_count: null,
    delivered_count: null,
    readback_count: null,
    verified_count: null,
    latency_ms: null,
    extra_tokens: 0,
    llm_calls: 0,
    source_ids: [],
    source_revision: null,
    build_revision: null,
    runtime_revision: null,
    config_generation: null,
    sampling_mode: mode,
    created_at: prompt.at,
    evidence: complete ? "route_plan" : "not_observed",
  }];
}

export function projectMemoryQualityEvents(prompt: FlowPrompt, mode: MemoryQualityMode = "lightweight"): MemoryQualityEvent[] {
  const tools = mergeToolReceipts(prompt.memory_route_receipt?.tool_events ?? []);
  const events = tools.map((event, index) => eventFromTool(prompt, event, index, mode));
  return events.length ? events : plannedNoCall(prompt, mode);
}

export function summarizeMemoryQualityEvents(events: MemoryQualityEvent[]) {
  const totals = { candidates: 0, returned: 0, delivered: 0, readback: 0, verified: 0, llmCalls: 0, extraTokens: 0 };
  for (const event of events) {
    if (event.candidate_count !== null) totals.candidates += event.candidate_count;
    if (event.returned_count !== null) totals.returned += event.returned_count;
    if (event.delivered_count !== null) totals.delivered += event.delivered_count;
    if (event.readback_count !== null) totals.readback += event.readback_count;
    if (event.verified_count !== null) totals.verified += event.verified_count;
    totals.llmCalls += event.llm_calls;
    totals.extraTokens += event.extra_tokens ?? 0;
  }
  const measuredEventCount = events.filter((event) => ["prompt_bound_receipt", "bounded_activity", "host_retained_result", "canonical_activity"].includes(event.evidence)).length;
  const plannedEventCount = events.filter((event) => event.evidence === "route_plan").length;
  const deliveryMeasuredEventCount = events.filter((event) => event.delivered_count !== null).length;
  const uniqueSourceCount = new Set(events.flatMap(event => event.source_ids)).size;
  const navigationEvents=events.filter(event=>typeof event.source_navigation_returned_count==="number");
  const sourceNavigationReturned=navigationEvents.length ? navigationEvents.reduce((count,event)=>count+(event.source_navigation_returned_count || 0),0) : null;
  return { eventCount: events.length, measuredEventCount, plannedEventCount, unknownEventCount: events.length - measuredEventCount - plannedEventCount, deliveryMeasuredEventCount, uniqueSourceCount, sourceNavigationReturned, totals, countSemantics: "occurrence_totals_may_overlap", byRoute: Object.groupBy(events, (event) => event.route), byStatus: Object.groupBy(events, (event) => event.status) };
}
