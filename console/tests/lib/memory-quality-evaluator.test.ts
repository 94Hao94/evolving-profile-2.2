import { expect, it } from "vitest";
import { evaluateMemoryQuality } from "@/lib/memory-quality-evaluator";
import { projectMemoryQualityEvents } from "@/lib/memory-quality-event";

it("keeps navigation-only returns unverified without reporting empty retrieval or successful answer use",()=>{
 const navigation=projectMemoryQualityEvents({prompt_id:"nav",at:"2026-10-09",user_prompt:"history",memory_route_receipt:{tool_events:[{tool:"user_recall",returned_count:0,source_navigation_returned_count:1,source_navigation_returned_ids:["nav"]}]}});
 const findings=evaluateMemoryQuality(navigation);
 expect(findings.map(finding=>finding.code)).toEqual(["unverified"]);
 expect(findings[0].message).toContain("source navigation");expect(findings[0].message).toContain("review");
 expect(navigation[0].verified_count).toBeNull();
});

it("finds missing calls and delivery gaps without an LLM", () => {
  const missing = projectMemoryQualityEvents({ prompt_id: "eval-p", at: "2026-10-03T00:00:00Z", user_prompt: "x", memory_route_receipt:{call_coverage:"exact_host_turn_complete"}, history_plan: { recommended_route: "recall", history_dependency: "likely", minimum_action: "recall_probe" } });
  const gap = projectMemoryQualityEvents({ prompt_id: "eval-q", at: "2026-10-03T00:00:00Z", user_prompt: "x", memory_route_receipt: { tool_events: [{ tool: "recall", returned_count: 2 }] } });
  expect(evaluateMemoryQuality([...missing, ...gap]).map((finding) => finding.code)).toEqual(["missing_call", "delivery_gap"]);
});
