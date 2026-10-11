import {readFile} from "node:fs/promises";
import {epStatePath} from "./ep-state-paths";

export type BankScopeProjection = {status:"declared_match"|"undeclared"|"unknown"|"mismatch";basis:"metadata_declaration"|"operator_state_root";declaredBankId:string|null};
const BANK_KEYS=new Set(["bank_id","bankId","source_bank_id","record_bank_id","target_bank_id"]);
// Read structured provenance, never scan prose or reuse old draft evidence.
const BODY_KEYS=new Set(["summary","summaries","summary_budget","summaryBudget","text","body","content","raw_content","raw_text","input","output","stdout","stderr","previous_reviewed_summary"]);
const COLLECTION_KEYS=new Set(["sessions","projects","records","links","profiles","episodes"]);
export function bankScopeProjection(value:unknown,bankId:string,shallow=false):BankScopeProjection {
 const declared:string[]=[];let unknown=false;
 const add=(value:unknown)=>{if(typeof value!=="string"||!value.trim()||["unknown","unavailable","unverified"].includes(value.toLowerCase()))unknown=true;else declared.push(value);};
 const visit=(value:unknown,depth:number)=>{
  if(depth>64){unknown=true;return;}
  if(Array.isArray(value)){for(const item of value)visit(item,depth+1);return;}
  if(!value||typeof value!=="object")return;
  for(const [key,item] of Object.entries(value)) {
   if(BANK_KEYS.has(key))add(item);
   else if(key==="bank_ids"||key==="bankIds"){if(Array.isArray(item))item.forEach(add);else unknown=true;}
   else if(key==="bank_scope" && typeof item==="string" && ["unknown","unavailable","unverified"].includes(item))unknown=true;
   else if(key==="bank_scope" && item && typeof item==="object" && ["unknown","unavailable","unverified"].includes(String((item as any).status)))unknown=true;
   else if(shallow && ["bank_scope","source_scope"].includes(key))visit(item,depth+1);
   else if(!BODY_KEYS.has(key) && (!shallow||!COLLECTION_KEYS.has(key)))visit(item,depth+1);
  }
 };
 visit(value,0);
 const mismatch=declared.some(id=>Boolean(bankId)&&id!==bankId);
 if(mismatch)return {status:"mismatch",basis:"metadata_declaration",declaredBankId:declared.find(id=>id!==bankId)??null};
 if(unknown||(!bankId&&declared.length))return {status:"unknown",basis:"metadata_declaration",declaredBankId:null};
 if(declared.length)return {status:"declared_match",basis:"metadata_declaration",declaredBankId:bankId};
 return {status:"undeclared",basis:"operator_state_root",declaredBankId:null};
}
export function bankScopeReadable(scope:BankScopeProjection){return scope.status==="declared_match"||scope.status==="undeclared";}
export function metadataReferencesAny(value:unknown,excludedIds:Set<string>,depth=0):boolean {
 if(excludedIds.size===0)return false;
 if(depth>64)return true;
 if(typeof value==="string")return excludedIds.has(value);
 if(Array.isArray(value))return value.some(item=>metadataReferencesAny(item,excludedIds,depth+1));
 if(!value||typeof value!=="object")return false;
 return Object.entries(value).some(([key,item])=>!BODY_KEYS.has(key)&&metadataReferencesAny(item,excludedIds,depth+1));
}
export async function configuredDataBankId(){
 if(process.env.EVOLVING_PROFILE_BANK_ID)return process.env.EVOLVING_PROFILE_BANK_ID;
 try {return String(JSON.parse(await readFile(epStatePath("codex.json"),"utf8")).bankId||"");}catch{return "";}
}
