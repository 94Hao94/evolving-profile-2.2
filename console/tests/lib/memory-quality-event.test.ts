import { describe, expect, it } from "vitest";
import { projectMemoryQualityEvents, summarizeMemoryQualityEvents } from "@/lib/memory-quality-event";
import type { FlowPrompt } from "@/lib/flow-projection";

const base: FlowPrompt = { prompt_id: "p-quality", at: "2026-10-03T00:00:00Z", user_prompt: "检查质量" };

it("records source navigation separately from body outcomes and fact-source totals",()=>{
 const events=projectMemoryQualityEvents({...base,memory_route_receipt:{tool_events:[{tool:"user_recall",returned_count:0,source_navigation_returned_count:2,source_navigation_returned_ids:["nav-a","nav-b"],delivery:{host_visibility:"unknown",answer_use:"not_measured"}}]}});
 expect(events[0]).toMatchObject({status:"source_navigation_returned",returned_count:0,delivered_count:null,verified_count:null,source_ids:[],source_navigation_returned_count:2,source_navigation_returned_ids:["nav-a","nav-b"]});
 expect(summarizeMemoryQualityEvents(events)).toMatchObject({uniqueSourceCount:0,sourceNavigationReturned:2,totals:{returned:0,delivered:0,verified:0}});
});

describe("memory quality event projection", () => {
  it("honors valid canonical plane/stage and explicit agent scenario purpose",()=>{
    const prompt:any={...base,memory_route_receipt:{tool_events:[{tool:"record_agent_process_draft",plane:"agent_process",stage:"extract",source_type:"mcp_tool_activity"},{tool:"read_scenario_summary",plane:"agent_process",stage:"readback",purpose:"agent_process",source_type:"mcp_tool_activity",returned_count:1}]}};
    expect(projectMemoryQualityEvents(prompt).map(event=>[event.plane,event.stage,event.route])).toEqual([["agent_process","extract","record_agent_process_draft"],["agent_process","readback","agent_scenario_summary"]]);
  });
  it("classifies every process mutation prefix using the canonical registry",()=>{
    const prompt:any={...base,memory_route_receipt:{tool_events:[{tool:"promote_agent_process_memory"},{tool:"manage_agent_process_rollout"},{tool:"write_agent_process_workspace"},{tool:"record_agent_process_draft"}]}};
    expect(projectMemoryQualityEvents(prompt).map(event=>[event.plane,event.stage])).toEqual([["agent_process","promote"],["agent_process","revalidate"],["agent_process","associate"],["agent_process","extract"]]);
  });
  it("uses the process stage registry for recording, context preparation and revalidation",()=>{
    const prompt:any={...base,memory_route_receipt:{tool_events:[{tool:"record_agent_trajectory",returned_count:1},{tool:"prepare_agent_process_context",returned_count:1},{tool:"revalidate_agent_process_memory",returned_count:1}]}};
    expect(projectMemoryQualityEvents(prompt).map(event=>[event.plane,event.stage])).toEqual([["agent_process","capture"],["agent_process","retrieve"],["agent_process","revalidate"]]);
  });
  it("does not claim no-call when the runtime source has incomplete coverage",()=>{
    const events=projectMemoryQualityEvents({...base,history_plan:{recommended_route:"recall",minimum_action:"recall_probe"},memory_route_receipt:{tool_events:[]}});
    expect(events[0]).toMatchObject({status:"not_measured",evidence:"not_observed"});
  });
  it("classifies agent and preference supplementary reads as readback",()=>{
    const prompt:any={...base,memory_route_receipt:{tool_events:[{tool:"read_agent_process_memory",returned_count:1,tool_call_id:"a",source_type:"codex_host_tool_result"},{tool:"read_preference_unit",returned_count:1,tool_call_id:"b"}]}};
    const events=projectMemoryQualityEvents(prompt);
    expect(events[0]).toMatchObject({plane:"agent_process",stage:"readback",readback_count:1,evidence:"host_retained_result"});
    expect(events[1]).toMatchObject({plane:"user_memory",dimension:"preferences",stage:"readback",readback_count:1});
  });
  it("deduplicates the same occurrence without collapsing repeated real calls",()=>{
    const prompt:any={...base,memory_route_receipt:{tool_events:[{tool:"user_recall",tool_call_id:"a",returned_count:2,memory_ids:["1","2"]},{tool:"user_recall",tool_call_id:"a",returned_count:2,memory_ids:["1","2"]},{tool:"user_recall",tool_call_id:"b",returned_count:1,memory_ids:["1"]}]}};
    const summary=summarizeMemoryQualityEvents(projectMemoryQualityEvents(prompt));
    expect(summary.eventCount).toBe(2);expect(summary.totals.returned).toBe(3);expect(summary.uniqueSourceCount).toBe(2);
  });
  it("counts actual JEV usage but does not charge a cached review again", () => {
    const prompt: any = { ...base, memory_route_receipt: { tool_events: [
      { tool: "user_recall", returned_count: 2, jev_review: { calls: 1, usage: { input_tokens: 42 } } },
      { tool: "user_recall", returned_count: 2, jev_review: { calls: 0, cache_hit: true, usage: { input_tokens: 42 } } },
    ] } };
    const summary = summarizeMemoryQualityEvents(projectMemoryQualityEvents(prompt));
    expect(summary.totals).toMatchObject({ llmCalls: 1, extraTokens: 42 });
  });
  it("projects a required route without a call as not_called, not zero", () => {
    const events = projectMemoryQualityEvents({ ...base, history_plan: { recommended_route: "recall", history_dependency: "likely", minimum_action: "recall_probe" }, memory_route_receipt:{call_coverage:"exact_host_turn_complete"} });
    expect(events).toHaveLength(1);
    expect(events[0].status).toBe("not_called");
    expect(events[0].candidate_count).toBeNull();
    expect(events[0].returned_count).toBeNull();
  });

  it("keeps a called empty result distinct from no call", () => {
    const events = projectMemoryQualityEvents({ ...base, memory_route_receipt: { tool_events: [{ tool: "recall", returned_count: 0, candidate_count: 8 }] } });
    expect(events[0]).toMatchObject({ route: "user_recall", status: "returned_zero", candidate_count: 8, returned_count: 0 });
  });

  it("keeps preference and history as separate parallel events", () => {
    const events = projectMemoryQualityEvents({ ...base, memory_route_receipt: { tool_events: [
      { tool: "get_preference", returned_count: 4, candidate_count: 6 },
      { tool: "recall", returned_count: 2, candidate_count: 9 },
    ] } });
    expect(events.map((event) => event.route)).toEqual(["user_preference", "user_recall"]);
    expect(summarizeMemoryQualityEvents(events).totals).toMatchObject({ candidates: 15, returned: 6 });
  });

  it("does not copy a parent aggregate into child nodes", () => {
    const events = projectMemoryQualityEvents({ ...base, memory_route_receipt: { tool_events: [{ tool: "research", returned_count: 3, candidate_count: 12 }] } });
    expect(events).toHaveLength(1);
    expect(events[0].candidate_count).toBe(12);
    expect(events[0].delivered_count).toBeNull();
  });

  it("uses no LLM calls in lightweight mode", () => {
    const events = projectMemoryQualityEvents({ ...base, memory_route_receipt: { tool_events: [{ tool: "read_source", returned_count: 1 }] } }, "lightweight");
    expect(events[0].sampling_mode).toBe("lightweight");
    expect(events[0].llm_calls).toBe(0);
    expect(events[0].extra_tokens).toBe(0);
  });
  it("projects scenario search, gate, and readback as scenario-plane tools", () => {
    const events = projectMemoryQualityEvents({ ...base, memory_route_receipt: { tool_events: [
      { tool: "search_scenario_summary", returned_count: 2 },
      { tool: "scenario_gate", returned_count: 1 },
      { tool: "read_scenario_summary", returned_count: 1 },
    ] } });
    expect(events.map((event) => event.route)).toEqual(["user_scenario_summary_search", "user_scenario_gate", "user_scenario_summary"]);
    expect(events.map((event) => event.plane)).toEqual(["scenario", "scenario", "scenario"]);
  });
});
