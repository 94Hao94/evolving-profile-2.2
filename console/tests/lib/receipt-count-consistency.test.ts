import { describe, expect, it } from "vitest";
import { summarizeFlowLane } from "@/lib/flow-receipt";
import { projectToolBranches } from "@/lib/topology-mapping";
import { buildTopologySpec } from "@/lib/topology-spec";
import { nodeMetric } from "@/lib/topology-presentation";

describe("observed counts never convert unknown delivery to zero",()=>{
  it("keeps a lane's unknown delivery when the tool did return a body",()=>{
    const lane=summarizeFlowLane("user_memory",[{trace_id:"call",lane:"user_memory",stage:"user_recall",status:"candidate_returned",returned_count:3,delivered_count:null}]);
    expect(lane.counts.returned).toBe(3);
    expect(lane.counts.delivered).toBeNull();
    expect(lane.counts.candidates).toBeNull();
  });
  it("keeps branch, packet, context and execution delivery unknown consistently",()=>{
    const branches=projectToolBranches([{tool:"user_recall",tool_call_id:"one",session_id:"s",turn_id:"t",returned_count:3,candidate_count:4,delivery:{host_visibility:"unknown"}}],false);
    expect(branches.get("user-memory-recall")?.counts).toMatchObject({returned:3,delivered:null,candidates:4});
    const {nodes}=buildTopologySpec({english:false,summaries:new Map(),sourceScope:"prompt_bound",branchSummaries:branches});
    for(const id of ["user-memory-recall","user-root","user-packet","context","execution"]){
      const n=nodes.find(n=>n.id===id)!;
      expect(n.data.counts.delivered,id).toBeNull();
      expect(nodeMetric(id,n.data,false),id).toContain("送达 —");
    }
  });
  it("counts real zero separately from unknown and accepts explicit host receipt evidence",()=>{
    const branches=projectToolBranches([{tool:"user_recall",tool_call_id:"zero",returned_count:0,candidate_count:8,delivered_count:0},{tool:"user_preference",tool_call_id:"seen",returned_count:2,delivery:{host_visibility:"observed"}}],false);
    expect(branches.get("user-memory-recall")?.counts.delivered).toBe(0);
    expect(branches.get("user-memory-preference")?.counts.delivered).toBe(2);
    expect(branches.get("user-memory-preference")?.receipts[0].delivered_count).toBe(2);
  });
  it("does not let one known call certify a different unknown call",()=>{
    const lane=summarizeFlowLane("user_memory",[{trace_id:"a",lane:"user_memory",stage:"user_recall",status:"delivered",returned_count:2,delivered_count:2},{trace_id:"b",lane:"user_memory",stage:"user_recall",status:"candidate_returned",returned_count:1,delivered_count:null}]);
    expect(lane.counts.returned).toBe(3);
    expect(lane.counts.delivered).toBeNull();
  });
});
