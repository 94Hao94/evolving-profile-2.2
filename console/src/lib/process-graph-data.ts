import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { epStatePath } from "./ep-state-paths";
import { needsProcessRevalidation, processRecordTitle } from "./process-presentation";
import { parseScenarioPage, ScenarioRequestError } from "./scenario-data";
import {bankScopeProjection,bankScopeReadable,configuredDataBankId,metadataReferencesAny} from "./bank-data-scope";
import {createNodeRevisionReader,referencedSourceIds,revisionAuthority,readOperatorRevisionAuthority} from "./node-revision";
export async function readProcessSnapshot(requestedBank:string|null=null) {
  const bankId=await configuredDataBankId();
  if(requestedBank && requestedBank!==bankId)throw new ScenarioRequestError(403,"bank_scope_mismatch");
  let text="",data:any={},status="ready";
  try {text=await readFile(epStatePath("process-memory/records.json"),"utf8");data=JSON.parse(text);}
  catch {status="unavailable";}
  if(!bankScopeReadable(bankScopeProjection(data,bankId,true)))throw new ScenarioRequestError(403,"process_snapshot_bank_scope_mismatch");
  const allRecords=Array.isArray(data.records) ? data.records as any[]:[];
  const excludedRecordIds=new Set<string>();
  for(const row of allRecords)if(!bankScopeReadable(bankScopeProjection(row,bankId)))excludedRecordIds.add(row.process_memory_id);
  let changed=true;
  while(changed){
    changed=false;
    for(const row of allRecords){
      if(excludedRecordIds.has(row.process_memory_id))continue;
      if(metadataReferencesAny(row,excludedRecordIds)){excludedRecordIds.add(row.process_memory_id);changed=true;}
    }
  }
  const records=allRecords.filter(row=>!excludedRecordIds.has(row.process_memory_id));
  const profiles=Object.fromEntries(Object.entries(data.profiles||{}).filter(([,profile])=>bankScopeReadable(bankScopeProjection(profile,bankId))&&!metadataReferencesAny(profile,excludedRecordIds)));
  return {data:{...data,records,profiles},records,bankId,excludedRecordIds,
    nodeAuthority:{operator:await readOperatorRevisionAuthority(),recordBook:revisionAuthority(data),bookScope:bankScopeProjection(data,bankId,true)},
    scopeFiltering:{excludedRecords:excludedRecordIds.size,undeclaredRecords:records.filter(row=>bankScopeProjection(row,bankId).status==="undeclared").length},
    version:text ? createHash("sha256").update(`${bankId}:${text}`).digest("hex"):"unavailable",status};
}

type Snapshot = Awaited<ReturnType<typeof readProcessSnapshot>>;
export function processRevisionReader(snapshot:Snapshot){
 return createNodeRevisionReader(snapshot.bankId,snapshot.nodeAuthority,snapshot.records.map(row=>({id:row.process_memory_id,value:row,references:referencedSourceIds(row)})));
}
export function processMetadata({ data, records, version, status,bankId,scopeFiltering }: Snapshot) {
  return { status, version,bankId:bankId||null,scopeFiltering, record_count: records.length, by_kind: records.reduce((acc: Record<string, number>, row: any) => { const kind = String(row?.kind || "unknown"); acc[kind] = (acc[kind] || 0) + 1; return acc; }, {}),
    skill_candidates: records.filter(row => row?.kind === "skill" && row?.status === "candidate").length,
    profiles: Object.keys(data.profiles || {}).length, revalidation_queue: records.filter(needsProcessRevalidation).length, updated_at: data.updated_at || null,
    recent: records.slice(-12).reverse().map(row => ({ id: row.process_memory_id, kind: row.kind, phase: row.phase, maturity: row.maturity, outcome: row.outcome,
      task_archetype: (row.task_archetype ?? []).slice(0, 8), dimensions: (row.process_dimensions ?? []).slice(0, 8), model_family: row.model_profile?.family || null,
      at: row.updated_at || row.created_at || null, source_count: Array.isArray(row.source_trace_ids) ? row.source_trace_ids.length : 0 })) };
}
export function processGraphPage(snapshot: Snapshot, params: URLSearchParams) {
  if (params.get("version") && params.get("version") !== snapshot.version) throw new ScenarioRequestError(409, "stale_version_refresh_required");
  const { offset, limit } = parseScenarioPage(params, 240);
  const kind = params.get("kind") || "all";
  if (!["all", "rollout", "trace", "event", "process_observation", "episode", "pattern", "skill", "capability_observation", "process_draft"].includes(kind)) throw new ScenarioRequestError(400, "invalid_process_kind");
  let records = snapshot.records.filter(row => kind === "all" || (kind === "rollout" ? needsProcessRevalidation(row) : row.kind === kind));
  if (kind === "all") {
    const derived = new Set(["episode","pattern","skill","capability_observation","process_draft"]);
    const preferred = records.filter(row=>derived.has(row.kind)||needsProcessRevalidation(row));
    const preferredIds = new Set(preferred.map(row=>row.process_memory_id));
    records = [...preferred,...records.filter(row=>!preferredIds.has(row.process_memory_id)).reverse()];
  }
  const selected = records.slice(offset, offset + limit);
  const nodeRevision=processRevisionReader(snapshot);
  const nodes = selected.map(row => ({ label_generated: row.kind === "capability_observation" && (!row.text || row.text === row.kind), id: `agent:${row.process_memory_id}`,nodeVersion:nodeRevision(row.process_memory_id), type: `agent_${row.kind}`,
    label: String(processRecordTitle(row)).slice(0,160),bankScope:bankScopeProjection(row,snapshot.bankId), phase: row.phase, maturity: row.maturity, status: row.drift_status || "stable", transfer_status: row.transfer_scope?.transfer_status || null, at: row.updated_at || row.created_at || null }));
  const ids = new Set(nodes.map(node => node.id));
  const edges: Array<{ source: string; target: string; type: string }> = [];
  let matchingEdges = 0;
  const add = (source: string, target: string, type: string) => { if (!ids.has(source) || !ids.has(target)) return; matchingEdges++; if (edges.length < 500) edges.push({ source, target, type }); };
  for (const row of selected) for (const id of row.derived_from ?? []) add(`agent:${id}`, `agent:${row.process_memory_id}`, "derived_from");
  const groups = new Map<string, any[]>();
  for (const row of selected) { const context = row.primary_context || {}; const key = context.session_id ? `session:${context.session_id}` : context.task_id ? `task:${context.task_id}` : ""; if (key) groups.set(key, [...(groups.get(key) || []),row]); }
  for (const group of groups.values()) { group.sort((a,b) => String(a.created_at || "").localeCompare(String(b.created_at || ""))); for (let i=1; i<group.length; i++) add(`agent:${group[i-1].process_memory_id}`,`agent:${group[i].process_memory_id}`,group[i].primary_context?.task_id ? "same_task_next_phase" : "same_session_next_event"); }
  return { version: snapshot.version, processMemory: processMetadata(snapshot), page: { offset, limit, total: records.length,
    nextOffset: offset + nodes.length < records.length ? offset + nodes.length : null, edgeCountInWindow: matchingEdges, edgesTruncated: matchingEdges > edges.length, scope: "induced_node_window_not_complete_graph" },
    graph: { nodes, edges, timeline: nodes.map(node => ({ id: node.id, type: node.type, at: node.at, label: node.label, status: node.maturity })).sort((a,b) => String(a.at || "").localeCompare(String(b.at || ""))) } };
}
