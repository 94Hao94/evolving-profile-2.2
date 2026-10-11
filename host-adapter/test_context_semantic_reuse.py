import json
from lib.context_associations import project_key
from lib.context_incremental import ContextIncremental
from lib.context_summary import read_context_index,write_context_index
from test_context_incremental import rollout
from test_single_episode_readback import single


def append_tool(path):
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','payload':{'type':'function_call',
            'name':'read_only_tool','arguments':'{}'}})+'\n')


def accepted(tmp_path,monkeypatch):
    mcp,path,index,row,eid=single(tmp_path,monkeypatch)
    row.update(project_key=project_key('/project'),bank_id='bank-a')
    p=ContextIncremental(tmp_path/'state',path.parent,bank_id='bank-a')
    write_context_index(p.index,[row],[])
    return p,path,row


def test_tool_append_reuses_only_accepted_same_source_without_provider(tmp_path,monkeypatch):
    p,path,row=accepted(tmp_path,monkeypatch);p.discover(now=100)
    def forbidden(*_):raise AssertionError('accepted same source reached provider')
    first=p.run_once({},now=200,processor=forbidden)
    assert first['status']=='complete' and first['summary_reused'] is True
    append_tool(path);p.discover(now=300)
    second=p.run_once({},now=400,processor=forbidden)
    assert second['status']=='complete' and second['summary_reused'] is True
    restored=read_context_index(p.index)['sessions'][0]
    assert restored['summary']==row['summary']
    assert restored['source_revision']==row['source_revision']
    assert restored['summary_reuse']['provider_calls']==0


def test_tool_append_never_replenishes_three_attempt_failure_budget(tmp_path):
    import uuid
    sid=str(uuid.uuid4());root=tmp_path/'sessions';path=rollout(root,sid)
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100);calls=[]
    def invalid(*_):calls.append(1);raise ValueError('scenario_state_invalid')
    for now in (200,500,1000):p.run_once({},now=now,processor=invalid)
    assert len(calls)==3
    for now in (2000,3000):
        append_tool(path);p.discover(now=now)
        result=p.run_once({},now=now+100,processor=invalid)
        assert result['status']=='failed' and result['error_code']=='scenario_state_invalid'
        assert result['unchanged_source'] is True
    assert len(calls)==3
    with p.db() as db:assert db.execute('SELECT attempts FROM jobs').fetchone()[0]==3


def test_actual_new_user_message_starts_new_source_budget(tmp_path):
    import uuid
    sid=str(uuid.uuid4());root=tmp_path/'sessions';path=rollout(root,sid)
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100);calls=[]
    def invalid(*_):calls.append(1);raise ValueError('scenario_state_invalid')
    for now in (200,500,1000):p.run_once({},now=now,processor=invalid)
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'turn_context','payload':{'turn_id':'new-turn'}})+'\n')
        stream.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user',
            'content':[{'type':'input_text','text':'A genuinely new request'}]}})+'\n')
    p.discover(now=2000);result=p.run_once({},now=2100,processor=invalid)
    assert result['status']=='retrying'
    assert len(calls)==4
    with p.db() as db:assert db.execute('SELECT attempts FROM jobs').fetchone()[0]==1


def test_scope_change_cannot_reuse_old_canonical_identity(tmp_path,monkeypatch):
    p,path,row=accepted(tmp_path,monkeypatch)
    p.discover(now=100);p.run_once({},now=200,processor=lambda *_:row)
    lines=path.read_text().splitlines();header=json.loads(lines[0]);header['payload']['cwd']='/different-project'
    lines[0]=json.dumps(header);path.write_text('\n'.join(lines)+'\n')
    p.discover(now=300);calls=[]
    def invalid(*_):calls.append(1);raise ValueError('scenario_state_invalid')
    result=p.run_once({},now=400,processor=invalid)
    assert result['status']=='retrying' and len(calls)==1
    assert result.get('summary_reused') is not True


def test_legacy_capped_failure_without_input_revision_is_not_granted_new_attempts(tmp_path):
    import uuid
    sid=str(uuid.uuid4());root=tmp_path/'sessions';path=rollout(root,sid)
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    with p.db() as db:
        db.execute("UPDATE jobs SET status='failed',attempts=3,error_code='scenario_state_invalid'")
    append_tool(path);p.discover(now=300)
    def forbidden(*_):raise AssertionError('unknown legacy source received new budget')
    result=p.run_once({},now=400,processor=forbidden)
    assert result['status']=='failed' and result['legacy_source_revision_unknown'] is True
    with p.db() as db:assert db.execute('SELECT attempts FROM jobs').fetchone()[0]==3


def test_unreviewed_old_summary_cannot_be_promoted_by_same_body(tmp_path,monkeypatch):
    p,path,row=accepted(tmp_path,monkeypatch)
    row['automated_source_coverage']['accept']=False
    write_context_index(p.index,[row],[]);p.discover(now=100);calls=[]
    def invalid(*_):calls.append(1);raise ValueError('scenario_state_invalid')
    result=p.run_once({},now=200,processor=invalid)
    assert len(calls)==1 and result.get('summary_reused') is not True
    assert read_context_index(p.index)['sessions'][0]['status']!='model_reviewed'


def test_protected_source_tool_append_remains_protected_without_provider(tmp_path):
    import uuid
    sid=str(uuid.uuid4());root=tmp_path/'sessions';path=rollout(root,sid,text='不要写入记忆。')
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    def forbidden(*_):raise AssertionError('protected content reached model')
    assert p.run_once({},now=200,processor=forbidden)['status']=='protected'
    append_tool(path);p.discover(now=300)
    result=p.run_once({},now=400,processor=forbidden)
    assert result['status']=='protected' and result['unchanged_source'] is True
    with p.db() as db:assert db.execute('SELECT attempts FROM jobs').fetchone()[0]==1


def test_final_answer_and_context_metadata_change_require_new_source_review(tmp_path,monkeypatch):
    p,path,row=accepted(tmp_path,monkeypatch);p.discover(now=100)
    p.run_once({},now=200,processor=lambda *_:row);calls=[]
    def invalid(*_):calls.append(1);raise ValueError('scenario_state_invalid')
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer',
            'content':[{'type':'output_text','text':'A new final answer'}]}})+'\n')
    p.discover(now=300);assert p.run_once({},now=400,processor=invalid)['status']=='retrying'
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user',
            'content':[{'type':'input_text','text':'<external_codex_apps_open_page>{"page_id":"new-context"}</external_codex_apps_open_page>'}]}})+'\n')
    p.discover(now=500);assert p.run_once({},now=700,processor=invalid)['status']=='retrying'
    assert len(calls)==2


def test_reuse_finish_rechecks_concurrent_raw_source_change_under_lock(tmp_path,monkeypatch):
    p,path,row=accepted(tmp_path,monkeypatch);p.discover(now=100);finish=p.finish
    def race(job,published,**kwargs):
        with path.open('a') as stream:
            stream.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user',
                'content':[{'type':'input_text','text':'Source changed concurrently'}]}})+'\n')
        return finish(job,published,**kwargs)
    monkeypatch.setattr(p,'finish',race)
    result=p.run_once({},now=200,processor=lambda *_:(_ for _ in ()).throw(AssertionError('provider called')))
    assert result['status']=='superseded'
    assert read_context_index(p.index)['sessions'][0]['status']!='model_reviewed'


def test_valid_independent_canonical_can_restore_after_legacy_failure_without_new_model(tmp_path,monkeypatch):
    p,path,row=accepted(tmp_path,monkeypatch);p.discover(now=100)
    with p.db() as db:db.execute("UPDATE jobs SET status='failed',attempts=3,error_code='scenario_state_invalid'")
    write_context_index(p.index,[row],[]);append_tool(path);p.discover(now=300)
    result=p.run_once({},now=400,processor=lambda *_:(_ for _ in ()).throw(AssertionError('provider called')))
    assert result['status']=='complete' and result['summary_reused'] is True
    reuse=read_context_index(p.index)['sessions'][0]['summary_reuse']
    assert reuse['prior_job_status']=='failed' and reuse['prior_error_code']=='scenario_state_invalid'
    assert reuse['prior_attempts']==3


def test_legacy_hold_records_only_current_baseline_then_new_actual_request_can_progress(tmp_path):
    import uuid
    sid=str(uuid.uuid4());root=tmp_path/'sessions';path=rollout(root,sid)
    p=ContextIncremental(tmp_path/'state',root);p.discover(now=100)
    with p.db() as db:db.execute("UPDATE jobs SET status='failed',attempts=3,error_code='scenario_state_invalid'")
    append_tool(path);p.discover(now=300)
    def forbidden(*_):raise AssertionError('legacy unknown reached provider')
    assert p.run_once({},now=400,processor=forbidden)['legacy_source_revision_unknown'] is True
    with p.db() as db:
        witness=json.loads(db.execute("SELECT value FROM metadata WHERE key=?",('legacy-observed:'+sid,)).fetchone()[0])
        assert witness['previous_input_revision']=='unknown'
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user',
            'content':[{'type':'input_text','text':'New request after the witnessed hold'}]}})+'\n')
    p.discover(now=500);calls=[]
    def invalid(*_):calls.append(1);raise ValueError('scenario_state_invalid')
    assert p.run_once({},now=700,processor=invalid)['status']=='retrying'
    assert len(calls)==1


def test_completed_transport_append_with_lost_review_evidence_fails_closed(tmp_path,monkeypatch):
    p,path,row=accepted(tmp_path,monkeypatch);p.discover(now=100)
    p.run_once({},now=200,processor=lambda *_:row)
    value=read_context_index(p.index);value['sessions'][0]['automated_source_coverage']['accept']=False
    write_context_index(p.index,value['sessions'],value['projects']);append_tool(path);p.discover(now=300)
    result=p.run_once({},now=400,processor=lambda *_:(_ for _ in ()).throw(AssertionError('provider called')))
    assert result['error_code']=='scenario_accepted_summary_unavailable'
    assert result['provider_calls']==0
