"""Recovery uses real persistence/queue/process engines and fake external transport."""
import json
from pathlib import Path
import uuid
import subprocess
import sys

import pytest

from lib.memory_recovery import RecoveryEngine, RecoveryError
from lib.retention_queue import RetentionQueue


class FakeClient:
    def __init__(self):
        self.calls = []
        self.operations = {}
        self.fail = False

    def _request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if self.fail:
            raise RuntimeError('HTTP 401 secret must never appear publicly')
        if method == 'GET' and '/operations/' in path:
            identity = path.split('/')[-1]
            if identity not in self.operations:
                raise RuntimeError('HTTP 404 missing')
            return {'status': self.operations[identity]}
        if method == 'GET' and path.endswith('/operations?type=retain&limit=100&offset=0'):
            return {'operations': [], 'total': 0}
        if method == 'POST' and path.endswith('/memories'):
            self.operations[body['operation_id']] = 'processing'
            return {'success': True, 'operation_id': body['operation_id']}
        if method == 'POST' and path.endswith('/retry'):
            self.operations[path.split('/')[-2]] = 'processing'
            return {'success': True}
        if method == 'GET' and '/operations?' in path:
            return {'operations': [], 'total': 0}
        if method == 'GET' and '/memories/list?' in path:
            return {'items': [], 'total': 0}
        if method == 'GET' and path.endswith('/stats'):
            return {'pending_consolidation': 0, 'failed_consolidation': 0}
        raise AssertionError((method, path, body))

    def operation_status(self, bank, op, **kwargs):
        return self._request('GET', f'/v1/default/banks/{bank}/operations/{op}')


def engine(tmp_path, client=None, settings=None):
    config = {'bankId': 'bank-a', 'evolvingProfileApiUrl': 'http://fake.invalid',
              'retainQueuePath': str(tmp_path/'codex/state/retention-queue.json'),
              'sessionRoot': str(tmp_path/'sessions')}
    return RecoveryEngine(tmp_path, config=config, settings=settings or {}, client=client or FakeClient())


def capture(tmp_path, bank='bank-a', sid='session-a'):
    q = RetentionQueue(tmp_path/'codex/state/retention-queue.json')
    q.capture(sid, 1, bank, '/project', '[role:user;at:2026-01-01] Original evidence', {})
    return q


def start(e, dimensions=None, **scope):
    preview = e.dispatch({'action':'preview', 'bank_id':'bank-a', 'dimensions':dimensions or ['facts','experiences','entities'], **scope})['preview']
    result = e.dispatch({'action':'start', 'bank_id':'bank-a', 'plan_id':preview['plan_id'], 'idempotency_key':str(uuid.uuid4())})
    return result['job']['job_id']


def test_preview_unknown_remote_counts_and_no_transport(tmp_path):
    c=FakeClient(); e=engine(tmp_path,c)
    p=e.dispatch({'action':'preview','bank_id':'bank-a'})['preview']
    assert not c.calls
    assert next(d for d in p['dimensions'] if d['id']=='observations')['pending_count'] is None
    assert p['estimated_tokens'] is None


def test_project_only_preview_requires_provider_without_any_model_calls(tmp_path):
    c=FakeClient(); e=engine(tmp_path,c)
    preview=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['project_summaries']})['preview']
    assert preview['requires_provider'] is True
    assert not c.calls
    disabled=engine(tmp_path,c,settings={'modules':{'scenario_summary':{'record':False}}})
    preview=disabled.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['project_summaries']})['preview']
    assert preview['requires_provider'] is False
    assert not c.calls


def test_idempotency_restart_scope_and_public_redaction(tmp_path):
    capture(tmp_path); e=engine(tmp_path)
    p=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['facts']})['preview']
    args={'action':'start','bank_id':'bank-a','plan_id':p['plan_id'],'idempotency_key':str(uuid.uuid4())}
    first=e.dispatch(args); second=engine(tmp_path).dispatch(args)
    assert first==second
    assert 'intent' not in json.dumps(first)
    with pytest.raises(RecoveryError,match='job_not_found'):
        e.dispatch({'action':'status','bank_id':'other','job_id':first['job']['job_id']})


def test_acceptance_is_not_completion_and_resume_deduplicates(tmp_path):
    q=capture(tmp_path); capture(tmp_path,'bank-b','session-b'); c=FakeClient(); e=engine(tmp_path,c)
    jid=start(e)
    result=e.run_job(jid)['job']
    assert result['status']=='running'
    assert len(q._read()['items'])==2
    submitted=[v for v in c.calls if v[0]=='POST' and v[1].endswith('/memories')]
    assert len(submitted)==1 and '/bank-a/' in submitted[0][1]
    c.operations[submitted[0][2]['operation_id']]='completed'
    finished=engine(tmp_path,c).run_job(jid)['job']
    assert finished['status']=='complete'
    assert [r['bank_id'] for r in q._read()['items']]==['bank-b']
    assert len([v for v in c.calls if v[0]=='POST' and v[1].endswith('/memories')])==1


def test_provider_failure_preserves_raw_and_idempotent_intent(tmp_path):
    q=capture(tmp_path); c=FakeClient(); c.fail=True; e=engine(tmp_path,c); jid=start(e)
    failed=e.run_job(jid)['job']; assert failed['status']=='waiting_provider'
    assert q._read()['items'][0]['content'].endswith('Original evidence')
    assert 'secret' not in json.dumps(failed)
    c.fail=False
    e.dispatch({'action':'resume','bank_id':'bank-a','job_id':jid})
    e.run_job(jid)
    requests=[v for v in c.calls if v[0]=='POST' and v[1].endswith('/memories')]
    assert len(requests)==1


def test_cancel_resume_and_disabled_dimensions(tmp_path):
    capture(tmp_path); e=engine(tmp_path,settings={'modules':{'facts':{'record':False},'experiences':{'record':False},'entities':{'record':False}}})
    jid=start(e); e.dispatch({'action':'cancel','bank_id':'bank-a','job_id':jid})
    assert e.run_job(jid)['job']['status']=='cancelled'
    e.dispatch({'action':'resume','bank_id':'bank-a','job_id':jid})
    report=e.run_job(jid)['job']
    assert all(d['status']=='disabled' for d in report['dimensions'])
    assert not e.client.calls


def test_scoped_queue_does_not_submit_unselected_session(tmp_path):
    capture(tmp_path,sid='selected'); capture(tmp_path,sid='unselected'); c=FakeClient(); e=engine(tmp_path,c)
    jid=start(e,session_ids=['selected']); e.run_job(jid)
    body=next(v[2] for v in c.calls if v[0]=='POST')
    assert 'unselected' not in body['items'][0]['content']


def test_empty_local_evidence_is_not_queued_complete(tmp_path):
    e=engine(tmp_path); jid=start(e,['agent_traces','agent_episodes'])
    result=e.run_job(jid)['job']
    assert all(d['status']=='no_eligible_evidence' for d in result['dimensions'])
    assert result['status']=='complete'


def test_date_range_required_and_id_fields_validated(tmp_path):
    e=engine(tmp_path)
    with pytest.raises(RecoveryError,match='invalid_range'):
        e.dispatch({'action':'preview','bank_id':'bank-a','mode':'date_range'})
    with pytest.raises(RecoveryError,match='invalid_job_id'):
        e.dispatch({'action':'status','bank_id':'bank-a','job_id':'../../escape'})


def rollout(tmp_path,sid):
    root=tmp_path/'sessions'; root.mkdir(exist_ok=True)
    rows=[{'type':'session_meta','payload':{'id':sid,'cwd':'/original-project'}},
          {'type':'response_item','timestamp':'2026-10-01T01:00:00Z','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'Original user request'}]}},
          {'type':'response_item','timestamp':'2026-10-01T01:01:00Z','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'Original assistant claim'}]}}]
    path=root/f'rollout-2026-10-01-{sid}.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
    return path


def test_raw_gap_capture_preserves_role_time_and_skips_on_fingerprint_restart(tmp_path):
    sid=str(uuid.uuid4()); raw=rollout(tmp_path,sid); original=raw.read_bytes(); e=engine(tmp_path)
    e.queue.capture(sid,1,'bank-a','/original-project','[role: user; at: 2026-10-01T01:00:00Z]\nOriginal user request',{})
    jid=start(e,['raw_sources']); result=e.run_job(jid)['job']; assert result['status']=='complete'
    queue=e.queue._read(); assert len(queue['items'])==2
    content='\n'.join(r['content'] for r in queue['items'])
    assert 'role: user' in content and 'role: assistant' in content and '2026-10-01T01:00:00Z' in content
    second=engine(tmp_path); second.run_job(start(second,['raw_sources']))
    assert len(second.queue._read()['items'])==2
    assert raw.read_bytes()==original


def test_raw_sources_without_bank_capture_checkpoint_are_held(tmp_path):
    sid=str(uuid.uuid4()); rollout(tmp_path,sid); e=engine(tmp_path)
    result=e.run_job(start(e,['raw_sources']))['job']
    assert result['status']=='review_pending'
    assert result['dimensions'][0]['error_code']=='raw_source_or_bank_capture_checkpoint_unavailable'
    assert not e.queue._read()['items']


def test_preview_uses_metadata_not_full_transcript_parser(tmp_path,monkeypatch):
    sid=str(uuid.uuid4()); rollout(tmp_path,sid); e=engine(tmp_path)
    def forbidden(*_,**__): raise AssertionError('full transcript parsed in preview')
    monkeypatch.setattr('lib.memory_recovery_processors.read_session_source',forbidden)
    preview=e.dispatch({'action':'preview','bank_id':'bank-a'})['preview']
    raw=next(r for r in preview['dimensions'] if r['id']=='raw_sources')
    assert raw['source_count']==1 and raw['pending_count'] is None


def test_origin_and_exclusions_apply_before_grouping(tmp_path):
    q=capture(tmp_path,sid='user'); q.capture('probe',2,'bank-a','/project','test content',{'prompt_origin':'test_probe'})
    c=FakeClient(); e=engine(tmp_path,c); e.config['retainExcludedSessionIds']=['excluded']; q.capture('excluded',3,'bank-a','/project','excluded content',{})
    e.run_job(start(e))
    payload=next(v[2] for v in c.calls if v[0]=='POST')
    assert 'test content' not in payload['items'][0]['content'] and 'excluded content' not in payload['items'][0]['content']
    assert len(q._read()['items'])==3


def test_lost_response_reuses_exact_operation_uuid(tmp_path):
    class LoseResponse(FakeClient):
        def _request(self,method,path,body=None,**kwargs):
            result=super()._request(method,path,body,**kwargs)
            if method=='POST' and path.endswith('/memories') and not getattr(self,'lost',False):
                self.lost=True; raise ConnectionError('response lost')
            return result
    c=LoseResponse(); e=engine(tmp_path,c); q=capture(tmp_path); jid=start(e)
    assert e.run_job(jid)['job']['status']=='waiting_provider'
    e.dispatch({'action':'resume','bank_id':'bank-a','job_id':jid}); e.run_job(jid)
    submits=[v for v in c.calls if v[0]=='POST' and v[1].endswith('/memories')]
    assert len(submits)==1
    # A recovered remote acceptance must attach the local queue rows to op.
    assert q._read()['items'][0].get('operation_id')==submits[0][2]['operation_id']


def test_partial_module_disable_never_extracts_closed_dimension(tmp_path):
    c=FakeClient(); e=engine(tmp_path,c,{'modules':{'entities':{'record':False}}}); capture(tmp_path)
    result=e.run_job(start(e))['job']
    assert result['status']=='partial'
    assert not c.calls


def test_cli_protocol_isolated_stdin_and_safe_errors(tmp_path):
    script=Path(__file__).with_name('memory_recovery.py')
    args=[sys.executable,str(script),'--state-root',str(tmp_path)]
    preview=subprocess.run(args,input=json.dumps({'action':'preview','bank_id':'isolated','dimensions':['agent_traces']}),text=True,capture_output=True)
    assert preview.returncode==0 and json.loads(preview.stdout)['preview']['bank_id']=='isolated'
    bad=subprocess.run(args,input='not json',text=True,capture_output=True)
    assert bad.returncode==2 and json.loads(bad.stdout)=={'error':{'code':'invalid_json'}}


def test_new_queue_arrivals_are_deferred_and_owner_change_rejected(tmp_path):
    e=engine(tmp_path); preview=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['facts']})['preview']
    capture(tmp_path)
    result=e.dispatch({'action':'start','bank_id':'bank-a','plan_id':preview['plan_id'],'idempotency_key':str(uuid.uuid4())})
    assert e.run_job(result['job']['job_id'])['job']['status']=='complete'
    e.config['bankId']='another-bank'
    with pytest.raises(RecoveryError,match='plan_source_changed'):
        e.dispatch({'action':'start','bank_id':'bank-a','plan_id':preview['plan_id'],'idempotency_key':str(uuid.uuid4())})


def test_append_between_preview_start_pins_original_source_watermark(tmp_path):
    sid=str(uuid.uuid4()); raw=rollout(tmp_path,sid); e=engine(tmp_path)
    e.queue.capture(sid,1,'bank-a','/original-project','original user',{})
    preview=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['raw_sources']})['preview']
    with raw.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','timestamp':'2026-10-01T01:03:00Z','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'New arrival deferred'}]}})+'\n')
    result=e.dispatch({'action':'start','bank_id':'bank-a','plan_id':preview['plan_id'],'idempotency_key':str(uuid.uuid4())})
    e.run_job(result['job']['job_id'])
    assert all('New arrival deferred' not in r['content'] for r in e.queue._read()['items'])


def test_unknown_source_timestamp_is_held_in_date_scope(tmp_path):
    q=capture(tmp_path); data=q._read(); data['items'][0].pop('captured_at'); q._write(data)
    e=engine(tmp_path); jid=start(e,mode='date_range',**{'from':'2026-01-01T00:00:00Z'})
    result=e.run_job(jid)['job']
    assert result['status']=='review_pending'
    assert result['dimensions'][0]['error_code']=='retain_source_scope_unavailable'
    assert len(q._read()['items'])==1


def test_agent_real_raw_tool_capture_and_event_derivation_are_idempotent(tmp_path):
    from lib.process_memory import ProcessMemoryStore
    sid=str(uuid.uuid4()); path=rollout(tmp_path,sid)
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','timestamp':'2026-10-01T01:02:00Z','payload':{'type':'function_call','name':'actual_tool','call_id':'actual-call','arguments':'private arguments never copied'}})+'\n')
    e=engine(tmp_path); e.queue.capture(sid,1,'bank-a','/original-project','original user',{})
    jid=start(e,['agent_traces','agent_events']); result=e.run_job(jid)['job']
    assert result['status']=='complete'
    records=ProcessMemoryStore(tmp_path/'process-memory/records.json').all()
    assert [r['kind'] for r in records]==['trace','event']
    assert all(r['maturity']=='observed' and r['outcome']=='ambiguous' for r in records)
    assert 'private arguments' not in json.dumps(records)
    e.run_job(start(e,['agent_traces','agent_events']))
    assert len(ProcessMemoryStore(tmp_path/'process-memory/records.json').all())==2


def test_completed_queue_snapshot_before_start_is_skipped(tmp_path):
    q=capture(tmp_path); e=engine(tmp_path)
    preview=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['facts']})['preview']
    batch=q.ready_batches(force_tail=True,config={})[0]
    q.mark_submitted(batch['batch_id'],'completed-external'); q.reconcile({'completed-external':'completed'})
    job=e.dispatch({'action':'start','bank_id':'bank-a','plan_id':preview['plan_id'],'idempotency_key':str(uuid.uuid4())})['job']
    assert e.run_job(job['job_id'])['job']['status']=='complete'
    assert not [v for v in e.client.calls if v[0]=='POST']


def test_bank_source_move_and_same_size_rewrite_reject_snapshot(tmp_path):
    q=capture(tmp_path); e=engine(tmp_path); p=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['facts']})['preview']
    data=q._read(); data['items'][0]['bank_id']='bank-b'; q._write(data)
    with pytest.raises(RecoveryError,match='plan_source_changed'):
        e.dispatch({'action':'start','bank_id':'bank-a','plan_id':p['plan_id'],'idempotency_key':str(uuid.uuid4())})


def test_real_episode_and_capability_processors_respect_independent_evidence(tmp_path):
    from lib.process_memory import ProcessMemoryStore
    store=ProcessMemoryStore(tmp_path/'process-memory/records.json')
    store.record_trajectory({'process_memory_id':'actual-receipt','text':'Task result checked by actual test','bank_id':'bank-a','outcome':'correct',
        'model_profile':{'family':'model-a','version':'1'},'task_archetype':['coding'],'phase':'verify',
        'verification_evidence':[{'verifier_kind':'automated_test','status':'passed','id':'actual-test'}]})
    store.record_trajectory({'process_memory_id':'unverified-claim','text':'Assistant says worked','bank_id':'bank-a','outcome':'correct','model_profile':{'family':'model-a','version':'1'},'verification_evidence':[]})
    e=engine(tmp_path); result=e.run_job(start(e,['agent_episodes','agent_capabilities']))['job']
    records=store.all(); episodes=[r for r in records if r['kind']=='episode']; caps=[r for r in records if r['kind']=='capability_observation']
    assert len(episodes)==len(caps)==1
    assert episodes[0]['source_trace_ids']==['actual-receipt'] and caps[0]['bank_id']=='bank-a'
    assert result['status']=='review_pending' # unverified claim remains held
    e.run_job(start(e,['agent_episodes','agent_capabilities']))
    assert len([r for r in store.all() if r['kind']=='capability_observation'])==1


def test_cancel_during_last_processor_never_reports_complete(tmp_path,monkeypatch):
    from lib.memory_recovery_processors import ProcessorAdapter
    e=engine(tmp_path); jid=start(e,['agent_revalidation'])
    def processor(adapter,job,dims):
        e.dispatch({'action':'cancel','bank_id':'bank-a','job_id':jid})
        return {'status':'complete','processed':0,'total':0,'failed':0}
    monkeypatch.setattr(ProcessorAdapter,'agent_process',processor)
    assert e.run_job(jid)['job']['status']=='cancelled'


def test_toolcall_appended_after_preview_is_deferred(tmp_path):
    from lib.process_memory import ProcessMemoryStore
    sid=str(uuid.uuid4()); path=rollout(tmp_path,sid); e=engine(tmp_path)
    e.queue.capture(sid,1,'bank-a','/original-project','user',{})
    preview=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['agent_traces']})['preview']
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','timestamp':'2026-10-01T01:05:00Z','payload':{'type':'function_call','name':'new_tool_deferred','call_id':'new-call','arguments':'not copied'}})+'\n')
    job=e.dispatch({'action':'start','bank_id':'bank-a','plan_id':preview['plan_id'],'idempotency_key':str(uuid.uuid4())})['job']
    e.run_job(job['job_id'])
    assert not ProcessMemoryStore(tmp_path/'process-memory/records.json').all()
    e.run_job(start(e,['agent_traces']))
    assert len(ProcessMemoryStore(tmp_path/'process-memory/records.json').all())==1


def test_actual_remote_task_payload_contents_retry_and_explicit_resume(tmp_path):
    op=str(uuid.uuid4())
    class ActualRemote(FakeClient):
        def __init__(self): super().__init__(); self.operations[op]='failed'
        def _request(self,method,path,body=None,**kwargs):
            if method=='GET' and '/operations?' in path:
                self.calls.append((method,path,body))
                assert 'type=batch_retain&exclude_parents=true' in path
                return {'operations':[{'id':op,'task_type':'batch_retain','status':self.operations[op]}],'total':1}
            if method=='GET' and '?include_payload=true' in path:
                self.calls.append((method,path,body))
                return {'status':self.operations[op],'task_payload':{'contents':[{'content':'Actual preserved raw source','document_id':'original-doc',
                    'metadata':{'project':'/actual-project','session_ids':'actual-session','retained_at':'2026-10-01T00:00:00Z'}}]}}
            return super()._request(method,path,body,**kwargs)
    c=ActualRemote(); e=engine(tmp_path,c); jid=start(e)
    assert e.run_job(jid)['job']['status']=='running'
    assert len([v for v in c.calls if v[0]=='POST' and v[1].endswith('/retry')])==1
    c.operations[op]='failed'
    assert e.run_job(jid)['job']['status']=='waiting_provider'
    e.dispatch({'action':'resume','bank_id':'bank-a','job_id':jid})
    assert e.run_job(jid)['job']['status']=='running'
    assert len([v for v in c.calls if v[0]=='POST' and v[1].endswith('/retry')])==2
    c.operations[op]='completed'
    assert e.run_job(jid)['job']['status']=='complete'


def test_explicit_resume_resets_failed_local_batch_retry_budget(tmp_path):
    c=FakeClient(); e=engine(tmp_path,c); capture(tmp_path); jid=start(e); e.run_job(jid)
    op=next(v[2]['operation_id'] for v in c.calls if v[0]=='POST' and v[1].endswith('/memories'))
    c.operations[op]='failed'; assert e.run_job(jid)['job']['status']=='running'
    c.operations[op]='failed'; assert e.run_job(jid)['job']['status']=='waiting_provider'
    e.dispatch({'action':'resume','bank_id':'bank-a','job_id':jid}); assert e.run_job(jid)['job']['status']=='running'
    c.operations[op]='completed'; assert e.run_job(jid)['job']['status']=='complete'
    assert len([v for v in c.calls if v[0]=='POST' and v[1].endswith('/memories')])==1


def test_explicit_resume_resets_failed_consolidation_retry_budget(tmp_path):
    op=str(uuid.uuid4())
    class Consolidation(FakeClient):
        def __init__(self): super().__init__(); self.pending=1
        def _request(self,method,path,body=None,**kwargs):
            if method=='GET' and path.endswith('/stats'): return {'pending_consolidation':self.pending,'failed_consolidation':0}
            if method=='POST' and path.endswith('/consolidate'):
                self.calls.append((method,path,body)); self.operations[op]='processing'; return {'operation_id':op}
            return super()._request(method,path,body,**kwargs)
    c=Consolidation(); e=engine(tmp_path,c); jid=start(e,['observations']); e.run_job(jid)
    c.operations[op]='failed'; assert e.run_job(jid)['job']['status']=='running'
    c.operations[op]='failed'; assert e.run_job(jid)['job']['status']=='waiting_provider'
    e.dispatch({'action':'resume','bank_id':'bank-a','job_id':jid}); assert e.run_job(jid)['job']['status']=='running'
    c.operations[op]='completed'; c.pending=0; assert e.run_job(jid)['job']['status']=='complete'


def test_remote_ingestion_date_is_not_original_source_date(tmp_path):
    op=str(uuid.uuid4())
    class RemoteOldSource(FakeClient):
        def _request(self,method,path,body=None,**kwargs):
            if method=='GET' and '/operations?' in path: return {'operations':[{'id':op,'status':'failed'}],'total':1}
            if method=='GET' and '?include_payload=true' in path:
                return {'created_at':'2026-10-01T00:00:00Z','task_payload':{'contents':[{'content':'[role:user;at:2026-01-01] January original source',
                    'metadata':{'project':'/project','session_ids':'old-session','retained_at':'2026-10-01T00:00:00Z'}}]}}
            return super()._request(method,path,body,**kwargs)
    c=RemoteOldSource(); e=engine(tmp_path,c)
    jid=start(e,mode='date_range',**{'from':'2026-10-01T00:00:00Z','to':'2026-10-31T00:00:00Z'})
    result=e.run_job(jid)['job']
    assert result['status']=='review_pending'
    assert not [call for call in c.calls if call[0]=='POST']


def test_empty_revalidation_does_not_use_unrelated_store_size_as_work_total(tmp_path):
    from lib.process_memory import ProcessMemoryStore
    store=ProcessMemoryStore(tmp_path/'process-memory/records.json')
    for i in range(30): store.record_trajectory({'process_memory_id':f'trace-{i}','text':'Unrelated stable observed source','bank_id':'bank-a'})
    e=engine(tmp_path); result=e.run_job(start(e,['agent_revalidation']))['job']
    assert result['stages'][0]['status']=='no_eligible_evidence'
    assert result['stages'][0]['processed']==result['stages'][0]['total']==0


@pytest.mark.parametrize('prior_status',['queued','running'])
def test_lost_detached_worker_status_is_resumable_with_saved_cohort(tmp_path,prior_status):
    from lib.memory_recovery import atomic,read_json,lock
    e=engine(tmp_path); jid=start(e,['agent_revalidation']); path=e.job_path(jid)
    original=read_json(path); original.update(status=prior_status,updated_at='2000-01-01T00:00:00Z'); atomic(path,original)
    status=e.dispatch({'action':'status','bank_id':'bank-a','job_id':jid})['job']
    assert status['status']=='failed' and status['can_resume'] and status['error_code']=='recovery_worker_interrupted'
    e.dispatch({'action':'resume','bank_id':'bank-a','job_id':jid})
    assert read_json(path)['scope']==original['scope']
    assert e.run_job(jid)['job']['status']=='complete'


def test_status_never_takes_over_active_runner_lock(tmp_path):
    from lib.memory_recovery import atomic,read_json,lock
    e=engine(tmp_path); jid=start(e,['agent_revalidation']); path=e.job_path(jid)
    original=read_json(path); original.update(status='running',updated_at='2000-01-01T00:00:00Z'); atomic(path,original)
    with lock(path.with_suffix('.runner.lock')):
        assert e.dispatch({'action':'status','bank_id':'bank-a','job_id':jid})['job']['status']=='running'
    assert e.dispatch({'action':'cancel','bank_id':'bank-a','job_id':jid})['job']['status']=='cancelled'


def test_process_cohort_defers_external_records_added_between_preview_start_and_run(tmp_path):
    from lib.process_memory import ProcessMemoryStore
    store=ProcessMemoryStore(tmp_path/'process-memory/records.json')
    def trace(identity):
        store.record_trajectory({'process_memory_id':identity,'text':'Original independent task check','bank_id':'bank-a','outcome':'correct','phase':'verify',
            'model_profile':{'family':'model-a'},'verification_evidence':[{'verifier_kind':'automated_test','status':'passed','id':identity+'-test'}]})
    trace('before-preview'); e=engine(tmp_path); p=e.dispatch({'action':'preview','bank_id':'bank-a','dimensions':['agent_capabilities']})['preview']
    trace('background-before-start')
    job=e.dispatch({'action':'start','bank_id':'bank-a','plan_id':p['plan_id'],'idempotency_key':str(uuid.uuid4())})['job']
    trace('background-after-start'); e.run_job(job['job_id'])
    assert [r['sample_id'] for r in store.all() if r['kind']=='capability_observation']==['before-preview']
