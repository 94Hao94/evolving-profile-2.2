import {beforeEach,afterEach,it,expect,vi} from "vitest";
import {mkdtemp,mkdir,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import productionRows from "../fixtures/context-coverage-prod.json";
let root="";
const req=(query="")=>new Request(`http://fixture.invalid/api/scenario?${query}`);
const published=productionRows.find(row=>row.status==="episode_directory_ready")!;
let index:any, associations:any;
beforeEach(async()=>{
 root=await mkdtemp(path.join(tmpdir(),"ep-runtime-bank-scope-"));
 vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT",root);vi.stubEnv("EVOLVING_PROFILE_BANK_ID","bank-a");vi.stubEnv("EVOLVING_PROFILE_DATAPLANE_API_URL","http://fixture.invalid");
 vi.stubGlobal("fetch",async()=>Response.json({total:20}));vi.resetModules();
 await mkdir(path.join(root,"context"));await writeFile(path.join(root,"codex.json"),JSON.stringify({bankId:"bank-a"}));
 index={bank_id:"bank-a",status:"ready",sessions:[{...published,bank_id:"bank-a",summary:{compact:"Good directory"}}],projects:[{context_id:"project:local",project_key:"local",bank_id:"bank-a",identity_status:"unverified_workspace_bucket",session_ids:[published.session_id],summary:{compact:"Local workspace"}}]};
 associations={bank_id:"bank-a",links:[],linked_records:0,scanned_records:10};
});
afterEach(async()=>{vi.unstubAllEnvs();vi.unstubAllGlobals();await rm(root,{recursive:true,force:true});});
async function save(){await writeFile(path.join(root,"context/context-index.json"),JSON.stringify(index));await writeFile(path.join(root,"context/context-bank-associations.json"),JSON.stringify(associations));}
it.each([
 ["session","session:foreign",{bank_id:"bank-b"}],
 ["nested source reference","session:foreign",{bank_id:"bank-a",source_refs:[{bank_id:"bank-b",source_id:"foreign-source"}]}],
 ["episode","session:foreign",{bank_id:"bank-a",episodes:[{episode_id:"episode:foreign:1",bankId:"bank-b",summary:{compact:"Foreign episode body"}}]}],
 ["episode source locator","session:foreign",{bank_id:"bank-a",episodes:[{episode_id:"episode:foreign:1",source_offsets:[{bank_id:"bank-b",source_path:"foreign-path",byte_offset:0}],summary:{compact:"Foreign episode body"}}]}],
 ["explicit unknown","session:foreign",{bank_id:null}],
 ["unknown scope object","session:foreign",{bank_scope:{status:"unknown"}}],
 ["conflicting aliases","session:foreign",{bank_id:"bank-a",bankId:"bank-b"}],
 ["workspace","project:foreign",{bank_id:"bank-b"}],
 ["association","bank:foreign",{bank_id:"bank-b"}],
] as const)("filters %s scope conflicts from graph/statistics and refuses direct body reads",async(_name,id,scope)=>{
 if(id.startsWith("session:"))index.sessions.push({context_id:id,session_id:"foreign",status:"model_reviewed",summary:{compact:"Foreign body must not escape"},...scope});
 else if(id.startsWith("project:"))index.projects.push({context_id:id,project_key:"foreign",summary:{compact:"Foreign body must not escape"},...scope});
 else {associations.links.push({record_id:"foreign",record_type:"world",...scope});associations.linked_records=1;}
 await save();
 const graph=await import("@/app/api/evolving-profile/scenario/graph/route");
 const value=await (await graph.GET(req())).json();
 expect(value.graph.nodes.map((node:any)=>node.id)).not.toContain(id);
 expect(value.context.graphStats.nodes).toBe(2);
 expect(value.page.total).toBe(2);
 expect(value.context.graphStats.edges).toBe(1);
 expect(value.graph.bankRecordLinks.linked).toBe(0);
 if(id.startsWith("bank:"))expect(value.graph.bankRecordLinks).toMatchObject({scanned:null,reportedScanned:10,coverage:"unknown",unscanned:null});
 const detail=await import("@/app/api/evolving-profile/scenario/detail/route");
 const response=await detail.GET(req(`id=${encodeURIComponent(id)}`));
 expect(response.status).toBe(403);
 expect(await response.text()).not.toContain("Foreign body");
 if(id.startsWith("session:")){
  const episodes=await import("@/app/api/evolving-profile/scenario/episodes/route");
  expect((await episodes.GET(req(`id=${encodeURIComponent(id)}`))).status).toBe(403);
 }
});
it("returns the real same-Bank published episode and marks undeclared legacy scope without claiming a verified match",async()=>{
 index.sessions.push({context_id:"session:legacy",session_id:"legacy",summary:{compact:"Legacy local navigation"}});await save();
 const graph=await import("@/app/api/evolving-profile/scenario/graph/route");
 const value=await (await graph.GET(req())).json();
 expect(value.graph.nodes.find((node:any)=>node.id===published.context_id).bankScope.status).toBe("declared_match");
 expect(value.graph.nodes.find((node:any)=>node.id==="session:legacy").bankScope).toMatchObject({status:"undeclared",basis:"operator_state_root",declaredBankId:null});
 const detail=await import("@/app/api/evolving-profile/scenario/detail/route");
 const episode=published.episodes[0];
 const body=await (await detail.GET(req(`id=${published.context_id}&episodeId=${episode.episode_id}&tier=full&version=${value.version}`))).json();
 expect(body.node.summary.full).toBe(episode.summary.full);
 expect(body.node.bankScope.status).toBe("undeclared");
 expect(body.node.parentBankScope.status).toBe("declared_match");
});
it("does not return a workspace summary that depends on an excluded Session",async()=>{
 index.sessions.push({context_id:"session:foreign",session_id:"foreign",bank_id:"bank-b",summary:{compact:"Foreign source"}});
 index.projects[0].session_ids.push("foreign");await save();
 const graph=await import("@/app/api/evolving-profile/scenario/graph/route");
 const value=await (await graph.GET(req())).json();
 expect(value.context.graphStats.nodes).toBe(1);expect(value.graph.edges).toEqual([]);
 const detail=await import("@/app/api/evolving-profile/scenario/detail/route");
 expect((await detail.GET(req("id=project:local"))).status).toBe(403);
});
it("searches actual titles and working-directory metadata without searching summary bodies",async()=>{
 index.sessions[0].title="Quantum Materials";index.sessions[0].cwd="/research/quantum-workspace";index.sessions[0].summary.compact="Unsearchable body sentinel";await save();
 const {GET}=await import("@/app/api/evolving-profile/scenario/graph/route");
 const title=await (await GET(req("q=Quantum"))).json();expect(title.page.total).toBe(1);expect(title.graph.nodes[0].id).toBe(published.context_id);
 const directory=await (await GET(req("q=quantum-workspace"))).json();expect(directory.page.total).toBe(1);
 const body=await (await GET(req("q=Unsearchable"))).json();expect(body.page.total).toBe(0);
});
it("process graph, counts, sources and direct detail apply the same declared Bank/source rules",async()=>{
 await mkdir(path.join(root,"process-memory"));
 const good="pm_trace_"+"a".repeat(32),foreign="pm_trace_"+"b".repeat(32),derived="pm_skill_"+"c".repeat(32),unknown="pm_trace_"+"d".repeat(32),legacy="pm_trace_"+"e".repeat(32);
 await writeFile(path.join(root,"process-memory/records.json"),JSON.stringify({bank_id:"bank-a",records:[{process_memory_id:good,kind:"trace",bank_id:"bank-a",text:"Good body"},{process_memory_id:foreign,kind:"trace",bankId:"bank-b",text:"Foreign process body"},{process_memory_id:derived,kind:"skill",bank_id:"bank-a",derived_from:[foreign],text:"Mixed derived body"},{process_memory_id:unknown,kind:"trace",bank_id:null,text:"Unknown declared body"},{process_memory_id:legacy,kind:"trace",text:"Undeclared local process evidence"}]}));
 const graph=await import("@/app/api/evolving-profile/process-memory/graph/route");const value=await (await graph.GET(req("bankId=bank-a"))).json();
 expect(value.processMemory.record_count).toBe(2);expect(value.page.total).toBe(2);
 expect(value.graph.nodes.map((node:any)=>node.id).sort()).toEqual([`agent:${good}`,`agent:${legacy}`].sort());
 const detail=await import("@/app/api/evolving-profile/process-memory/[id]/route");
 for(const id of [foreign,derived,unknown]){const response=await detail.GET(req("bankId=bank-a"),{params:Promise.resolve({id})});expect(response.status).toBe(403);expect(await response.text()).not.toContain("body");}
 expect((await graph.GET(req("bankId=bank-b"))).status).toBe(403);
 expect((await detail.GET(req("bankId=bank-b"),{params:Promise.resolve({id:good})})).status).toBe(403);
});
it("a process snapshot with a different declared Bank cannot break the ordinary runtime status or expose its counts",async()=>{
 await save();await mkdir(path.join(root,"process-memory"));
 await writeFile(path.join(root,"process-memory/records.json"),JSON.stringify({bank_id:"bank-b",records:[{process_memory_id:"pm_trace_"+"a".repeat(32),text:"Foreign process body"}]}));
 const {GET}=await import("@/app/api/evolving-profile/runtime/route");
 const response=await GET(req("bankId=bank-a"));expect(response.status).toBe(200);
 const value=await response.json();expect(value.processMemory.status).toBe("not_available_for_bank");expect(value.processMemory.record_count).toBe(0);
 expect(JSON.stringify(value)).not.toContain("Foreign process body");
});
it("excludes summaries that reference a known foreign indexed source without repeating its Bank declaration",async()=>{
 index.sessions.push({context_id:"session:foreign",session_id:"foreign",bank_id:"bank-b",summary:{compact:"Foreign body"}});
 index.sessions[0].source_refs=[{context_id:"session:foreign"}];await save();
 const {GET}=await import("@/app/api/evolving-profile/scenario/graph/route");
 const value=await (await GET(req())).json();expect(value.context.graphStats.nodes).toBe(0);expect(value.graph.nodes).toEqual([]);
 const detail=await import("@/app/api/evolving-profile/scenario/detail/route");expect((await detail.GET(req(`id=${published.context_id}`))).status).toBe(403);
});
it("rejects conflicting snapshot provenance before treating a matching top-level Bank as sufficient",async()=>{
 index.origin={bankId:"bank-b"};await save();
 const {GET}=await import("@/app/api/evolving-profile/scenario/graph/route");expect((await GET(req())).status).toBe(403);
});
