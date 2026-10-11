import { expect,it } from "vitest";
import { mergeToolReceipts,normalizeToolResult } from "@/lib/tool-receipt";
import { projectToolBranches } from "@/lib/topology-mapping";
// Locator shape from the actual EP 5.1 history return (private audit sample).
import { navigation } from "../fixtures/source-navigation";
it("does not strip denial flags when projecting original source text",()=>{
 const denied={returned_count:1,memory:{id:"m"},source:{text:"blocked original",permission_status:"denied",hard_scope_match:false}};
 expect(normalizeToolResult("read_source",denied).mapping_items).toEqual([]);
 expect(normalizeToolResult("read_source",denied).returned_count).toBe(1);
 expect(normalizeToolResult("user_recall",{returned_count:1,permission_status:"denied",memories:[{id:"m",text:"blocked root"}]}).mapping_items).toEqual([]);
});
it("guards the whole envelope without changing its physical return count",()=>{
 for(const flags of [{scope_verification:{status:"mismatch"}},{scope_verification:"invalid"},{scope_verification:[]},{permission_status:"denied"}]){
  const out=normalizeToolResult("user_recall",{...flags,memories:[{id:"m",text:"blocked by envelope"}]});
  expect(out.mapping_items).toEqual([]);expect(out.returned_count).toBe(1);
 }
});
it("keeps raw return count but does not project bodies explicitly denied at capture",()=>{
 const body={returned_count:4,memories:[{id:"a",text:"blocked",permission_status:"denied"},{id:"b",text:"mismatch",scope_verification:{status:"mismatch"}},{id:"c",text:"hard false",hard_scope_match:false},{id:"d",text:"allowed"}]};
 const out=normalizeToolResult("user_recall",body);
 expect(out.returned_count).toBe(4);expect(out.memory_ids).toEqual(["d"]);
 expect(out.mapping_items.map(item=>item.text)).toEqual(["allowed"]);
});
it("keeps signed prepared snapshot expansion when host normalization has a bounded preview",()=>{
 const text="正文".repeat(10000),snapshot={ref:"a".repeat(64),check_id:"c",session_id:"s",turn_id:"t",tool_call_id:"call"};
 const canonical={tool:"user_recall",tool_call_id:"call",session_id:"s",turn_id:"t",returned_content_snapshot:snapshot,mapping_items:[{id:"m",text:text.slice(0,2000),text_truncated:true,item_index:0,total_chars:text.length,text_sha256:"hash"}]};
 const host={tool:"user_recall",tool_call_id:"call",session_id:"s",turn_id:"t",result_body:{memories:[{id:"m",text}]}};
 const branch=projectToolBranches(mergeToolReceipts([canonical],[host]),false).get("user-memory-recall")!;
 const item=branch.items.find(item=>item.returned_item?.id==="m")!.returned_item!;
 expect(item).toMatchObject({text_truncated:true,item_index:0,snapshot});
 expect(normalizeToolResult("user_recall",host.result_body).mapping_items[0]).toMatchObject({text_truncated:true,total_chars:text.length});
});
it("keeps actually returned bodyless source navigation separate from body and discovery counts",()=>{
 const out=normalizeToolResult("user_recall",{memories:[],source_navigation:[navigation],source_navigation_returned_count:1,source_navigation_reference_count:191});
 expect(out.returned_count).toBe(0);expect(out.memory_ids).toEqual([]);expect(out.mapping_items).toEqual([]);
 expect(out).toMatchObject({source_navigation_returned_count:1,source_navigation_returned_ids:[navigation.memory_id],source_navigation:[navigation]});
});
it("does not double count mixed returns or expose navigation text and denied locators",()=>{
 const out=normalizeToolResult("user_research",{memories:[{id:"body",text:"actual body"}],source_navigation:[{...navigation,text:"unrelated body",title:"private title"},{...navigation,memory_id:"denied",permission_status:"denied"}],source_navigation_returned_count:2});
 expect(out.returned_count).toBe(1);expect(out.memory_ids).toEqual(["body"]);expect(out.mapping_items).toEqual([{id:"body",text:"actual body",scenario_id:undefined}]);
 expect(out.source_navigation).toEqual([navigation]);expect(JSON.stringify(out)).not.toContain("unrelated body");
});
it("does not invent navigation delivery for a legacy result or discovery-only global count",()=>{
 expect(normalizeToolResult("user_recall",{memories:[],source_navigation_reference_count:191})).not.toHaveProperty("source_navigation_returned_count");
});
it("does not count unidentified legacy canonical entries again when exact host turn is complete",()=>{
 const canonical=[{tool:"user_recall",returned_count:1},{tool:"read_source",tool_call_id:"mcp:a",returned_count:1}];
 const host=[{tool:"user_recall",tool_call_id:"h",returned_count:1},{tool:"read_source",tool_call_id:"mcp:a",returned_count:1,source_type:"codex_host_tool_result"}];
 expect(mergeToolReceipts(canonical,host,{hostTurnComplete:true})).toHaveLength(2);
});
it("uses explicit zero, preserves unavailable unknown, and normalizes unit body without translating it",()=>{
 expect(normalizeToolResult("read_source",{status:"unavailable"}).returned_count).toBeNull();
 expect(normalizeToolResult("read_preference_unit",{returned_count:0,unit:{id:"a",text:"原文"}}).returned_count).toBe(0);
 expect(normalizeToolResult("read_preference_unit",{unit:{id:"a",text:"原文"}}).mapping_items[0].text).toBe("原文");
});
it("does not erase canonical classification when a richer host body omits metadata",()=>{
 const out=mergeToolReceipts([{tool:"record_agent_process_draft",tool_call_id:"a",plane:"agent_process",stage:"extract",returned_count:1}],[{tool:"record_agent_process_draft",tool_call_id:"a",plane:undefined,stage:undefined,returned_count:2}]);
 expect(out[0]).toMatchObject({plane:"agent_process",stage:"extract",returned_count:2});
});
