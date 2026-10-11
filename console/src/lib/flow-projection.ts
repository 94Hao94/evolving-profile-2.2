export type FlowEvidenceItem = {
  id: string;
  type?: string;
  text?: string;
  score?: number;
  title?: string;
};

import { projectMemoryQualityEvents, summarizeMemoryQualityEvents, type MemoryQualityEvent, type MemoryQualityMode } from "@/lib/memory-quality-event";
import { mergeToolReceipts } from "./tool-receipt";
import type { SourceNavigation } from "./tool-receipt";

export type FlowPrompt = {
  candidate_groups?: Array<{id:string;actor:string;count?:number|null}>;
  prompt_id: string;
  at: string;
  user_prompt: string;
  source?: string;
  system_probe?: { actor?: string; state?: string; calls?: number; returned_count?: number; text_returned_count?:number;locator_returned_count?:number;candidate_count?: number | null; context_tokens?: number; max_tokens?: number; token_counter?: string; reason?: string; error_type?: string; delivery_stage?: string; admission?: { mode?: string; admitted_count?: number; rejected_count?: number; focus_terms?: string[]; reason?: string } } | null;
  history_plan?: { recommended_route?: string; history_dependency?: string; minimum_action?: string; reason?: string; required_slots?: string[]; context_source?: string; boundary?: string; fallback_route?: string | null; fallback_trigger?: string | null; candidate_policy?: string } | null;
  task_state?: { current_objective?:string; current_message?:string; continuation?:boolean; continuation_context?:string|null; source?:string; authority?:string } | null;
  evidence_decision?: { need?:string; known_from_current_context?:boolean; unresolved_slots?:string[]; chosen_route?:string; sufficiency?:string; conflicts?:string[]; source_ids?:string[]; next_action?:string|null; stop_reason?:string|null; boundary?:string } | null;
  history_decision?: "needed" | "not_needed" | "unknown" | "agent_decides";
  history_decision_evidence?: string;
  memory_route_receipt?: {
    call_coverage?: "exact_host_turn_complete" | "partial" | "unknown";
    decision?: string;
    recommended_route?: string;
    reason?: string;
    confidence?: number;
    confidence_semantics?: string;
    matched_nodes?: string[];
    catalog_probe?: { status?: string; candidate_count?: number | null; matched_entities?: string[]; catalog_coverage?: string };
    catalog_hints?: Array<{ topic_id?: string; title?: string; abstract?: string; overview?: string; entities?: string[]; time_range?: {start?:string|null;end?:string|null}; source_count?:number; source_count_semantics?:string; coverage?:{sampled?:number;total?:number|null;semantics?:string}; pending_changes?:number|null; conflicts?:string[]|null; pending_changes_status?:string; conflict_status?:string; content_status?:string; refreshed_at?:string|null; boundary?:string; memory_id?: string; type?: string; topic?: string; mentioned_at?: string | null; occurred_start?: string | null; occurred_end?: string | null; state?: string }>;
    agent_may_override?: boolean;
    route_required?: boolean;
    route_started?: boolean;
    tool_called?: boolean;
    returned_count?: number;
    source_navigation_returned_count?: number;
    source_navigation_returned_ids?: string[];
    delivery_state?: string;
    unresolved?: string[];
    tool_events?: Array<{ source_navigation_returned_count?: number; source_navigation_returned_ids?: string[]; source_navigation?: SourceNavigation[]; tool?: string; at?: string; route?: string; check_id?: string | null; session_id?: string | null; turn_id?: string | null; hook_invocation_id?: string | null; research_id?: string | null; query?: string | null; candidate_count?: number | null; returned_count?: number | null; next_offset?: number | null; memory_id?: string | null; memory_ids?: string[]; scenario_ids?: string[]; scenario_navigation_roles?: Array<[string,string]>; scenario_type?: string | null; scenario_tier?: string | null; scenario_episode_id?: string | null; scenario_episode_title?: string | null; scenario_episode_count?: number | null; scenario_episodes?: Array<{episode_id?:string;title?:string;title_authority?:string;start_message_id?:string;start_user_message_id?:string;end_message_id?:string;source_message_count?:number;source_revision?:string;status?:string}>; scenario_summary_status?: string | null; scenario_summary_text?: string | null; scenario_summary_truncated?: boolean; scope_hypothesis_count?: number | null; scope_route_policy?: {state?:string;required_next_action?:string;defer_bank_retrieval_until_scope_check?:boolean}; scenario_decision?: string | null; delivery?: { host_visibility?: string; answer_use?: string } }>;
  } | null;
  time_window_activity?: {
    state?: "observed" | "not_observed";
    window_minutes?: number;
    start?: string;
    end?: string;
    event_count?: number;
    by_tool?: Record<string, { calls?: number; returned?: number; candidates?: number; latest_at?: string | null }>;
    events?: Array<{ tool?: string; at?: string; check_id?: string | null; returned_count?: number | null; candidate_count?: number | null; research_id?: string | null; memory_id?: string | null; memory_ids?: string[] }>;
    unattributed_activity?: { state?: "observed" | "not_observed"; event_count?: number; by_tool?: Record<string, { calls?: number; returned?: number; latest_at?: string | null }>; boundary?: string } | null;
    items?: FlowEvidenceItem[];
    boundary?: string;
    candidate_count?: number;
    candidate_research_ids?: string[];
    candidate_items?: Array<FlowEvidenceItem & { candidate_only?: boolean }>;
    candidate_queries?: Array<{ query?: string; candidate_count?: number; anchors?: string[] }>;
  } | null;
  time_window_guidance_activity?: {
    state?: "observed" | "not_observed";
    window_minutes?: number;
    start?: string;
    end?: string;
    event_count?: number;
    returned_count?: number;
    deferred_count?: number;
    ids?: string[];
    unattributed_activity?: { state?: "observed" | "not_observed"; event_count?: number; returned_count?: number; deferred_count?: number; by_tool?: Record<string, { calls?: number; returned?: number; deferred?: number; latest_at?: string | null }>; boundary?: string } | null;
    boundary?: string;
  } | null;
  instruction_receipt?: {
    instruction_version: string;
    content_sha256: string;
    core_text: string;
    source_file: string;
    stage: string;
    model_context_visibility: string;
  } | null;
  navigation_map?: {
    schema?: string;
    context_chars?: number;
    context_text?: string;
    preferences?: { status?: string; approved_count?: number; preview_count?: number; dimensions?: Array<{ id?: string; label?: string; count?: number; scopes?: Array<{ scope?: string }> }> };
    bank?: { status?: string; entity_count?: number; searchable_entity_count?: number; manifest_count?: number;
      hierarchy_coverage?: { total_memory_count?: number; indexed_memory_count?: number; unassigned_memory_count?: number; leaf_count?: number; generated_at?: string };
      topics?: Array<{ topic_id?: string; title?: string; navigation_summary?: string; source_count?: number; source_count_semantics?: string; memory_count?: number; children?: string[] }> };
  } | null;
  routes?: Record<string, string>;
  guidance_receipt?: {
    host_state?: string;
    coverage?: string;
    deferred_count?: number;
    deferred?: Array<{ id: string; revision?: string; reason?: string }>;
    guidance_items?: FlowEvidenceItem[];
    model_sections?: FlowEvidenceItem[];
    stable_profile_count?: number;
    preference_candidate_count?: number;
    stable_profile?: FlowEvidenceItem[];
  } | null;
  historical_audit?: {
    route?: string;
    state?: "observed" | "executed_empty" | "executed_no_result" | "not_observed" | "unknown";
    mode?: "hook_auto_recall" | "candidate_discovery" | "research" | "system_probe";
    controller_state?: "admission_applied" | "not_in_candidate_path" | "not_used" | "system_probe_direct";
    candidate_count?: number | null;
    qualified_count?: number | null;
    rejected_count?: number | null;
    prepared_item_count?: number | null;
    returned_to_host_count?: number | null;
    unread_candidate_count?: number | null;
    delivery_state?: string;
    items?: FlowEvidenceItem[];
    admission_items?: FlowEvidenceItem[];
    deferred_items?: FlowEvidenceItem[];
  } | null;
};

export function projectFlowAudit(prompt: FlowPrompt, qualityMode: MemoryQualityMode = "lightweight") {
  const receipt = prompt.guidance_receipt;
  const history = prompt.historical_audit;
  const promptToolEvents = mergeToolReceipts(prompt.memory_route_receipt?.tool_events ?? []);
  const boundHistory = promptToolEvents.filter(event =>
    ["recall", "user_recall", "research", "user_research", "read_research", "read_source", "find_sources", "search_scenario_summary", "search_scenario_contexts", "read_scenario_summary", "read_context_summary", "scenario_gate"].includes(event.tool ?? ""));
  const actualEvents = boundHistory.length ? boundHistory :
    prompt.time_window_activity?.boundary === "same_prompt_binding_only" ? prompt.time_window_activity.events ?? [] : [];
  const observedToolEvents = promptToolEvents.length ? promptToolEvents : actualEvents;
  const measuredResults = observedToolEvents.filter(event => typeof event.returned_count === "number");
  const actualRoute = actualEvents.length ? [...new Set(actualEvents.map(event => event.tool))].join("+") : null;
  const actualReturned = actualEvents.reduce((sum, event) => sum + (event.returned_count ?? 0), 0);
  const actualCountKnown=actualEvents.length>0 && actualEvents.every(event=>typeof event.returned_count==="number" && Number.isFinite(event.returned_count) && event.returned_count>=0);
  const actualReturnedCount=actualCountKnown ? actualReturned : null;
  const navigationEvents=actualEvents.filter((event:any)=>typeof event.source_navigation_returned_count==="number");
  const navigationCount=navigationEvents.length ? navigationEvents.reduce((sum:number,event:any)=>sum+event.source_navigation_returned_count,0) : null;
  const observedNavigationCount=observedToolEvents.reduce((sum:number,event:any)=>sum+(event.source_navigation_returned_count || 0),0);
  const plannedRoute = prompt.history_plan?.recommended_route ?? prompt.memory_route_receipt?.recommended_route;
  const routeRequired = Boolean(
    prompt.history_plan?.minimum_action === "recall_probe" ||
    prompt.history_plan?.history_dependency === "likely" ||
    plannedRoute === "recall" || plannedRoute === "research"
  );
  const routeStarted = actualEvents.length > 0;
  const routeStatus = routeRequired && !routeStarted
    ? "ep_history_verification_incomplete"
    : routeStarted
      ? (actualReturnedCount===null ? "ep_history_tool_called_unmeasured" : actualReturnedCount > 0 ? "ep_history_verified_candidate_returned" : navigationCount ? "ep_history_source_navigation_returned" : "ep_history_tool_called_empty")
      : "ep_history_not_required";
  const guidanceItems = [
    ...(receipt?.stable_profile ?? []).map((item) => ({ ...item, type: item.type ?? "stable_profile" })),
    ...(receipt?.guidance_items ?? []),
    ...(receipt?.model_sections ?? []).map((section) => ({ ...section, type: section.type ?? "fusion_model" })),
  ];
  const navigation = prompt.navigation_map;

  const qualityEvents: MemoryQualityEvent[] = projectMemoryQualityEvents(prompt, qualityMode);
  return {
    hostEvidence: {
      entry: prompt.instruction_receipt ? "prepared" : "unknown",
      mcp: observedToolEvents.length ? "observed" : "unknown",
      history: actualEvents.length ? "observed" : "not_observed",
      result: measuredResults.length
        ? measuredResults.some(event => (event.returned_count ?? 0) > 0) ? "returned" : observedNavigationCount ? "source_navigation_returned" : "returned_empty"
        : "unknown",
    },
    entry: {
      value: receipt?.host_state ?? prompt.routes?.entry_guidance ?? "not_observed",
      source: prompt.source ?? "unknown",
      coverage: receipt?.coverage ?? "unknown",
      instruction: prompt.instruction_receipt ?? null,
      taskState: prompt.task_state ?? null,
    },
    guidance: {
      count: guidanceItems.length,
      deferred: receipt?.deferred_count ?? 0,
      items: guidanceItems,
      stableProfileCount: receipt?.stable_profile_count ?? receipt?.stable_profile?.length ?? 0,
      candidateCount: receipt?.preference_candidate_count ?? receipt?.guidance_items?.length ?? 0,
      coverage: receipt?.coverage ?? "not_observed",
    },
    map: {
      receipt: navigation ?? null,
      l0: {
        status: navigation ? "observed" : "not_observed",
        contextChars: navigation?.context_chars ?? null,
        preferenceCount: navigation?.preferences?.approved_count ?? null,
        preferencePreviewCount: navigation?.preferences?.preview_count ?? null,
        dimensions: navigation?.preferences?.dimensions ?? [],
        topicCount: navigation?.bank?.manifest_count ?? null,
        topics: navigation?.bank?.topics ?? [],
        coverage: navigation?.bank?.hierarchy_coverage ?? null,
      },
      l1: {
        previewEntities: navigation?.bank?.entity_count ?? null,
        searchableEntities: navigation?.bank?.searchable_entity_count ?? null,
        topics: navigation?.bank?.topics ?? [],
      },
      l2: {
        actualRoute: history?.route ?? prompt.routes?.historical_memory ?? "not_observed",
        candidates: history?.candidate_count ?? null,
        returned: history?.returned_to_host_count ?? null,
        unread: history?.unread_candidate_count ?? null,
        items: history?.items ?? [],
      },
    },
    history: {
      value: actualRoute ?? history?.route ?? prompt.routes?.historical_memory ?? "not_observed",
      state: actualRoute ? (actualReturnedCount===null ? "executed_no_result" : actualReturnedCount || navigationCount ? "observed" : "executed_empty") : history?.state ?? prompt.routes?.historical_memory ?? "unknown",
      calls: actualEvents.length,
      decision: actualRoute ? "needed" : prompt.history_decision ?? ((history?.state ?? prompt.routes?.historical_memory ?? "unknown") === "unknown" ? "unknown" : "needed"),
      decisionEvidence: prompt.history_decision_evidence ?? "not_observed",
      routeReceipt: prompt.memory_route_receipt ?? null,
      mode: history?.mode ?? (history?.route === "recall" || history?.route === "recall_and_research" ? "hook_auto_recall" : "not_observed"),
      controller: history?.controller_state ?? (history?.route === "recall" || history?.route === "recall_and_research" ? "admission_applied" : "not_used"),
      metrics: {
        candidates: history?.candidate_count ?? null,
        returned: actualRoute ? actualReturnedCount : history?.returned_to_host_count ?? null,
        unread: history?.unread_candidate_count ?? null,
        rejected: history?.rejected_count ?? null,
      },
      items: history?.items ?? [],
      delivered: history?.delivery_state === "observed" ? history?.returned_to_host_count ?? null : null,
      delivery: history?.delivery_state ?? "not_observed",
      evidenceDecision: prompt.evidence_decision ?? null,
      routeAudit: {
        route_required: routeRequired,
        route_started: routeStarted,
        tool_called: routeStarted,
        returned_count: actualReturnedCount,
        ...(navigationCount!==null ? {source_navigation_returned_count:navigationCount,source_navigation_returned_ids:[...new Set(navigationEvents.flatMap((event:any)=>event.source_navigation_returned_ids || []))]} : {}),
        delivery_state: routeStarted ? (actualReturnedCount===null ? "not_measured" : actualReturnedCount > 0 ? "returned" : navigationCount ? "source_navigation_returned" : "returned_empty") : "not_started",
        unresolved: routeRequired && !routeStarted ? ["EP历史工具尚未调用，不能把本地文件搜索或候选提示算作历史核验"] : [],
        status: routeStatus,
      },
    },
    systemProbe: prompt.system_probe ?? null,
    historyPlan: prompt.history_plan ?? null,
    timeWindowActivity: prompt.time_window_activity ?? null,
    timeWindowGuidanceActivity: prompt.time_window_guidance_activity ?? null,
    quality: {
      mode: qualityMode,
      events: qualityEvents,
      summary: summarizeMemoryQualityEvents(qualityEvents),
    },
  };
}
