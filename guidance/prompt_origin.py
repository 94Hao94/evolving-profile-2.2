"""Read-only Prompt attribution from native host metadata, never Prompt wording."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

ORIGIN_KINDS = ('human', 'subagent', 'automation', 'memory-maintenance', 'unknown')
MEMORY_SOURCES = {'memory_consolidation', 'memory_maintenance', 'memory-maintenance'}
AUTOMATION_SOURCES = {'automation', 'heartbeat', 'scheduled', 'background'}
AUTOMATION_ORIGINS = {'automatic', 'automation', 'system', 'agent_generated', 'agent_tool_call', 'test_probe'}
_HEADER_CACHE = {}
_TURN_CACHE = {}
SCAN_BYTE_BUDGET = 12 * 1024 * 1024
SCAN_CHUNK = 512 * 1024
SOURCE_INDEX_LIMIT = 256


def normalize_native_prompt(value):
    from lib.content import extract_user_request
    return ' '.join(extract_user_request(value).split()).strip()


def _native_user_witness(raw, offset):
    # This cheap check only avoids parsing unrelated huge tool records. The
    # parsed native role/type/turn below is the authority, never this text test.
    if not re.search(br'"role"\s*:\s*"user"|"type"\s*:\s*"UserMessage"', raw):
        return None
    try:
        record = json.loads(raw); payload = record.get('payload') or {}
        if record.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'user':
            meta = payload.get('internal_chat_message_metadata_passthrough') or {}
            kinds=meta.get('content_item_kinds')
            if isinstance(kinds,list) and kinds and not any(isinstance(kind,str) and kind.startswith('user.') for kind in kinds):return None
            turn = meta.get('turn_id') or payload.get('turn_id'); item = payload
        elif record.get('type') == 'event_msg' and payload.get('type') == 'item_completed' and (payload.get('item') or {}).get('type') == 'UserMessage':
            turn = payload.get('turn_id'); item = payload['item']
        else:
            return None
        if not isinstance(turn, str) or not turn:
            return None
        text = '\n'.join(part.get('text', '') for part in item.get('content') or [] if isinstance(part, dict) and isinstance(part.get('text', ''), str))
        normalized = normalize_native_prompt(text)
        if not normalized:
            return None
        return {'native_turn_id': turn, 'native_message_id': item.get('id'),
                'message_sha256': hashlib.sha256(raw).hexdigest(), 'normalized_prompt_sha256': hashlib.sha256(normalized.encode()).hexdigest(),
                'preview': normalized[:600], 'normalized_length': len(normalized), 'source_byte_offset': offset,
                'record_kind': record.get('type'),'message_record_bytes':len(raw)}
    except (ValueError, TypeError, AttributeError):
        return None


def _matches_witness(row, witness):
    preview = ' '.join(str(row.get('prompt_preview') or '').split()).strip()
    fingerprint = str(row.get('prompt_fingerprint') or '')
    has_fingerprint=bool(re.fullmatch(r'[0-9a-f]{16}', fingerprint))
    if has_fingerprint and witness['normalized_prompt_sha256'][:16] != fingerprint:
        return False
    return bool(preview and preview == witness['preview'].strip() and
                (has_fingerprint or witness['normalized_length'] <= 600))


def _occurrence_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
        return None
    try:
        delta = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    except (ValueError, OverflowError):
        return None


def select_prompt_occurrences(rows, allowed_sources=('codex-userpromptsubmit',)):
    """Latest recorded time wins; equal/invalid times use the last append.

    Select before origin, host, query and source filtering. A newer broken
    source stays unknown; an older working source cannot replace it. Missing
    immutable identity remains an independent auditable occurrence. Tied
    output times keep the first occurrence's stable list position.
    """
    selected = []
    positions = {}
    for raw in rows:
        if not isinstance(raw, dict) or raw.get('source') not in allowed_sources:
            continue
        prompt = ' '.join(str(raw.get('prompt_preview') or '').split()).strip()
        if not prompt:
            continue
        row = {**raw, 'prompt_preview': prompt}
        identity = tuple(row.get(key) for key in ('session_id', 'turn_id', 'hook_invocation_id'))
        complete = all(isinstance(value, str) and value.strip() for value in identity)
        position = positions.get(identity) if complete else None
        if position is None:
            if complete:
                positions[identity] = len(selected)
            selected.append(row)
        else:
            previous_time = _occurrence_time(selected[position].get('at'))
            current_time = _occurrence_time(row.get('at'))
            if previous_time is None or (current_time is not None and current_time >= previous_time):
                selected[position] = row
    return sorted(selected, key=lambda row: (_occurrence_time(row.get('at')) is not None, _occurrence_time(row.get('at')) or 0), reverse=True)


def classify_prompt_origin(row: dict, metadata: dict | None, *, source_path=None, source_revision=None, error=None) -> dict:
    metadata = metadata or {}
    native_id = str(metadata.get('id') or '')
    expected = str(row.get('session_id') or '')
    source = metadata.get('source')
    thread_source = str(metadata.get('thread_source') or '').casefold()
    subagent = source.get('subagent') if isinstance(source, dict) else None
    spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
    # Desktop child Hook session_id is the parent namespace. Accept that only
    # when all native parent declarations corroborate the child relationship.
    parent_match = bool(isinstance(spawn, dict) and expected and
                        spawn.get('parent_thread_id') == expected and
                        metadata.get('parent_thread_id') == expected and
                        metadata.get('session_id') == expected and native_id)
    reason = error or 'native_source_not_classified'
    kind = 'unknown'
    if metadata and (not expected or (native_id != expected and not parent_match)):
        reason = 'session_identity_mismatch'
    elif metadata:
        memory_kind = subagent if isinstance(subagent, str) else (subagent.get('other') if isinstance(subagent, dict) else None)
        if (isinstance(memory_kind, str) and memory_kind in MEMORY_SOURCES) or thread_source in MEMORY_SOURCES:
            kind, reason = 'memory-maintenance', 'native_memory_maintenance'
        elif subagent is not None or thread_source in {'subagent', 'guardian_review'}:
            kind, reason = 'subagent', 'native_subagent'
        elif (thread_source in AUTOMATION_SOURCES or thread_source.startswith('hermes-background') or
              source == 'exec' or str(row.get('prompt_origin') or '').casefold() in AUTOMATION_ORIGINS):
            kind, reason = 'automation', 'native_automation'
        elif thread_source in {'user', 'realtime_voice', 'composer_link'} and isinstance(source, str) and source in {'vscode', 'cli', 'app'}:
            kind, reason = 'human', 'native_human_session'
    return {'origin_kind': kind, 'origin_status': 'unknown' if kind == 'unknown' else 'verified',
            'origin_evidence': {'reason': reason, 'source_path': source_path, 'source_revision': source_revision,
                                'native_session_id': native_id or None, 'native_session_source': source,
                                'thread_source': thread_source or None,
                                'boundary': 'native_session_metadata_not_prompt_text'}}


class PromptOriginResolver:
    """One snapshot per projection; cache entries carry exact header revisions.

    No lifetime cache of classification: every new projection checks the file
    stat and reads its bounded header. Repeated occurrences reuse only this
    resolver's same-stat source snapshot.
    """
    def __init__(self, roots):
        self.roots = [Path(root).expanduser().resolve() for root in roots]
        self.headers = {}
        self.index = None
        self.bytes_left = SCAN_BYTE_BUDGET
        self.deadline = time.monotonic() + 1.5

    def _paths(self, session_id):
        if self.index is None:
            self.index = {}
            for root in self.roots:
                if not root.is_dir():
                    continue
                for path in root.rglob('rollout-*.jsonl'):
                    # Resolve the exact native UUID suffix, not a glob made
                    # from an ingress-controlled session identifier.
                    metadata, revision, error = self._read(path)
                    if error:continue
                    identity = str(metadata.get('id') or '')
                    self.index.setdefault(identity, []).append(path)
                    source=metadata.get('source');subagent=source.get('subagent') if isinstance(source,dict) else None
                    spawn=subagent.get('thread_spawn') if isinstance(subagent,dict) else None
                    parent=metadata.get('parent_thread_id')
                    if isinstance(spawn,dict) and parent and spawn.get('parent_thread_id')==parent and metadata.get('session_id')==parent:
                        self.index.setdefault(str(parent), []).append(path)
        return self.index.get(str(session_id or ''), [])

    def _read(self, path):
        try:
            path = Path(path).expanduser().resolve()
            if path.suffix != '.jsonl' or not any(path.is_relative_to(root) for root in self.roots):
                return {}, None, 'source_path_outside_native_roots'
            stat = path.stat()
            stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            cached = _HEADER_CACHE.get(str(path))
            if cached and cached[0] == stamp:
                return cached[1:]
            with path.open('rb') as stream:
                raw = stream.readline(262145)
            if len(raw) > 262144 or not raw.endswith(b'\n'):
                return {}, None, 'native_header_incomplete_or_over_budget'
            header = json.loads(raw)
            if not isinstance(header, dict) or header.get('type') != 'session_meta' or not isinstance(header.get('payload'), dict):
                return {}, None, 'native_session_metadata_missing'
            metadata = {key: header['payload'].get(key) for key in ('id', 'session_id', 'parent_thread_id', 'source', 'thread_source')}
            result = (metadata, hashlib.sha256(raw).hexdigest(), None)
            self.headers[str(path)] = (stamp, *result)
            if len(_HEADER_CACHE)>2048:_HEADER_CACHE.clear()
            _HEADER_CACHE[str(path)] = (stamp,*result)
            return result
        except (OSError, ValueError, TypeError, RuntimeError):
            return {}, None, 'native_source_unavailable'

    def _scan_segment(self, path, state, start, length, aligned=False):
        if self.bytes_left<=0 or time.monotonic()>self.deadline:return None
        length=min(length,self.bytes_left)
        with Path(path).open('rb') as stream:
            stream.seek(start);data=stream.read(length)
        self.bytes_left-=len(data)
        skipping=aligned and state.get('skip_record')
        first=0 if (start==0 or aligned) and not skipping else (data.find(b'\n')+1)
        if first==0 and (skipping or (start and not aligned)):
            return start+len(data) if aligned else None
        if skipping:state['skip_record']=False
        end=data.rfind(b'\n')+1
        if end<=first:
            if skipping:return start+end
            if aligned:
                state['skip_record']=True
                if re.search(br'"role"\s*:\s*"user"|"type"\s*:\s*"UserMessage"',data[:2048]):state['oversized_user_gap']=True
                return start+len(data)
            return None
        offset=start+first
        for raw in data[first:end].splitlines(keepends=True):
            stamp=re.search(br'"timestamp"\s*:\s*"([^"]+)"',raw[:300])
            if stamp:
                epoch=_occurrence_time(stamp[1].decode())
                if epoch is not None:state['anchors'][offset]=epoch
                if len(state['anchors'])>4096:state['anchors'].pop(next(iter(state['anchors'])))
            witness=_native_user_witness(raw,offset)
            if witness:
                turn=witness['native_turn_id'];items=state['turns'].setdefault(turn,[])
                existing=next((item for item in items if item['normalized_prompt_sha256']==witness['normalized_prompt_sha256']),None)
                if existing is None:items.append(witness)
                elif existing['record_kind']!='response_item' and witness['record_kind']=='response_item':items[items.index(existing)]=witness
            offset+=len(raw)
        return start+end

    def _time_probe(self,path,state,row):
        target=_occurrence_time(row.get('at'))
        if target is None:return
        location=None
        with Path(path).open('rb') as stream:
            for _ in range(4):
                lower=[position for position,stamp in state['anchors'].items() if stamp<target]
                upper=[position for position,stamp in state['anchors'].items() if stamp>=target]
                low=max(lower,default=0);high=min(upper,default=state['size'])
                if high<=low:break
                if high-low<=262144:location=(low,high);break
                for step in range(1,9):
                    if self.bytes_left<65536 or time.monotonic()>self.deadline:break
                    middle=low+(high-low)*step//9;stream.seek(middle);data=stream.read(65536);self.bytes_left-=len(data)
                    boundary=data.find(b'\n')
                    if boundary<0:continue
                    prefix=data[boundary+1:boundary+300];match=re.search(br'"timestamp"\s*:\s*"([^"]+)"',prefix)
                    stamp=_occurrence_time(match[1].decode()) if match else None
                    if stamp is not None:state['anchors'][middle+boundary+1]=stamp
                location=(low,high)
        if location is not None:
            location=(max((position for position,stamp in state['anchors'].items() if stamp<target),default=0),
                      min((position for position,stamp in state['anchors'].items() if stamp>=target),default=state['size']))
            self._scan_segment(path,state,max(0,location[0]-65536),262144)
            if location[1]-location[0]>131072:self._scan_segment(path,state,max(0,location[1]-131072),262144)

    def _turn_witnesses(self,path,metadata,revision,row,shallow=False):
        path=Path(path).expanduser().resolve();stat=path.stat();key=str(path)
        state=_TURN_CACHE.get(key)
        if (state is None or state['inode']!=stat.st_ino or state['revision']!=revision or stat.st_size<state['size'] or
            (stat.st_size==state['size'] and stat.st_mtime_ns!=state['mtime'])):
            if self.bytes_left<=0 or time.monotonic()>self.deadline:
                return [],{'state':'partial','indexed_prefix_bytes':0,'source_bytes':stat.st_size,'witness_revalidation':'budget_exhausted'}
            state={'inode':stat.st_ino,'revision':revision,'size':stat.st_size,'mtime':stat.st_mtime_ns,'cursor':0,'tail_at':None,'turns':{},'anchors':{},'skip_record':False,'oversized_user_gap':False}
            if key not in _TURN_CACHE and len(_TURN_CACHE)>=SOURCE_INDEX_LIMIT:_TURN_CACHE.pop(next(iter(_TURN_CACHE)))
            _TURN_CACHE[key]=state
        else:
            # Touch one entry, never clear all proven locators/cursors because
            # later rows in a bounded window could not be scanned.
            _TURN_CACHE.pop(key);_TURN_CACHE[key]=state
        state['size']=stat.st_size;state['mtime']=stat.st_mtime_ns
        turn=str(row.get('turn_id') or '')
        if not any(_matches_witness(row,item) for item in state['turns'].get(turn,[])):
            if state['cursor']<stat.st_size:
                end=self._scan_segment(path,state,state['cursor'],262144 if shallow else SCAN_CHUNK,aligned=True)
                if end is not None:state['cursor']=end
            if not shallow and stat.st_size>SCAN_CHUNK and state['tail_at']!=stat.st_size:
                self._scan_segment(path,state,max(0,stat.st_size-SCAN_CHUNK),SCAN_CHUNK);state['tail_at']=stat.st_size
            if not shallow and turn not in state['turns'] and state['cursor']<stat.st_size:self._time_probe(path,state,row)
        matches=[];fresh_state=None
        for item in state['turns'].get(turn,[]):
            length=item.get('message_record_bytes',0)
            if length<1 or self.bytes_left<length or time.monotonic()>self.deadline:
                fresh_state='budget_exhausted';continue
            # A file can grow after rewriting an earlier record. Revalidate
            # only this exact bounded locator; growth is never proof of append.
            with path.open('rb') as stream:
                stream.seek(item['source_byte_offset']);raw=stream.read(length)
            self.bytes_left-=len(raw)
            if len(raw)!=length or hashlib.sha256(raw).hexdigest()!=item['message_sha256']:
                state['turns']={};state['anchors']={};state['cursor']=0;state['tail_at']=None
                state['skip_record']=False;state['oversized_user_gap']=False
                fresh_state='source_changed';matches=[];break
            if _matches_witness(row,item):matches.append(item)
        return matches,{'state':'complete' if state['cursor']>=stat.st_size and not state['skip_record'] and not state['oversized_user_gap'] else 'partial',
                       'indexed_prefix_bytes':state['cursor'],'source_bytes':stat.st_size,'witness_revalidation':fresh_state}

    def resolve(self, row):
        explicit = row.get('transcript_path')
        if explicit is not None and (not isinstance(explicit, str) or not explicit.strip() or '\x00' in explicit):
            return classify_prompt_origin(row, None, error='native_source_path_invalid')
        paths = [explicit] if explicit is not None else self._paths(row.get('session_id'))
        if not paths:return classify_prompt_origin(row,None,error='native_source_missing')
        if not row.get('turn_id'):return classify_prompt_origin(row,None,error='native_turn_identity_missing')
        matches=[];progress={};first_error=None
        # Small children are examined before a long-running parent. A bounded
        # incremental cursor and time probes avoid rescanning large append-only
        # sources on each status request. Only actual native user records count.
        paths=sorted(paths,key=lambda path:Path(path).stat().st_size if Path(path).exists() else 0)[:64]
        prepared=[]
        for path in paths:
            metadata,revision,error=self._read(path)
            if error:
                first_error=first_error or (error,str(path));continue
            session_class=classify_prompt_origin(row,metadata,source_path=str(path),source_revision=revision)
            if session_class['origin_evidence']['reason']=='session_identity_mismatch':
                first_error=first_error or ('session_identity_mismatch',str(path));continue
            prepared.append((path,metadata,revision))
        for shallow in ([True,False] if explicit is None else [False]):
          if matches or any(item.get('witness_revalidation')=='source_changed' for item in progress.values()):break
          for path,metadata,revision in prepared:
            try:witnesses,scan=self._turn_witnesses(path,metadata,revision,row,shallow=shallow)
            except (OSError,ValueError,TypeError,RuntimeError):witnesses=[];scan={'state':'unavailable'}
            progress[str(path)]=scan
            for witness in witnesses:
                value=classify_prompt_origin(row,metadata,source_path=str(path),source_revision=revision)
                value['origin_evidence'].update({k:witness.get(k) for k in ('native_turn_id','native_message_id','message_sha256','normalized_prompt_sha256','source_byte_offset')})
                value['origin_evidence']['boundary']='native_session_and_exact_user_message_turn'
                matches.append(value)
        if any(item.get('witness_revalidation')=='source_changed' for item in progress.values()):matches=[]
        identities={(value['origin_evidence']['native_session_id'],value['origin_kind']) for value in matches}
        if len(identities)>1:return classify_prompt_origin(row,None,error='native_prompt_source_conflict')
        if matches:return matches[0]
        if first_error and not progress:return classify_prompt_origin(row,None,source_path=first_error[1],error=first_error[0])
        error=('native_prompt_source_changed' if any(item.get('witness_revalidation')=='source_changed' for item in progress.values()) else
               'native_prompt_source_scan_incomplete' if any(item.get('witness_revalidation')=='budget_exhausted' for item in progress.values()) else
               'native_prompt_message_not_found' if progress and all(item['state']=='complete' for item in progress.values()) else 'native_prompt_source_scan_incomplete')
        value=classify_prompt_origin(row,None,error=error)
        value['origin_evidence']['verification_progress']={'candidate_files':len(paths),'complete_files':sum(item['state']=='complete' for item in progress.values()),'indexed_prefix_bytes':sum(item.get('indexed_prefix_bytes',0) for item in progress.values()),'source_bytes':sum(item.get('source_bytes',0) for item in progress.values())}
        return value

    def resolve_rows(self,rows,detail_id=None,*,page_offset=0,page_limit=20,host='all',query_text='',prompt_source='natural'):
        """Spend the shared budget on user-session candidates before background.

        Header classes only schedule verification; they never establish final
        author attribution. Preserve the original occurrence order in output.
        An explicitly selected audit gets first access to its source budget.
        """
        priorities=[];peek={}
        for position,row in enumerate(rows):
            fp=row.get('prompt_fingerprint') or hashlib.sha256(str(row.get('prompt_preview') or '').encode()).hexdigest()[:16]
            selected=detail_id and str(fp)+':'+str(row.get('at') or '')==detail_id
            path=row.get('transcript_path');hint='unknown'
            if isinstance(path,str) and path and '\x00' not in path:
                if path not in peek:peek[path]=self._read(path)
                metadata,_,error=peek[path]
                if not error:hint=classify_prompt_origin(row,metadata)['origin_kind']
            # An index hit is only a scheduling hint. resolve() still freshly
            # checks the actual raw locator/hash before exposing any proof.
            state=_TURN_CACHE.get(str(Path(path).expanduser().resolve())) if isinstance(path,str) and path and '\x00' not in path else None
            states=[state] if state else ([] if path else list(_TURN_CACHE.values()))
            indexed=any(any(_matches_witness(row,item) for item in cached.get('turns',{}).get(str(row.get('turn_id') or ''),[])) for cached in states)
            priorities.append({'selected':selected,'indexed':indexed,'hint':hint,'position':position,'row':row})
        terms=str(query_text or '').split();source=str(prompt_source or 'natural').casefold()
        eligible=[entry for entry in priorities if (str(host or 'all').casefold()=='all' or str(entry['row'].get('host_id') or 'codex').casefold()==str(host).casefold()) and
                  (not terms or all(term in entry['row'].get('prompt_preview','') for term in terms)) and
                  (source not in {'natural','human'} or entry['hint']=='human' or (entry['hint']=='unknown' and not entry['row'].get('transcript_path')))]
        page_positions={entry['position'] for entry in eligible[max(0,int(page_offset)):max(0,int(page_offset))+max(1,min(50,int(page_limit)))]}
        def priority(entry):
            if entry['selected']:return -3
            if entry['position'] in page_positions:return -2 if entry['indexed'] else -1
            if entry['indexed']:return 0
            return 1 if entry['hint']=='human' else 2 if entry['hint']=='unknown' and not entry['row'].get('transcript_path') else 3
        self.deadline=time.monotonic()+1.5
        # Keep one forward chunk available for a page whose own messages are
        # already indexed. This is request work, not a background service.
        reserve=SCAN_CHUNK if self.bytes_left>=2*SCAN_CHUNK else 0
        self.bytes_left-=reserve
        result={}
        for entry in sorted(priorities,key=lambda entry:(priority(entry),entry['position'])):
            position=entry['position'];row=entry['row']
            result[position]={**row,**self.resolve(row)}
        self.bytes_left+=reserve
        if reserve:
            candidates=[]
            for entry in sorted(priorities,key=lambda entry:(entry['position'] not in page_positions,entry['position'])):
                value=result[entry['position']];path=(value.get('origin_evidence') or {}).get('source_path')
                if value.get('origin_status')=='verified' and isinstance(path,str) and path not in candidates:candidates.append(path)
            for path in candidates:
                native=Path(path).expanduser().resolve();state=_TURN_CACHE.get(str(native))
                if not state or state['cursor']>=state['size']:continue
                metadata,revision,error=self._read(native);stat=native.stat()
                if error or revision!=state['revision'] or stat.st_ino!=state['inode'] or stat.st_size<state['size'] or (stat.st_size==state['size'] and stat.st_mtime_ns!=state['mtime']):continue
                end=self._scan_segment(native,state,state['cursor'],min(reserve,SCAN_CHUNK),aligned=True)
                if end is not None:state['cursor']=end
                break
        return [result[position] for position in range(len(rows))]
