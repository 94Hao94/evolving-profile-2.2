import { describe, it, expect } from "vitest";
import { projectToolBranches } from "@/lib/topology-mapping";
import { navigation } from "../fixtures/source-navigation";
import { nodeMetric, summarizeReceiptScopes } from "@/lib/topology-presentation";

it("maps navigation-only locator returns to existing Recall details without counting them as body delivery",()=>{
 const branch=projectToolBranches([{tool:"user_recall",tool_call_id:"nav-call",returned_count:0,delivered_count:0,source_navigation:[navigation],source_navigation_returned_count:1}],true).get("user-memory-recall")!;
 expect(branch.counts).toMatchObject({returned:0,delivered:0});expect(branch.status).toBe("observed");
 expect(branch.items).toContainEqual({kind:"source_navigation",text:navigation.memory_id,source_navigation:navigation});
 expect(nodeMetric("user-memory-recall",branch as any,true)).toContain("Source navigation returned 1");
 expect(summarizeReceiptScopes([...branch.receipts,...branch.receipts])).toMatchObject({all:{returned:0,delivered:0,calls:1},sourceNavigationReturned:1});
});

describe("Prompt-bound topology mappings", () => {
  it("counts repeated actual scans but does not label cumulative delivery as unique",()=>{
    const m=projectToolBranches([{tool:"agent_recall",query:"repair",tool_call_id:"a",candidate_count:12,returned_count:2,memory_ids:["1","2"]},{tool:"agent_recall",query:"repair",tool_call_id:"b",candidate_count:12,returned_count:1,memory_ids:["1"]}],false);
    expect(m.get("agent-process-memory-process-retrieval")?.counts.candidates).toBe(24);
  });
  it("preserves a disabled RAG receipt instead of presenting a successful empty retrieval",()=>{
    const m=projectToolBranches([{tool:"rag_search",tool_call_id:"rag",returned_count:0,result_status:"disabled_by_runtime_settings"}],false);
    expect(m.get("rag-root")?.status).toBe("disabled");
  });
  it("keeps a real empty Agent call observed and preserves non-empty counts", () => {
    const m = projectToolBranches([
      {tool:"agent_recall",returned_count:0,candidate_count:0,tool_call_id:"a"},
      {tool:"agent_research",returned_count:6,candidate_count:22,tool_call_id:"b",delivered_count:6},
    ], false);
    expect(m.get("agent-process-memory-process-retrieval")?.status).toBe("observed");
    expect(m.get("agent-process-memory-agent-research")?.counts).toMatchObject({candidates:22,returned:6,delivered:6});
  });
  it("does not duplicate scenario calls across the user and agent lanes", () => {
    const m = projectToolBranches([
      {tool:"agent_research",returned_count:2,tool_call_id:"a"},
      {tool:"read_scenario_summary",returned_count:1,tool_call_id:"b"},
    ], false);
    expect(m.get("agent-process-memory-agent-scenario-summary")?.counts.returned).toBe(1);
    expect(m.get("user-memory-scenario-summary")).toBeUndefined();
  });
  it("does not count one paginated discovery total for every page", () => {
    const m = projectToolBranches([
      {tool:"user_research",research_id:"r",candidate_count:155,returned_count:6,tool_call_id:"a"},
      {tool:"read_research",research_id:"r",candidate_count:155,returned_count:8,tool_call_id:"b"},
    ], false);
    expect(m.get("user-memory-research")?.counts).toMatchObject({candidates:155,returned:14});
  });
  it("keeps process source readback separate from Agent Recall", () => {
    const m = projectToolBranches([
      {tool:"agent_recall",returned_count:6,candidate_count:12,tool_call_id:"a"},
      {tool:"read_agent_process_memory",returned_count:1,tool_call_id:"b"},
    ], false);
    expect(m.get("agent-process-memory-process-retrieval")?.counts.returned).toBe(6);
    expect(m.get("agent-process-memory-process-retrieval")?.items.some(i=>i.kind==="read_agent_process_memory")).toBe(true);
  });
});
