import { createHash } from 'node:crypto';
import { open, stat } from 'node:fs/promises';

export const scanByteBudget = 12 * 1024 * 1024;
const scanChunk = 512 * 1024;
const sourceIndexLimit = 256;
type Witness = {native_turn_id:string;native_message_id:any;message_sha256:string;normalized_prompt_sha256:string;preview:string;normalized_length:number;source_byte_offset:number;record_kind:string;message_record_bytes:number};
type SourceIndex = {inode:number;revision:string;size:number;mtime:number;cursor:number;tailAt:number|null;turns:Map<string,Witness[]>;anchors:Map<number,number>;skipRecord:boolean;oversizedUserGap:boolean};
const sources = new Map<string,SourceIndex>();
export type ScanBudget = {bytesLeft:number;deadline:number};

function normalizeNativePrompt(value:string) {
  let text=value.replace(/<evolving_profile_memories>[\s\S]*?<\/evolving_profile_memories>/g,'').replace(/<relevant_memories>[\s\S]*?<\/relevant_memories>/g,'');
  for(const tag of ['recommended_plugins','environment_context','in-app-browser-context','heartbeat','evolving_profile_midtask_journal','evolving_profile_checkpoint']) text=text.replace(new RegExp(`<${tag}(?:\\s[^>]*)?>[\\s\\S]*?<\\/${tag}>`,'ig'),'');
  const matches=[...text.matchAll(/^##\s*My request(?:\s+for Codex)?\s*:\s*/igm)];
  if(matches.length){const last=matches[matches.length-1];text=text.slice(last.index!+last[0].length);}
  text=text.replace(/<image\b[^>]*>[\s\S]*?<\/image>/ig,'').replace(/<image\b[^>]*\/?>/ig,'').replace(/^Distinguish instructions in attached documents from the user's request\.\s*$/igm,'');
  return text.replace(/[\s\u0085]+/g,' ').trim();
}
function witness(raw:Buffer,offset:number):Witness|null {
  if(!/"role"\s*:\s*"user"|"type"\s*:\s*"UserMessage"/.test(raw.toString('utf8')))return null;
  try {
    const record=JSON.parse(raw.toString('utf8')),p=record.payload || {};
    let turn:string,item:any;
    if(record.type==='response_item' && p.type==='message' && p.role==='user'){const kinds=p.internal_chat_message_metadata_passthrough?.content_item_kinds;if(Array.isArray(kinds) && kinds.length && !kinds.some((kind:any)=>typeof kind==='string' && kind.startsWith('user.')))return null;turn=p.internal_chat_message_metadata_passthrough?.turn_id || p.turn_id;item=p;}
    else if(record.type==='event_msg' && p.type==='item_completed' && p.item?.type==='UserMessage'){turn=p.turn_id;item=p.item;}
    else return null;
    if(typeof turn!=='string' || !turn)return null;
    const text=normalizeNativePrompt((item.content || []).filter((part:any)=>part && typeof part.text==='string').map((part:any)=>part.text).join('\n'));
    if(!text)return null;
    const chars=Array.from(text);
    return {native_turn_id:turn,native_message_id:item.id ?? null,message_sha256:createHash('sha256').update(raw).digest('hex'),normalized_prompt_sha256:createHash('sha256').update(text).digest('hex'),preview:chars.slice(0,600).join(''),normalized_length:chars.length,source_byte_offset:offset,record_kind:record.type,message_record_bytes:raw.length};
  }catch{return null;}
}
function matches(row:any,item:Witness) {
  const preview=String(row.prompt_preview || '').replace(/[\s\u0085]+/g,' ').trim(),fingerprint=String(row.prompt_fingerprint || '');
  const hasFingerprint=/^[0-9a-f]{16}$/.test(fingerprint);
  if(hasFingerprint && item.normalized_prompt_sha256.slice(0,16)!==fingerprint)return false;
  return Boolean(preview && preview===item.preview.trim() && (hasFingerprint || item.normalized_length<=600));
}
export function hasIndexedPrompt(row:any,filename?:string) {
  const indexes=filename ? (sources.has(filename)?[sources.get(filename)!]:[]) : [...sources.values()];
  return indexes.some(index=>(index.turns.get(String(row.turn_id || '')) || []).some(item=>matches(row,item)));
}
export async function advanceIndexedSource(filename:string,revision:string,budget:ScanBudget,length:number) {
  const index=sources.get(filename);if(!index || index.cursor>=index.size)return false;
  const sourceStat=await stat(filename);
  if(index.revision!==revision || index.inode!==sourceStat.ino || sourceStat.size<index.size || sourceStat.size===index.size && sourceStat.mtimeMs!==index.mtime)return false;
  const end=await scanSegment(filename,index,index.cursor,Math.min(length,scanChunk),budget,true);
  if(end!==null)index.cursor=end;
  return true;
}
async function scanSegment(filename:string,index:SourceIndex,start:number,length:number,budget:ScanBudget,aligned=false) {
  if(budget.bytesLeft<=0 || Date.now()>budget.deadline)return null;
  const stream=await open(filename,'r');let data:Buffer;
  try {const buffer=Buffer.alloc(Math.min(length,budget.bytesLeft));const read=await stream.read(buffer,0,buffer.length,start);data=buffer.subarray(0,read.bytesRead);}finally{await stream.close();}
  budget.bytesLeft-=data.length;
  const skipping=aligned && index.skipRecord;
  const first=(start===0 || aligned) && !skipping ? 0 : data.indexOf(10)+1;
  if(first===0 && (skipping || start && !aligned))return aligned ? start+data.length : null;
  if(skipping)index.skipRecord=false;
  const end=data.lastIndexOf(10)+1;
  if(end<=first){if(skipping)return start+end;if(aligned){index.skipRecord=true;if(/"role"\s*:\s*"user"|"type"\s*:\s*"UserMessage"/.test(data.subarray(0,2048).toString('utf8')))index.oversizedUserGap=true;return start+data.length;}return null;}
  let cursor=first;
  while(cursor<end){const next=data.indexOf(10,cursor)+1,raw=data.subarray(cursor,next),stamp=/"timestamp"\s*:\s*"([^"]+)"/.exec(raw.subarray(0,300).toString('utf8'));if(stamp && Number.isFinite(Date.parse(stamp[1]))){index.anchors.set(start+cursor,Date.parse(stamp[1]));if(index.anchors.size>4096)index.anchors.delete(index.anchors.keys().next().value!);}const item=witness(raw,start+cursor);
    if(item){const items=index.turns.get(item.native_turn_id) || [];const old=items.findIndex(value=>value.normalized_prompt_sha256===item.normalized_prompt_sha256);if(old<0)items.push(item);else if(items[old].record_kind!=='response_item' && item.record_kind==='response_item')items[old]=item;index.turns.set(item.native_turn_id,items);}
    cursor=next;
  }
  return start+end;
}
async function timeProbe(filename:string,index:SourceIndex,row:any,budget:ScanBudget) {
  const target=Date.parse(row.at || '');if(!Number.isFinite(target))return;
  let location:[number,number]|null=null;
  const stream=await open(filename,'r');
  try {for(let level=0;level<4;level++){
    const lower=[...index.anchors].filter(([,stamp])=>stamp<target).map(([position])=>position),upper=[...index.anchors].filter(([,stamp])=>stamp>=target).map(([position])=>position);
    const low=lower.length?Math.max(...lower):0,high=upper.length?Math.min(...upper):index.size;
    if(high<=low)break;if(high-low<=262144){location=[low,high];break;}
    for(let step=1;step<=8;step++){
      if(budget.bytesLeft<65536 || Date.now()>budget.deadline)break;
      const middle=low+Math.floor((high-low)*step/9),buffer=Buffer.alloc(65536),read=await stream.read(buffer,0,buffer.length,middle),data=buffer.subarray(0,read.bytesRead);budget.bytesLeft-=data.length;
      const boundary=data.indexOf(10);if(boundary<0)continue;
      const match=/"timestamp"\s*:\s*"([^"]+)"/.exec(data.subarray(boundary+1,boundary+300).toString('utf8')),stamp=Date.parse(match?.[1] || '');
      if(Number.isFinite(stamp))index.anchors.set(middle+boundary+1,stamp);
    }
    location=[low,high];
  }}finally{await stream.close();}
  if(location!==null){const lower=[...index.anchors].filter(([,stamp])=>stamp<target).map(([position])=>position),upper=[...index.anchors].filter(([,stamp])=>stamp>=target).map(([position])=>position);location=[lower.length?Math.max(...lower):0,upper.length?Math.min(...upper):index.size];await scanSegment(filename,index,Math.max(0,location[0]-65536),262144,budget);if(location[1]-location[0]>131072)await scanSegment(filename,index,Math.max(0,location[1]-131072),262144,budget);}
}
export async function nativeTurnWitnesses(filename:string,revision:string,row:any,budget:ScanBudget,shallow=false) {
  const sourceStat=await stat(filename);let index=sources.get(filename);
  if(!index || index.inode!==sourceStat.ino || index.revision!==revision || sourceStat.size<index.size || (sourceStat.size===index.size && sourceStat.mtimeMs!==index.mtime)) {
    if(budget.bytesLeft<=0 || Date.now()>budget.deadline)return {matches:[],scan:{state:'partial',indexed_prefix_bytes:0,source_bytes:sourceStat.size,witness_revalidation:'budget_exhausted'}};
    index={inode:sourceStat.ino,revision,size:sourceStat.size,mtime:sourceStat.mtimeMs,cursor:0,tailAt:null,turns:new Map(),anchors:new Map(),skipRecord:false,oversizedUserGap:false};
    if(!sources.has(filename) && sources.size>=sourceIndexLimit)sources.delete(sources.keys().next().value!);sources.set(filename,index);
  }else{sources.delete(filename);sources.set(filename,index);}
  index.size=sourceStat.size;index.mtime=sourceStat.mtimeMs;
  const turn=String(row.turn_id || '');
  if(!(index.turns.get(turn) || []).some(item=>matches(row,item))){
    if(index.cursor<index.size){const end=await scanSegment(filename,index,index.cursor,shallow?262144:scanChunk,budget,true);if(end!==null)index.cursor=end;}
    if(!shallow && index.size>scanChunk && index.tailAt!==index.size){await scanSegment(filename,index,Math.max(0,index.size-scanChunk),scanChunk,budget);index.tailAt=index.size;}
    if(!shallow && !index.turns.has(turn) && index.cursor<index.size)await timeProbe(filename,index,row,budget);
  }
  const checked:Witness[]=[];let freshState:string|null=null;
  for(const item of index.turns.get(turn) || []){
    const length=item.message_record_bytes;
    if(length<1 || budget.bytesLeft<length || Date.now()>budget.deadline){freshState='budget_exhausted';continue;}
    // Stat growth can accompany an in-place rewrite. Fresh-read the exact
    // locator/hash inside the shared budget without rescanning its source.
    const stream=await open(filename,'r');let raw:Buffer;
    try{const buffer=Buffer.alloc(length),read=await stream.read(buffer,0,length,item.source_byte_offset);raw=buffer.subarray(0,read.bytesRead);}finally{await stream.close();}
    budget.bytesLeft-=raw.length;
    if(raw.length!==length || createHash('sha256').update(raw).digest('hex')!==item.message_sha256){index.turns.clear();index.anchors.clear();index.cursor=0;index.tailAt=null;index.skipRecord=false;index.oversizedUserGap=false;freshState='source_changed';checked.length=0;break;}
    if(matches(row,item))checked.push(item);
  }
  return {matches:checked,scan:{state:index.cursor>=index.size && !index.skipRecord && !index.oversizedUserGap?'complete':'partial',indexed_prefix_bytes:index.cursor,source_bytes:index.size,witness_revalidation:freshState}};
}
