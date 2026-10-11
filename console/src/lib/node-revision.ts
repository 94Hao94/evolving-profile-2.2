import {createHash} from "node:crypto";
import {readFile} from "node:fs/promises";
import {epStatePath} from "./ep-state-paths";

const BODY_KEYS=new Set(["summary","summaries","summary_budget","summaryBudget","text","body","content","raw_content","raw_text","input","output","stdout","stderr","previous_reviewed_summary"]);
const DIRECT_REFS=new Set(["derived_from","source_trace_ids","source_ids","source_context_ids","source_record_ids"]);
const SOURCE_CONTAINERS=new Set(["source_refs","sources","source_offsets"]);
const SOURCE_IDS=new Set(["id","context_id","process_memory_id","source_id","source_record_id","record_id"]);
export function referencedSourceIds(value:unknown):string[]{
 const ids=new Set<string>();
 const add=(item:unknown)=>{if(typeof item==="string")ids.add(item);else if(Array.isArray(item))item.forEach(add);};
 const visit=(item:unknown,inSource=false,allowBareId=inSource)=>{
  if(typeof item==="string"){if(allowBareId)ids.add(item);return;}
  if(Array.isArray(item)){item.forEach(value=>visit(value,inSource,allowBareId));return;}
  if(!item||typeof item!=="object")return;
  for(const [key,value] of Object.entries(item)){
   if(DIRECT_REFS.has(key)||(inSource&&SOURCE_IDS.has(key)))add(value);
   else if(SOURCE_CONTAINERS.has(key))visit(value,true,key!=="source_offsets");
   else if(!BODY_KEYS.has(key)&&value&&typeof value==="object")visit(value,inSource&&!Array.isArray(value),false);
  }
 };
 visit(value);return [...ids];
}
export function revisionAuthority(value:Record<string,any>){
 const keys=["schema","bank_id","bankId","bank_scope","source_scope","scope","permissions","permission","permission_revision","permission_version","scope_revision","access","acl","read_scope","authorization","authz","policy","owner_id","tenant_id","source_of_truth","evidence_role"];
 return Object.fromEntries(keys.filter(key=>key in value).map(key=>[key,value[key]]));
}
export async function readOperatorRevisionAuthority(){
 try{return revisionAuthority(JSON.parse(await readFile(epStatePath("codex.json"),"utf8")));}catch{return {};}
}
function canonical(value:unknown):string {
 if(Array.isArray(value))return `[${value.map(canonical).join(",")}]`;
 if(value&&typeof value==="object")return `{${Object.keys(value).sort().map(key=>`${JSON.stringify(key)}:${canonical((value as any)[key])}`).join(",")}}`;
 return JSON.stringify(value)??"null";
}
const digest=(value:unknown)=>createHash("sha256").update(canonical(value)).digest("hex");
export type RevisionRecord={id:string;value:unknown;references:string[]};
// Request-local digest cache contains only IDs/hashes. No persisted body cache
// and no implicit parent/sibling lookup: dependencies must be explicit IDs.
export function createNodeRevisionReader(bankId:string,authority:unknown,records:RevisionRecord[]){
 const lookup=new Map(records.map(record=>[record.id,record]));
 const hashes=new Map<string,string>();
 const authorityHash=digest({bankId,authority,protocol:"evolving-profile.node-revision.v1"});
 return (id:string)=>{
  const visited=new Set<string>();
  const visit=(key:string)=>{
   if(visited.has(key))return;
   const record=lookup.get(key);if(!record)return;
   visited.add(key);
   if(!hashes.has(key))hashes.set(key,digest(record.value));
   record.references.forEach(visit);
  };
  visit(id);
  return digest({authority:authorityHash,id,dependencies:[...visited].sort().map(key=>[key,hashes.get(key)])});
 };
}
