import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { NextRequest } from 'next/server';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { appendFile,readFile } from 'node:fs/promises';

let root: string;
let get: typeof import('@/app/api/evolving-profile/guidance/[resource]/route').GET;
beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'ep-origin-fallback-'));
  vi.stubEnv('EVOLVING_PROFILE_STATE_ROOT', root);
  vi.stubEnv('EVOLVING_PROFILE_HOST_SESSIONS_ROOT', path.join(root, 'sessions'));
  vi.resetModules();
  // Only the external unavailable service is replaced; local files,
  // metadata attribution, receipt joins and pagination execute normally.
  vi.stubGlobal('fetch', async () => { throw new Error('status unavailable'); });
  await mkdir(path.join(root, 'audit'), { recursive: true });
  await mkdir(path.join(root, 'sessions'), { recursive: true });
  get = (await import('@/app/api/evolving-profile/guidance/[resource]/route')).GET;
});
afterEach(async () => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  await rm(root, { recursive: true, force: true });
});
async function ingress(id: string, metadata: any, prompt = '继续解释机制') {
  const filename = path.join(root, 'sessions', `rollout-day-${id}.jsonl`);
  if (metadata) await writeFile(filename, JSON.stringify({ type: 'session_meta', payload: metadata }) + '\n'+JSON.stringify({type:'response_item',payload:{type:'message',role:'user',id:`msg-${id}`,internal_chat_message_metadata_passthrough:{turn_id:id,content_item_kinds:['user.text']},content:[{type:'input_text',text:prompt}]}})+'\n');
  return { at: '2026-10-09T00:00:00Z', source: 'codex-userpromptsubmit', host_id: 'codex',
    session_id: id, turn_id: id, hook_invocation_id: id, prompt_fingerprint: id,
    transcript_path: metadata ? filename : undefined, prompt_origin: 'user_direct', prompt_preview: prompt };
}
async function list(query = '') {
  const response = await get(new NextRequest(`http://localhost/api/evolving-profile/guidance/prompts${query}`), { params: Promise.resolve({ resource: 'prompts' }) });
  expect(response.status).toBe(200);
  return response.json();
}
function primary(query = '') {
  const parameters = Object.fromEntries(new URLSearchParams(query.replace(/^\?/, '')));
  const repository = fileURLToPath(new URL('../../../', import.meta.url));
  const script = `import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('origin_parity',Path(sys.argv[1])/'status/evolving_profile_status_server.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m._hook_output_rows=lambda:[]
m.guidance_delivery_list=lambda *a,**kw:{}
m.research_snapshot=lambda *a,**kw:{}
p=json.loads(sys.argv[2])
print(json.dumps(m.guidance_prompt_list(limit=p.get('limit',20),cursor=p.get('cursor','0'),host=p.get('host','all'),query_text=p.get('q',''),prompt_source=p.get('prompt_source','natural'))))`;
  return JSON.parse(execFileSync('python3', ['-c', script, repository, JSON.stringify(parameters)], { encoding: 'utf8', env: { ...process.env,
    EVOLVING_PROFILE_RUNTIME_ROOT: repository, EVOLVING_PROFILE_HOST_ARCHIVED_SESSIONS_ROOT: path.join(root, 'archived-sessions') } }));
}
function originProjection(value: any) {
  return { total: value.total, natural_total: value.natural_total, audit_total: value.audit_total, source_counts: value.source_counts,
    statistics_denominator: value.statistics_denominator, next_cursor: value.next_cursor,
    items: value.items.map((row: any) => ({ at: row.at, user_prompt: row.user_prompt, origin_kind: row.origin_kind, origin_status: row.origin_status, origin_evidence: row.origin_evidence })) };
}
it('fallback defaults to verified humans while keeping background and unknown audit populations', async () => {
  const rows = [
    await ingress('human', { id: 'human', source: 'vscode', thread_source: 'user' }, 'Memory Writing Agent: Phase 2 和 test probe 是什么意思？'),
    await ingress('child', { id: 'child', source: { subagent: { thread_spawn: { parent_thread_id: 'parent', depth: 1 } } }, thread_source: 'subagent' }),
    await ingress('unknown', null),
    await ingress('voice', { id: 'voice', source: 'vscode', thread_source: 'realtime_voice' }, '嗯，然后呢？'),
  ];
  await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), rows.map(row => JSON.stringify(row)).join('\n') + '\n');
  const natural = await list('?limit=1');
  expect(natural).toMatchObject({ total: 2, count: 1, audit_total: 4, natural_total: 2, statistics_denominator: 2, source_filter: 'natural', has_more: true, next_cursor: '1' });
  expect(natural.items[0]).toMatchObject({ origin_kind: 'human', origin_status: 'verified' });
  const second = await list('?limit=1&cursor=1');
  expect(second.items[0]).toMatchObject({ origin_kind: 'human', user_prompt: '嗯，然后呢？' });
  expect(second.next_cursor).toBeNull();
  expect((await list('?prompt_source=all')).items).toHaveLength(4);
  expect((await list('?prompt_source=unknown')).items[0].origin_kind).toBe('unknown');
  expect((await list('?prompt_source=subagent')).items[0].origin_kind).toBe('subagent');
  expect((await list('?cursor=9999')).items).toEqual([]);
});
it('fallback checks metadata revision anew and does not trust ingress user_direct or forged aliases', async () => {
  const row = await ingress('changed', { id: 'changed', source: 'vscode', thread_source: 'user' });
  await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), JSON.stringify(row) + '\n');
  const before = await list();
  expect(before.total).toBe(1);
  await writeFile(row.transcript_path!, JSON.stringify({ type: 'session_meta', payload: { id: 'foreign', session_id: 'changed', source: 'vscode', thread_source: 'user' } }) + '\n');
  const after = await list('?prompt_source=all');
  expect(after.items[0]).toMatchObject({ origin_kind: 'unknown', origin_evidence: { reason: 'session_identity_mismatch' } });
  expect(after.items[0].origin_evidence.source_revision).not.toBe(before.items[0].origin_evidence.source_revision);
  expect((await list()).total).toBe(0);
});
it('fallback tolerates invalid audit lines and headers, duplicate records, empty and invalid filters', async () => {
  const human = await ingress('human', { id: 'human', source: 'vscode', thread_source: 'user' });
  const bad = await ingress('bad', { id: 'bad' });
  await writeFile(bad.transcript_path!, 'null\n');
  await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), [human, human, null, [], bad].map(row => JSON.stringify(row)).join('\n') + '\n');
  const audit = await list('?prompt_source=all&limit=invalid');
  expect(audit.total).toBe(2);
  expect(audit.source_counts).toMatchObject({ human: 1, unknown: 1 });
  expect((await list('?prompt_source=bogus')).source_filter).toBe('natural');
  await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), '');
  expect((await list()).items).toEqual([]);
});
it('primary and fallback select the latest occurrence before classification, host and query filters', async () => {
  const valid = await ingress('same', { id: 'same', source: 'vscode', thread_source: 'user' }, 'new wording');
  const old = { ...valid, at: '2026-10-09T00:00:00Z', host_id: 'old-host', prompt_preview: 'old wording', transcript_path: path.join(root, 'sessions', 'absent.jsonl') };
  const newer = { ...valid, at: '2026-10-09T00:00:01Z', host_id: 'new-host' };
  for (const { rows, naturalTotal } of [{ rows: [old, newer], naturalTotal: 1 }, { rows: [{ ...old, transcript_path: valid.transcript_path }, { ...newer, transcript_path: old.transcript_path }], naturalTotal: 0 }]) {
    await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), rows.map(row => JSON.stringify(row)).join('\n') + '\n');
    for (const query of ['?prompt_source=all', '', '?host=old-host&prompt_source=all', '?q=old&prompt_source=all', '?q=new&prompt_source=all', '?host=new-host&prompt_source=all']) {
      const main = primary(query), fallback = await list(query);
      expect(originProjection(main)).toEqual(originProjection(fallback));
      if (query.includes('old')) expect(main.audit_total).toBe(0);
      else {
        expect(main.natural_total).toBe(naturalTotal);
        if (main.items.length) expect(main.items[0].at).toBe('2026-10-09T00:00:01Z');
      }
    }
  }
});
it('primary and fallback preserve missing identities and separate sessions, with last append winning equal timestamps', async () => {
  const first = await ingress('same', { id: 'same', source: 'vscode', thread_source: 'user' }, 'first');
  const last = { ...first, prompt_preview: 'last', transcript_path: path.join(root, 'sessions', 'absent.jsonl') };
  const other = await ingress('other', { id: 'other', source: 'vscode', thread_source: 'user' }, 'other session');
  const noIdentity = { ...first, hook_invocation_id: undefined };
  await writeFile(other.transcript_path!,JSON.stringify({type:'session_meta',payload:{id:'other',source:'vscode',thread_source:'user'}})+'\n'+JSON.stringify({type:'response_item',payload:{type:'message',role:'user',id:'other-message',internal_chat_message_metadata_passthrough:{turn_id:first.turn_id},content:[{type:'input_text',text:other.prompt_preview}]}})+'\n');
  await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), [first, last, { ...other, turn_id: first.turn_id, hook_invocation_id: first.hook_invocation_id }, noIdentity, noIdentity].map(row => JSON.stringify(row)).join('\n') + '\n');
  const main = primary('?prompt_source=all'), fallback = await list('?prompt_source=all');
  expect(originProjection(main)).toEqual(originProjection(fallback));
  expect(main).toMatchObject({ audit_total: 4, natural_total: 3 });
  expect(main.items[0]).toMatchObject({ user_prompt: 'last', origin_kind: 'unknown' });
});
it('primary and fallback keep non-string and invalid paths unknown without losing a valid human row', async () => {
  const human = await ingress('human', { id: 'human', source: 'vscode', thread_source: 'user' });
  const values = [[], ['bad'], 5, {}, '', 'bad\u0000path', path.join(root, 'outside.jsonl')];
  for (const value of values) {
    const bad = { ...human, session_id: 'bad', hook_invocation_id: 'bad', transcript_path: value };
    await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), [human, bad].map(row => JSON.stringify(row)).join('\n') + '\n');
    const main = primary('?prompt_source=all'), fallback = await list('?prompt_source=all');
    expect(originProjection(main)).toEqual(originProjection(fallback));
    expect(main).toMatchObject({ natural_total: 1, audit_total: 2 });
    expect(main.items[1].origin_kind).toBe('unknown');
  }
});
it('primary and fallback order UTC offsets and microseconds before selecting and paginating occurrences', async () => {
  const valid = await ingress('same', { id: 'same', source: 'vscode', thread_source: 'user' });
  const missing = path.join(root, 'sessions', 'absent.jsonl');
  for (const { latest, earlier } of [
    { latest: '2026-10-09T09:00:00+08:00', earlier: '2026-10-09T00:30:00Z' },
    { latest: '2026-10-09T00:00:00.123999Z', earlier: '2026-10-09T00:00:00.123001Z' },
    { latest: '2026-10-09T00:00:00Z', earlier: '2026-02-30T00:00:00Z' },
  ]) {
    const other = await ingress('other', { id: 'other', source: 'vscode', thread_source: 'user' });
    await writeFile(path.join(root, 'audit/prompt-ingress.jsonl'), [{ ...valid, at: latest }, { ...valid, at: earlier, transcript_path: missing }, { ...other, at: '2026-10-08T00:00:00Z' }].map(row => JSON.stringify(row)).join('\n') + '\n');
    const main = primary('?limit=1'), fallback = await list('?limit=1');
    expect(originProjection(main)).toEqual(originProjection(fallback));
    expect(main).toMatchObject({ natural_total: 2, audit_total: 2, total: 2, next_cursor: '1' });
    expect(main.items[0].at).toBe(latest);
    const second = primary('?limit=1&cursor=1');
    expect(originProjection(second)).toEqual(originProjection(await list('?limit=1&cursor=1')));
    expect(second.next_cursor).toBeNull();
  }
});
it('primary and fallback bind an unpathed Prompt to its actual child turn rather than a human parent header', async () => {
  await ingress('parent',{id:'parent',source:'vscode',thread_source:'user'},'human request');
  const child=await ingress('child',{id:'child',session_id:'parent',parent_thread_id:'parent',source:{subagent:{thread_spawn:{parent_thread_id:'parent',depth:1}}},thread_source:'subagent'},'dispatched request');
  const row={...child,session_id:'parent',transcript_path:null};
  await writeFile(path.join(root,'audit/prompt-ingress.jsonl'),JSON.stringify(row)+'\n');
  const main=primary('?prompt_source=all'),fallback=await list('?prompt_source=all');
  expect(originProjection(main)).toEqual(originProjection(fallback));
  expect(main).toMatchObject({natural_total:0,audit_total:1});
  expect(main.items[0]).toMatchObject({origin_kind:'subagent',origin_evidence:{native_session_id:'child'}});
});
it('primary and fallback reject header-only and tool-argument-only attribution while preserving true voice and quoted user turns', async () => {
  const parent=await ingress('parent',{id:'parent',source:'vscode',thread_source:'user'},'true human');
  const voice=await ingress('voice',{id:'voice',source:'vscode',thread_source:'realtime_voice'},'继续解释 spawn_agent 的引用例子');
  const missing={...parent,turn_id:'missing',prompt_preview:'dispatched request'};
  await writeFile(parent.transcript_path!,JSON.stringify({type:'session_meta',payload:{id:'parent',source:'vscode',thread_source:'user'}})+'\n'+JSON.stringify({type:'event_msg',payload:{type:'item_completed',turn_id:'missing',item:{type:'CollabAgentToolCall',tool:'spawn_agent',prompt:'dispatched request'}}})+'\n');
  await writeFile(path.join(root,'audit/prompt-ingress.jsonl'),[missing,voice].map(row=>JSON.stringify(row)).join('\n')+'\n');
  const main=primary('?prompt_source=all'),fallback=await list('?prompt_source=all');
  expect(originProjection(main)).toEqual(originProjection(fallback));
  expect(main).toMatchObject({natural_total:1,audit_total:2});
  expect(main.items.map((row:any)=>row.origin_kind)).toEqual(['unknown','human']);
});
it('both implementations bind Page/environment envelopes and reject conflicting native sources for one turn', async () => {
  const prompt='请解释这段引用：spawn_agent Memory Writing Agent';
  const human=await ingress('human',{id:'human',source:'vscode',thread_source:'user'},prompt);
  const lines=(await readFile(human.transcript_path!,'utf8')).trim().split('\n');
  const message=JSON.parse(lines[1]);message.payload.content[0].text='<environment_context>host setup</environment_context>\n<external_codex_apps_open_page>{"page_id":"page"}</external_codex_apps_open_page>\n## My request for Codex:\n'+prompt;
  await writeFile(human.transcript_path!,lines[0]+'\n'+JSON.stringify(message)+'\n');
  await writeFile(path.join(root,'audit/prompt-ingress.jsonl'),JSON.stringify(human)+'\n');
  const main=primary(),fallback=await list();expect(originProjection(main)).toEqual(originProjection(fallback));expect(main.natural_total).toBe(1);
  const child=await ingress('child',{id:'child',session_id:'human',parent_thread_id:'human',source:{subagent:{thread_spawn:{parent_thread_id:'human',depth:1}}},thread_source:'subagent'},prompt);
  const childLines=(await readFile(child.transcript_path!,'utf8')).trim().split('\n'),childMessage=JSON.parse(childLines[1]);childMessage.payload.internal_chat_message_metadata_passthrough.turn_id='human';
  await writeFile(child.transcript_path!,childLines[0]+'\n'+JSON.stringify(childMessage)+'\n');
  await writeFile(path.join(root,'audit/prompt-ingress.jsonl'),JSON.stringify({...human,transcript_path:null})+'\n');
  const conflict=primary('?prompt_source=all');expect(originProjection(conflict)).toEqual(originProjection(await list('?prompt_source=all')));
  expect(conflict.items[0]).toMatchObject({origin_kind:'unknown',origin_evidence:{reason:'native_prompt_source_conflict'}});
});
it('append retains the existing witness index and large non-user records cannot trap its cursor', async () => {
  const {PromptOriginResolver}=await import('@/lib/prompt-origin.server');
  const row=await ingress('human',{id:'human',source:'vscode',thread_source:'user'});
  await appendFile(row.transcript_path!,JSON.stringify({type:'response_item',payload:{type:'function_call_output',output:'x'.repeat(1200000)}})+'\n');
  expect((await new PromptOriginResolver([path.join(root,'sessions')]).resolve(row)).origin_kind).toBe('human');
  await appendFile(row.transcript_path!,JSON.stringify({type:'event_msg',payload:{type:'task_complete',turn_id:'human'}})+'\n');
  const warm=new PromptOriginResolver([path.join(root,'sessions')]);expect((await warm.resolve(row)).origin_kind).toBe('human');
  expect(12*1024*1024-(warm as any).budget.bytesLeft).toBeGreaterThan(0);
  expect(12*1024*1024-(warm as any).budget.bytesLeft).toBeLessThan(4096);
  let result:any;for(let i=0;i<4;i++)result=await new PromptOriginResolver([path.join(root,'sessions')]).resolve({...row,turn_id:'missing'});
  expect(result.origin_evidence).toMatchObject({reason:'native_prompt_message_not_found',verification_progress:{complete_files:1}});
});
it('verified full message fingerprint preserves a real user when the 600-character preview ends on whitespace', async () => {
  const text='a'.repeat(599)+' tail';
  const row=await ingress('human',{id:'human',source:'vscode',thread_source:'user'},text);
  row.prompt_preview=text.slice(0,600);row.prompt_fingerprint=createHash('sha256').update(text).digest('hex').slice(0,16);
  await writeFile(path.join(root,'audit/prompt-ingress.jsonl'),JSON.stringify(row)+'\n');
  const main=primary(),fallback=await list();expect(originProjection(main)).toEqual(originProjection(fallback));expect(main.natural_total).toBe(1);
});
it('in-place message rewrite plus growth cannot reuse a cached author proof in either live resolver', async () => {
  const {PromptOriginResolver}=await import('@/lib/prompt-origin.server');
  const row=await ingress('human',{id:'human',source:'vscode',thread_source:'user'},'original request');
  row.prompt_fingerprint=createHash('sha256').update(row.prompt_preview).digest('hex').slice(0,16);
  const original=await new PromptOriginResolver([path.join(root,'sessions')]).resolve(row);expect(original.origin_kind).toBe('human');
  const lines=(await readFile(row.transcript_path!,'utf8')).trim().split('\n'),message=JSON.parse(lines[1]);message.payload.content[0].text='different request';
  await writeFile(row.transcript_path!,lines[0]+'\n'+JSON.stringify(message)+'\n'+JSON.stringify({type:'event_msg',payload:{type:'token_count'}})+'\n');
  const changed=await new PromptOriginResolver([path.join(root,'sessions')]).resolve(row);expect(changed.origin_kind).toBe('unknown');expect(changed.origin_evidence.message_sha256).not.toBe(original.origin_evidence.message_sha256);
});
it('truncated long and complete short message boundaries require exact full evidence in primary and fallback', async () => {
  for(const text of ['short complete request','x'.repeat(600)+' native suffix']) {
    const identity=text.length<=600?'short':'long';
    const row=await ingress(identity,{id:identity,source:'vscode',thread_source:'user'},text);
    for(const {fingerprint,want} of [
      {fingerprint:undefined,want:text.length<=600?'human':'unknown'},
      {fingerprint:'legacy',want:text.length<=600?'human':'unknown'},
      {fingerprint:'0'.repeat(16),want:'unknown'},
      {fingerprint:createHash('sha256').update(text).digest('hex').slice(0,16),want:'human'},
    ]) {
      await writeFile(path.join(root,'audit/prompt-ingress.jsonl'),JSON.stringify({...row,prompt_preview:text.slice(0,600),prompt_fingerprint:fingerprint})+'\n');
      const main=primary('?prompt_source=all'),fallback=await list('?prompt_source=all');expect(originProjection(main)).toEqual(originProjection(fallback));expect(main.items[0].origin_kind).toBe(want);
    }
  }
});
it('bounded window scans make durable in-memory progress after hundreds of unscanned sources', async () => {
  const {PromptOriginResolver}=await import('@/lib/prompt-origin.server');
  const row=await ingress('big',{id:'big',source:'vscode',thread_source:'user'},'target native request');
  const lines=(await readFile(row.transcript_path!,'utf8')).trim().split('\n'),filler=JSON.stringify({type:'response_item',payload:{type:'function_call_output',output:'x'.repeat(2000)}})+'\n';
  await writeFile(row.transcript_path!,lines[0]+'\n'+filler.repeat(700)+lines[1]+'\n'+filler.repeat(700));
  const other=[];for(let i=0;i<140;i++)other.push(await ingress(`other-${i}`,{id:`other-${i}`,source:'vscode',thread_source:'user'},'different question'));
  let found=false;const prefixes:number[]=[];
  for(let i=0;i<8;i++){
    const resolver=new PromptOriginResolver([path.join(root,'sessions')]);(resolver as any).budget.bytesLeft=512*1024;
    const result=(await resolver.resolveRows([row,...other]))[0];
    expect(512*1024-(resolver as any).budget.bytesLeft).toBeLessThanOrEqual(512*1024);
    if(result.origin_kind==='human'){found=true;break;}
    prefixes.push(result.origin_evidence.verification_progress.indexed_prefix_bytes);
  }
  expect(found).toBe(true);expect(Math.max(...prefixes)).toBeGreaterThan(Math.min(...prefixes));
});
it('known raw locators are freshly verified before unresolved scans spend the shared budget', async () => {
  const {PromptOriginResolver}=await import('@/lib/prompt-origin.server');
  const proven=await ingress('proven',{id:'proven',source:'vscode',thread_source:'user'},'already verified');
  expect((await new PromptOriginResolver([path.join(root,'sessions')]).resolve(proven)).origin_kind).toBe('human');
  const blocked=await ingress('blocked',{id:'blocked',source:'vscode',thread_source:'user'},'not yet indexed');
  const lines=(await readFile(blocked.transcript_path!,'utf8')).trim().split('\n'),filler=JSON.stringify({type:'response_item',payload:{type:'function_call_output',output:'x'.repeat(2000)}})+'\n';
  await writeFile(blocked.transcript_path!,lines[0]+'\n'+filler.repeat(700)+lines[1]+'\n'+filler.repeat(700));
  for(let i=0;i<3;i++){
    const resolver=new PromptOriginResolver([path.join(root,'sessions')]);(resolver as any).budget.bytesLeft=4096;
    const result=await resolver.resolveRows([blocked,proven]);expect(result[1].origin_kind).toBe('human');
    expect(4096-(resolver as any).budget.bytesLeft).toBeLessThanOrEqual(4096);
  }
});
it('known page proofs leave an unfinished source advancing once per bounded window', async () => {
  const {PromptOriginResolver}=await import('@/lib/prompt-origin.server');
  const row=await ingress('first',{id:'first',source:'vscode',thread_source:'user'},'first page proof');
  const lines=(await readFile(row.transcript_path!,'utf8')).trim().split('\n'),filler=JSON.stringify({type:'response_item',payload:{type:'function_call_output',output:'x'.repeat(2000)}})+'\n',later=JSON.parse(lines[1]);
  later.payload.id='msg-later';later.payload.internal_chat_message_metadata_passthrough.turn_id='later';later.payload.content[0].text='later page proof';
  await writeFile(row.transcript_path!,lines[0]+'\n'+lines[1]+'\n'+filler.repeat(700)+JSON.stringify(later)+'\n'+filler.repeat(700));
  for(let i=0;i<4;i++)expect((await new PromptOriginResolver([path.join(root,'sessions')]).resolveRows([row]))[0].origin_kind).toBe('human');
  const lookup=new PromptOriginResolver([path.join(root,'sessions')]);(lookup as any).budget.bytesLeft=4096;
  expect((await lookup.resolve({...row,turn_id:'later',prompt_preview:'later page proof',prompt_fingerprint:undefined})).origin_kind).toBe('human');
});
it('current page candidates receive lookup budget before an unresolved earlier occurrence', async () => {
  const {PromptOriginResolver}=await import('@/lib/prompt-origin.server');
  const blocked=await ingress('blocked',{id:'blocked',source:'vscode',thread_source:'user'},'earlier row');
  const lines=(await readFile(blocked.transcript_path!,'utf8')).trim().split('\n'),filler=JSON.stringify({type:'response_item',payload:{type:'function_call_output',output:'x'.repeat(2000)}})+'\n';
  await writeFile(blocked.transcript_path!,lines[0]+'\n'+filler.repeat(700)+lines[1]+'\n'+filler.repeat(700));
  const page=await ingress('page',{id:'page',source:'vscode',thread_source:'user'},'current page user');
  const resolver=new PromptOriginResolver([path.join(root,'sessions')]);(resolver as any).budget.bytesLeft=4096;
  const result=await resolver.resolveRows([blocked,page],{pageOffset:1,pageLimit:1,promptSource:'all'});
  expect(result[1].origin_kind).toBe('human');expect(4096-(resolver as any).budget.bytesLeft).toBeLessThanOrEqual(4096);
});
