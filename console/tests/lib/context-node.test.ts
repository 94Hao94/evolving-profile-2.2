import { describe, expect, it } from "vitest";
import { describeManualSourceCoverage, projectSessionContextNode, summaryDisplayStatus, selectContextEpisode } from "@/lib/context-node";
import productionRows from "../fixtures/context-coverage-prod.json";

it("projects actual v2/v3 automatic source reviews without treating them as human or external fact verification",()=>{
 for(const row of productionRows) {
  const node=projectSessionContextNode(row as any);
  expect(node.automatedSourceCoverage).toMatchObject({status:"reviewed",reviewedSourceMessageCount:row.source_message_count,coverageProtocol:"per_user_source_to_state_and_summary.v2",hostValidationVersion:"source-coverage-primary-context.v3"});
  expect(node.manualSourceCoverage).toBeUndefined();
 }
 const stale=structuredClone(productionRows[0]);stale.source_revision="changed";
 expect(projectSessionContextNode(stale as any).automatedSourceCoverage?.status).toBe("unverified");
 for(const change of ["rejected","clipped","missing_host_witness","raw_fallback"]) {
  const row=structuredClone(productionRows[0]);
  if(change==="rejected") row.automated_source_coverage.accept=false;
  if(change==="clipped") row.automated_source_coverage.model_review_receipt.input_clipped=true;
  if(change==="missing_host_witness") row.automated_source_coverage.model_review_receipt.host_validation_version="";
  if(change==="raw_fallback") row.status="raw_available";
  expect(projectSessionContextNode(row as any).automatedSourceCoverage?.status).toBe("unverified");
 }
});
it("distinguishes abbreviated previews from real summary-budget truncation",()=>{
 expect(summaryDisplayStatus("Shortened report…",undefined)).toBe("preview_omitted");
 expect(summaryDisplayStatus("Shortened report…",{truncated:false})).toBe("preview_omitted");
 expect(summaryDisplayStatus("Complete rendered text",{truncated:true})).toBe("budget_truncated");
 expect(summaryDisplayStatus("Complete rendered text",{source_already_truncated:true})).toBe("budget_truncated");
 expect(summaryDisplayStatus("Complete rendered text",{truncated:false})).toBe("complete");
});
it("makes both actual 944 episode payloads selectable with all three summary tiers",()=>{
 const row=productionRows.find(row=>row.status==="episode_directory_ready")!;
 const node=projectSessionContextNode(row as any);
 expect(node.episodes).toHaveLength(2);
 for(const episode of node.episodes) {
  const selected=selectContextEpisode(node,episode.id);
  expect(selected.id).toBe(episode.id);
  expect(Object.keys(selected.summary)).toEqual(["compact","standard","full"]);
  expect(selected.summary.full).toBe(episode.summary.full);
  expect(selected.automatedSourceCoverage).toMatchObject({status:"reviewed",reviewedSourceMessageCount:episode.sourceMessageCount});
 }
 expect(selectContextEpisode(node,"unknown")).toBe(node);
});
it("projects each actual episode source file and offsets without reporting missing sources",()=>{
 const row=productionRows.find(row=>row.status==="episode_directory_ready")!;
 const node=projectSessionContextNode(row as any);
 expect(node.sourceIds).toEqual(row.source_ids);
 for(const actual of row.episodes) {
  const selected=selectContextEpisode(node,actual.episode_id);
  expect(selected.sourceIds).toEqual(actual.source_file_ids);
  expect(selected.sourceOffsets).toEqual(actual.source_offsets.map(offset=>({sourcePath:offset.source_path,byteOffset:offset.byte_offset,messageId:offset.message_id})));
 }
});
it("never inherits sibling or parent files into an episode and uses its own offset file identity",()=>{
 const row={context_id:"root",session_id:"session",source_ids:["/parent/a","/parent/b"],episodes:[
  {episode_id:"a",source_file_ids:["/a"],source_offsets:[{source_path:"/a",byte_offset:0,message_id:"a0"},{source_path:"/b",byte_offset:3,message_id:"b0"}]},
  {episode_id:"b",source_file_ids:["/b"],source_offsets:[{source_path:"/b",byte_offset:5,message_id:"b1"}]},
  {episode_id:"offset-only",source_offsets:[{source_path:"/offset",byte_offset:8,message_id:"o1"}]},
  {episode_id:"unlocated"},
 ]};
 const node=projectSessionContextNode(row);
 expect(selectContextEpisode(node,"a").sourceIds).toEqual(["/a"]);
 expect(selectContextEpisode(node,"a").sourceOffsets).toEqual([{sourcePath:"/a",byteOffset:0,messageId:"a0"}]);
 expect(selectContextEpisode(node,"b").sourceIds).toEqual(["/b"]);
 expect(selectContextEpisode(node,"offset-only").sourceIds).toEqual(["/offset"]);
 expect(selectContextEpisode(node,"unlocated").sourceIds).toEqual([]);
});

describe("projectSessionContextNode", () => {
  it("exposes reviewed message coverage and task-scope verdict", () => {
    const node = projectSessionContextNode({
      context_id: "session:thread-1",
      session_id: "thread-1",
      project_key: "project-1",
      status: "model_reviewed",
      review_scope: "conversation_only_not_external_fact_verification",
      raw_source_files: ["/rollouts/thread-1.jsonl"],
      source_message_count: 160,
      manual_source_coverage: {
        scope_verdict: "whole_session_scope_acceptable",
        reviewed_source_message_count: 160,
        episode_scope_verdict: "single_coherent_task",
      },
    });

    expect(node).toMatchObject({
      id: "session:thread-1",
      type: "session",
      sourceMessageCount: 160,
      manualSourceCoverage: {
        scopeVerdict: "whole_session_scope_acceptable",
        reviewedSourceMessageCount: 160,
        episodeScopeVerdict: "single_coherent_task",
      },
    });
  });
});

describe("describeManualSourceCoverage", () => {
  it("localizes coverage labels and message units without translating summary bodies",()=>{
    expect(describeManualSourceCoverage({scopeVerdict:"whole_session_scope_acceptable",reviewedSourceMessageCount:2,episodeScopeVerdict:"single_coherent_task"},3,"en")).toBe("Whole session reviewed · 2/3 messages · Single coherent task");
  });
  it("keeps explicit zero counts visible", () => {
    expect(describeManualSourceCoverage({
      scopeVerdict: "whole_session_scope_acceptable",
      reviewedSourceMessageCount: 0,
      episodeScopeVerdict: "single_coherent_task",
    }, 0)).toBe("全会话已复核 · 0/0 条消息 · 单一连贯任务");
  });

  it("does not invent a review state for legacy rows without coverage", () => {
    expect(describeManualSourceCoverage(undefined, 160)).toBeNull();
  });
});
