// Read-only projection of host-retained tool results. Never patches EP ledgers.
import { normalizeToolResult, mergeToolReceipts } from "./tool-receipt";
export function projectHostTurnReplay(rows:any[],binding:{session_id:string;turn_id:string},canonical:any[]=[]) {
  const sourceMatches=rows.find(row=>row.type==="session_meta")?.payload?.id===binding.session_id;
  let terminal=-1,latestStarted=-1;
  const pending=new Set<string>();
  for (const [index,row] of rows.entries()) {
    const payload=row.payload;
    if(row.type!=="event_msg" || payload?.turn_id!==binding.turn_id || payload.thread_id && payload.thread_id!==binding.session_id) continue;
    if (["task_complete","turn_end"].includes(payload.type)) terminal=index;
    if (["task_started","turn_started"].includes(payload.type)) latestStarted=index;
    if(payload.item?.type==="McpToolCall" && payload.item?.server==="evolving_profile_controller") {
      if(payload.type==="item_started") {pending.add(payload.item.id);latestStarted=index;}
      if(payload.type==="item_completed") pending.delete(payload.item.id);
    }
  }
  const complete=sourceMatches && terminal>=0 && terminal>latestStarted && pending.size===0;
  const host=projectHostToolRows(rows,binding);
  const events=mergeToolReceipts(canonical,host,{hostTurnComplete:complete});
  const call_coverage=complete ? "exact_host_turn_complete" as const : sourceMatches ? "partial" as const : "unknown" as const;
  const returned_count=complete && events.every(event=>typeof event.returned_count==="number") ? events.reduce((sum,event)=>sum+event.returned_count,0) : null;
  const navigationEvents=events.filter(event=>typeof event.source_navigation_returned_count==="number");
  return {tool_events:events,call_coverage,returned_count,...(navigationEvents.length ? {source_navigation_returned_count:navigationEvents.reduce((sum,event)=>sum+event.source_navigation_returned_count,0),source_navigation_returned_ids:[...new Set(navigationEvents.flatMap(event=>event.source_navigation_returned_ids || []))]} : {})};
}
export function projectHostToolRows(rows: any[], binding: {session_id:string;turn_id:string}) {
  const events: any[]=[]; const seen=new Set<string>();
  let lane="user";
  for (const row of rows) {
    const p=row.payload, item=p?.item;
    if(row.type!=="event_msg" || p?.type!=="item_completed" || p.turn_id!==binding.turn_id
      || p.thread_id!==binding.session_id || item?.type!=="McpToolCall" || item.server!=="evolving_profile_controller" || seen.has(item.id)) continue;
    seen.add(item.id);
    const tool=item.tool,args=item.arguments || {};
    let value:any=item.error?.data || item.result?.error?.data || {};
    for(const c of item.result?.content || []) if(c.type==="text") {
      try {
        value=JSON.parse(c.text);
        // Some Codex host replays preserve the MCP envelope inside the
        // tool-result text. Unwrap bounded nested content layers so the
        // actual EP schema (returned_count/records/total_count) survives
        // host projection. Never follow arbitrary objects or more than two
        // wrapper layers.
        for(let depth=0;depth<2 && value && Array.isArray(value.content);depth++) {
          const nested=value.content.find((block:any)=>block?.type==="text" && typeof block.text==="string");
          if(!nested) break;
          try { value=JSON.parse(nested.text); } catch {
            // Large host responses may be truncated after the outer MCP
            // envelope. Keep bounded count fields that precede the truncated
            // records instead of turning the call into an unknown receipt.
            const tail=nested.text;
            const schema=tail.match(/"schema"\s*:\s*"([^"]+)"/)?.[1];
            const returned=tail.match(/"returned_count"\s*:\s*(\d+)/)?.[1];
            const total=tail.match(/"total_count"\s*:\s*(\d+)/)?.[1];
            if(schema || returned || total) value={schema,status:"observed",
              ...(returned ? {returned_count:Number(returned)} : {}),
              ...(total ? {total_count:Number(total)} : {})};
            break;
          }
        }
        break;
      } catch {}
    }
    const failed=item.status==="failed" || Boolean(item.result?.isError || item.error || item.result?.error);
    const records=value.memories || value.records || value.items || value.results || value.sources || (value.record?[value.record]:[]);
    const list=Array.isArray(records)?records:[];
    const normalized=normalizeToolResult(tool,value,failed);
    const returned=normalized.returned_count;
    if(/^(agent_|read_agent)/.test(tool)) lane="agent";
    else if(/^(user_|read_source|find_sources|read_research)/.test(tool)) lane="user";
    const scenario=/scenario|context_summary/.test(tool);
    const mapping=normalized.mapping_items;
    events.push({tool,tool_call_id:value.tool_call_id || value.occurrence_id || item.id,host_call_id:item.id,at:row.timestamp,session_id:binding.session_id,turn_id:binding.turn_id,
      check_id:args.check_id || null,binding_state:"exact_host_turn",source_type:"codex_host_tool_result",
      query:args.query || null,workspace_id:value.workspace_id,research_id:value.research_id,
      candidate_count:typeof value.discovered_reference_count==="number"?value.discovered_reference_count
        :typeof value.candidate_count==="number"?value.candidate_count
      :/^(agent_recall|agent_research)$/.test(tool) && typeof value.total_count==="number"?value.total_count
      :/^(agent_recall|agent_research)$/.test(tool) && typeof value.returned_count==="number"?value.returned_count:null,
      returned_count:failed?0:returned,delivered_count:failed?0:returned,
      delivery:{host_visibility:failed?"failed":"observed",answer_use:"not_measured",evidence:"host_retained_tool_result"},
      memory_ids:normalized.memory_ids,
      ...(normalized.source_navigation ? {source_navigation:normalized.source_navigation,source_navigation_returned_count:normalized.source_navigation_returned_count,source_navigation_returned_ids:normalized.source_navigation_returned_ids} : {}),
      scenario_ids:normalized.scenario_ids,
      memory_plane:scenario?(value.memory_plane || value.purpose || args.memory_plane || args.purpose || lane):lane,
      mapping_items:mapping,failed,
      plane:value.plane,stage:value.stage,purpose:value.purpose || args.purpose || (scenario ? "unspecified" : lane),
      scenario_summary_status:scenario ? value.status || list[0]?.status : undefined,
      scenario_summary_kind:scenario ? list[0]?.summary_kind : undefined,
      summary_processing:value.summary_processing,
      verification_evidence:value.record?.verification_evidence || [],
      process_record:value.record ? {kind:value.record.kind,outcome:value.record.outcome,failure_signature:value.record.failure_signature,repair_actions:value.record.repair_actions,maturity:value.record.maturity} : undefined,
      result_status:value.status,
      scenario_summary_text:scenario?list.map((i:any)=>i.summary||"").filter(Boolean).join("\n").slice(0,8000):"",
      provenance_note:"宿主原始工具结果回放；进入上下文不等于回答采用或事实核验"});
  }
  return events;
}
