import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { epStatePath } from "./ep-state-paths";
import { isScenarioBankScope, summarizeAssociationCoverage, summarizeScenarioStatus } from "./scenario-status";
import { projectSessionContextNode } from "./context-node";
import { dataplaneBankUrl, getDataplaneHeaders } from "./evolving-client";
import {bankScopeProjection,bankScopeReadable,metadataReferencesAny} from "./bank-data-scope";
import {createNodeRevisionReader,referencedSourceIds,revisionAuthority} from "./node-revision";

type Row = Record<string, any>;
export class ScenarioRequestError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
async function readJson(relative: string, fallback: Row) {
  try {
    const text = await readFile(epStatePath(relative), "utf8");
    return { value: JSON.parse(text) as Row, digest: createHash("sha256").update(text).digest("hex") };
  } catch { return { value: fallback, digest: "unavailable" }; }
}
export async function readScenarioSnapshot(requestedBank: string | null) {
  const profile = await readJson("codex.json", {});
  const bankId = String(process.env.EVOLVING_PROFILE_BANK_ID || profile.value.bankId || "");
  if (!bankId || !isScenarioBankScope(requestedBank, bankId)) throw new ScenarioRequestError(403, "bank_scope_mismatch");
  const [index, associations] = await Promise.all([
    readJson("context/context-index.json", { status: "unavailable", sessions: [], projects: [] }),
    readJson("context/context-bank-associations.json", { links: [] }),
  ]);
  for (const source of [index.value,associations.value]) {
    if(!bankScopeReadable(bankScopeProjection(source,bankId,true)))throw new ScenarioRequestError(403,"bank_snapshot_scope_mismatch");
  }
  const excludedNodeIds = new Set<string>();
  const excludedSessions = new Set<string>();
  const sessionRows = rows(index.value.sessions);
  const initialSessions = sessionRows.filter(row=>{
    const readable=bankScopeReadable(bankScopeProjection(row,bankId));
    if(!readable){excludedNodeIds.add(row.context_id);excludedSessions.add(row.session_id);}
    return readable;
  });
  let referencesChanged=true;
  while(referencesChanged){
    referencesChanged=false;
    const excludedRefs=new Set([...excludedNodeIds,...excludedSessions]);
    for(const row of initialSessions){
      if(excludedNodeIds.has(row.context_id))continue;
      if(metadataReferencesAny(row,excludedRefs)){excludedNodeIds.add(row.context_id);excludedSessions.add(row.session_id);referencesChanged=true;}
    }
  }
  const sessions=initialSessions.filter(row=>!excludedNodeIds.has(row.context_id));
  const projects = rows(index.value.projects).filter(row=>{
    const readable=bankScopeReadable(bankScopeProjection(row,bankId)) && !metadataReferencesAny(row,new Set([...excludedNodeIds,...excludedSessions]));
    if(!readable)excludedNodeIds.add(row.context_id);
    return readable;
  });
  const allLinks = index.digest==="unavailable" ? [] : rows(associations.value.links);
  const links = allLinks.filter(row=>{
    const readable=bankScopeReadable(bankScopeProjection(row,bankId)) && !metadataReferencesAny(row,new Set([...excludedNodeIds,...excludedSessions]));
    if(!readable)excludedNodeIds.add(`bank:${row.record_id}`);
    return readable;
  });
  const scopedIndex:Row={...index.value,sessions,projects};
  const scopedAssociations:Row={...associations.value,links};
  // A version binds the exact index, association snapshot and configured Bank.
  // Node IDs are looked up in these records, never interpreted as file paths.
  return { index: scopedIndex, associations: scopedAssociations, bankId,excludedNodeIds,
    nodeAuthority:{operator:revisionAuthority(profile.value),index:revisionAuthority(index.value),associations:revisionAuthority(associations.value),indexScope:bankScopeProjection(index.value,bankId,true),associationScope:bankScopeProjection(associations.value,bankId,true)},
    scopeFiltering:{excludedNodes:excludedNodeIds.size,excludedAssociations:allLinks.length-links.length,undeclaredNodes:[...sessions,...projects,...links].filter(row=>bankScopeProjection(row,bankId).status==="undeclared").length},
    version: createHash("sha256").update(`${bankId}:${index.digest}:${associations.digest}`).digest("hex") };
}
export type ScenarioSnapshot = Awaited<ReturnType<typeof readScenarioSnapshot>>;
const rows = (value: any): Row[] => Array.isArray(value) ? value : [];
const label = (value: any) => String(value ?? "").slice(0, 160);
export function scenarioRevisionReader(snapshot:ScenarioSnapshot){
  const records=[...rows(snapshot.index.projects),...rows(snapshot.index.sessions)].map(row=>({id:row.context_id,value:row,references:[...referencedSourceIds(row),...rows(row.session_ids).map(id=>`session:${id}`)]}));
  records.push(...rows(snapshot.associations.links).map(row=>({id:`bank:${row.record_id}`,value:row,references:[...referencedSourceIds(row),...rows(row.session_ids).map(id=>`session:${id}`)]})));
  for(const row of rows(snapshot.index.sessions))for(const episode of rows(row.episodes)){
    const {episodes:_episodes,summary:_summary,summary_budget:_budget,previous_reviewed_summary:_previous,...parentProvenance}=row;
    records.push({id:`${row.context_id}/${episode.episode_id}`,value:{episode,parentProvenance},references:referencedSourceIds(episode)});
  }
  return createNodeRevisionReader(snapshot.bankId,snapshot.nodeAuthority,records);
}
function checkScenarioNodeVersion(snapshot:ScenarioSnapshot,params:URLSearchParams,nodeVersion:string){
  if(params.has("nodeVersion")){
    if(params.get("nodeVersion")!==nodeVersion)throw new ScenarioRequestError(409,"stale_node_revision_refresh_required");
  }else checkScenarioVersion(snapshot,params);
}
export function scenarioMetadata(snapshot: ScenarioSnapshot, liveTotal: number | null = null) {
  const { index, associations } = snapshot;
  const projects = rows(index.projects), sessions = rows(index.sessions), links = rows(associations.links);
  const verifiedProjects = projects.filter(row => row.identity_status === "verified_project").length;
  const edges = snapshotRelations(snapshot,new Set(allMetadata(snapshot).map(node=>node.id))).length;
  const scopedScanned=snapshot.scopeFiltering.excludedAssociations ? null:Number(associations.scanned_records??0);
  return { status: index.status ?? "ready", schema: index.schema ?? "evolving-profile.context-index.v1", version: snapshot.version,
    sessionCount: sessions.length, projectCount: projects.length, workspaceCount:projects.length-verifiedProjects,verifiedProjectCount:verifiedProjects, ...summarizeScenarioStatus(index),
    sourceOfTruth: index.source_of_truth ?? "codex_rollout_or_ep_session_index", evidenceRole: "context_navigation_only",
    updatedAt: index.updated_at ?? null, scopeFiltering:snapshot.scopeFiltering,graphStats: { nodes: projects.length + sessions.length + links.length, edges,
      timeline: projects.length + sessions.length + links.length, sessions: sessions.length, workspaces: projects.length - verifiedProjects,
      verifiedProjects, bankRecords: links.length, unit: "indexed_nodes_and_snapshot_relations" },
    bankRecordLinks: { available: links.length > 0, linked: links.length, reportedLinked: Number(associations.linked_records ?? links.length), sampled: 0,
      scanned: scopedScanned,reportedScanned:Number(associations.scanned_records??0),indexedAt: associations.indexed_at ?? null,
      sourceIndexUpdatedAt: associations.source_index_updated_at ?? null,
      ...(scopedScanned==null ? {liveTotal,unscanned:null,coverage:"unknown"}:summarizeAssociationCoverage(scopedScanned, liveTotal)), snapshotOnly: true,
      reason:snapshot.scopeFiltering.excludedAssociations ? "bank_source_conflicts_filtered":links.length ? "read_only_snapshot_not_live_bank_coverage" : "association sidecar not available" },
    bankId: snapshot.bankId };
}
function nodeMetadata(row: Row, type: string,bankId:string) {
  return { id: String(row.context_id ?? `bank:${row.record_id}`), type,
    label: label(type === "project" || type === "workspace" ? row.project_key : type === "session" ? row.session_id : row.record_id),
    title:label(row.title ?? row.name ?? row.session_title ?? row.project_name),
    workspacePath:String(row.cwd ?? row.working_directory ?? row.workdir ?? row.workspace_path ?? row.project_path ?? "").slice(0,4096),
    bankScope:bankScopeProjection(row,bankId),
    identityStatus: type === "workspace" || type === "project" ? row.identity_status ?? "unverified_workspace_bucket" : undefined,
    status: row.status ?? (type.startsWith("bank_") ? "linked" : undefined), projectKey: row.project_key,
    at: row.updated_at ?? row.at ?? null, sourceRevision: row.source_revision ?? null,
    sessionCount: rows(row.session_ids).length, episodeCount: rows(row.episodes).length };
}
function allMetadata(snapshot: ScenarioSnapshot) {
  return [...rows(snapshot.index.projects).map(row => nodeMetadata(row, row.identity_status === "verified_project" ? "project" : "workspace",snapshot.bankId)),
    ...rows(snapshot.index.sessions).map(row => nodeMetadata(row, "session",snapshot.bankId)),
    ...rows(snapshot.associations.links).map(row => nodeMetadata(row, `bank_${row.record_type || "record"}`,snapshot.bankId))];
}
export function parseScenarioPage(params: URLSearchParams, maxLimit: number) {
  const integer = (key: string, fallback: number, min: number) => {
    const raw = params.get(key);
    if (raw == null) return fallback;
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw)) || Number(raw) < min) throw new ScenarioRequestError(400, `invalid_${key}`);
    return Number(raw);
  };
  return { offset: integer("offset", 0, 0), limit: Math.min(maxLimit, integer("limit", maxLimit, 1)) };
}
export function checkScenarioVersion(snapshot: ScenarioSnapshot, params: URLSearchParams) {
  const version = params.get("version");
  if (version && version !== snapshot.version) throw new ScenarioRequestError(409, "stale_version_refresh_required");
}
function validId(id: string | null): string {
  if (!id || id.length > 4096 || /[\u0000-\u001f]/.test(id) || !/^(session|project|workspace|bank|episode):/.test(id) || id.includes("..")) throw new ScenarioRequestError(400, "invalid_node_id");
  return id;
}
function lookupNode(snapshot: ScenarioSnapshot, id: string) {
  if(snapshot.excludedNodeIds.has(id))throw new ScenarioRequestError(403,"node_source_bank_scope_denied");
  const project = rows(snapshot.index.projects).find(row => row.context_id === id);
  if (project) return { row: project, type: project.identity_status === "verified_project" ? "project" : "workspace" };
  const session = rows(snapshot.index.sessions).find(row => row.context_id === id);
  if (session) return { row: session, type: "session" };
  const bank = rows(snapshot.associations.links).find(row => `bank:${row.record_id}` === id);
  if (bank) return { row: bank, type: `bank_${bank.record_type || "record"}` };
  throw new ScenarioRequestError(404, "node_not_found_in_bank_snapshot");
}
function snapshotRelations(snapshot: ScenarioSnapshot, ids: Set<string>) {
  const edges: Array<{source:string;target:string;type:string}> = [];
  const add = (source:string,target:string,type:string) => { if (ids.has(source) && ids.has(target)) edges.push({source,target,type}); };
  const projects = rows(snapshot.index.projects);
  for (const row of projects) for (const id of rows(row.session_ids)) add(row.context_id,`session:${id}`,row.identity_status === "verified_project" ? "project_contains_session" : "workspace_contains_session");
  for (const row of rows(snapshot.associations.links)) {
    if (row.project_key) { const project = projects.find(project=>project.project_key===row.project_key); if (project) add(project.context_id,`bank:${row.record_id}`,project.identity_status==="verified_project" ? "project_contains_bank_record" : "workspace_links_bank_record"); }
    for (const id of rows(row.session_ids)) add(`session:${id}`,`bank:${row.record_id}`,"session_supports_bank_record");
  }
  return edges;
}
export function scenarioGraphPage(snapshot: ScenarioSnapshot, params: URLSearchParams,liveTotal:number|null=null) {
  checkScenarioVersion(snapshot, params);
  const mode = params.get("mode") || "nodes";
  if (!["nodes","relations","timeline"].includes(mode)) throw new ScenarioRequestError(400,"invalid_graph_mode");
  const { offset, limit } = parseScenarioPage(params, mode==="relations" ? 120 : 240);
  const q = (params.get("q") ?? "").trim().toLowerCase();
  if (q.length > 512) throw new ScenarioRequestError(400, "invalid_query");
  const all = allMetadata(snapshot);
  const matched = q ? all.filter(node => `${node.id} ${node.label} ${node.title} ${node.workspacePath} ${node.projectKey ?? ""}`.toLowerCase().includes(q)) : all;
  const matchedIds = new Set(matched.map(node=>node.id));
  const allIds = new Set(all.map(node=>node.id));
  const relations = snapshotRelations(snapshot, allIds);
  let nodes, edges, total, matchingEdges;
  if (mode==="relations") {
    const filtered = q ? relations.filter(edge=>matchedIds.has(edge.source)||matchedIds.has(edge.target)) : relations;
    edges = filtered.slice(offset,offset+limit);
    const ids = new Set(edges.flatMap(edge=>[edge.source,edge.target]));
    nodes = all.filter(node=>ids.has(node.id));
    total = filtered.length;
    matchingEdges = edges.length;
  } else {
    const ordered = mode==="timeline" ? matched.slice().sort((a,b)=>String(b.at??"").localeCompare(String(a.at??""))) : matched;
    nodes = ordered.slice(offset,offset+limit);
    const ids = new Set(nodes.map(node=>node.id));
    const windowEdges = relations.filter(edge=>ids.has(edge.source)&&ids.has(edge.target));
    matchingEdges = windowEdges.length;
    edges = windowEdges.slice(0,500);
    total = matched.length;
  }
  const nodeRevision=scenarioRevisionReader(snapshot);
  nodes=nodes.map(node=>({...node,nodeVersion:nodeRevision(node.id)}));
  const context = scenarioMetadata(snapshot,liveTotal);
  const shown = mode==="relations" ? edges.length : nodes.length;
  return { schema:"evolving-profile.scenario-graph.v1", version:snapshot.version, bankId:snapshot.bankId, context,
    page:{ offset,limit,total,indexedTotal:mode==="relations" ? relations.length : all.length,nextOffset:offset+shown<total ? offset+shown:null,
      unit:mode==="relations" ? "relations" : "nodes",nodeCount:nodes.length,edgeCountInWindow:matchingEdges,edgeLimit:mode==="relations" ? 120:500,
      edgesTruncated:matchingEdges>edges.length,scope:mode==="relations" ? "relation_window_with_endpoints" : "induced_node_window_not_complete_graph" },
    graph:{ nodes,edges,timeline:nodes.map(node=>({id:node.id,type:node.type,at:node.at,label:node.label,status:node.status})).sort((a,b)=>String(a.at??"").localeCompare(String(b.at??""))),
      bankRecordLinks:{...context.bankRecordLinks,sampled:nodes.filter(node=>node.type.startsWith("bank_")).length} } };
}
export function scenarioEpisodePage(snapshot: ScenarioSnapshot, params: URLSearchParams) {
  if(!params.has("nodeVersion"))checkScenarioVersion(snapshot, params);
  const id = validId(params.get("id"));
  const { row, type } = lookupNode(snapshot, id);
  if (type !== "session") throw new ScenarioRequestError(400, "episodes_require_session");
  const nodeRevision=scenarioRevisionReader(snapshot),nodeVersion=nodeRevision(id);
  checkScenarioNodeVersion(snapshot,params,nodeVersion);
  const { offset, limit } = parseScenarioPage(params, 100);
  const episodes = rows(row.episodes);
  const items = episodes.slice(offset, offset + limit).map(episode => ({ id: episode.episode_id,nodeVersion:nodeRevision(`${id}/${episode.episode_id}`), label: label(episode.title || episode.episode_id), status: episode.status, bankScope:bankScopeProjection(episode,snapshot.bankId),sourceRevision: row.source_revision ?? null }));
  return { version: snapshot.version,nodeVersion, bankId: snapshot.bankId, sourceRevision: row.source_revision ?? null, items,
    page: { offset, limit, total: episodes.length, nextOffset: offset + items.length < episodes.length ? offset + items.length : null } };
}
export function scenarioDetail(snapshot: ScenarioSnapshot, params: URLSearchParams) {
  if(!params.has("nodeVersion"))checkScenarioVersion(snapshot, params);
  const id = validId(params.get("id"));
  const { row, type } = lookupNode(snapshot, id);
  const tier = params.get("tier") || "compact";
  if (!["compact", "standard", "full"].includes(tier)) throw new ScenarioRequestError(400, "invalid_tier");
  if (params.get("sourceRevision") && params.get("sourceRevision") !== row.source_revision) throw new ScenarioRequestError(409, "stale_source_revision");
  const episodeId = params.get("episodeId");
  if (episodeId) validId(episodeId);
  const projected = type === "session" ? projectSessionContextNode(row as any) : { ...nodeMetadata(row, type,snapshot.bankId), summary: row.summary ?? {}, summaryBudget: row.summary_budget ?? {}, sourceIds: row.source_ids ?? [] };
  const episode = episodeId && type === "session" ? (projected as any).episodes.find((episode: Row) => episode.id === episodeId) : null;
  if (episodeId && !episode) throw new ScenarioRequestError(404, "episode_not_found_in_session");
  const selected = (episode || projected) as Row;
  const selectedRaw=episodeId ? rows(row.episodes).find(item=>item.episode_id===episodeId)! : row;
  const nodeVersion=scenarioRevisionReader(snapshot)(episodeId ? `${id}/${episodeId}`:id);
  checkScenarioNodeVersion(snapshot,params,nodeVersion);
  const text = String(selected.summary?.[tier] ?? "");
  const textOffset = parseScenarioPage(new URLSearchParams({offset:params.get("textOffset") || "0"}),32768).offset;
  const textLimit = 32768;
  // Exactly one tier and one node/episode; no sibling summaries or nested graph.
  const { episodes: _episodes, summary: _summary, summaryBudget: _budget, sourceIds, sourceOffsets, ...metadata } = selected;
  return { version: snapshot.version,nodeVersion, bankId: snapshot.bankId, tier, node: { ...metadata,bankScope:bankScopeProjection(selectedRaw,snapshot.bankId),parentBankScope:episodeId ? bankScopeProjection(row,snapshot.bankId):undefined,sourceRevision: row.source_revision ?? null,
    summary: { [tier]: text.slice(textOffset,textOffset+textLimit) },
    summaryPage: { offset:textOffset,limit:textLimit,total:text.length,nextOffset:textOffset+textLimit<text.length ? textOffset+textLimit:null,unit:"utf16_code_units" }, summaryBudget: { [tier]: selected.summaryBudget?.[tier] ?? {} },
    sourceCount: rows(sourceIds).length, sourceIds: rows(sourceIds).slice(0, 3), sourceOffsetCount: rows(sourceOffsets).length, sourceOffsets: rows(sourceOffsets).slice(0, 3) } };
}
export async function scenarioResponse(request: Request, kind: "graph" | "detail" | "episodes") {
  const params = new URL(request.url).searchParams;
  try {
    const snapshot = await readScenarioSnapshot(params.get("bankId"));
    let liveTotal:number|null=null;
    if (kind === "graph") {
      checkScenarioVersion(snapshot,params);
      try {
        const response=await fetch(dataplaneBankUrl(snapshot.bankId,"/memories/list?limit=0"),{headers:getDataplaneHeaders(),cache:"no-store",signal:AbortSignal.timeout(1500)});
        if(response.ok) {const total=(await response.json()).total;if(Number.isSafeInteger(total)&&total>=0)liveTotal=total;}
      } catch { /* A failed live count remains unknown; the snapshot is not live coverage. */ }
    }
    const value = kind === "graph" ? scenarioGraphPage(snapshot, params,liveTotal) : kind === "detail" ? scenarioDetail(snapshot, params) : scenarioEpisodePage(snapshot, params);
    return Response.json(value, { headers: { "cache-control": "no-store" } });
  } catch (error) {
    if (error instanceof ScenarioRequestError) return Response.json({ error: error.message }, { status: error.status, headers: { "cache-control": "no-store" } });
    return Response.json({ error: "scenario_read_failed" }, { status: 500 });
  }
}
