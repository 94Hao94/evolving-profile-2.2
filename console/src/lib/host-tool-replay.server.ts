import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";
import { projectHostTurnReplay } from "./host-tool-replay";
import { mergeToolReceipts } from "./tool-receipt";
const root=EP_HOST_SESSIONS;
const ingressPath=epStatePath("audit/prompt-ingress.jsonl");
const cache=new Map<string,{mtime:number;events:any[];coverage:"exact_host_turn_complete"|"partial"|"unknown"}>();
export async function readReplayPrompts():Promise<any[]> {
  try {return JSON.parse(await readFile(epStatePath("audit/host-replay-prompts.json"),"utf8"));}
  catch{return [];}
}
export async function enrichHostToolReceipt(promptId:string,detail:any) {
  try {
    const ingress=(await readFile(ingressPath,"utf8")).trim().split("\n").flatMap(l=>{try{return [JSON.parse(l)]}catch{return []}});
    // The full timestamp is part of identity: repeated identical Prompts must not steal each other's calls.
    const row=[...ingress,...await readReplayPrompts()].findLast((r:any)=>`${r.prompt_fingerprint}:${r.at}`===promptId);
    if(!row?.session_id || !row.turn_id || !/^[-0-9a-f]{36}$/.test(row.session_id)) return detail;
    const day=String(row.at).slice(0,10).split("-");
    const dirs=[path.join(root,...day)];
    // Local files may use the host-local date; also inspect the adjacent date on UTC boundaries.
    for(const delta of [-1,1]) {const d=new Date(row.at);d.setUTCDate(d.getUTCDate()+delta);dirs.push(path.join(root,...d.toISOString().slice(0,10).split("-")));}
    let filename:string|undefined;
    for(const d of dirs){let fs:string[]=[];try{fs=await readdir(d)}catch{};const match=fs.find(f=>f.endsWith(`-${row.session_id}.jsonl`));if(match){filename=path.join(d,match);break;}}
    if(!filename)return detail;
    const mtime=(await stat(filename)).mtimeMs,key=`${filename}:${row.turn_id}`;
    let entry=cache.get(key);
    if(!entry || entry.mtime!==mtime) {
      const rows=(await readFile(filename,"utf8")).split("\n").flatMap(l=>{try{return[JSON.parse(l)]}catch{return[]}});
      const projected=projectHostTurnReplay(rows,{session_id:row.session_id,turn_id:row.turn_id});
      entry={mtime,events:projected.tool_events,coverage:projected.call_coverage};
      if(cache.size>100)cache.clear();cache.set(key,entry);
    }
    const complete=entry.coverage==="exact_host_turn_complete";
    const events=mergeToolReceipts(detail?.memory_route_receipt?.tool_events || [],entry.events,{hostTurnComplete:complete});
    return {...detail,session_id:row.session_id,turn_id:row.turn_id,
      memory_route_receipt:{...detail?.memory_route_receipt,tool_events:events,
        call_coverage:entry.coverage,
        receipt_source:"codex_host_tool_result",returned_count:complete && events.every(event=>typeof event.returned_count==="number") ? events.reduce((s,e)=>s+e.returned_count,0) : null,
        audit_boundary:"exact_session_turn_host_replay_not_original_ep_ledger",
        original_ep_events:detail?.memory_route_receipt?.tool_events || []},
      audit_source_scope:"prompt_bound_host_result_replay"};
  }catch{return detail;}
}
