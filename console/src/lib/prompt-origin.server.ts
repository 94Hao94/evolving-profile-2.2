import { createHash } from 'node:crypto';
import { open, readdir, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { classifyPromptOrigin } from './prompt-origin';
import { nativeTurnWitnesses,scanByteBudget,hasIndexedPrompt,advanceIndexedSource } from './native-prompt-witness.server';
const headerCache=new Map<string,{stamp:string;result:{metadata:any;revision?:string;error?:string}}>();

// Per-request cache only: each new list projection rechecks source stat and
// exact header SHA. No lifetime classification cache can mask source changes.
export class PromptOriginResolver {
  private headers = new Map<string, Promise<{ metadata: any; revision?: string; error?: string }>>();
  private index?: Promise<Map<string, string[]>>;
  private budget={bytesLeft:scanByteBudget,deadline:Date.now()+1500};
  constructor(private roots: string[]) {}
  private async paths(sessionId: string) {
    if (!this.index) this.index = (async () => {
      const index = new Map<string, string[]>();
      const pending = [...this.roots];
      while (pending.length) {
        const directory = pending.pop()!;
        let entries;
        try { entries = await readdir(directory, { withFileTypes: true }); } catch { continue; }
        for (const entry of entries) {
          const filename = path.join(directory, entry.name);
          if (entry.isDirectory()) pending.push(filename);
          else if (entry.isFile() && entry.name.startsWith('rollout-') && entry.name.endsWith('.jsonl')) {
            const header=await this.read(filename);if(header.error)continue;
            const meta=header.metadata,identity=String(meta.id || '');
            const nativePath=await realpath(filename);
            index.set(identity,[...(index.get(identity) || []),nativePath]);
            const spawn=meta.source?.subagent?.thread_spawn,parent=meta.parent_thread_id;
            if(spawn && parent && spawn.parent_thread_id===parent && meta.session_id===parent)index.set(String(parent),[...(index.get(String(parent)) || []),nativePath]);
          }
        }
      }
      return index;
    })();
    return (await this.index).get(sessionId) || [];
  }
  private async read(filename: string) {
    try {
      const resolved = await realpath(filename).catch(() => path.resolve(filename));
      const nativeRoots = (await Promise.all(this.roots.map(async root => [path.resolve(root), await realpath(root).catch(() => path.resolve(root))]))).flat();
      const allowed = nativeRoots.some(root => {
        const relative = path.relative(root, resolved);
        return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
      });
      if (path.extname(resolved) !== '.jsonl' || !allowed) return { metadata: null, error: 'source_path_outside_native_roots' };
      const sourceStat = await stat(resolved);
      const stamp = `${resolved}:${sourceStat.ino}:${sourceStat.size}:${sourceStat.mtimeMs}`;
      const cached=headerCache.get(resolved);if(cached?.stamp===stamp)return cached.result;
      if (!this.headers.has(stamp)) this.headers.set(stamp, (async () => {
        const stream = await open(resolved, 'r');
        try {
          const buffer = Buffer.alloc(262145);
          const { bytesRead } = await stream.read(buffer, 0, buffer.length, 0);
          const end = buffer.subarray(0, bytesRead).indexOf(10);
          if (end < 0 || end + 1 > 262144) return { metadata: null, error: 'native_header_incomplete_or_over_budget' };
          const raw = buffer.subarray(0, end + 1);
          const header = JSON.parse(raw.toString('utf8'));
          if (header.type !== 'session_meta' || !header.payload || typeof header.payload !== 'object' || Array.isArray(header.payload)) return { metadata: null, error: 'native_session_metadata_missing' };
          const metadata = Object.fromEntries(['id', 'session_id', 'parent_thread_id', 'source', 'thread_source'].map(key => [key, header.payload[key] ?? null]));
          return { metadata, revision: createHash('sha256').update(raw).digest('hex') };
        } finally { await stream.close(); }
      })());
      const result=await this.headers.get(stamp)!;
      if(headerCache.size>2048)headerCache.clear();headerCache.set(resolved,{stamp,result});
      return result;
    } catch { return { metadata: null, error: 'native_source_unavailable' }; }
  }
  async resolve(row: any) {
    const explicit = row.transcript_path;
    if (explicit !== undefined && explicit !== null && (typeof explicit !== 'string' || !explicit.trim() || explicit.includes('\u0000'))) return classifyPromptOrigin(row, null, { error: 'native_source_path_invalid' });
    const paths = explicit !== undefined && explicit !== null ? [explicit] : await this.paths(String(row.session_id || ''));
    if(!paths.length)return classifyPromptOrigin(row,null,{error:'native_source_missing'});
    if(!row.turn_id)return classifyPromptOrigin(row,null,{error:'native_turn_identity_missing'});
    const sized=await Promise.all(paths.map(async (filename:string)=>({filename,size:await stat(filename).then(value=>value.size).catch(()=>0)})));
    sized.sort((a,b)=>a.size-b.size);
    const found:any[]=[],progress=new Map<string,any>(),prepared:any[]=[];let firstError:{error:string;path:string}|undefined;
    for(const {filename} of sized.slice(0,64)) {
      const result=await this.read(filename);
      if(result.error){firstError ||= {error:result.error,path:filename};continue;}
      const source=classifyPromptOrigin(row,result.metadata,{source_path:filename,source_revision:result.revision});
      if(source.origin_evidence.reason==='session_identity_mismatch'){firstError ||= {error:'session_identity_mismatch',path:filename};continue;}
      prepared.push({filename,result,source});
    }
    for(const shallow of (explicit===undefined || explicit===null ? [true,false] : [false])) {
      if(found.length || [...progress.values()].some(item=>item.witness_revalidation==='source_changed'))break;
      for(const {filename,result,source} of prepared) {
      let witnesses:any[]=[],scan:any={state:'unavailable'};
      try {const resolved=await realpath(filename),indexed=await nativeTurnWitnesses(resolved,result.revision!,row,this.budget,shallow);witnesses=indexed.matches;scan=indexed.scan;}catch{}
      progress.set(filename,scan);
      for(const witness of witnesses)found.push({...source,origin_evidence:{...source.origin_evidence,...Object.fromEntries(['native_turn_id','native_message_id','message_sha256','normalized_prompt_sha256','source_byte_offset'].map(key=>[key,witness[key]])),boundary:'native_session_and_exact_user_message_turn'}});
      }
    }
    if([...progress.values()].some(item=>item.witness_revalidation==='source_changed'))found.length=0;
    const identities=new Set(found.map(value=>JSON.stringify([value.origin_evidence.native_session_id,value.origin_kind])));
    if(identities.size>1)return classifyPromptOrigin(row,null,{error:'native_prompt_source_conflict'});
    if(found.length)return found[0];
    if(firstError && !progress.size)return classifyPromptOrigin(row,null,{source_path:firstError.path,error:firstError.error});
    const scans=[...progress.values()];
    const reason=scans.some(item=>item.witness_revalidation==='source_changed')?'native_prompt_source_changed':scans.some(item=>item.witness_revalidation==='budget_exhausted')?'native_prompt_source_scan_incomplete':scans.length && scans.every(item=>item.state==='complete')?'native_prompt_message_not_found':'native_prompt_source_scan_incomplete';
    const value:any=classifyPromptOrigin(row,null,{error:reason});
    value.origin_evidence.verification_progress={candidate_files:Math.min(sized.length,64),complete_files:scans.filter(item=>item.state==='complete').length,indexed_prefix_bytes:scans.reduce((sum,item)=>sum+(item.indexed_prefix_bytes || 0),0),source_bytes:scans.reduce((sum,item)=>sum+(item.source_bytes || 0),0)};
    return value;
  }
  async resolveRows(rows:any[],options:{pageOffset?:number;pageLimit?:number;host?:string;queryText?:string;promptSource?:string}={}) {
    const peek=new Map<string,any>(),priorities:any[]=[];
    for(let position=0;position<rows.length;position++){
      const row=rows[position],filename=row.transcript_path;let hint='unknown';
      if(typeof filename==='string' && filename && !filename.includes('\u0000')){
        if(!peek.has(filename))peek.set(filename,await this.read(filename));
        const header=peek.get(filename);if(!header.error)hint=classifyPromptOrigin(row,header.metadata).origin_kind;
      }
      const nativePath=typeof filename==='string' && filename && !filename.includes('\u0000') ? await realpath(filename).catch(()=>filename) : undefined;
      const indexed=hasIndexedPrompt(row,nativePath);
      priorities.push({position,row,indexed,hint});
    }
    const terms=String(options.queryText || '').split(/\s+/).filter(Boolean),host=String(options.host || 'all').toLowerCase(),source=String(options.promptSource || 'natural').toLowerCase();
    const eligible=priorities.filter(entry=>(host==='all' || String(entry.row.host_id || 'codex').toLowerCase()===host) && (!terms.length || terms.every(term=>entry.row.prompt_preview.includes(term))) &&
      (!['natural','human'].includes(source) || entry.hint==='human' || entry.hint==='unknown' && !entry.row.transcript_path));
    const offset=Math.max(0,options.pageOffset || 0),limit=Math.max(1,Math.min(50,options.pageLimit || 20)),pagePositions=new Set(eligible.slice(offset,offset+limit).map(entry=>entry.position));
    for(const entry of priorities)entry.priority=pagePositions.has(entry.position)?(entry.indexed?-2:-1):entry.indexed?0:entry.hint==='human'?1:entry.hint==='unknown' && !entry.row.transcript_path?2:3;
    this.budget.deadline=Date.now()+1500;
    const reserve=this.budget.bytesLeft>=2*512*1024 ? 512*1024 : 0;this.budget.bytesLeft-=reserve;
    priorities.sort((a,b)=>a.priority-b.priority || a.position-b.position);
    const result:any[]=[];for(const {position,row} of priorities)result[position]={...row,...await this.resolve(row)};
    this.budget.bytesLeft+=reserve;
    if(reserve){
      const paths:string[]=[];
      for(const entry of [...priorities].sort((a,b)=>Number(!pagePositions.has(a.position))-Number(!pagePositions.has(b.position)) || a.position-b.position)){
        const value=result[entry.position],filename=value.origin_evidence?.source_path;
        if(value.origin_status==='verified' && typeof filename==='string' && !paths.includes(filename))paths.push(filename);
      }
      for(const filename of paths){const header=await this.read(filename);if(header.error)continue;const native=await realpath(filename);if(await advanceIndexedSource(native,header.revision!,this.budget,reserve))break;}
    }
    return result;
  }
}
