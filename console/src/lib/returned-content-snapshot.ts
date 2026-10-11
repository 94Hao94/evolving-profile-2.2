import { lstat,readFile } from "node:fs/promises";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { EP_STATE_ROOT } from "./ep-state-paths";

export type ReturnedContentSnapshotRef={ref:string;check_id:string;session_id:string;turn_id:string;tool_call_id:string;coverage?:string};
export type ReturnedContentItem={id?:string;text:string;item_index?:number;text_truncated?:boolean;text_sha256?:string;total_chars?:number;source_role?:string;applies_when?:string[];exceptions?:string[];scope?:unknown;snapshot?:ReturnedContentSnapshotRef};
type RequestBinding={check_id:string;session_id:string;turn_id:string;tool_call_id:string;item_index:number;offset:number};
const denied=(status:number,error:string)=>({status,body:{error}});

/** Reads a single exact invocation snapshot. Never retrieves newer Bank content. */
export async function readReturnedContentSnapshot(ref:string,request:RequestBinding,root=EP_STATE_ROOT){
  if(!/^[a-f0-9]{64}$/.test(ref)||!Number.isInteger(request.item_index)||request.item_index<0||request.item_index>=200||!Number.isInteger(request.offset)||request.offset<0)return denied(400,"invalid_snapshot_request");
  const file=join(root,"audit","returned-content",ref+".json");
  try{
    const stat=await lstat(file);
    if(stat.isSymbolicLink()||!stat.isFile())return denied(403,"snapshot_not_regular_file");
    if(stat.size>12*1024*1024)return denied(413,"snapshot_exceeds_read_budget");
    const bytes=await readFile(/*turbopackIgnore: true*/ file);
    if(createHash("sha256").update(bytes).digest("hex")!==ref)return denied(409,"snapshot_bytes_changed");
    const snapshot=JSON.parse(bytes.toString("utf8"));
    if(snapshot.schema!=="evolving-profile.returned-content.v1")return denied(409,"snapshot_schema_unavailable");
    for(const key of ["check_id","session_id","turn_id","tool_call_id"] as const){
      if(typeof request[key]!=="string"||!request[key]||request[key]!==snapshot[key])return denied(403,"snapshot_binding_mismatch");
    }
    const item=Array.isArray(snapshot.items)?snapshot.items[request.item_index]:null;
    if(!item||item.item_index!==request.item_index||typeof item.text!=="string")return denied(404,"snapshot_item_unavailable");
    const points=Array.from(item.text);
    if(request.offset>points.length)return denied(400,"snapshot_offset_out_of_bounds");
    const end=Math.min(request.offset+16000,points.length);
    return {status:200,body:{id:item.id,text_sha256:item.text_sha256,text:points.slice(request.offset,end).join(""),offset:request.offset,next_offset:end<points.length?end:null,
      total_chars:item.total_chars,archived_chars:points.length,complete:end===points.length&&snapshot.coverage==="complete"&&!item.archive_truncated,
      coverage:snapshot.coverage,authority:"historical_return_snapshot_not_live_fact_or_answer_adoption"}};
  }catch(error){return denied((error as NodeJS.ErrnoException).code==="ENOENT"?404:503,"snapshot_unavailable");}
}
