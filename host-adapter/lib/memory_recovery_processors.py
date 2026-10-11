"""Adapters to existing EP processors, with unchanged source/maturity gates."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import urllib.parse
import uuid

from .memory_recovery import atomic, fingerprint, in_scope, lock, now, read_json, range_scope_unknown
from .retention_policy import retention_exclusion
from .scenario_source import read_session_source


class ProcessorBlocked(Exception):
    def __init__(self,status,code): self.status,self.code=status,code


class ProcessorAdapter:
    def __init__(self,engine):
        self.e=engine
        self.source_root=Path(__file__).resolve().parents[2]
        self._metadata_cache={}
        self._sources_cache={}

    def request(self,method,path,body=None,timeout=30):
        try: return self.e.client._request(method,path,body,timeout=timeout)
        except Exception as error:
            raise ProcessorBlocked('waiting_provider','provider_unavailable') from error

    def prefix(self,job): return '/v1/default/banks/'+urllib.parse.quote(job['bank_id'],safe='')

    def check_cancel(self,job):
        if (read_json(self.e.job_path(job['job_id']),{}) or {}).get('cancel_requested'):
            raise ProcessorBlocked('cancelled','cancel_requested')

    def progress(self,job,stage_id,processed,total):
        stage=next(s for s in job['stages'] if s['id']==stage_id)
        stage.update(processed=processed,total=total)
        self.e.save(job)

    def track_process(self,job,identity):
        cohort=job['scope'].get('process_record_ids')
        if cohort is not None and identity not in cohort: cohort.append(identity)

    def source_metadata(self,scope):
        """Fast header/stat inventory. Never parse rollout bodies in an HTTP request."""
        cache_key=fingerprint(scope)
        if cache_key in self._metadata_cache: return self._metadata_cache[cache_key]
        sessions={}
        q=self.e.queue._read()
        prefix=scope['bank_id']+'::'
        for key,cursor in q['sessions'].items():
            if key.startswith(prefix):
                project,sid=key[len(prefix):].rsplit('::',1)
                sessions[sid]={'session_id':sid,'project':project,'cursor':cursor,'bank_capture_checkpoint':True}
        root=Path(self.e.config.get('sessionRoot') or self.e.state_root/'codex/sessions')
        # Production session root is explicit; isolated state never scans HOME.
        fixed=self.e.config.get('bankId')==scope['bank_id'] and not self.e.config.get('dynamicBankId',False)
        if root.is_dir():
            for path in root.rglob('rollout-*.jsonl'):
                if not fixed and path.stem[-36:] not in sessions: continue
                try:
                    with path.open() as stream: meta=json.loads(next(stream)).get('payload') or {}
                    sid=str(uuid.UUID(meta.get('id')))
                    stat=path.stat()
                    item=sessions.setdefault(sid,{'session_id':sid,'project':str(meta.get('cwd') or 'unknown'),'cursor':0,'bank_capture_checkpoint':False})
                    with path.open('rb') as raw:
                        head=raw.read(min(4096,stat.st_size)); raw.seek(max(0,stat.st_size-4096)); tail=raw.read(min(4096,stat.st_size))
                    item.setdefault('source_files',[]).append({'path':str(path),'inode':stat.st_ino,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                        'head_sha256':hashlib.sha256(head).hexdigest(),'tail_sha256':hashlib.sha256(tail).hexdigest()})
                except (ValueError,TypeError,StopIteration,OSError): continue
        rows=[]
        for sid,row in sessions.items():
            if scope.get('session_ids') and sid not in scope['session_ids']: continue
            if retention_exclusion(row['project'],[sid],self.e.config) or sid in self.e.config.get('diagnosticSessionIds',[]): continue
            rows.append({**row,'root':str(root)})
        self._metadata_cache[cache_key]=rows
        return rows

    def sources(self,scope):
        cache_key=fingerprint(scope)
        if cache_key in self._sources_cache: return self._sources_cache[cache_key]
        rows=list(self.iter_sources(scope))
        self._sources_cache[cache_key]=rows
        return rows

    def iter_sources(self,scope):
        """One source at a time so recovery never loads a whole history corpus."""
        for row in self.source_metadata(scope):
            sid=row['session_id']
            try: source=read_session_source(sid,row['root'],max_chars=10000000)
            except ValueError: continue
            watermarks=scope.get('source_watermarks')
            if watermarks is not None:
                frames=watermarks.get(sid)
                if frames is None: continue
                limits={f['path']:f['size'] for f in frames}
                messages=[m for m in source['messages'] if m.get('source_path') in limits and m.get('byte_offset',0)<limits[m['source_path']]]
                from .scenario_source import revision_for_messages
                source={**source,'messages':messages,'source_files':list(limits),'source_revision':revision_for_messages(messages) if messages else None,
                        'total_chars':sum(len(m['text']) for m in messages),'status':'complete' if messages else 'source_empty','source_byte_limits':limits}
            messages=source['messages']
            selected=[m for m in messages if in_scope({'session_id':sid,'captured_at':m.get('at')},scope)]
            unknown_times=sum(range_scope_unknown({'session_id':sid,'captured_at':m.get('at')},scope) for m in messages)
            yield {**row,'source':source,'selected':selected,'scope_timestamp_unknown_count':unknown_times}

    def process_records(self,scope):
        from .process_memory import ProcessMemoryStore, _is_diagnostic_process_record
        path=Path(self.e.config.get('processMemoryPath') or self.e.state_root/'process-memory/records.json')
        store=ProcessMemoryStore(path); all_rows=store.all()
        mapped={r['session_id'] for r in self.source_metadata(scope)}
        eligible=[]; unknown=[]
        for row in all_rows:
            if scope.get('process_record_ids') is not None and row.get('process_memory_id') not in scope['process_record_ids']: continue
            context=row.get('primary_context') or {}
            bank=row.get('bank_id') or context.get('bank_id')
            if bank and bank!=scope['bank_id']: continue
            if not bank and context.get('session_id') not in mapped: unknown.append(row); continue
            if not in_scope(row,scope) or _is_diagnostic_process_record(row): continue
            if retention_exclusion(context.get('cwd') or context.get('project'),[context.get('session_id')],self.e.config): continue
            eligible.append(row)
        return store,eligible,unknown

    def inventory(self,scope):
        rows=self.e.scoped_items(scope); sources=self.source_metadata(scope); unknown_scope=self.e.unknown_scope_items(scope)
        _,process,unknown=self.process_records(scope)
        dims={}
        local_count=len(rows)
        for dim in ('facts','experiences','entities'):
            dims[dim]={'status':'pending' if local_count else 'inventory_pending','pending_count':local_count if local_count else None,'source_count':local_count if local_count else None}
            if unknown_scope: dims[dim].update(status='review_pending',pending_count=None,error_code='source_timestamp_unavailable')
        dims['raw_sources']={'status':'pending' if sources else 'source_unavailable',
                             'pending_count':None,'source_count':len(sources) if sources else None,
                             'error_code':'raw_gap_inventory_requires_background_source_inspection'}
        mapping={'agent_traces':{'trace'},'agent_events':{'event','process_observation','process_draft'},'agent_episodes':{'trace','episode'},
                 'agent_patterns':{'episode','pattern'},'agent_strategies':{'pattern','skill'},'agent_capabilities':{'trace','capability_observation'}}
        for dim,kinds in mapping.items():
            found=[r for r in process if r.get('kind') in kinds]
            dims[dim]={'status':'pending' if found else ('unsupported_scope' if unknown else 'no_eligible_evidence'),
                       'pending_count':len(found),'source_count':len(found)}
        revalidation=[r for r in process if r.get('drift_status') in {'watch','revalidation_required','deprecated'}]
        dims['agent_revalidation']={'status':'pending' if revalidation else 'no_eligible_evidence','pending_count':len(revalidation),'source_count':len(revalidation)}
        for dim in ('observations','preferences','mental_models'):
            dims[dim]={'status':'inventory_pending','pending_count':None,'source_count':None,'error_code':'remote_inventory_not_read_in_preview'}
        dims['session_summaries']={'status':'pending' if sources else 'source_unavailable','pending_count':None,'source_count':len(sources) if sources else None}
        from .context_summary import read_context_index
        index=read_context_index(self.e.state_root/'context/context-index.json')
        verified={p.get('project_key') for p in index.get('projects',[]) if p.get('bank_id')==scope['bank_id'] and p.get('identity_status')=='verified_project'}
        dims['project_summaries']={'status':'pending' if verified else 'unsupported_scope','pending_count':None,'source_count':len(verified) if verified else None,
                                    **({'error_code':'verified_project_scope_missing'} if not verified else {})}
        source_dimensions={'raw_sources','session_summaries','project_summaries','agent_traces'}
        need_sources=bool(set(scope['dimensions']) & source_dimensions)
        settings_signature={'modules':self.e.settings.get('modules') or {},'routing':self.e.settings.get('routing') or {},
                            'bankId':self.e.config.get('bankId'),'dynamicBankId':self.e.config.get('dynamicBankId'),
                            'retainExcludedCwds':self.e.config.get('retainExcludedCwds',[]),'retainExcludedSessionIds':self.e.config.get('retainExcludedSessionIds',[])}
        return {'dimensions':dims,'estimated_tokens':None,
                'revision':{'queue':[(r['id'],r.get('operation_id')) for r in rows],
                            'queue_sources':{r['id']:fingerprint({k:r.get(k) for k in ('bank_id','session_id','project','content','metadata','captured_at')}) for r in rows},
                            'sources':[(r['session_id'],r.get('source_files'),r['cursor']) for r in sources] if need_sources else [],
                            'process':[(r.get('process_memory_id'),fingerprint(r)) for r in process],
                            'unknown_scope_queue':[r['id'] for r in unknown_scope],'settings_signature':fingerprint(settings_signature)}}

    def compatible_snapshot(self,previous,current):
        if previous.get('settings_signature')!=current.get('settings_signature'): return False
        current_process={r[0]:r[1] for r in current.get('process') or []}
        if any(current_process.get(r[0])!=r[1] for r in previous.get('process') or []) or previous.get('unknown_scope_queue')!=current.get('unknown_scope_queue'): return False
        all_items={r['id']:r for r in self.e.queue._read()['items']}
        for item_id,original in previous.get('queue_sources',{}).items():
            if item_id in all_items:
                actual=fingerprint({k:all_items[item_id].get(k) for k in ('bank_id','session_id','project','content','metadata','captured_at')})
                if actual!=original: return False
        existing={row[0]:row for row in current.get('sources',[])}
        for sid,frames,cursor in previous.get('sources',[]):
            now_source=existing.get(sid)
            if not now_source or int(now_source[2])<int(cursor): return False
            by_path={f['path']:f for f in now_source[1] or []}
            for frame in frames or []:
                latest=by_path.get(frame['path'])
                if not latest or latest['inode']!=frame['inode'] or latest['size']<frame['size']: return False
                if latest['size']==frame['size']:
                    if latest['mtime_ns']!=frame['mtime_ns']: return False
                    continue
                # Verify the pinned original boundary on append; newly arrived
                # bytes are excluded from this job by the frozen watermark.
                with open(frame['path'],'rb') as stream:
                    head=stream.read(min(4096,frame['size'])); stream.seek(max(0,frame['size']-4096)); tail=stream.read(min(4096,frame['size']))
                if hashlib.sha256(head).hexdigest()!=frame['head_sha256'] or hashlib.sha256(tail).hexdigest()!=frame['tail_sha256']: return False
        return True

    def result(self,job,dims,status='complete',processed=0,total=None,failed=0,code=None):
        for dim in dims: self.e.set_dimension(job,dim,status=status,processed=processed,failed=failed,**({'error_code':code} if code else {}))
        return {'status':status,'processed':processed,'total':total,'failed':failed,**({'error_code':code} if code else {})}

    def run(self,stage,job,dimensions): return getattr(self,stage)(job,dimensions)

    def raw_capture(self,job,dims):
        metadata=self.source_metadata(job['scope']); processed=0; held=0
        # Source-only capture cannot start a disabled mixed retain extraction.
        if any(not self.e.enabled(d) for d in ('facts','experiences','entities')):
            return self.result(job,dims,'unsupported_scope',code='mixed_retain_modules_disabled')
        # A missing cursor is not proof of an unretained conversation: historical
        # imports may already exist in Bank documents. Hold without a checkpoint.
        candidates=[]
        for item in metadata:
            if not item.get('bank_capture_checkpoint'):
                held+=1; continue
            candidates.append(item['session_id'])
        capture_scope={**job['scope'],'session_ids':candidates}
        sources=self.iter_sources(capture_scope) if candidates else []
        for row in sources:
            self.check_cancel(job)
            if row.get('scope_timestamp_unknown_count'): held+=row['scope_timestamp_unknown_count']
            source=row['source']
            if source['status']!='complete': held+=1; continue
            messages=source['messages']; cursor=int(row['cursor'])
            if cursor>=len(messages): continue
            fresh=messages[cursor:]
            fresh=[m for m in fresh if m in row['selected']]
            if not fresh: continue
            text='\n\n'.join(f"[role: {m['role']}; at: {m.get('at')}; turn: {m.get('turn_id')}; raw_line_sha256: {m['raw_line_sha256']}]\n{m['text']}" for m in fresh)
            # Date subsets never advance the whole-session capture cursor past
            # excluded turns. A sidecar dedup key captures exact ordered sources.
            digest=fingerprint([m['raw_line_sha256'] for m in fresh])
            scoped=bool(job['scope'].get('from') or job['scope'].get('to'))
            session_key=row['session_id'] if not scoped else row['session_id']+':recovery:'+digest
            at=fresh[-1].get('at')
            accepted=self.e.queue.capture(session_key,len(messages),job['bank_id'],row['project'],text,
                    {'source':'manual-recovery','original_session_id':row['session_id'],'source_revision':source['source_revision'],'raw_fingerprint':digest},captured_at=at)
            if accepted:
                processed+=len(fresh)
                if job['scope'].get('queue_item_ids') is not None:
                    additions=[r['id'] for r in self.e.queue._read()['items'] if (r.get('metadata') or {}).get('raw_fingerprint')==digest]
                    job['scope']['queue_item_ids']=list(dict.fromkeys(job['scope']['queue_item_ids']+additions))
                self.progress(job,'raw_capture',processed,len(metadata))
        status='review_pending' if held else ('complete' if processed else 'no_eligible_evidence')
        return self.result(job,dims,status,processed,len(metadata),code='raw_source_or_bank_capture_checkpoint_unavailable' if held else None)

    def retain(self,job,dims):
        if any(not self.e.enabled(d) for d in ('facts','experiences','entities')):
            return self.result(job,dims,'unsupported_scope',code='mixed_retain_modules_disabled')
        dispatch=self.e.queue.path.with_suffix('.dispatch.lock')
        try:
            with lock(dispatch,blocking=False): return self._retain_locked(job,dims)
        except BlockingIOError: return self.result(job,dims,'running',code='shared_retention_worker_busy')

    def _retain_locked(self,job,dims):
        work=job['work'].setdefault('retain',{'batches':{},'remote_operations':{},'processed':0})
        prefix=self.prefix(job)
        # Inspect remote failed/inflight retains even after original local queue
        # acknowledgement was lost. Scope/origin payload gates protect probes.
        scoped=bool(job['scope'].get('from') or job['scope'].get('to') or job['scope'].get('session_ids'))
        offset=0; unknown_remote=0
        while True:
            # Actual API stores retained children as batch_retain; parent rows
            # are payload-less aggregators, so inspect/retry executable children.
            page=self.request('GET',prefix+f'/operations?type=batch_retain&exclude_parents=true&limit=100&offset={offset}')
            operations=page.get('operations') or []
            for op in operations:
                opid=op.get('operation_id') or op.get('id')
                if not opid or op.get('status') not in {'failed','cancelled','pending','processing'}: continue
                detail=self.request('GET',prefix+'/operations/'+urllib.parse.quote(str(opid),safe='')+'?include_payload=true')
                payload=detail.get('task_payload') or detail.get('payload') or {}
                items=payload.get('contents') or payload.get('items') or (payload.get('request') or {}).get('items') or []
                if not items: unknown_remote+=1; continue # origin cannot be checked; never widen recovery
                eligible=True
                for item in items:
                    meta=item.get('metadata') or {}; sids=str(meta.get('session_ids') or meta.get('session_id') or '').split(',')
                    if meta.get('prompt_origin')=='test_probe' or retention_exclusion(meta.get('project'),sids,self.e.config): eligible=False
                    if scoped:
                        if not sids or any(job['scope'].get('session_ids') and sid not in job['scope']['session_ids'] for sid in sids): eligible=False
                        if job['scope'].get('from') or job['scope'].get('to'):
                            # retained_at/operation.created_at are ingestion
                            # times, never original conversation timestamps.
                            first=meta.get('source_captured_start') or meta.get('source_captured_at') or meta.get('captured_at')
                            last=meta.get('source_captured_end') or first
                            if not first or not last:
                                eligible=False; unknown_remote+=1
                            elif any(not in_scope({'session_id':sid,'captured_at':at},job['scope']) for sid in sids for at in (first,last)):
                                eligible=False
                if eligible: work['remote_operations'].setdefault(str(opid),{'retried':False})
            total=page.get('total')
            if not operations or len(operations)<100 or isinstance(total,int) and offset+len(operations)>=total: break
            offset+=len(operations)
        rows=self.e.scoped_items(job['scope']); by_id={r['id']:r for r in rows}
        for row in rows:
            if row.get('operation_id'):
                work['batches'].setdefault(row['batch_id'],{'operation_id':row['operation_id'],'item_ids':[],'completed':False})['item_ids'].append(row['id'])
        pending=False
        for opid,item in work['remote_operations'].items():
            if item.get('completed'): continue
            status=self.request('GET',prefix+'/operations/'+urllib.parse.quote(opid,safe='')).get('status')
            if status=='completed': item['completed']=True; continue
            if status in {'failed','cancelled'}:
                if item.get('retried'): raise ProcessorBlocked('waiting_provider','remote_retain_failed')
                self.request('POST',prefix+'/operations/'+urllib.parse.quote(opid,safe='')+'/retry'); item['retried']=True
            pending=True
        for batch in work['batches'].values():
            batch['item_ids']=list(dict.fromkeys(batch['item_ids']))
            if batch.get('completed'): continue
            op=batch['operation_id']
            try: remote=self.e.client.operation_status(job['bank_id'],op,timeout=10)
            except Exception as error:
                if 'HTTP 404 ' in str(error): remote={'status':'not_found'}
                else: raise ProcessorBlocked('waiting_provider','provider_unavailable') from error
            if remote.get('status')!='not_found' and any(i in by_id and not by_id[i].get('operation_id') for i in batch['item_ids']):
                if not self.e.queue.mark_submitted(batch['batch_id'],op,item_ids=batch['item_ids']): raise ProcessorBlocked('review_pending','retain_acknowledgement_conflict')
            if remote.get('status')=='completed':
                self.e.queue.reconcile({op:'completed'}); batch['completed']=True; work['processed']+=len(batch['item_ids']); self.e.save(job); continue
            if remote.get('status') in {'failed','cancelled'}:
                if batch.get('retried'): raise ProcessorBlocked('waiting_provider','remote_retain_failed')
                self.request('POST',prefix+'/operations/'+op+'/retry'); batch['retried']=True; self.e.save(job); pending=True; continue
            if remote.get('status')=='not_found':
                snapshot=[by_id[i] for i in batch['item_ids'] if i in by_id]
                if len(snapshot)!=len(batch['item_ids']): raise ProcessorBlocked('review_pending','retain_intent_source_missing')
                response=self.request('POST',prefix+'/memories',batch['body'],timeout=self.e.config.get('retainSubmitTimeout',30))
                if response.get('operation_id')!=op or not response.get('success'): raise ProcessorBlocked('waiting_provider','async_acceptance_missing')
                if not self.e.queue.mark_submitted(batch['batch_id'],op,item_ids=batch['item_ids']): raise ProcessorBlocked('review_pending','retain_acknowledgement_conflict')
            pending=True
        if pending: return self.result(job,dims,'running',work['processed'],None)
        rows=self.e.scoped_items(job['scope'])
        if rows:
            # Filter BEFORE batching, preserving exact project and item identity.
            state={'items':[r for r in rows if not r.get('operation_id')]}
            batches=self.e.queue._select_batches(state,force_tail=True,min_age_seconds=0)
            if not batches: return self.result(job,dims,'review_pending',work['processed'],len(rows),code='retain_source_held')
            batch=batches[0]; op=str(uuid.uuid5(uuid.NAMESPACE_URL,f"ep-retain:{job['bank_id']}:{batch['batch_id']}"))
            def expand(value):
                replacements={'project_key':hashlib.sha256(batch['project'].encode()).hexdigest()[:12], 'bank_id':job['bank_id'],
                              'session_id':','.join(batch['session_ids']),'timestamp':now()}
                for key,val in replacements.items(): value=str(value).replace('{'+key+'}',val)
                return value
            item={'content':batch['content'],'document_id':'codex-batch-'+batch['batch_id'],'context':self.e.config.get('retainContext','codex'),
                  'metadata':{'source':'codex-hook-token-batch','project':batch['project'],'session_ids':','.join(batch['session_ids']),
                              'retained_at':now(),'estimated_tokens':str(batch['estimated_tokens']),
                              'source_captured_start':min(r['captured_at'] for r in state['items'] if r['id'] in batch['item_ids']),
                              'source_captured_end':max(r['captured_at'] for r in state['items'] if r['id'] in batch['item_ids']),
                              **{k:expand(v) for k,v in self.e.config.get('retainMetadata',{}).items()}},
                  'tags':[expand(t) for t in self.e.config.get('retainTags',[])], 'observation_scopes':self.e.config.get('retainObservationScopes','shared')}
            if self.e.config.get('retainStrategy'): item['strategy']=self.e.config['retainStrategy']
            body={'async':True,'operation_id':op,'items':[item]}
            intent={'batch_id':batch['batch_id'],'operation_id':op,'item_ids':batch['item_ids'],'body':body,'completed':False}
            work['batches'][batch['batch_id']]=intent; self.e.save(job)
            response=self.request('POST',prefix+'/memories',body,timeout=self.e.config.get('retainSubmitTimeout',30))
            if response.get('operation_id')!=op or not response.get('success'): raise ProcessorBlocked('waiting_provider','async_acceptance_missing')
            if not self.e.queue.mark_submitted(batch['batch_id'],op,item_ids=batch['item_ids']): raise ProcessorBlocked('review_pending','retain_acknowledgement_conflict')
            return self.result(job,dims,'running',work['processed'],len(rows))
        if self.e.unknown_scope_items(job['scope']) or unknown_remote:
            return self.result(job,dims,'review_pending',work['processed'],None,code='retain_source_scope_unavailable')
        return self.result(job,dims,'complete' if work['processed'] or work['remote_operations'] else 'no_eligible_evidence',work['processed'],work['processed'])

    def consolidation(self,job,dims):
        if any(job['scope'].get(k) for k in ('from','to','session_ids')):
            return self.result(job,dims,'unsupported_scope',code='consolidation_range_filter_unavailable')
        work=job['work'].setdefault('consolidation',{})
        prefix=self.prefix(job)
        if work.get('operation_id'):
            status=self.request('GET',prefix+'/operations/'+work['operation_id']).get('status')
            if status in {'failed','cancelled'}:
                if work.get('retried'): raise ProcessorBlocked('waiting_provider','remote_consolidation_failed')
                self.request('POST',prefix+'/operations/'+work['operation_id']+'/retry'); work['retried']=True
                return self.result(job,dims,'running',0,None)
            if status!='completed': return self.result(job,dims,'running',0,None)
            work.pop('operation_id'); work.pop('retried',None); work['rounds']=work.get('rounds',0)+1
        stats=self.request('GET',prefix+'/stats')
        pending,failed=stats.get('pending_consolidation'),stats.get('failed_consolidation')
        if not isinstance(pending,int) or not isinstance(failed,int): raise ProcessorBlocked('review_pending','consolidation_inventory_unknown')
        if not pending and not failed: return self.result(job,dims,'complete' if work.get('rounds') else 'no_eligible_evidence',work.get('source_total',0),work.get('source_total',0))
        work['source_total']=max(work.get('source_total',0),pending+failed)
        if failed: self.request('POST',prefix+'/consolidation/recover')
        response=self.request('POST',prefix+'/consolidate',{})
        if not response.get('operation_id'): raise ProcessorBlocked('waiting_provider','async_acceptance_missing')
        work['operation_id']=response['operation_id']; self.e.save(job)
        return self.result(job,dims,'running',0,pending+failed)

    def guidance_modules(self):
        sys.path.insert(0,str(self.source_root/'guidance'))
        import observation_rebuild, publish_observation_guidance, rebuild_models
        # Bound all source reads to selected Bank API/client, including auth.
        observation_rebuild.get=lambda path,timeout=30:self.request('GET',path,timeout=timeout)
        publish_observation_guidance.get=observation_rebuild.get
        env_path=self.e.state_root/'profiles/evolving-profile-api.env'
        def provider_config():
            env={}
            if env_path.is_file():
                for line in env_path.read_text().splitlines():
                    if line and not line.startswith('#') and '=' in line:
                        key,value=line.split('=',1); env[key]=value
            configured=(self.e.settings.get('providers') or {}).get('primary') or {}
            for field,name in [('base_url','BASE_URL'),('api_key','API_KEY'),('model','MODEL')]:
                val=configured.get(field) or env.get('EVOLVING_PROFILE_API_LLM_'+name) or env.get('HINDSIGHT_API_LLM_'+name)
                if not val: raise ProcessorBlocked('waiting_provider','provider_configuration_unavailable')
                env['EVOLVING_PROFILE_API_LLM_'+name]=val; env['HINDSIGHT_API_LLM_'+name]=val
            return env
        observation_rebuild.load_env=provider_config; publish_observation_guidance.load_env=provider_config; rebuild_models.load_env=provider_config
        return observation_rebuild,publish_observation_guidance,rebuild_models,provider_config

    def preference_models(self,job,dims):
        config_path=self.e.state_root/'guidance-v1/guidance-v1.json'
        config=read_json(config_path)
        if not config or config.get('bank_id')!=job['bank_id']:
            return self.result(job,dims,'unsupported_scope',code='guidance_registry_bank_unbound')
        if any(job['scope'].get(k) for k in ('from','to','session_ids')):
            return self.result(job,dims,'unsupported_scope',code='guidance_range_source_coverage_unavailable')
        try:
            with lock(self.e.state_root/'guidance-v1/worker.lock',blocking=False): return self._guidance_locked(job,dims,config_path)
        except BlockingIOError: return self.result(job,dims,'running',code='shared_guidance_worker_busy')

    def _guidance_locked(self,job,dims,config_path):
        observation,publish,models,provider=self.guidance_modules()
        observation.BANK=job['bank_id']
        from mcp_runtime import load_repository
        repo=load_repository(config_path)
        work=job['work'].setdefault('preference_models',{})
        root=self.e.root/'artifacts'/job['job_id']/'guidance'
        held=0; processed=0
        if 'preferences' in dims:
            rows=observation.fetch_inputs()
            fingerprints={str(r['id']):observation.observation_fingerprint(r) for r in rows}
            worker=read_json(self.e.state_root/'guidance-v1/worker-state.json',{})
            completed=read_json(self.e.root/'guidance-fingerprints'/f'{fingerprint(job["bank_id"])}.json',{})
            from guidance_worker import changed_observation_ids
            # Respect automatic worker success watermark; own watermark is
            # advanced only for completed source review, never provider failure.
            previous={**(worker.get('seen_observation_fingerprints') or {}),**completed}
            changed=[i for i in fingerprints if previous.get(i)!=fingerprints[i]]
            if changed:
                with redirect_stdout(io.StringIO()): observation.main(str(root/'review'),set(changed))
                dispositions=read_json(root/'review/observation-dispositions.json',{})
                unrecoverable=[r for r in dispositions.get('results',[]) if r.get('reason')=='provider_output_unrecoverable']
                if unrecoverable:
                    state=read_json(root/'review/state.json',{})
                    for row in unrecoverable: state.get('results',{}).pop(row['id'],None)
                    atomic(root/'review/state.json',state)
                    raise ProcessorBlocked('waiting_provider','preference_provider_unavailable')
                with redirect_stdout(io.StringIO()): publish.main(str(root/'review/observation-dispositions.json'),str(root/'review/source-map.json'),str(config_path),str(root/'publication'))
                publication=read_json(root/'publication/publication-report.json',{})
                bad=[key for key,review in (publication.get('reviews') or {}).items() if str(review.get('reason','')).startswith('provider_output_unrecoverable')]
                if bad:
                    state=read_json(root/'publication/state.json',{})
                    for key in bad: state.get('reviews',{}).pop(key,None); state.get('held',{}).pop(key,None)
                    atomic(root/'publication/state.json',state)
                    raise ProcessorBlocked('waiting_provider','preference_provider_unavailable')
                held=publication.get('held_count',0); processed=len(changed)
                completed.update({key:fingerprints[key] for key in changed}); atomic(self.e.root/'guidance-fingerprints'/f'{fingerprint(job["bank_id"])}.json',completed)
                work['preferences_processed']=processed
            self.e.set_dimension(job,'preferences',status='review_pending' if held else ('complete' if changed else 'no_eligible_evidence'),processed=processed,failed=0)
        if 'mental_models' in dims:
            units=[u for u in repo.active_units() if (u.get('preference_audit') or {}).get('state') in {'approved','restricted'}]
            revision=fingerprint([(u['id'],u['revision']) for u in units])
            stamp=self.e.root/'model-fingerprints'/f'{fingerprint(job["bank_id"])}.json'
            prior=read_json(stamp,{})
            if len(units)<2:
                self.e.set_dimension(job,'mental_models',status='no_eligible_evidence',processed=0,failed=0)
            elif prior.get('revision')==revision:
                self.e.set_dimension(job,'mental_models',status='complete',processed=0,failed=0)
            else:
                try:
                    with redirect_stdout(io.StringIO()): models.main(str(config_path),str(root/'models'))
                except ProcessorBlocked: raise
                except Exception as error: raise ProcessorBlocked('waiting_provider','mental_model_provider_unavailable') from error
                report=read_json(root/'models/model-rebuild-report.json',{})
                model_held=report.get('candidate_count',0); held+=model_held
                atomic(stamp,{'revision':revision,'at':now()})
                self.e.set_dimension(job,'mental_models',status='review_pending' if model_held else 'complete',processed=len(report.get('published') or []),failed=0)
        return {'status':'review_pending' if held else 'complete','processed':processed,'total':None,'failed':0}

    def scenarios(self,job,dims):
        from .scenario_model import request_episode_bundle, request_episode_bundle_review, fingerprint_episode_bundle
        from .memory_recovery_scenario import request_source_coverage_review, publish_automated_session, MAX_SOURCE_CHARS, accepted_session_source
        from .context_summary import read_context_index, build_session_context, update_context_index
        metadata=self.source_metadata(job['scope']); sources=self.iter_sources(job['scope']); root=self.e.root/'artifacts'/job['job_id']/'scenarios'
        processed=0; held=0
        index_path=self.e.state_root/'context/context-index.json'
        index=read_context_index(index_path)
        existing={s.get('session_id'):s for s in index.get('sessions',[]) if s.get('bank_id')==job['bank_id']}
        _,_,_,provider=self.guidance_modules()
        for row in sources:
            self.check_cancel(job)
            source=row['source']
            if source['status']!='complete': held+=1; continue
            if any(job['scope'].get(k) for k in ('from','to')) and len(row['selected'])!=len(source['messages']): held+=1; continue
            if 'session_summaries' not in dims: continue
            if source['total_chars']>MAX_SOURCE_CHARS: held+=1; continue
            published=existing.get(row['session_id']) or {}
            if job['scope']['mode']=='pending' and accepted_session_source(published,source):
                continue
            path=root/(row['session_id']+'.json'); old=read_json(path,{})
            config=provider()
            bundle=None; review=None
            params={'base_url':config['EVOLVING_PROFILE_API_LLM_BASE_URL'],'api_key':config['EVOLVING_PROFILE_API_LLM_API_KEY'],'model':config['EVOLVING_PROFILE_API_LLM_MODEL']}
            try:
                bundle=old.get('bundle') if old.get('source_revision')==source['source_revision'] else None
                if not bundle: bundle=request_episode_bundle(source,**params)
                atomic(path,{'source_revision':source['source_revision'],'bundle':bundle,'status':'review_pending','publication_gate':'automated_full_source_coverage_required'})
                review=old.get('review') if old.get('source_revision')==source['source_revision'] else None
                if not review: review=request_episode_bundle_review(source,bundle,**params)
                audit=request_source_coverage_review(source,bundle,**params)
                project_key=hashlib.sha256(row['project'].encode()).hexdigest()[:16]
                base=existing.get(row['session_id']) or build_session_context(row['session_id'],project_key,source['source_files'],'')
                published=publish_automated_session(base,source,bundle,review,audit,row['root'])
                published['bank_id']=job['bank_id']
            except ValueError:
                held+=1
                atomic(path,{'source_revision':source['source_revision'],'bundle':bundle,'review':review,'status':'review_pending','error_code':'scenario_source_coverage_not_accepted'})
                continue
            except Exception as error: raise ProcessorBlocked('waiting_provider','scenario_model_unavailable') from error
            atomic(path,{'source_revision':source['source_revision'],'bundle':bundle,'review':review,'bundle_sha256':fingerprint_episode_bundle(bundle),
                         'status':'complete','source_coverage_audit':audit,'audit_authority':'automated_source_coverage','no_human_confirmation_claim':True})
            def upsert(latest):
                conflict=next((s for s in latest.get('sessions',[]) if s.get('context_id')==published['context_id'] and s.get('bank_id') not in (None,job['bank_id'])),None)
                if conflict: raise ValueError('scenario_bank_ownership_conflict')
                sessions=[s for s in latest.get('sessions',[]) if s.get('context_id')!=published['context_id']]
                sessions.append(published)
                return {**latest,'sessions':sessions}
            update_context_index(index_path,upsert)
            existing[row['session_id']]=published
            processed+=1; self.e.set_dimension(job,'session_summaries',status='running',processed=processed,failed=0); self.progress(job,'scenarios',processed,len(metadata))
        if 'session_summaries' in dims:
            self.e.set_dimension(job,'session_summaries',status='review_pending' if held else ('complete' if metadata else 'no_eligible_evidence'),processed=processed,failed=0,
                                 **({'error_code':'scenario_source_coverage_not_accepted_or_budget_exceeded'} if held else {}))
        if 'project_summaries' in dims:
            # Project identity is never inferred from a path hash or Session.
            from .memory_recovery_project import request_project_draft, request_project_review, publish_automated_project
            index=read_context_index(self.e.state_root/'context/context-index.json')
            projects=[p for p in index.get('projects',[]) if p.get('bank_id')==job['bank_id'] and p.get('identity_status')=='verified_project']
            sessions=[s for s in index.get('sessions',[]) if s.get('bank_id')==job['bank_id'] and s.get('status') in {'model_reviewed','episode_directory_ready'} and in_scope(s,job['scope'])]
            candidates=[]; project_processed=0; project_held=0
            for project in projects:
                self.check_cancel(job)
                members=[s for s in sessions if s.get('project_key')==project.get('project_key')]
                expected=list(project.get('session_ids') or [])
                if not expected or set(expected)!={s['session_id'] for s in members}:
                    project_held+=1; candidates.append({'project_key':project['project_key'],'status':'review_pending','error_code':'project_source_coverage_incomplete'}); continue
                source_scope={**job['scope'],'session_ids':expected}
                project_raw=[row['source'] for row in self.iter_sources(source_scope)]
                revision=fingerprint([{s['thread_id']:s['source_revision'] for s in project_raw},project.get('identity_evidence')])
                if job['scope']['mode']=='pending' and project.get('source_revision')==revision and project.get('reviewer_kind')=='automated_project_source_coverage': continue
                cfg=provider(); params={'base_url':cfg['EVOLVING_PROFILE_API_LLM_BASE_URL'],'api_key':cfg['EVOLVING_PROFILE_API_LLM_API_KEY'],'model':cfg['EVOLVING_PROFILE_API_LLM_MODEL']}
                try:
                    draft=request_project_draft(project,members,project_raw,**params)
                    audit=request_project_review(project,members,project_raw,draft,**params)
                    published_project=publish_automated_project(project,members,project_raw,draft,audit,metadata[0]['root'] if metadata else self.e.config.get('sessionRoot'))
                except ValueError:
                    project_held+=1; candidates.append({'project_key':project['project_key'],'status':'review_pending','error_code':'project_source_coverage_not_accepted'}); continue
                except Exception as error: raise ProcessorBlocked('waiting_provider','project_model_unavailable') from error
                def upsert_project(latest):
                    current=next((p for p in latest.get('projects',[]) if p.get('context_id')==published_project['context_id']),None)
                    identity_fields=('bank_id','project_key','identity_status','identity_evidence','session_ids')
                    if not current or any(current.get(field)!=project.get(field) for field in identity_fields):
                        raise ValueError('project_source_or_membership_changed')
                    latest_members={s.get('session_id'):s for s in latest.get('sessions',[])}
                    member_fields=('bank_id','project_key','source_revision','source_message_count','status')
                    for member in members:
                        actual=latest_members.get(member['session_id'])
                        if not actual or any(actual.get(field)!=member.get(field) for field in member_fields):
                            raise ValueError('project_source_or_membership_changed')
                    rows=[p for p in latest.get('projects',[]) if p.get('context_id')!=published_project['context_id']]
                    rows.append(published_project)
                    return {**latest,'projects':rows}
                try:
                    update_context_index(index_path,upsert_project)
                except ValueError:
                    project_held+=1
                    candidates.append({'project_key':project['project_key'],'status':'review_pending','error_code':'project_source_or_membership_changed'})
                    atomic(root/('project-'+fingerprint(project['project_key'])+'.json'),{'status':'review_pending','draft':draft,'review':audit,'error_code':'project_source_or_membership_changed'})
                    continue
                atomic(root/('project-'+fingerprint(project['project_key'])+'.json'),{'status':'complete','draft':draft,'review':audit,'published':published_project})
                project_processed+=1; processed+=1
                self.progress(job,'scenarios',processed,len(metadata))
            if candidates: atomic(root/'project-candidates.json',{'projects':candidates,'publication_gate':'automated_project_source_coverage_required'})
            held+=project_held
            self.e.set_dimension(job,'project_summaries',status='review_pending' if project_held else ('complete' if projects else ('unsupported_scope' if metadata else 'no_eligible_evidence')),processed=project_processed,failed=0,
                                 **({'error_code':'project_source_coverage_not_accepted'} if project_held else ({'error_code':'verified_project_scope_missing'} if not projects else {})))
        states=[d['status'] for d in job['dimensions'] if d['id'] in dims]
        return {'status':'review_pending' if held else ('unsupported_scope' if 'unsupported_scope' in states else ('complete' if metadata else 'no_eligible_evidence')),'processed':processed,'total':len(metadata),'failed':0}

    def agent_process(self,job,dims):
        from .process_memory import ProcessMemoryStore, _has_verifier, normalize_record
        from .process_memory_evaluation import build_revalidation_queue
        store,records,unknown=self.process_records(job['scope'])
        captured=0; events_added=0
        if 'agent_traces' in dims:
            # Recover actual attempted tool calls from original structured rows.
            # Arguments/output bodies are never copied; no outcome is inferred.
            for source in self.source_metadata(job['scope']):
                self.check_cancel(job)
                if not source.get('bank_capture_checkpoint'): continue
                for location in source.get('source_files') or []:
                    path=Path(location['path'])
                    frames=(job['scope'].get('source_watermarks') or {}).get(source['session_id'])
                    pinned={frame['path']:frame['size'] for frame in frames or []}
                    if job['scope'].get('source_watermarks') is not None and str(path) not in pinned: continue
                    byte_limit=pinned.get(str(path))
                    with path.open('rb') as stream:
                        while True:
                            offset=stream.tell()
                            if byte_limit is not None and offset>=byte_limit: break
                            raw=stream.readline()
                            if not raw: break
                            if byte_limit is not None and stream.tell()>byte_limit: break
                            try: entry=json.loads(raw)
                            except (ValueError,UnicodeError): continue
                            payload=entry.get('payload') or {}
                            if entry.get('type')!='response_item' or payload.get('type') not in {'function_call','custom_tool_call'}: continue
                            name=str(payload.get('name') or '')
                            if not name or name.startswith(('record_agent_','promote_agent_')): continue
                            if not in_scope({'session_id':source['session_id'],'captured_at':entry.get('timestamp')},job['scope']): continue
                            digest=hashlib.sha256(raw).hexdigest(); identity='trace:raw:'+digest
                            self.track_process(job,identity)
                            with store.locked():
                                if any(r.get('process_memory_id')==identity for r in store.all()): continue
                                store.record_trajectory({'process_memory_id':identity,'text':f'tool={name}; attempted invocation recorded in original structured source',
                                    'toolchain':[name],'outcome':'ambiguous','maturity':'observed','phase':'act','bank_id':job['bank_id'],
                                    'primary_context':{'session_id':source['session_id'],'bank_id':job['bank_id'],'cwd':source['project']},
                                    'created_at':entry.get('timestamp') or now(),'raw_source_locator':{'source_path':str(path),'byte_offset':offset,'raw_line_sha256':digest},
                                    'verification_evidence':[],'evidence_role':'observed_tool_invocation_not_task_result_verification'})
                                captured+=1
            store,records,unknown=self.process_records(job['scope'])
        if 'agent_events' in dims:
            for trace in records:
                if trace.get('kind')!='trace' or not trace.get('toolchain'): continue
                identity='event:trace:'+fingerprint(trace['process_memory_id'])
                self.track_process(job,identity)
                with store.locked():
                    if any(r.get('process_memory_id')==identity for r in store.all()): continue
                    store._append(normalize_record({**trace,'process_memory_id':identity,'kind':'event','maturity':'observed',
                        'source_trace_ids':[trace['process_memory_id']],'derived_from':[trace['process_memory_id']],
                        'text':'Observed tool event: '+str(trace.get('text') or ''),'evidence_role':'original_trace_projection_not_new_verification'}))
                    events_added+=1
            store,records,unknown=self.process_records(job['scope'])
        if not records: return self.result(job,dims,'unsupported_scope' if unknown else 'no_eligible_evidence',0,0,code='process_record_bank_unbound' if unknown else None)
        # Existing store does atomic files but no cross-process write lock.
        # Keep mutation restricted to explicit Bank sources and re-read before
        # append. Automatic derivation runs on an isolated source subset first.
        held=0; processed=captured+events_added
        known={r['process_memory_id'] for r in records}; traces=[r for r in records if r.get('kind')=='trace']
        artifacts=self.e.root/'artifacts'/job['job_id']/'agent'
        audit=[store.audit_source_integrity(r,known_ids=known) for r in records]
        atomic(artifacts/'source-audit.json',{'bank_id':job['bank_id'],'records':audit,'audit_kind':'automated_local_source_link_audit_not_outcome_verification'})
        for dim in ('agent_traces','agent_events'):
            if dim in dims:
                count=sum(r.get('kind') in ({'trace'} if dim=='agent_traces' else {'event','process_observation','process_draft'}) for r in records)
                self.e.set_dimension(job,dim,status='complete' if count else 'no_eligible_evidence',processed=captured if dim=='agent_traces' else events_added,failed=0,source_count=count)
        if any(d in dims for d in ('agent_episodes','agent_patterns','agent_strategies')):
            derived_path=artifacts/'derived-store.json'; subset={'schema':'agent-process-memory.v1','records':records,'profiles':{}}
            atomic(derived_path,subset); derived=ProcessMemoryStore(derived_path)
            if 'agent_episodes' in dims or 'agent_patterns' in dims:
                derived.auto_promote_verified_process(limit=max(100,len(records)),
                    allow_episodes=self.e.enabled('agent_episodes'),allow_patterns=self.e.enabled('agent_patterns'))
            generated=[r for r in derived.all() if r['process_memory_id'] not in known]
            wanted={'episode':'agent_episodes','pattern':'agent_patterns'}
            # Persist enabled prerequisite episodes too; otherwise a derived
            # Pattern would link to records that only existed in the sandbox.
            generated=[r for r in generated if self.e.enabled(wanted.get(r.get('kind'),'agent_traces'))]
            if 'agent_strategies' in dims:
                existing_sources={tuple(sorted(r.get('derived_from') or [])) for r in records if r.get('kind')=='skill'}
                patterns=[r for r in derived.all() if r.get('kind')=='pattern' and tuple([r['process_memory_id']]) not in existing_sources]
                for pattern in patterns:
                    if pattern.get('maturity') in {'replicated','generalized'} and pattern['process_memory_id'] in known:
                        result=derived.record_skill_candidate([pattern['process_memory_id']],{'text':pattern.get('text'),'bank_id':job['bank_id']})
                        generated.append(result); held+=1
            if generated:
                with store.locked():
                    data=store._read(); existing={r['process_memory_id'] for r in data['records']}
                    source_keys={(r.get('kind'),tuple(sorted(r.get('derived_from') or r.get('source_trace_ids') or []))):r['process_memory_id'] for r in data['records']}
                    remapped={}
                    for row in generated:
                        row={**row,'derived_from':[remapped.get(i,i) for i in row.get('derived_from') or []]}
                        row['verification_evidence']=[{**v,'ids':[remapped.get(i,i) for i in v['ids']]} if isinstance(v,dict) and isinstance(v.get('ids'),list) else v for v in row.get('verification_evidence') or []]
                        source_key=(row.get('kind'),tuple(sorted(row.get('derived_from') or row.get('source_trace_ids') or [])))
                        if row['process_memory_id'] not in existing and source_key not in source_keys:
                            data['records'].append({**row,'bank_id':job['bank_id']}); processed+=1; source_keys[source_key]=row['process_memory_id']
                            self.track_process(job,row['process_memory_id'])
                        elif source_key in source_keys:
                            remapped[row['process_memory_id']]=source_keys[source_key]
                            self.track_process(job,source_keys[source_key])
                    store._write(data)
            for dim,kind in [('agent_episodes','episode'),('agent_patterns','pattern'),('agent_strategies','skill')]:
                if dim not in dims: continue
                matching=[r for r in generated if r.get('kind')==kind]
                eligible=[r for r in records if r.get('kind')==({'episode':'trace','pattern':'episode','skill':'pattern'}[kind])]
                unverified=any(not _has_verifier(r.get('verification_evidence') or []) for r in eligible)
                state='review_pending' if kind=='skill' and matching or unverified else ('complete' if matching or eligible else 'no_eligible_evidence')
                if state=='review_pending': held+=1
                self.e.set_dimension(job,dim,status=state,processed=len(matching),failed=0)
        if 'agent_capabilities' in dims:
            receipts=[r for r in records if r.get('kind')=='capability_observation']
            # Actual task-local capability processor requires independent,
            # model-identified samples. Self-reported outcomes never qualify.
            eligible=[r for r in traces if _has_verifier(r.get('verification_evidence') or []) and (r.get('model_profile') or {}).get('family')]
            existing={r.get('sample_id') for r in receipts}; added=0
            for trace in eligible:
                sample=trace['process_memory_id']
                if sample in existing: continue
                evidence=trace.get('verification_evidence') or []
                verifier=next((v for v in evidence if isinstance(v,dict) and v.get('verifier_kind') and v.get('status') in {'passed','verified','accepted'}),None)
                if not verifier: continue
                try:
                    capability=store.record_capability_observation({'model_family':trace['model_profile']['family'],'model_version':trace['model_profile'].get('version'),
                        'task_archetype':(trace.get('task_archetype') or ['other'])[0],'phase':trace.get('phase','observe'),'outcome':trace.get('outcome'),
                        'verifier_kind':verifier['verifier_kind'],'status':verifier['status'],'verification_evidence':evidence,'sample_id':sample,
                        'evidence_refs':[sample],'bank_id':job['bank_id'],'primary_context':trace.get('primary_context')})
                    self.track_process(job,capability['process_memory_id'])
                    added+=1
                except ValueError: held+=1
            self.e.set_dimension(job,'agent_capabilities',status='complete' if added or receipts else 'no_eligible_evidence',processed=added,failed=0)
        if 'agent_revalidation' in dims:
            queue=build_revalidation_queue(records)
            atomic(artifacts/'revalidation-queue.json',{'bank_id':job['bank_id'],'records':queue,'required':'fresh_independent_task_verification','status':'review_pending' if queue else 'no_eligible_evidence'})
            if queue: held+=len(queue)
            self.e.set_dimension(job,'agent_revalidation',status='review_pending' if queue else 'no_eligible_evidence',processed=len(queue),failed=0)
        statuses=[d['status'] for d in job['dimensions'] if d['id'] in dims]
        if statuses and all(s=='no_eligible_evidence' for s in statuses):
            return {'status':'no_eligible_evidence','processed':0,'total':0,'failed':0}
        # Store cardinality is unrelated to requested missing work. Keep a
        # derived multi-output workload unestimated; sparse reads get 0/0.
        return {'status':'review_pending' if held else 'complete','processed':processed,'total':None if processed or held else 0,'failed':0}
