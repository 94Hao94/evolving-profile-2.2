import {beforeEach,afterEach,it,expect,vi} from "vitest";
import {mkdtemp,mkdir,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
let root="",index:any,process:any;
const req=(query="")=>new Request(`http://fixture.invalid/api/node?${query}`);
const trace="pm_trace_"+"a".repeat(32),skill="pm_skill_"+"b".repeat(32);
beforeEach(async()=>{
 root=await mkdtemp(path.join(tmpdir(),"ep-node-revision-"));vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT",root);vi.stubEnv("EVOLVING_PROFILE_BANK_ID","bank-a");vi.stubEnv("EVOLVING_PROFILE_DATAPLANE_API_URL","http://fixture.invalid");vi.stubGlobal("fetch",async()=>Response.json({total:2}));vi.resetModules();
 await mkdir(path.join(root,"context"));await mkdir(path.join(root,"process-memory"));await writeFile(path.join(root,"codex.json"),JSON.stringify({bankId:"bank-a"}));
 index={bank_id:"bank-a",status:"ready",updated_at:"old",projects:[],sessions:[{context_id:"session:chosen",session_id:"chosen",bank_id:"bank-a",source_revision:"source-r1",summary:{compact:"Chosen original",full:"Full original"},episodes:[{episode_id:"episode:chosen:one",title:"First",summary:{compact:"Episode original"}}]}]};
 process={bank_id:"bank-a",updated_at:"old",records:[{process_memory_id:trace,kind:"trace",bank_id:"bank-a",text:"Original source",verification_evidence:[]},{process_memory_id:skill,kind:"skill",bank_id:"bank-a",text:"Original strategy",derived_from:[trace]}]};await save();
});
afterEach(async()=>{vi.unstubAllEnvs();vi.unstubAllGlobals();await rm(root,{recursive:true,force:true});});
async function save(){await writeFile(path.join(root,"context/context-index.json"),JSON.stringify(index));await writeFile(path.join(root,"context/context-bank-associations.json"),JSON.stringify({bank_id:"bank-a",links:[]}));await writeFile(path.join(root,"process-memory/records.json"),JSON.stringify(process));}
async function scenarioGraph(){const {GET}=await import("@/app/api/evolving-profile/scenario/graph/route");return (await GET(req())).json();}
async function processGraph(){const {GET}=await import("@/app/api/evolving-profile/process-memory/graph/route");return (await GET(req())).json();}
it("unchanged selected scenario detail and episode directory survive unrelated book appends, while legacy and graph paging stay strict",async()=>{
 const old=await scenarioGraph();const node=old.graph.nodes[0];expect(node.nodeVersion).toEqual(expect.stringMatching(/^[a-f0-9]{64}$/));
 const params=new URLSearchParams({id:node.id,version:old.version,nodeVersion:node.nodeVersion});
 index.sessions.push({context_id:"session:other",session_id:"other",summary:{compact:"Another body"}});index.updated_at="new";await save();
 const detail=await import("@/app/api/evolving-profile/scenario/detail/route");const response=await detail.GET(req(params.toString()));expect(response.status).toBe(200);const value=await response.json();expect(value.node.summary.compact).toBe("Chosen original");expect(value.nodeVersion).toBe(node.nodeVersion);expect(value.version).not.toBe(old.version);
 const episodes=await import("@/app/api/evolving-profile/scenario/episodes/route");const directory=await (await episodes.GET(req(params.toString()))).json();expect(directory.items[0].nodeVersion).toMatch(/^[a-f0-9]{64}$/);expect(directory.page.total).toBe(1);
 expect((await detail.GET(req(`id=${node.id}&version=${old.version}`))).status).toBe(409);
 const graph=await import("@/app/api/evolving-profile/scenario/graph/route");expect((await graph.GET(req(`version=${old.version}&nodeVersion=${node.nodeVersion}`))).status).toBe(409);
 const episode=await detail.GET(req(new URLSearchParams({id:node.id,episodeId:directory.items[0].id,nodeVersion:directory.items[0].nodeVersion,version:old.version}).toString()));expect(episode.status).toBe(200);expect((await episode.json()).node.summary.compact).toBe("Episode original");
});
it.each(["body","source revision","permission metadata","Bank scope","source dependency"])("scenario %s changes invalidate the old node proof",async(change)=>{
 if(change==="source dependency")index.sessions[0].source_refs=[{context_id:"session:source"}],index.sessions.push({context_id:"session:source",session_id:"source",bank_id:"bank-a",summary:{compact:"Source original"}});
 await save();const old=await scenarioGraph();const node=old.graph.nodes.find((node:any)=>node.id==="session:chosen");expect(node.nodeVersion).toEqual(expect.stringMatching(/^[a-f0-9]{64}$/));
 if(change==="body")index.sessions[0].summary.compact="Changed body";
 if(change==="source revision")index.sessions[0].source_revision="source-r2";
 if(change==="permission metadata")index.sessions[0].permissions={read:false};
 if(change==="Bank scope")index.sessions[0].bank_id="bank-b";
 if(change==="source dependency")index.sessions[1].summary.compact="Changed dependency body";
 await save();const {GET}=await import("@/app/api/evolving-profile/scenario/detail/route");const response=await GET(req(new URLSearchParams({id:node.id,nodeVersion:node.nodeVersion,version:old.version}).toString()));expect(response.status).toBe(change==="Bank scope"?403:409);expect(await response.text()).not.toContain("Changed body");
});
it("process graph metadata and source locators carry stable server proofs for cross-book source clicks",async()=>{
 const old=await processGraph();const node=old.graph.nodes.find((node:any)=>node.id===`agent:${skill}`);expect(node.nodeVersion).toEqual(expect.stringMatching(/^[a-f0-9]{64}$/));
 const detail=await import("@/app/api/evolving-profile/process-memory/[id]/route");const initial=await (await detail.GET(req(`version=${old.version}&nodeVersion=${node.nodeVersion}`),{params:Promise.resolve({id:skill})})).json();const sourceVersion=initial.sources[0].nodeVersion;expect(sourceVersion).toEqual(expect.stringMatching(/^[a-f0-9]{64}$/));expect(initial.sources[0].verification_evidence).toBeUndefined();
 process.records.push({process_memory_id:"pm_trace_"+"c".repeat(32),kind:"trace",text:"Unrelated append"});process.updated_at="new";await save();
 const response=await detail.GET(req(`version=${old.version}&nodeVersion=${sourceVersion}`),{params:Promise.resolve({id:trace})});expect(response.status).toBe(200);expect((await response.json()).record.text).toBe("Original source");
 expect((await detail.GET(req(`version=${old.version}`),{params:Promise.resolve({id:trace})})).status).toBe(409);
 const graph=await import("@/app/api/evolving-profile/process-memory/graph/route");expect((await graph.GET(req(`version=${old.version}&nodeVersion=${node.nodeVersion}`))).status).toBe(409);
});
it.each(["selected body","source body","source scope","selected permission","configured Bank"])("process %s changes reject old node/source proofs",async(change)=>{
 const old=await processGraph();const node=old.graph.nodes.find((node:any)=>node.id===`agent:${skill}`);expect(node.nodeVersion).toEqual(expect.stringMatching(/^[a-f0-9]{64}$/));
 if(change==="selected body")process.records[1].text="New strategy";
 if(change==="source body")process.records[0].text="New source";
 if(change==="source scope")process.records[0].bank_id="bank-b";
 if(change==="selected permission")process.records[1].permissions={read:false};
 if(change==="configured Bank")vi.stubEnv("EVOLVING_PROFILE_BANK_ID","bank-b");
 await save();const {GET}=await import("@/app/api/evolving-profile/process-memory/[id]/route");const response=await GET(req(`version=${old.version}&nodeVersion=${node.nodeVersion}&bankId=bank-a`),{params:Promise.resolve({id:skill})});expect(response.status).toBe(["source scope","configured Bank"].includes(change)?403:409);expect(await response.text()).not.toContain("New strategy");
});
it.each(["source_refs","sources"])("%s string arrays bind known source and transitive dependencies while unrelated appends stay readable",async(container)=>{
 const leaf="pm_trace_"+"c".repeat(32);
 process.records[1].derived_from=[];process.records[1][container]=[trace];
 process.records[0].source_refs=[leaf];process.records.push({process_memory_id:leaf,kind:"trace",bank_id:"bank-a",text:"Leaf original"});await save();
 const old=await processGraph(),node=old.graph.nodes.find((node:any)=>node.id===`agent:${skill}`),{GET}=await import("@/app/api/evolving-profile/process-memory/[id]/route");
 const read=()=>GET(req(`nodeVersion=${node.nodeVersion}&version=${old.version}`),{params:Promise.resolve({id:skill})});
 process.records.push({process_memory_id:"pm_trace_"+"d".repeat(32),kind:"trace",text:"Unrelated record"});await save();expect((await read()).status).toBe(200);
 process.records[2].text="Leaf changed";await save();expect((await read()).status).toBe(409);
 process.records[2].text="Leaf original";process.records[0].text="Source changed";await save();expect((await read()).status).toBe(409);
 process.records[0].text="Original source";process.records[0].bank_id="bank-b";await save();expect((await read()).status).toBe(403);
});
it("unknown source locators, source paths and IDs in prose do not borrow an indexed source identity",async()=>{
 process.records[1].derived_from=[];process.records[1].source_refs=["unknown locator"];
 process.records[1].source_offsets=[{source_path:trace,byte_offset:0}];process.records[1].text=`Narrative mentions ${trace}`;await save();
 const old=await processGraph(),node=old.graph.nodes.find((node:any)=>node.id===`agent:${skill}`);
 process.records[0].text="Changed unrelated trace";await save();
 const {GET}=await import("@/app/api/evolving-profile/process-memory/[id]/route");expect((await GET(req(`nodeVersion=${node.nodeVersion}&version=${old.version}`),{params:Promise.resolve({id:skill})})).status).toBe(200);
});
