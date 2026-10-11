import {beforeEach,afterEach,it,expect,vi} from "vitest";
import {mkdtemp,mkdir,writeFile,readFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
const hooks=vi.hoisted(()=>({index:0,states:[] as any[]}));
vi.mock("react",async original=>({...await original<typeof import("react")>(),useEffect:()=>{},useMemo:(factory:any)=>factory(),useRef:(initial:any)=>({current:initial}),useState:(initial:any)=>{
 const index=hooks.index++;if(!(index in hooks.states))hooks.states[index]=typeof initial==="function" ? initial():initial;
 return [hooks.states[index],(value:any)=>{hooks.states[index]=typeof value==="function" ? value(hooks.states[index]):value;}];
}}));
vi.mock("next-intl",()=>({useTranslations:()=>(key:string)=>key}));
let root="";
beforeEach(async()=>{root=await mkdtemp(path.join(tmpdir(),"ep-process-click-"));vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT",root);vi.resetModules();hooks.index=0;hooks.states=["table"];await mkdir(path.join(root,"process-memory"));});
afterEach(async()=>{vi.unstubAllEnvs();vi.unstubAllGlobals();await rm(root,{recursive:true,force:true});});
function find(node:any,predicate:(value:any)=>boolean):any[] {
 if(Array.isArray(node))return node.flatMap(child=>find(child,predicate));
 if(!node?.props)return [];return [...(predicate(node)?[node]:[]),...find(node.props.children,predicate)];
}
it("a source-chain click reads an indexed record outside the current graph window",async()=>{
 const sourceId="pm_trace_"+"a".repeat(32),targetId="pm_skill_"+"b".repeat(32);
 await writeFile(path.join(root,"process-memory/records.json"),JSON.stringify({records:[{process_memory_id:sourceId,kind:"trace",text:"Original cross-page evidence"},{process_memory_id:targetId,kind:"skill",text:"Strategy",derived_from:[sourceId]}]}));
 const {readProcessSnapshot}=await import("@/lib/process-graph-data");
 const version=(await readProcessSnapshot()).version;
 const {GET}=await import("@/app/api/evolving-profile/process-memory/[id]/route");
 // Native fetch accepts relative browser URLs; resolve them to the fixture origin.
 vi.stubGlobal("fetch",async(input:string)=>GET(new Request(new URL(input,"http://fixture.invalid")),{params:Promise.resolve({id:decodeURIComponent(new URL(input,"http://fixture.invalid").pathname.split("/").at(-1)!)})}));
 const {AgentProcessView}=await import("@/components/agent-process-view");
 const props={english:true,version,graph:{nodes:[{id:`agent:${targetId}`,type:"agent_skill",label:"Strategy"}],edges:[],timeline:[]}};
 const render=()=>{hooks.index=0;return AgentProcessView(props);};
 const target=find(render(),element=>typeof element.props.onAction==="function")[0];
 expect(await target.props.onAction()).toBe(true);
 const source=find(render(),element=>typeof element.props.onAction==="function" && element.props.children?.includes?.("Original cross-page evidence"))[0];
 expect(source).toBeDefined();
 const file=path.join(root,"process-memory/records.json"),book=JSON.parse(await readFile(file,"utf8"));
 book.records.push({process_memory_id:"pm_trace_"+"f".repeat(32),kind:"trace",text:"Unrelated append between clicks"});await writeFile(file,JSON.stringify(book));
 expect(await source.props.onAction()).toBe(true);
 const paragraphs=find(render(),element=>element.type==="p").map(element=>element.props.children);
 expect(paragraphs).toContain(`agent:${sourceId}`);
 expect(paragraphs).toContain("Original cross-page evidence");
});
