import { expect, it } from "vitest";
import { projectHostToolRows, projectHostTurnReplay } from "@/lib/host-tool-replay";
import { mergeToolReceipts } from "@/lib/tool-receipt";
import { navigation } from "../fixtures/source-navigation";

it("projects actual host navigation-only and mixed returns without answer adoption or body double counting",()=>{
 for(const memories of [[],[{id:"body",text:"actual returned body"}]]) {
  const row=structuredClone(rows[0]);row.payload.item.tool="user_recall";
  row.payload.item.result.content[0].text=JSON.stringify({memories,source_navigation:[navigation],source_navigation_returned_count:1,source_navigation_reference_count:191});
  const event=projectHostToolRows([row],{session_id:"s",turn_id:"t"})[0];
  expect(event).toMatchObject({returned_count:memories.length,delivered_count:memories.length,source_navigation_returned_count:1,source_navigation_returned_ids:[navigation.memory_id],delivery:{host_visibility:"observed",answer_use:"not_measured"}});
  expect(event.memory_ids).toEqual(memories.map(memory=>memory.id));expect(event.mapping_items).toHaveLength(memories.length);
 }
});
const rows = [{timestamp:"2026-10-06T15:35:00Z",type:"event_msg",payload:{type:"item_completed",thread_id:"s",turn_id:"t",item:{type:"McpToolCall",id:"call-a",server:"evolving_profile_controller",tool:"agent_recall",status:"completed",arguments:{query:"repair"},result:{content:[{type:"text",text:JSON.stringify({returned_count:2,total_count:13,records:[{process_memory_id:"p1",text:"real repair"},{process_memory_id:"p2",text:"other repair"}]})}]}}}}];
const meta={type:"session_meta",payload:{id:"s"}};
it("keeps running or wrong-turn-end host coverage partial and preserves unmatched canonical occurrences",()=>{
 const canonical=[{tool:"agent_research",tool_call_id:"pending",returned_count:null}];
 for(const terminal of [[],[{type:"event_msg",payload:{type:"task_complete",turn_id:"other"}}]]) {
  const out=projectHostTurnReplay([meta,...rows,...terminal],{session_id:"s",turn_id:"t"},canonical);
  expect(out.call_coverage).toBe("partial");expect(out.returned_count).toBeNull();expect(out.tool_events).toHaveLength(2);
 }
});
it("asserts completed host coverage only with matching source session and terminal turn",()=>{
 const complete={type:"event_msg",payload:{type:"task_complete",turn_id:"t"}};
 const out=projectHostTurnReplay([meta,...rows,complete],{session_id:"s",turn_id:"t"});
 expect(out.call_coverage).toBe("exact_host_turn_complete");expect(out.returned_count).toBe(2);
 expect(projectHostTurnReplay([{type:"session_meta",payload:{id:"foreign"}},...rows,complete],{session_id:"s",turn_id:"t"}).call_coverage).toBe("unknown");
});
it("restores actual Agent records on an exact turn without rewriting live receipt",()=>{
  const out=projectHostToolRows(rows,{session_id:"s",turn_id:"t"});
  expect(out[0]).toMatchObject({tool:"agent_recall",returned_count:2,candidate_count:13,delivered_count:2,source_type:"codex_host_tool_result",tool_call_id:"call-a"});
  expect(out[0].mapping_items[0].text).toBe("real repair");
  expect(projectHostToolRows(rows,{session_id:"s",turn_id:"wrong"})).toEqual([]);
});
it("unwraps a nested MCP content envelope for Agent Research counts",()=>{
 const row=structuredClone(rows[0]);
 row.payload.item.tool="agent_research";
 row.payload.item.result.content[0].text=JSON.stringify({
  content:[{type:"text",text:JSON.stringify({
   schema:"evolving-profile.agent-process-research.v1",
   status:"observed",returned_count:16,total_count:42,
   records:Array.from({length:16},(_,i)=>({process_memory_id:"pm-"+i,text:"经验 "+i}))
  })}]
 });
 const out=projectHostToolRows([row],{session_id:"s",turn_id:"t"});
 expect(out[0]).toMatchObject({tool:"agent_research",candidate_count:42,returned_count:16,delivered_count:16});
 expect(out[0].memory_ids).toHaveLength(16);
});
it("keeps measured canonical Agent Research when host replay is unmeasured",()=>{
 const canonical=[{tool:"agent_research",tool_call_id:"mcp:canonical",candidate_count:42,returned_count:16}];
 const host=[{tool:"agent_research",tool_call_id:"exec:host",candidate_count:null,returned_count:null}];
 expect(mergeToolReceipts(canonical,host,{hostTurnComplete:true})).toHaveLength(2);
});
it("preserves process validation evidence and draft dimensions for the visible details",()=>{
  const row=structuredClone(rows[0]);
  row.payload.item.tool="record_agent_process_draft";
  row.payload.item.result.content[0].text=JSON.stringify({record:{process_memory_id:"draft",kind:"process_draft",text:"repair",failure_signature:["last_element_omitted"],repair_actions:["iterate all"],verification_evidence:[{status:"passed",tests:10,scope:"current_python"}]}});
  const out=projectHostToolRows([row],{session_id:"s",turn_id:"t"});
  expect(out[0].verification_evidence).toEqual([{status:"passed",tests:10,scope:"current_python"}]);
  expect(out[0].process_record?.repair_actions).toEqual(["iterate all"]);
});
it("preserves a real preference unit and its actual count",()=>{
  const row=structuredClone(rows[0]);
  row.payload.item.tool="read_preference_unit";
  row.payload.item.result.content[0].text=JSON.stringify({returned_count:1,unit:{id:"u1",text:"Keep original memory body"}});
  const out=projectHostToolRows([row],{session_id:"s",turn_id:"t"});
  expect(out[0]).toMatchObject({returned_count:1,delivered_count:1,memory_ids:["u1"],mapping_items:[{id:"u1",text:"Keep original memory body"}]});
});
it("preserves raw scenario fallback and explicit purpose instead of claiming reviewed coverage",()=>{
 const row=structuredClone(rows[0]);row.payload.item.tool="read_scenario_summary";
 row.payload.item.result.content[0].text=JSON.stringify({tool_call_id:"mcp:canonical",purpose:"agent_process",returned_count:1,status:"available_unreviewed",summary_processing:{status:"raw_available_summary_pending",foreground_model_call:false},items:[{scenario_id:"s",summary:"raw text",summary_kind:"raw_message_excerpt"}]});
 expect(projectHostToolRows([row],{session_id:"s",turn_id:"t"})[0]).toMatchObject({tool_call_id:"mcp:canonical",host_call_id:"call-a",purpose:"agent_process",scenario_summary_status:"available_unreviewed",scenario_summary_kind:"raw_message_excerpt",summary_processing:{status:"raw_available_summary_pending"}});
});
it("keeps canonical occurrence and stage from MCP error data for host replay merge",()=>{
 const row:any=structuredClone(rows[0]);row.payload.item.tool="promote_agent_process_memory";row.payload.item.status="failed";row.payload.item.result=undefined;
 row.payload.item.error={data:{tool_call_id:"mcp:error",plane:"agent_process",stage:"promote",purpose:"agent_process"}};
 expect(projectHostToolRows([row],{session_id:"s",turn_id:"t"})[0]).toMatchObject({tool_call_id:"mcp:error",plane:"agent_process",stage:"promote",failed:true,returned_count:0});
});
