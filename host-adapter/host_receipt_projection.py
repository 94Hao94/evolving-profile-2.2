"""Read-only, bounded projection of actual PostToolUse response observations.

This observes a host hook boundary, not model attention or untruncated context.
Never infer a receipt from a user message, shell echo or server stdout.
"""
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

_NATIVE_CONTEXT_CACHE = {}

def read_native_context_receipts(path, session_id, deadline_seconds=.6):
    """Incrementally read host-authored developer messages, never Hook claims.

    User/tool/assistant text cannot establish receipt. A native turn ID and
    the session header bind every observation; truncation is reported rather
    than treating a linked full-output file as model-visible context.
    """
    path=Path(path)
    try:
        stat=path.stat()
        with path.open('rb') as stream:
            header=json.loads(stream.readline())
        if header.get('type')!='session_meta' or (header.get('payload') or {}).get('id')!=session_id:return {}
        key=str(path)
        cache=_NATIVE_CONTEXT_CACHE.get(key)
        if cache is None or cache['offset']>stat.st_size or cache['inode']!=stat.st_ino:
            cache={'offset':0,'inode':stat.st_ino,'turn':None,'receipts':{}}
            _NATIVE_CONTEXT_CACHE[key]=cache
        started=time.monotonic()
        with path.open('rb') as stream:
            stream.seek(cache['offset'])
            while time.monotonic()-started<deadline_seconds:
                offset=stream.tell();raw=stream.readline()
                if not raw or not raw.endswith(b'\n'):break
                cache['offset']=stream.tell()
                value=json.loads(raw);payload=value.get('payload') or {}
                if value.get('type')=='event_msg' and payload.get('type')=='task_started':
                    cache['turn']=payload.get('turn_id')
                if value.get('type')=='event_msg' and payload.get('type')=='task_complete' and payload.get('turn_id')==cache['turn'] and cache['turn'] not in cache['receipts']:
                    cache['receipts'][cache['turn']]={'state':'completed_without_new_memory_packet','session_id':session_id,'turn_id':cache['turn'],
                        'visible_record_count':0,'visible_record_ids':[],'truncated':False,'observed_at':value.get('timestamp'),
                        'completion_record_sha256':hashlib.sha256(raw).hexdigest(),'transcript_byte_offset':offset,
                        'boundary':'completed_native_turn_no_new_hindsight_packet','model_attention':'not_measured'}
                elif value.get('type')=='turn_context' and payload.get('turn_id'):
                    cache['turn']=payload['turn_id']
                if value.get('type')!='response_item' or payload.get('type')!='message' or payload.get('role')!='developer' or not cache['turn']:continue
                text='\n'.join(c.get('text','') for c in payload.get('content') or [] if isinstance(c,dict))
                if '<evolving_profile_memories>' not in text or '<evolving_profile_memory_packet' not in text:continue
                ids=list(dict.fromkeys(re.findall(r'claim=([A-Za-z0-9:_-]+)',text)))
                cache['receipts'][cache['turn']]={
                    'state':'native_context_observed','session_id':session_id,'turn_id':cache['turn'],
                    'observed_at':value.get('timestamp'),'visible_record_ids':ids,'visible_record_count':len(ids),
                    'truncated':'Warning: truncated output' in text.split('<evolving_profile_memories>',1)[0] or '</evolving_profile_memory_packet>' not in text,
                    'context_sha256':hashlib.sha256(text.encode()).hexdigest(),'transcript_byte_offset':offset,
                    'boundary':'native_developer_message','model_attention':'not_measured',
                }
                if len(cache['receipts'])>200:cache['receipts'].pop(next(iter(cache['receipts'])))
        return dict(cache['receipts'])
    except (OSError,ValueError,TypeError):return {}

_CONTROLLER_NAMES = {'evolving_profile_controller', 'hindsight_controller'}
TOOLS={f'mcp__{controller}__{operation}' for controller in _CONTROLLER_NAMES
       for operation in ('user_recall','user_research','user_preference','agent_recall','agent_research','recall','research','read_research','get_preference')}
READ_TOOLS=TOOLS | {f'mcp__{controller}__{operation}' for controller in _CONTROLLER_NAMES
                    for operation in ('read_source','find_sources')}


def resolve_tool_response(source, capture_path):
    """Read a legacy inline response or a hash-verified archived response."""
    if not isinstance(source, dict):
        return None
    if source.get('tool_response') is not None:
        return source.get('tool_response')
    if not isinstance(source.get('tool_response_archive'), dict):
        return None
    try:
        from ham.adapter import read_archived_tool_response

        return read_archived_tool_response(source, Path(capture_path).parent)
    except (OSError, ValueError, TypeError, ImportError):
        return None


def summarize_tools(rows):
    unique, duplicates = {}, 0
    for index, row in enumerate(rows):
        key = row.get('call_id') or ('unbound', index)
        if key in unique:
            duplicates += 1
        else:
            unique[key] = row
    rows = list(unique.values())
    recognized = [r for r in rows if r.get('response_recognized') and not r.get('failed')]
    failed = sum(bool(r.get('failed')) for r in rows)
    unparsed = sum(not r.get('failed') and not r.get('response_recognized') for r in rows)
    ids = list(dict.fromkeys(mid for r in recognized for mid in (r.get('record_ids') or [])))
    status = ('partial_tool_failure' if recognized and failed else 'tool_failed' if failed else
              'partial_unparsed' if recognized and unparsed else 'tool_return_unparsed' if unparsed else
              'observed' if recognized else 'not_observed')
    return {'status':status, 'tool_calls':len(rows), 'failed_calls':failed, 'unparsed_calls':unparsed,
            'successful_calls':len(recognized), 'record_count':len(ids) if recognized else None,
            'record_ids':ids, 'tools':list(dict.fromkeys(r.get('tool') for r in rows if r.get('tool'))),
            'duplicate_call_ids':duplicates, 'records':[], 'calls':rows,
            'boundary':'host_PostToolUse_tool_response', 'model_attention':'not_measured'}


def response_records(response):
    """Extract only recognized MCP response records, never shell text or queries."""
    if not isinstance(response, dict) or response.get('isError'):
        return []
    records = []
    for block in response.get('content') or []:
        if not isinstance(block, dict) or block.get('type') != 'text':
            continue
        try:
            value = json.loads(block.get('text', ''))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get('mode') == 'official_discovery_evidence_only':
            records.extend({'id':r['id'], 'text':r.get('text',''), 'type':r.get('type'),
                            'document_id':r.get('document_id')} for r in value.get('memories',[])
                           if r.get('id') and r.get('state') == 'valid')
        elif 'source' in value and (value.get('memory') or {}).get('id'):
            memory, source = value['memory'], value.get('source') or {}
            records.append({'id':memory['id'], 'text':source.get('content') or source.get('text') or memory.get('text',''),
                            'type':'source', 'document_id':memory.get('document_id')})
        elif value.get('mode') == 'literal_original_source_search':
            records.extend({'id':r['anchor_memory_id'], 'text':r.get('excerpt') or r.get('text',''),
                            'type':'source'} for r in value.get('items',[]) if r.get('anchor_memory_id'))
    return records


def response_navigation(response):
    """Project only returned source locators, never their bodies or discovery totals."""
    if not isinstance(response, dict) or response.get('isError'):
        return []
    rows, seen = [], set()
    for block in response.get('content') or []:
        if not isinstance(block, dict) or block.get('type') != 'text':
            continue
        try: value = json.loads(block.get('text', ''))
        except (ValueError, TypeError): continue
        if not isinstance(value, dict): continue
        for row in value.get('source_navigation') or []:
            if not isinstance(row, dict) or not isinstance(row.get('memory_id'), str) or not row['memory_id'] or row['memory_id'] in seen:
                continue
            witness = row.get('scope_verification') or {}
            scope = row.get('scope_status') or (witness.get('status') if isinstance(witness,dict) else None)
            if (row.get('permission_status') in {'denied','blocked'} or scope in {'denied','mismatch'}
                or row.get('hard_scope_match') is False or row.get('state') in {'invalidated','withdrawn'}):
                continue
            seen.add(row['memory_id'])
            locator = {key:row[key] for key in ('memory_id','document_id','chunk_id','source_revision','subject_relation','claim_verification','authority') if isinstance(row.get(key),str)}
            action = row.get('next_action') if isinstance(row.get('next_action'),dict) else {}
            arguments = action.get('arguments') if isinstance(action.get('arguments'),dict) else {}
            if action.get('tool') == 'read_source' and arguments.get('memory_id') == row['memory_id']:
                locator['next_action'] = {'tool':'read_source','arguments':{key:arguments[key] for key in ('memory_id','scope') if isinstance(arguments.get(key),str)}}
            rows.append(locator)
    return rows


def _load_ingress_identity(session, hook_invocation_id, prompt_fingerprint=None, ingress_path=None):
    """Resolve a Controller row that omitted turn_id to one native ingress.

    The match is deliberately identity-first. Fingerprint/time fallback is
    used only when the Controller row has no hook invocation id and exactly one
    nearby ingress exists, preventing same-word turns from being merged.
    """
    path = Path(ingress_path or Path.home()/'.evolving-profile/audit/prompt-ingress.jsonl')
    if not path.is_file() or not session:
        return None
    matches = []
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream.readlines()[-500:]:
                try: row = json.loads(line)
                except (ValueError, TypeError): continue
                if row.get('session_id') != session: continue
                if hook_invocation_id and row.get('hook_invocation_id') == hook_invocation_id:
                    return row
                if prompt_fingerprint and row.get('prompt_fingerprint') == prompt_fingerprint:
                    matches.append(row)
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def read_turn_contributions(traces, checks_path, capture_path, limit=4000, deadline_seconds=2.0,
                            ingress_path=None):
    """Join distinct evidence channels by session AND turn without changing Hook counts."""
    turns = {}
    trace_keys = {}
    for trace in traces:
        session = trace.get('session_id')
        if not session: continue
        turn = trace.get('turn_id')
        if not turn:
            identity = _load_ingress_identity(session, trace.get('hook_invocation_id'),
                                              trace.get('user_prompt_fingerprint') or trace.get('query_fingerprint'),
                                              ingress_path)
            if identity and identity.get('turn_id'):
                turn = identity['turn_id']
                trace['turn_id'] = turn
                trace.setdefault('raw_user_prompt', identity.get('prompt_preview'))
        if not turn: continue
        key = (session, turn)
        turns.setdefault(key, {})
        trace_keys[id(trace)] = key
    report = {'turns':turns, 'capture_status':'unavailable', 'errors':[], 'scan_limit':limit}
    if not turns:
        return report
    db = None
    try:
        db = sqlite3.connect(Path(checks_path).as_uri()+'?mode=ro', uri=True, timeout=.3)
        for (session, turn), entry in turns.items():
            rows = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM observations WHERE session=? AND turn=? AND kind='tool' ORDER BY id",
                (session, turn))]
            entry['mcp'] = summarize_tools(rows)
    except (OSError, sqlite3.Error, ValueError) as error:
        report['errors'].append('checks:'+type(error).__name__)
    finally:
        if db is not None:
            db.close()
    for entry in turns.values():
        entry.setdefault('mcp', dict(summarize_tools([]), status='unavailable'))
        entry['answer'] = {'status':'not_observed', 'cited_mcp_ids':[], 'causal_benefit':'not_measured'}
    started, db, seen, size = time.monotonic(), None, set(), 0
    try:
        db = sqlite3.connect(Path(capture_path).as_uri()+'?mode=ro', uri=True, timeout=.3)
        db.set_progress_handler(lambda: int(time.monotonic()-started > deadline_seconds), 10000)
        # Filter by the envelope's own source identity, never substring matches
        # against the body, which may itself contain quoted historical traces.
        for (raw,) in db.execute('SELECT envelope_json FROM captures ORDER BY capture_order DESC LIMIT ?', (limit,)):
            size += len(raw)
            if size > 64*1024*1024 or time.monotonic()-started > deadline_seconds:
                report['capture_status'] = 'partial_window'
                break
            event = json.loads(raw)
            source = event.get('source_payload') or {}
            key = (source.get('session_id'), source.get('turn_id'))
            if key not in turns:
                continue
            entry = turns[key]
            hook = (event.get('provenance') or {}).get('hook')
            if hook == 'Stop' and entry['answer']['status'] == 'not_observed':
                answer = source.get('last_assistant_message')
                if isinstance(answer, str):
                    entry['answer'].update(status='answer_observed', text=answer,
                                           event_id=event.get('event_id'), at=event.get('recorded_at'))
            if hook != 'PostToolUse' or source.get('tool_name') not in READ_TOOLS:
                continue
            call = source.get('tool_use_id') or source.get('tool_call_id')
            recognized = next((r for r in entry['mcp']['calls'] if r.get('call_id') == call
                               and r.get('response_recognized') and not r.get('failed')), None)
            if not call or not recognized or (key, call) in seen:
                continue
            seen.add((key, call))
            response = resolve_tool_response(source, capture_path)
            navigation = response_navigation(response)
            if navigation:
                entry['mcp'].setdefault('source_navigation', [])
                for locator in navigation:
                    if not any(row['memory_id'] == locator['memory_id'] for row in entry['mcp']['source_navigation']):
                        entry['mcp']['source_navigation'].append(dict(locator,call_id=call,event_id=event.get('event_id'),at=event.get('recorded_at')))
                entry['mcp']['source_navigation_returned_ids'] = [row['memory_id'] for row in entry['mcp']['source_navigation']]
                entry['mcp']['source_navigation_returned_count'] = len(entry['mcp']['source_navigation_returned_ids'])
            for record in response_records(response):
                if record['id'] not in recognized.get('record_ids', []):
                    continue
                if any(r['id'] == record['id'] for r in entry['mcp']['records']):
                    continue
                # The standalone status process does not add the Hook scripts
                # directory to sys.path. Load the existing masker by location.
                import importlib.util
                spec = importlib.util.spec_from_file_location('contribution_source_safety', Path(__file__).with_name('source_safety.py'))
                masker = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(masker)
                mask_value = masker.mask_value
                entry['mcp']['records'].append(mask_value(dict(record, text=str(record.get('text') or '')[:1500],
                    call_id=call, event_id=event.get('event_id'), at=event.get('recorded_at'))))
        else:
            report['capture_status'] = 'observed_window'
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        report['capture_status'] = 'partial_window'
        report['errors'].append('capture:'+type(error).__name__)
    finally:
        if db is not None:
            db.close()
    for entry in turns.values():
        answer = entry['answer'].pop('text', '')
        entry['answer']['cited_mcp_ids'] = [mid for mid in entry['mcp']['record_ids']
            if re.search(r'(?<![\w-])'+re.escape(mid)+r'(?![\w-])', answer)]
        entry['answer']['boundary'] = 'literal_ID_in_same_turn_final_answer; not_causal_benefit'
        entry['mcp']['content_status'] = report['capture_status']
        entry['mcp']['missing_content_ids'] = [mid for mid in entry['mcp']['record_ids']
            if not any(r['id'] == mid for r in entry['mcp']['records'])]
    report['resolved_turn_count'] = len(turns)
    return report

def read_host_receipts(path,allowed,limit=4000,deadline_seconds=2.0):
    report={'status':'observed_window','research':{},'errors':[],'scanned':0,
        'scan_limit':limit,'model_context_visibility':'unknown'}
    path=Path(path)
    if not path.is_file():report['status']='unavailable';return report
    db=None;seen=set();started=time.monotonic();total_bytes=0
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0.3)
        db.set_progress_handler(lambda: int(time.monotonic()-started>deadline_seconds),10000)
        for (raw,) in db.execute('SELECT envelope_json FROM captures ORDER BY capture_order DESC LIMIT ?',(limit,)):
            report['scanned']+=1;total_bytes+=len(raw)
            if time.monotonic()-started>deadline_seconds or total_bytes>64*1024*1024:
                report['status']='partial_window';break
            try:
                if len(raw)>4*1024*1024:raise ValueError('oversize_event_not_projected')
                event=json.loads(raw);source=event.get('source_payload') or {}
                if event.get('provenance',{}).get('hook')!='PostToolUse' or source.get('tool_name') not in TOOLS:continue
                if not all(source.get(k) for k in ('session_id','turn_id','tool_use_id')):continue
                response=resolve_tool_response(source, path)
                if not isinstance(response,dict) or response.get('isError'):continue
                for block in response.get('content',[]):
                    if block.get('type')!='text':continue
                    value=json.loads(block.get('text',''));rid=value.get('research_id')
                    if rid not in allowed or value.get('mode')!='official_discovery_evidence_only':continue
                    ids=list(dict.fromkeys(m['id'] for m in value.get('memories',[]) if m.get('state')=='valid' and m.get('id')))
                    if any(mid not in allowed[rid] for mid in ids):raise ValueError('foreign_record_ids_in_receipt')
                    key=(rid,source['session_id'],source['turn_id'],source['tool_use_id'])
                    if key in seen:continue
                    seen.add(key)
                    row=report['research'].setdefault(rid,{'observed_record_ids':[], 'receipts':[],
                        'model_context_visibility':'unknown','answer_use':'not_measured'})
                    row['observed_record_ids']=list(dict.fromkeys(row['observed_record_ids']+ids))
                    row['receipts'].append({'event_id':event.get('event_id'),'session_id':source['session_id'],
                        'turn_id':source['turn_id'],'tool_use_id':source['tool_use_id'],'record_ids':ids,
                        'at':event.get('recorded_at'),'boundary':'host_PostToolUse_tool_response'})
            except (ValueError,KeyError,TypeError) as error:report['errors'].append(type(error).__name__+': '+str(error)[:100])
    except (sqlite3.Error,OSError) as error:
        report['status']='partial_window' if report['research'] else 'unavailable'
        report['errors'].append(type(error).__name__)
    finally:
        if db is not None:db.close()
    report['seconds']=time.monotonic()-started
    return report
