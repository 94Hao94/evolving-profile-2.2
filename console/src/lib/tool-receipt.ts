/** Shared read-only receipt normalization for canonical activity and host replay. */
export type ReceiptItem = { id?: string; text: string; scenario_id?: string; text_truncated?:boolean;total_chars?:number;source_role?:string };
function bodyAllowed(row:any):boolean{
  if(!row||typeof row!=="object")return false;
  const scope=row.scope_verification;
  return !(["denied","blocked"].includes(row.permission_status)||row.hard_scope_match===false||["denied","mismatch"].includes(row.scope_status)
    || scope!=null&&(typeof scope!=="object"||Array.isArray(scope)||["denied","mismatch"].includes(scope.status)));
}
export type SourceNavigation = {
  memory_id: string; document_id?: string; chunk_id?: string; source_revision?: string;
  subject_relation?: string; claim_verification?: string; authority?: string;
  next_action?: { tool: string; arguments: { memory_id: string; scope?: string } };
};
/** Only locators carried by this return; discovery totals never establish delivery. */
export function normalizeSourceNavigation(value: unknown): SourceNavigation[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.flatMap(row => {
    if (!row || typeof row.memory_id !== "string" || !row.memory_id || seen.has(row.memory_id)
      || ["denied","blocked"].includes(row.permission_status) || ["denied","mismatch"].includes(row.scope_status || row.scope_verification?.status)
      || row.hard_scope_match === false || ["invalidated","withdrawn"].includes(row.state)) return [];
    seen.add(row.memory_id);
    const locator: SourceNavigation = { memory_id: row.memory_id };
    for (const key of ["document_id","chunk_id","source_revision","subject_relation","claim_verification","authority"] as const)
      if (typeof row[key] === "string") locator[key] = row[key];
    if (row.next_action?.tool === "read_source" && row.next_action.arguments?.memory_id === row.memory_id)
      locator.next_action = {tool:"read_source",arguments:{memory_id:row.memory_id,...(typeof row.next_action.arguments.scope === "string" ? {scope:row.next_action.arguments.scope} : {})}};
    return [locator];
  });
}
export function normalizeToolResult(tool: string, value: any, failed = false) {
  const body = value && typeof value === "object" ? value : {};
  const guidance = body.guidance_view || body;
  const arrays = [body.memories, body.records, body.items, body.results, body.sources].find(Array.isArray) || [];
  let records = [...arrays];
  if (body.record) records.push(body.record);
  if (body.unit) records.push(body.unit);
  if (/preference/.test(tool)) records.push(...["included", "stable_profile", "guidance_items", "model_sections", "entries"].flatMap(key => Array.isArray(guidance[key]) ? guidance[key] : []));
  if (body.source?.text) records.push({ ...body.source,id: body.memory?.id || body.source?.id, text: body.source.text });
  const rawRecordCount=records.length;
  if(!bodyAllowed(body))records=[];
  const seen = new Set<string>();
  const items: ReceiptItem[] = records.flatMap((record: any) => {
    if(!bodyAllowed(record))return [];
    const id = record.id || record.process_memory_id || record.section_id || record.scenario_id;
    const text = String(record.text || record.text_preview || record.content || record.title || record.summary || "");
    const key = id || text;
    if (!key || seen.has(key)) return [];
    seen.add(key);
    return [{ id, text: text.slice(0, 8000), scenario_id: record.scenario_id,...(text.length>8000?{text_truncated:true,total_chars:Array.from(text).length}:{} ) }];
  });
  const count = typeof body.returned_count === "number" && Number.isFinite(body.returned_count) && body.returned_count >= 0 ? body.returned_count
    : rawRecordCount || (Array.isArray(arrays) && ["memories", "records", "items", "results", "sources"].some(key => Array.isArray(body[key])) ? 0 : null);
  const navigation = failed ? [] : normalizeSourceNavigation(body.source_navigation);
  return { returned_count: failed ? 0 : count, mapping_items: items, memory_ids: items.map(item => item.id).filter((id): id is string => Boolean(id)), scenario_ids: items.map(item => item.scenario_id).filter((id): id is string => Boolean(id)),
    ...(Array.isArray(body.source_navigation) ? {source_navigation:navigation,source_navigation_returned_count:navigation.length,source_navigation_returned_ids:navigation.map(row=>row.memory_id)} : {}) };
}

/** Prefer richer host evidence for the same real call; never merge by tool/time alone. */
export function mergeToolReceipts(canonical: any[], host: any[] = [], options: {hostTurnComplete?: boolean} = {}) {
  const events: any[] = []; const indices = new Map<string, number>();
  const identity=(event:any)=>event.tool_call_id || event.call_id || event.occurrence_id;
  const knownHost=new Set(host.map(identity).filter(Boolean));
  const primaryCanonical=options.hostTurnComplete && host.length ? canonical.filter(event =>
    knownHost.has(identity(event)) ||
    // A complete host replay can still contain a truncated nested MCP
    // envelope. Preserve the measured prompt-bound EP receipt when the host
    // occurrence has no parseable counts; null must not erase real counts.
    host.some(candidate => candidate.tool===event.tool &&
      candidate.candidate_count==null && candidate.returned_count==null &&
      (event.candidate_count!=null || event.returned_count!=null))
  ) : canonical;
  for (const event of [...primaryCanonical, ...host]) {
    const id = event.tool_call_id || event.call_id || event.occurrence_id;
    const key = id ? `${event.session_id || ""}:${event.turn_id || ""}:${id}` : null;
    const projected = event.result_body ? { ...event, ...normalizeToolResult(event.tool, event.result_body, Boolean(event.failed || event.error_type)) } : event;
    if(event.returned_content_snapshot&&Array.isArray(event.mapping_items))projected.mapping_items=event.mapping_items;
    const normalized=Object.fromEntries(Object.entries(projected).filter(([,value])=>value!==undefined));
    if (key && indices.has(key)) {
      const previous=events[indices.get(key)!];
      const merged={...previous,...normalized};
      if(previous.returned_content_snapshot&&Array.isArray(previous.mapping_items)){
        // Retain the signed prepared snapshot as its own audit evidence, never
        // assign its index to a different host item by array order.
        const hostItems=Array.isArray(normalized.mapping_items)?normalized.mapping_items:[];
        const extras=hostItems.filter((item:any)=>!previous.mapping_items.some((prior:any)=>prior.id===item.id&&(item.text===prior.text||item.text?.startsWith(prior.text))));
        merged.mapping_items=[...previous.mapping_items,...extras.map((item:any)=>({...item,source_role:"host_retained_return",snapshot:null}))];
      }
      events[indices.get(key)!]=merged;
    }
    else { if (key) indices.set(key, events.length); events.push(normalized); }
  }
  return events;
}
