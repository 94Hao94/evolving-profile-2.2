import { describe, expect, it } from "vitest";
import { projectFlowAudit, type FlowPrompt } from "@/lib/flow-projection";

it("keeps navigation-only receipts observed with zero bodies without calling the result empty",()=>{
 const audit=projectFlowAudit({prompt_id:"nav",at:"2026-10-09",user_prompt:"history",memory_route_receipt:{tool_events:[{tool:"user_recall",returned_count:0,source_navigation_returned_count:2,source_navigation_returned_ids:["nav-a","nav-b"]}]}} as any);
 expect(audit.hostEvidence.result).toBe("source_navigation_returned");
 expect(audit.history.state).toBe("observed");expect(audit.history.metrics.returned).toBe(0);
 expect(audit.history.routeAudit).toMatchObject({status:"ep_history_source_navigation_returned",delivery_state:"source_navigation_returned",source_navigation_returned_count:2});
});

it("actual bound Recall calls override the earlier not-needed routing decision", () => {
  const audit = projectFlowAudit({
    prompt_id: "p", at: "2026-09-26T12:32:49Z", user_prompt: "test",
    history_decision: "not_needed", routes: { historical_memory: "not_observed" },
    time_window_activity: {
      boundary: "same_prompt_binding_only", state: "observed",
      events: [{ tool: "recall", at: "2026-09-26T12:33:46Z", returned_count: 0, candidate_count: 22 }],
    },
  });
  expect(audit.history.value).toBe("recall");
  expect(audit.history.calls).toBe(1);
  expect(audit.history.metrics.returned).toBe(0);
  expect(audit.history.decision).toBe("needed");
});
it("uses the same occurrence inventory as the quality and topology projections",()=>{
 const prompt:any={prompt_id:"p",at:"2026-10-08",user_prompt:"x",memory_route_receipt:{tool_events:[{tool:"user_recall",tool_call_id:"a",returned_count:2},{tool:"user_recall",tool_call_id:"a",returned_count:2}]}};
 const audit=projectFlowAudit(prompt);
 expect(audit.history.calls).toBe(1);expect(audit.history.metrics.returned).toBe(2);
});
it("does not turn an observed call with unknown result count into an empty result",()=>{
 const prompt:any={prompt_id:"p",at:"2026-10-08",user_prompt:"x",memory_route_receipt:{call_coverage:"partial",tool_events:[{tool:"user_recall",tool_call_id:"running",returned_count:null}]}};
 const audit=projectFlowAudit(prompt);
 expect(audit.history.metrics.returned).toBeNull();expect(audit.history.state).toBe("executed_no_result");expect(audit.history.routeAudit.returned_count).toBeNull();
});

const prompt: FlowPrompt = {
  prompt_id: "prompt-1",
  at: "2026-09-17T00:11:08.000Z",
  user_prompt: "请继续检查记忆检索链路。",
  source: "codex-userpromptsubmit",
  guidance_receipt: {
    host_state: "host_tool_response_observed",
    coverage: "complete_active_set",
    deferred_count: 3,
    guidance_items: [{ id: "preference-1", text: "先看当前任务，再引用偏好。" }],
    model_sections: [{ id: "model-1", title: "证据优先" }],
  },
  historical_audit: {
    route: "agent_mcp_recall",
    state: "observed",
    mode: "candidate_discovery",
    controller_state: "not_in_candidate_path",
    candidate_count: 12,
    returned_to_host_count: 3,
    unread_candidate_count: 9,
    delivery_state: "not_measured",
    items: [{ id: "memory-1", type: "experience", text: "历史事实条目", score: 0.91 }],
  },
};

describe("projectFlowAudit", () => {
  it("marks required EP history as incomplete when the agent only received a route hint", () => {
    const audit = projectFlowAudit({
      ...prompt,
      history_plan: { recommended_route: "recall", history_dependency: "likely", minimum_action: "recall_probe" },
      memory_route_receipt: { recommended_route: "recall", tool_events: [] },
      historical_audit: null,
    });
    expect(audit.history.routeAudit.status).toBe("ep_history_verification_incomplete");
    expect(audit.history.routeAudit.tool_called).toBe(false);
    expect(audit.history.routeAudit.unresolved).toHaveLength(1);
  });

  it("marks an actual bound EP tool call separately from an empty result", () => {
    const audit = projectFlowAudit({
      ...prompt,
      history_plan: { recommended_route: "research", history_dependency: "likely", minimum_action: "agent_query" },
      memory_route_receipt: { recommended_route: "research", tool_events: [{ tool: "research", returned_count: 0 }] },
      historical_audit: null,
    });
    expect(audit.history.routeAudit.status).toBe("ep_history_tool_called_empty");
    expect(audit.history.routeAudit.tool_called).toBe(true);
  });
  it("treats scenario navigation and readback as real bound MCP tool events", () => {
    const audit = projectFlowAudit({
      ...prompt,
      memory_route_receipt: { tool_events: [
        { tool: "search_scenario_summary", returned_count: 2, scenario_ids: ["session-1", "session-2"] },
        { tool: "read_scenario_summary", returned_count: 1, scenario_ids: ["session-1"] },
      ] },
      historical_audit: null,
    });
    expect(audit.hostEvidence.history).toBe("observed");
    expect(audit.history.routeAudit.tool_called).toBe(true);
    expect(audit.history.value).toContain("search_scenario_summary");
    expect(audit.history.value).toContain("read_scenario_summary");
  });
  it("does not infer current-host MCP availability from an entry receipt or system probe", () => {
    const audit = projectFlowAudit({
      ...prompt,
      instruction_receipt: { instruction_version: "v1", content_sha256: "hash", core_text: "manual", source_file: "/manual", stage: "hook_context_prepared", model_context_visibility: "not_measured" },
      system_probe: { actor: "system_probe", state: "returned", calls: 1, returned_count: 2 },
      memory_route_receipt: { decision: "recall", tool_events: [] },
    });
    expect(audit.hostEvidence.entry).toBe("prepared");
    expect(audit.hostEvidence.mcp).toBe("unknown");
    expect(audit.hostEvidence.history).toBe("not_observed");
    expect(audit.hostEvidence.result).toBe("unknown");
  });

  it("distinguishes a preference result from an empty prompt-bound Recall result", () => {
    const preference = projectFlowAudit({ ...prompt, memory_route_receipt: {
      tool_events: [{ tool: "get_preference", returned_count: 6, check_id: "current" }],
    } });
    expect(preference.hostEvidence.mcp).toBe("observed");
    expect(preference.hostEvidence.history).toBe("not_observed");
    expect(preference.hostEvidence.result).toBe("returned");

    const recall = projectFlowAudit({ ...prompt, memory_route_receipt: {
      tool_events: [{ tool: "recall", returned_count: 0, check_id: "current" }],
    } });
    expect(recall.hostEvidence.mcp).toBe("observed");
    expect(recall.hostEvidence.history).toBe("observed");
    expect(recall.hostEvidence.result).toBe("returned_empty");
  });

  it("keeps bounded system discovery separate from agent tool activity", () => {
    const probe = { actor: "system_probe", state: "returned", calls: 1, returned_count: 3, context_tokens: 420, max_tokens: 500 };
    const audit = projectFlowAudit({ ...prompt, system_probe: probe });
    expect(audit.systemProbe).toEqual(probe);
    expect(audit.timeWindowActivity).toBeNull();
  });
  it("keeps the recorded manual separate from preference coverage without fabricating old delivery", () => {
    expect(projectFlowAudit(prompt).entry.instruction).toBeNull();
    const instruction = { instruction_version: 'v2', content_sha256: 'hash', core_text: '查历史前先明确对象', source_file: '/manual.py', stage: 'hook_context_prepared', model_context_visibility: 'not_measured' };
    const result = projectFlowAudit({ ...prompt, instruction_receipt: instruction });
    expect(result.entry.instruction).toEqual(instruction);
    expect(result.entry.coverage).toBe('complete_active_set');
    expect(result.entry.instruction?.model_context_visibility).toBe('not_measured');
  });
  it("keeps guidance and historical evidence as separate parallel branches", () => {
    const audit = projectFlowAudit(prompt);

    expect(audit.entry.value).toBe("host_tool_response_observed");
    expect(audit.guidance.count).toBe(2);
    expect(audit.guidance.deferred).toBe(3);
    expect(audit.guidance.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "preference-1" }),
      expect.objectContaining({ id: "model-1" }),
    ]));
    expect(audit.history.value).toBe("agent_mcp_recall");
    expect(audit.history.mode).toBe("candidate_discovery");
    expect(audit.history.controller).toBe("not_in_candidate_path");
    expect(audit.history.metrics).toEqual({ candidates: 12, returned: 3, unread: 9, rejected: null });
    expect(audit.history.items).toEqual([expect.objectContaining({ id: "memory-1" })]);
  });

  it("does not turn an unmeasured host delivery into zero delivered items", () => {
    const audit = projectFlowAudit(prompt);

    expect(audit.history.delivery).toBe("not_measured");
    expect(audit.history.delivered).toBeNull();
  });

  it("keeps task state, stable profile and evidence sufficiency distinct", () => {
    const audit = projectFlowAudit({
      ...prompt,
      task_state: { current_objective: "检查备份", continuation: false, source: "current_prompt" },
      evidence_decision: { need: "history", unresolved_slots: ["最终状态"], chosen_route: "recall", sufficiency: "insufficient" },
      guidance_receipt: { ...prompt.guidance_receipt, stable_profile_count: 1, preference_candidate_count: 1,
        stable_profile: [{ id: "stable-1", text: "当前要求优先" }] },
    });
    expect(audit.entry.taskState?.current_objective).toBe("检查备份");
    expect(audit.guidance.stableProfileCount).toBe(1);
    expect(audit.guidance.candidateCount).toBe(1);
    expect(audit.history.evidenceDecision?.sufficiency).toBe("insufficient");
  });

  it("projects the prompt-bound L0 map separately from L1 and L2 reads", () => {
    const audit = projectFlowAudit({
      ...prompt,
      navigation_map: {
        schema: "evolving-profile.entry-navigation.v2",
        context_chars: 2025,
        preferences: { status: "available", approved_count: 119, preview_count: 5, dimensions: [{ id: "reasoning", label: "分析与决策", count: 15, scopes: [{ scope: "决策支持" }] }] },
        bank: { status: "available", entity_count: 500, searchable_entity_count: 24839, manifest_count: 7, topics: [{ topic_id: "manifest:backup", title: "备份与恢复", navigation_summary: "备份和恢复记录" }] },
      },
    });
    expect(audit.map.l0.contextChars).toBe(2025);
    expect(audit.map.l0.topicCount).toBe(7);
    expect(audit.map.l1.searchableEntities).toBe(24839);
    expect(audit.map.l2.actualRoute).toBe("agent_mcp_recall");
    expect(audit.map.l2.returned).toBe(3);
  });

  it("preserves prompt-bound catalog hints for the actual chain", () => {
    const audit = projectFlowAudit({
      ...prompt,
      memory_route_receipt: {
        decision: "recall",
        recommended_route: "recall",
        reason: "目录发现相关历史主题。",
        confidence: 0.78,
        catalog_probe: { status: "observed", candidate_count: 3, matched_entities: ["小黛"] },
        catalog_hints: [{ memory_id: "memory-1", type: "experience", topic: "小黛回复链路" }],
      },
    });

    expect(audit.history.routeReceipt?.catalog_hints).toEqual([
      expect.objectContaining({ memory_id: "memory-1", topic: "小黛回复链路" }),
    ]);
  });

  it.each([
    ["unknown", "unknown"],
    ["not_observed", "not_observed"],
    ["executed_empty", "executed_empty"],
    ["executed_no_result", "executed_no_result"],
  ])("preserves historical evidence state %s without collapsing it", (state, expected) => {
    const audit = projectFlowAudit({
      ...prompt,
      historical_audit: null,
      routes: { historical_memory: state },
    });
    expect(audit.history.value).toBe(expected);
    expect(audit.history.items).toEqual([]);
  });

  it("keeps nearby time-window activity separate from prompt attribution", () => {
    const audit = projectFlowAudit({
      ...prompt,
      time_window_activity: {
        state: "observed",
        window_minutes: 10,
        event_count: 2,
        by_tool: { recall: { calls: 1, returned: 3 } },
        unattributed_activity: { state: "observed", event_count: 2, by_tool: { research: { calls: 2, returned: 6 } }, boundary: "candidate_details_omitted" },
        boundary: "same_prompt_binding_only",
      },
    });
    expect(audit.timeWindowActivity?.event_count).toBe(2);
    expect(audit.timeWindowActivity?.boundary).toBe("same_prompt_binding_only");
    expect(audit.timeWindowActivity?.unattributed_activity?.event_count).toBe(2);
    expect(audit.timeWindowActivity?.items).toBeUndefined();
  });
});
