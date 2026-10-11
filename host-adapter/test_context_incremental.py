import json
import uuid
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from lib.context_summary import read_context_index


def write_runtime_settings(state_root, overrides=None):
    """Explicit recording authorization for isolated worker/recovery fixtures."""
    settings = {'schema':'evolving-profile.runtime-settings.v1', 'generation':1,
                'routing':{'ep_enabled':True}, 'modules':{'scenario_summary':{'record':True}}}
    for key, value in (overrides or {}).items():
        if key in {'routing', 'modules'} and isinstance(value, dict):
            settings[key].update(value)
        else:
            settings[key] = value
    path = Path(state_root) / 'config/runtime-settings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))
    return path


def rollout(root, sid, text='Review the deployment gap', cwd='/workspace/demo', created_at='2026-10-08T01:00:00Z'):
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'rollout-2026-10-08-{sid}.jsonl'
    rows = [
        {'type': 'session_meta', 'timestamp':created_at, 'payload': {'id': sid, 'cwd': cwd, 'timestamp':created_at}},
        {'type': 'turn_context', 'payload': {'turn_id': 't1', 'model': 'actual-model', 'cwd': cwd}},
        {'type': 'response_item', 'timestamp': '2026-10-08T01:00:00Z', 'payload': {
            'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def test_discovery_is_debounced_bounded_and_updates_navigation_without_model(tmp_path):
    from lib.context_incremental import ContextIncremental
    root = tmp_path / 'sessions'
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for sid in ids: rollout(root, sid)
    pipeline = ContextIncremental(tmp_path / 'state', root)
    assert pipeline.discover(now=100, limit=2)['discovered'] == 2
    assert pipeline.claim(now=101) is None
    assert pipeline.discover(now=101, limit=2)['discovered'] == 1
    assert pipeline.progress()['pending'] == 3
    session = read_context_index(pipeline.index)['sessions'][0]
    assert session['status'] == 'raw_available_summary_pending'
    assert 'summary' not in session or not any(session['summary'].values())
    assert session['source_stat_revision']
    assert session['title'] == 'Review the deployment gap'


def test_source_changed_during_processing_supersedes_old_job_without_lost_update(tmp_path):
    from lib.context_incremental import ContextIncremental
    root = tmp_path / 'sessions'; sid = str(uuid.uuid4())
    path = rollout(root, sid)
    p = ContextIncremental(tmp_path / 'state', root)
    p.discover(now=100)
    job = p.claim(now=200)
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'event_msg', 'payload': {'message': 'more'}}) + '\n')
    p.discover(now=201)
    assert p.finish(job, {'context_id': 'session:' + sid, 'session_id': sid, 'status': 'model_reviewed'}, now=202) is False
    assert p.progress()['pending'] == 1
    current = read_context_index(p.index)['sessions'][0]
    assert current['status'] == 'raw_available_summary_pending'
    assert p.claim(now=300)['revision'] != job['revision']


def test_leases_resume_after_crash_and_retry_limit_keeps_raw_available(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); rollout(tmp_path / 'sessions', sid)
    p = ContextIncremental(tmp_path / 'state', tmp_path / 'sessions')
    p.discover(now=100)
    job = p.claim(now=200)
    assert p.claim(now=201) is None
    recovered = p.claim(now=2000)
    assert recovered['session_id'] == sid
    p.fail(recovered, 'provider_unavailable', now=2001)
    last = p.claim(now=3000)
    p.fail(last, 'provider_unavailable', now=3001)
    assert p.claim(now=10000) is None
    assert p.progress()['failed'] == 1
    assert read_context_index(p.index)['sessions'][0]['status'] == 'raw_available_summary_failed'


def test_coverage_rejection_persists_safe_reason_without_changing_retry_budget(tmp_path):
    from lib.context_incremental import ContextIncremental
    from lib.memory_recovery_scenario import CoverageError
    sid=str(uuid.uuid4());rollout(tmp_path/'sessions',sid)
    p=ContextIncremental(tmp_path/'state',tmp_path/'sessions');p.discover(now=100)
    def reject(*_args,**_kwargs):
        error=CoverageError('semantic_coverage_rejected',fields=('corrections_preserved',))
        error.unrelated={'api_key':'must-never-persist'}
        raise error
    for tick in (200,1000,2000):
        result=p.run_once({},now=tick,processor=reject)
        assert result['failure_detail']=={'reason':'semantic_coverage_rejected','fields':['corrections_preserved']}
    assert result['status']=='failed'
    with p.db() as db:
        job=dict(db.execute('SELECT status,attempts,error_code FROM jobs WHERE session_id=?',(sid,)).fetchone())
    assert job=={'status':'failed','attempts':3,'error_code':'automated_source_coverage_incomplete'}
    row=read_context_index(p.index)['sessions'][0]
    assert row['summary_failure_detail']==result['failure_detail']
    assert 'must-never-persist' not in json.dumps(row)


def test_workers_claim_each_session_once_and_preserve_existing_index(tmp_path):
    from lib.context_incremental import ContextIncremental
    from lib.context_summary import write_context_index
    root = tmp_path / 'sessions'
    for _ in range(8): rollout(root, str(uuid.uuid4()))
    p = ContextIncremental(tmp_path / 'state', root)
    write_context_index(p.index, [{'session_id': 'legacy', 'context_id': 'session:legacy'}], [])
    p.discover(now=100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda _: p.claim(now=200), range(8)))
    assert len({j['session_id'] for j in jobs}) == 8
    assert any(r['session_id'] == 'legacy' for r in read_context_index(p.index)['sessions'])


def test_worker_uses_ep_provider_and_failure_never_publishes_seed(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); rollout(tmp_path / 'sessions', sid)
    p = ContextIncremental(tmp_path / 'state', tmp_path / 'sessions')
    p.discover(now=100)
    def unavailable(source, row, session_root, config):
        assert config['EVOLVING_PROFILE_API_LLM_MODEL'] == 'ep-configured'
        raise ConnectionError('private endpoint/key must not enter progress')
    result = p.run_once({'EVOLVING_PROFILE_API_LLM_MODEL': 'ep-configured'}, now=200, processor=unavailable)
    assert result['status'] == 'retrying'
    assert 'private endpoint' not in json.dumps(p.progress())
    assert read_context_index(p.index)['sessions'][0]['status'] == 'raw_available_summary_pending'


def test_successful_processing_updates_index_and_coverage_and_preserves_model_identity(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); rollout(tmp_path / 'sessions', sid)
    p = ContextIncremental(tmp_path / 'state', tmp_path / 'sessions')
    p.discover(now=100)
    def checked(source, row, session_root, config):
        return {**row, 'status': 'model_reviewed', 'summary': {'compact': 'summary'},
                'source_revision': source['source_revision'], 'summary_model': config['EVOLVING_PROFILE_API_LLM_MODEL']}
    assert p.run_once({'EVOLVING_PROFILE_API_LLM_MODEL': 'ep-model'}, now=200, processor=checked)['status'] == 'complete'
    row = read_context_index(p.index)['sessions'][0]
    assert row['summary_kind'] == 'canonical_model_summary'
    assert row['summary_model'] == 'ep-model'
    assert p.progress()['succeeded'] == 1
    assert p.discover(now=300)['discovered'] == 0


def test_capacity_deferral_is_partial_coverage_and_eventually_discovers_unqueued_sources(tmp_path):
    from lib.context_incremental import ContextIncremental
    for _ in range(2): rollout(tmp_path / 'sessions', str(uuid.uuid4()))
    p = ContextIncremental(tmp_path / 'state', tmp_path / 'sessions', capacity=1)
    assert p.discover(now=100)['capacity_deferred'] == 1
    job = p.claim(now=200); p.fail(job, 'source_over_budget', now=201, terminal=True)
    assert p.progress()['status'] != 'complete'
    assert p.discover(now=300)['discovered'] == 1


def test_new_navigation_row_reads_raw_fallback_and_exposes_background_progress(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); root = tmp_path / 'sessions'
    rollout(root, sid)
    p = ContextIncremental(tmp_path / 'state', root); p.discover(now=100)
    monkeypatch.setattr(mcp, 'CONTEXT_INDEX_PATH', p.index)
    monkeypatch.setattr(mcp, 'THREAD_SESSION_ROOT', root)
    monkeypatch.setattr(mcp, 'runtime_disabled', lambda *_: None)
    monkeypatch.setattr(mcp.Path, 'home', lambda: tmp_path)
    result = json.loads(mcp.read_context_summary({'scenario_type': 'session', 'scenario_id': 'session:' + sid})['content'][0]['text'])
    assert result['status'] == 'available_unreviewed'
    assert result['items'][0]['summary_kind'] == 'raw_message_excerpt'
    assert 'Review the deployment gap' in result['items'][0]['summary']
    assert result['summary_processing']['status'] == 'raw_available_summary_pending'
    assert result['summary_processing']['foreground_model_call'] is False


def test_cli_discovery_needs_no_provider_and_queues_without_model_requests(tmp_path):
    sid = str(uuid.uuid4()); root = tmp_path / 'sessions'; rollout(root, sid)
    write_runtime_settings(tmp_path/'state')
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('context-incremental-worker.py')),
        '--state-root', str(tmp_path/'state'), '--session-root', str(root), '--discover-only'], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['discovery']['discovered'] == 1
    assert receipt['progress']['pending'] == 1
    assert receipt['model_calls'] == 0


def test_index_write_interruption_is_reconciled_next_discovery(tmp_path, monkeypatch):
    import lib.context_incremental as module
    sid = str(uuid.uuid4()); rollout(tmp_path/'sessions', sid)
    p = module.ContextIncremental(tmp_path/'state', tmp_path/'sessions')
    real_update = module.update_context_index
    def interrupted(*_args, **_kwargs): raise OSError('interrupted')
    monkeypatch.setattr(module, 'update_context_index', interrupted)
    with pytest.raises(OSError): p.discover(now=100)
    monkeypatch.setattr(module, 'update_context_index', real_update)
    assert p.discover(now=110)['discovered'] == 1
    assert read_context_index(p.index)['sessions'][0]['session_id'] == sid


def test_empty_queue_does_not_claim_coverage_complete_before_discovery(tmp_path):
    from lib.context_incremental import ContextIncremental
    p = ContextIncremental(tmp_path/'state', tmp_path/'sessions')
    assert p.progress()['status'] == 'not_discovered'


def test_disabled_scenario_recording_prevents_worker_discovery_and_models(tmp_path):
    sid = str(uuid.uuid4()); root = tmp_path / 'sessions'; rollout(root, sid)
    settings = tmp_path/'state/config/runtime-settings.json'; settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'modules':{'scenario_summary':{'record':False}}}))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('context-incremental-worker.py')),
        '--state-root', str(tmp_path/'state'), '--session-root', str(root), '--process'], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'disabled'
    assert not (tmp_path/'state/context/context-index.json').exists()


def test_source_append_before_next_discovery_marks_old_summary_stale_and_reads_raw(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); root = tmp_path/'sessions'; path = rollout(root, sid)
    p = ContextIncremental(tmp_path/'state', root); p.discover(now=100)
    p.run_once({}, now=200, processor=lambda source,row,*_: {**row, 'status':'model_reviewed','summary':{'compact':'OLD REVIEWED SUMMARY'}})
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','timestamp':'2026-10-08T02:00:00Z','payload':{
            'type':'message','role':'user','content':[{'type':'input_text','text':'The deployment gap is now corrected.'}]}})+'\n')
    monkeypatch.setattr(mcp,'CONTEXT_INDEX_PATH',p.index); monkeypatch.setattr(mcp,'THREAD_SESSION_ROOT',root)
    monkeypatch.setattr(mcp,'runtime_disabled',lambda *_:None); monkeypatch.setattr(mcp.Path,'home',lambda:tmp_path)
    result = json.loads(mcp.read_context_summary({'scenario_type':'session','scenario_id':'session:'+sid})['content'][0]['text'])
    assert result['status'] == 'available_unreviewed'
    assert result['summary_processing']['status'] == 'stale_source_pending'
    assert result['items'][0]['summary_kind'] == 'raw_message_excerpt'
    assert 'OLD REVIEWED SUMMARY' not in result['items'][0]['summary']


def test_scoped_catchup_does_not_claim_other_queued_sessions(tmp_path):
    from lib.context_incremental import ContextIncremental
    ids = sorted(str(uuid.uuid4()) for _ in range(2))
    for sid in ids: rollout(tmp_path/'sessions', sid)
    all_jobs = ContextIncremental(tmp_path/'state',tmp_path/'sessions'); all_jobs.discover(now=100)
    scoped = ContextIncremental(tmp_path/'state',tmp_path/'sessions',session_ids=[ids[1]])
    assert scoped.claim(now=200)['session_id'] == ids[1]
    assert scoped.claim(now=200) is None
    assert all_jobs.claim(now=200)['session_id'] == ids[0]


def test_no_memory_write_source_remains_raw_and_never_reaches_summary_provider(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); root = tmp_path/'sessions'
    rollout(root, sid, text='不查历史，也不要修改任何配置或记忆。只改写这一句话。')
    p = ContextIncremental(tmp_path/'state',root); p.discover(now=100)
    def provider(*_): raise AssertionError('Forbidden source reached model')
    result = p.run_once({}, now=200, processor=provider)
    assert result['status'] == 'protected'
    row = read_context_index(p.index)['sessions'][0]
    assert row['error_code'] == 'scenario_knowledge_write_prohibited'
    assert row['raw_source_files']
    assert row['status'] == 'raw_available_summary_protected'
    assert p.progress()['protected'] == 1
    assert p.progress()['failed'] == 0


def test_new_session_cutoff_does_not_enqueue_historical_backlog(tmp_path):
    from lib.context_incremental import ContextIncremental
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    rollout(tmp_path/'sessions',old,created_at='2026-09-20T01:00:00Z')
    rollout(tmp_path/'sessions',new)
    p = ContextIncremental(tmp_path/'state',tmp_path/'sessions',new_after='2026-10-08T00:00:00Z')
    assert p.discover(now=100)['discovered'] == 1
    assert p.claim(now=200)['session_id'] == new
    assert p.claim(now=200) is None
    assert p.progress()['status'] == 'partial_pending'


def test_large_native_metadata_header_is_discovered_and_later_request_titles_navigation(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); root = tmp_path/'sessions'; root.mkdir()
    path = root/f'rollout-day-{sid}.jsonl'
    rows = [
        {'type':'session_meta','timestamp':'2026-10-08T01:00:00Z','payload':{
            'id':sid,'cwd':'/workspace/native','base_instructions':{'text':'private instructions '*18000}}},
        {'type':'response_item','payload':{'type':'message','role':'developer',
            'content':[{'type':'input_text','text':'internal scaffolding '*6000}]}},
        {'type':'response_item','payload':{'type':'message','role':'user',
            'content':[{'type':'input_text','text':'Review the actual acceptance evidence'}]}},
    ]
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    p = ContextIncremental(tmp_path/'state',root)
    receipt = p.discover(now=100)
    assert receipt['discovered'] == 1
    assert read_context_index(p.index)['sessions'][0]['title'] == 'Review the actual acceptance evidence'
    assert 'private instructions' not in json.dumps(read_context_index(p.index))


def test_oversized_and_unparseable_metadata_are_visible_not_claimed_complete(tmp_path):
    from lib.context_incremental import ContextIncremental
    root = tmp_path/'sessions'; root.mkdir()
    large,bad = str(uuid.uuid4()),str(uuid.uuid4())
    (root/f'rollout-day-{large}.jsonl').write_text(json.dumps({'type':'session_meta','payload':{
        'id':large,'base_instructions':{'text':'private '*150000}}})+'\n')
    (root/f'rollout-day-{bad}.jsonl').write_text('{broken header}\n')
    p = ContextIncremental(tmp_path/'state',root)
    receipt = p.discover(now=100)
    assert receipt['discovered'] == 0
    assert receipt['oversized_headers'] == 1
    assert receipt['unparseable_headers'] == 1
    assert p.progress()['discovery_errors']['oversized_headers'] == 1
    assert p.progress()['discovery_errors']['unparseable_headers'] == 1
    assert p.progress()['status'] != 'complete'


def test_native_user_event_can_title_navigation_without_a_complete_response_item(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid = str(uuid.uuid4()); root = tmp_path/'sessions'; root.mkdir()
    path = root/f'rollout-day-{sid}.jsonl'
    path.write_text(json.dumps({'type':'session_meta','payload':{'id':sid}})+'\n'+json.dumps({
        'type':'event_msg','payload':{'type':'user_message','message':'Check the source revision before publication'}})+'\n')
    p = ContextIncremental(tmp_path/'state',root); p.discover(now=100)
    assert read_context_index(p.index)['sessions'][0]['title'] == 'Check the source revision before publication'


def test_app_page_context_cannot_be_navigation_title(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid=str(uuid.uuid4()); root=tmp_path/'sessions'; path=rollout(root,sid,text='Actual human source request')
    lines=path.read_text().splitlines(); row=json.loads(lines[2])
    row['payload']['content'][0]['text']='<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'
    lines.insert(2,json.dumps(row)); path.write_text('\n'.join(lines)+'\n')
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    assert read_context_index(p.index)['sessions'][0]['title'] == 'Actual human source request'


def test_known_partition_error_is_specific_terminal_and_private_errors_are_masked(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid=str(uuid.uuid4());root=tmp_path/'sessions';rollout(root,sid)
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    def split(*_):raise ValueError('scenario_episode_turn_split')
    result=p.run_once({},now=200,processor=split)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'scenario_episode_turn_split'
    assert read_context_index(p.index)['sessions'][0]['error_code'] == 'scenario_episode_turn_split'
    other=str(uuid.uuid4());rollout(root,other);p.discover(now=300)
    def private(*_):raise ValueError('private provider response must not be exposed')
    masked=p.run_once({},now=500,processor=private)
    assert masked['error_code'] == 'scenario_review_or_source_invalid'
    assert 'private provider response' not in json.dumps(masked)
    assert 'private provider response' not in json.dumps(read_context_index(p.index))


def test_model_schema_failures_have_bounded_retry_without_publishing(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid=str(uuid.uuid4());root=tmp_path/'sessions';rollout(root,sid)
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    def bad_model(*_):raise ValueError('scenario_state_invalid')
    assert p.run_once({},now=200,processor=bad_model)['status'] == 'retrying'
    assert p.run_once({},now=500,processor=bad_model)['status'] == 'retrying'
    result=p.run_once({},now=1000,processor=bad_model)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'scenario_state_invalid'
    assert p.claim(now=2000) is None


def test_assistant_only_native_source_is_explicitly_unusable_before_any_model_call(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid=str(uuid.uuid4());root=tmp_path/'sessions';path=rollout(root,sid)
    lines=path.read_text().splitlines();message=json.loads(lines[-1]);message['payload'].update(role='assistant',phase='final_answer')
    lines[-1]=json.dumps(message);path.write_text('\n'.join(lines)+'\n')
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    def forbidden(*_):raise AssertionError('assistant-only source reached provider')
    result=p.run_once({},now=200,processor=forbidden)
    assert result['error_code']=='scenario_source_no_user_messages'
    assert result['status']=='source_not_applicable'
    assert result['raw_fallback_available'] is True
    assert read_context_index(p.index)['sessions'][0]['error_code']=='scenario_source_no_user_messages'


def test_oversized_semantic_source_receipt_is_explicit_and_keeps_raw_fallback(tmp_path):
    from lib.context_incremental import ContextIncremental
    sid=str(uuid.uuid4());root=tmp_path/'sessions';rollout(root,sid,text='字'*60001)
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    def forbidden(*_):raise AssertionError('oversized source reached provider')
    result=p.run_once({},now=200,processor=forbidden)
    assert result['error_code']=='scenario_over_budget'
    assert result['source_status']=='over_budget'
    assert result['source_chars']==60001
    assert result['source_char_limit']==60000
    assert result['raw_fallback_available'] is True
