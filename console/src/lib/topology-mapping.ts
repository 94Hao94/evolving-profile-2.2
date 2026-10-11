import type { TopologyBranchSummary } from "./topology-spec";
import type { FlowReceipt } from "./flow-receipt";
import { sumObservedCounts } from "./flow-receipt";
import { mergeToolReceipts, normalizeSourceNavigation } from "./tool-receipt";
import type { TopologyItem } from "./topology-spec";
import { enumUiText } from "./inline-i18n";

// Map by actual tool identity, not by arbitrary words in the Prompt.
const targets: Record<string, string> = {
  user_preference:"user-memory-preference", get_preference:"user-memory-preference",
  read_preference_unit:"user-memory-preference",
  user_recall:"user-memory-recall", recall:"user-memory-recall",
  user_research:"user-memory-research", research:"user-memory-research", read_research:"user-memory-research",
  read_source:"user-memory-read-source", find_sources:"user-memory-read-source",
  agent_recall:"agent-process-memory-process-retrieval", search_agent_process_memory:"agent-process-memory-process-retrieval",
  agent_research:"agent-process-memory-agent-research",
  read_agent_process_memory:"agent-process-memory-process-retrieval",
  record_agent_trajectory:"agent-process-memory-observe-trajectory",
  record_agent_capability_observation:"agent-process-memory-capability-observation",
  revalidate_agent_process_memory:"agent-process-memory-migration-revalidation",
  evaluate_agent_process_memory:"agent-process-memory-compatibility-gate",
  record_agent_process_draft:"agent-process-memory-reusable-process-strategy",
  prepare_agent_process_context:"agent-packet", rag_search:"rag-root",
};
const scenarioTools = new Set(["search_scenario_summary","read_scenario_summary","search_scenario_contexts","read_context_summary","scenario_gate"]);
export function projectToolBranches(events: any[], english: boolean): Map<string, TopologyBranchSummary> {
  const grouped = new Map<string, any[]>();
  let priorLane = events.some(e=>/^(agent_recall|agent_research)$/.test(e.tool)) ? "agent" : "user";
  const seen = new Set<string>();
  for (const [index, event] of mergeToolReceipts(events).entries()) {
    const key = `${event.session_id || ""}:${event.turn_id || ""}:${event.tool_call_id || `${event.tool}:${event.at || index}`}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (/^(agent_|read_agent)/.test(event.tool)) priorLane="agent";
    else if (/^(user_|read_source|find_sources|read_research)/.test(event.tool)) priorLane="user";
    const scenarioLane = event.memory_plane || event.purpose || event.scenario_plane || priorLane;
    const target = scenarioTools.has(event.tool)
      ? (["agent", "agent_process"].includes(scenarioLane) ? "agent-process-memory-agent-scenario-summary" : "user-memory-scenario-summary")
      : targets[event.tool];
    if (!target) continue;
    grouped.set(target, [...(grouped.get(target) || []), event]);
  }
  const out = new Map<string, TopologyBranchSummary>();
  for (const [target, rows] of grouped) {
    const discoveries = new Map<string, number>();
    const returnedValues: Array<number|null>=[],deliveredValues: Array<number|null>=[];
    let candidateKnown=true;
    const receipts: FlowReceipt[] = [];
    const items: TopologyItem[] = [];
    for (const [index, e] of rows.entries()) {
      // Readback is visible inside Recall details but not added to retrieval counts.
      const readback = e.tool === "read_agent_process_memory" || e.tool === "read_preference_unit";
      const deliveredCount=typeof e.delivered_count==="number" ? e.delivered_count
        : ["observed","received"].includes(e.delivery?.host_visibility) && typeof e.returned_count==="number" ? e.returned_count : null;
      if (!readback) {
        if (typeof e.candidate_count === "number") {
          const key=e.research_id || e.tool_call_id || `${e.tool}:${e.at || index}`;
          discoveries.set(key, Math.max(discoveries.get(key)||0,e.candidate_count));
        }
        else candidateKnown=false;
        returnedValues.push(typeof e.returned_count==="number" ? e.returned_count : null);
        deliveredValues.push(deliveredCount);
      }
      const ids=[...(e.memory_ids || []),...(e.scenario_ids || []),...(e.process_memory_ids || [])];
      const contents=e.mapping_items || [];
      items.push({kind:e.tool,text:`${english?"Call":"调用"} ${e.tool_call_id || e.at || index} · ${e.source_type || "ep_live_receipt"}\n${e.query || ""}\n${english?"Returned":"返回"} ${e.returned_count ?? "—"} · ${english?"Delivered":"送达"} ${deliveredCount ?? "—"}`});
      for (const i of contents) items.push({kind:e.tool,text:i.text || i.text_preview || i.title || i.id || "—",returned_item:{...i,text:i.text || i.text_preview || i.title || "",source_role:i.source_role || (e.source_type==="codex_host_tool_result"?"host_retained_return":"returned_candidate"),snapshot:i.snapshot===null?undefined:e.returned_content_snapshot || undefined}});
      if (!contents.length) for (const id of ids) items.push({kind:e.tool,text:`来源定位：${id}（本回执未记录正文）`});
      const navigation=e.failed ? [] : normalizeSourceNavigation(e.source_navigation);
      for (const locator of navigation) items.push({kind:"source_navigation",text:locator.memory_id,source_navigation:locator});
      if (e.scenario_summary_text) items.push({kind:e.tool,text:e.scenario_summary_text});
      if (scenarioTools.has(e.tool)) {
        const display=(value:string)=>enumUiText(value,english ? "en" : "zh-CN");
        items.push({kind:english ? "Scenario coverage" : "情景覆盖",text:`${english ? "Purpose" : "用途"}: ${display(e.purpose || "unspecified")} · ${display(e.scenario_summary_status || e.result_status || "unknown")} · ${display(e.scenario_summary_kind || "unknown")}\n${display(e.summary_processing?.status || "unknown")}`});
      }
      for(const v of e.verification_evidence || []) items.push({kind:"验证证据",text:JSON.stringify(v)});
      if(e.process_record?.repair_actions?.length) items.push({kind:"修复动作",text:e.process_record.repair_actions.join(" · ")});
      if(e.process_record?.failure_signature?.length) items.push({kind:"失败特征",text:e.process_record.failure_signature.join(" · ")});
      receipts.push({trace_id:e.tool_call_id || `${e.tool}:${e.at || index}`,stage:e.tool,lane:target.startsWith("agent")?"agent_process":target.startsWith("rag")?"external_rag":"user_memory",
        status:e.failed?"failed":(e.returned_count>0?"candidate_returned":"observed"), source_type:e.source_type || "ep_live_receipt",
        candidate_count:readback?null:e.candidate_count,returned_count:e.returned_count ?? null,delivered_count:deliveredCount,
        ...(typeof e.source_navigation_returned_count==="number" ? {source_navigation_returned_count:e.source_navigation_returned_count,source_navigation_returned_ids:navigation.map(row=>row.memory_id)} : {}),
        started_at:e.at,summary:e.query || e.tool,source_ids:ids, verification_evidence:e.verification_evidence,relevance_audit:e.relevance_audit});
    }
    const returned=sumObservedCounts(returnedValues),delivered=sumObservedCounts(deliveredValues);
    out.set(target,{status:rows.some(e=>e.failed)?"failed":rows.every(e=>String(e.result_status||"").startsWith("disabled"))?"disabled":delivered?"delivered":returned?"candidate_returned":"observed",
      counts:{candidates:candidateKnown ? [...discoveries.values()].reduce((a,b)=>a+b,0) : null,returned,delivered,tokens:0},receipts,items});
    // A draft can document a repair, but is not a retrieved context packet.
    const repairs=rows.filter(e=>e.tool==="record_agent_process_draft" && e.process_record?.repair_actions?.length);
    if(repairs.length) out.set("agent-process-memory-repair-mode",{...out.get(target)!,receipts:receipts.filter(r=>repairs.some(e=>e.tool_call_id===r.trace_id)),items:items.filter(i=>i.kind==="修复动作" || i.kind==="失败特征" || i.kind==="record_agent_process_draft")});
  }
  return out;
}
