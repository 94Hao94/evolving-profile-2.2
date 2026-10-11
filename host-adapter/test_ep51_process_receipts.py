import contextlib
import io
import json
import importlib
import hashlib
from pathlib import Path

import pytest


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    module = importlib.import_module('evolving_profile_controller_mcp')
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(module, 'PROCESS_MEMORY_PATH', tmp_path / 'process.json')
    monkeypatch.setattr(module, 'runtime_disabled', lambda *_: None)
    monkeypatch.setattr(module, 'project_tool_review', lambda *_: {'status': 'disabled'})
    root = tmp_path / '.evolving-profile/audit'
    root.mkdir(parents=True)
    monkeypatch.setenv('EVOLVING_PROFILE_ROUTE_RECEIPT_ROOT', str(root/'memory-route-receipts'))
    (root / 'prompt-ingress.jsonl').write_text(json.dumps({
        'at': '2026-10-01T01:00:00Z', 'hook_invocation_id': 'c1',
        'session_id': 's1', 'turn_id': 't1', 'project_id': 'p1',
        'cwd': '/workspace/p1', 'transcript_path': '/raw/s1.jsonl',
        'model': 'configured-model',
    }) + '\n')
    return module


def invoke(mcp, tool, body, args=None):
    mcp.CURRENT_TOOL_CALL = {'name': tool, 'arguments': args or {'check_id': 'c1'},
                             'tool_call_id': 'call-7'}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mcp.reply(7, {'content': [{'type': 'text', 'text': json.dumps(body)}], 'isError': False})
    finally:
        mcp.CURRENT_TOOL_CALL = None


def test_source_dict_body_records_activity_and_completed_trace(mcp, tmp_path):
    invoke(mcp, 'read_source', {'memory': {'id': 'm1'},
                              'source': {'text': 'original', 'document_id': 'd1', 'offset': 0, 'end': 8}})
    event = json.loads((tmp_path / '.evolving-profile/audit/mcp-tool-activity.jsonl').read_text())
    assert event['source_read_count'] == 1
    assert event['tool_call_id'] == 'call-7'
    trace = json.loads((tmp_path / 'process.json').read_text())['records'][0]
    assert trace['primary_context']['session_id'] == 's1'
    assert trace['primary_context']['turn_id'] == 't1'
    assert trace['primary_context']['project_id'] == 'p1'
    assert trace['outcome'] == 'correct'
    assert trace['tool_result']['returned_count'] == 1
    assert trace['task_archetype'] != ['coordination_multi_agent']
    assert trace['source_locator']['transcript_path'] == '/raw/s1.jsonl'
    assert trace['source_locator']['tool_call_id'] == event['tool_call_id']


def test_unavailable_result_is_not_tool_success(mcp, tmp_path):
    invoke(mcp, 'read_scenario_summary', {'status': 'source_missing', 'items': []})
    trace = json.loads((tmp_path / 'process.json').read_text())['records'][0]
    assert trace['outcome'] == 'ambiguous'
    assert trace['tool_result']['status'] == 'source_missing'


def test_stale_check_does_not_supply_current_context(mcp, tmp_path):
    path = tmp_path / '.evolving-profile/audit/prompt-ingress.jsonl'
    with path.open('a') as stream:
        stream.write(json.dumps({'at': '2026-10-02T01:00:00Z', 'session_id': 's1',
                                 'turn_id': 't2', 'hook_invocation_id': 'c2'}) + '\n')
    invoke(mcp, 'user_recall', {'memories': []})
    trace = json.loads((tmp_path / 'process.json').read_text())['records'][0]
    assert trace['primary_context'] == {}
    assert trace['binding_state'] == 'stale_prompt_binding'


def test_conflicting_check_identity_is_never_bound(mcp, tmp_path):
    path = tmp_path / '.evolving-profile/audit/prompt-ingress.jsonl'
    with path.open('a') as stream:
        stream.write(json.dumps({'at': '2026-10-01T02:00:00Z', 'session_id': 's2',
                                 'turn_id': 't2', 'hook_invocation_id': 'c1'}) + '\n')
    value = mcp._prompt_binding_for_check_id('c1', '2026-10-01T03:00:00Z')
    assert value['state'] == 'ambiguous_prompt_binding'
    assert not value.get('session_id')


def test_future_ingress_is_not_bound_before_its_occurrence(mcp):
    value = mcp._prompt_binding_for_check_id('c1', '2026-09-01T03:00:00Z')
    assert value['state'] == 'unknown_check_id_at_call_time'


def test_failed_occurrence_has_same_identity_in_activity_and_process(mcp, tmp_path):
    mcp.CURRENT_TOOL_CALL = {'name':'read_source','arguments':{'check_id':'c1'},'tool_call_id':'failed-call'}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mcp.reply(1, error={'code':-32000,'message':'source unavailable'})
    finally: mcp.CURRENT_TOOL_CALL = None
    event = json.loads((tmp_path / '.evolving-profile/audit/mcp-tool-activity.jsonl').read_text())
    assert event['tool_call_id'] == 'failed-call'
    assert event['plane'] == 'user_memory'
    trace = json.loads((tmp_path/'process.json').read_text())['records'][0]
    assert trace['outcome'] == 'blocked'
    assert trace['tool_call_id'] == 'failed-call'


def test_scenario_schema_accepts_caller_plane_purpose(mcp):
    assert mcp.SCENARIO_SUMMARY_TOOL['inputSchema']['properties']['purpose']['enum'] == ['user_memory','agent_process','navigation']


def test_capture_receipt_is_not_projection_of_retrieval(mcp, tmp_path):
    invoke(mcp,'record_agent_trajectory',{'status':'recorded','record':{'process_memory_id':'p'}})
    event = json.loads((tmp_path/'.evolving-profile/audit/mcp-tool-activity.jsonl').read_text())
    assert event['plane'] == 'agent_process'
    assert event['stage'] == 'capture'
    assert not (tmp_path/'process.json').exists()


def failed_reply(mcp, check_id='c1'):
    mcp.CURRENT_TOOL_CALL = {'name':'read_source','arguments':{'check_id':check_id},'tool_call_id':'failed-call'}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mcp.reply(1,error={'code':-32000,'message':'source unavailable'})
    finally: mcp.CURRENT_TOOL_CALL = None


@pytest.mark.parametrize('order', ['failure_first','success_first'])
def test_success_and_failure_update_one_canonical_bound_receipt(mcp, tmp_path, order):
    root = tmp_path/'.evolving-profile/audit/memory-route-receipts'; root.mkdir()
    target = root/(hashlib.sha256(b'c1').hexdigest()+'.json')
    target.write_text(json.dumps({'check_id':'c1','route_required':True,'tool_events':[],
        'prompt_binding':{'session_id':'s1','turn_id':'t1','hook_invocation_id':'c1'}}))
    body = {'memory':{'id':'m1'},'source':{'text':'original'}}
    if order == 'failure_first':
        failed_reply(mcp); invoke(mcp,'read_source',body)
    else:
        invoke(mcp,'read_source',body); failed_reply(mcp)
    receipt = json.loads(target.read_text())
    assert len(receipt['tool_events']) == 2
    assert receipt['failed_tool_call_count'] == 1
    assert receipt['returned_count'] == 1
    assert receipt['delivery_state'] == 'partial_failure'
    assert receipt['prompt_binding'] == {'session_id':'s1','turn_id':'t1','hook_invocation_id':'c1'}
    assert sorted(p.name for p in root.glob('*.json')) == [target.name]


@pytest.mark.parametrize('failed', [True,False])
def test_receipt_with_wrong_binding_is_not_overwritten_by_either_reply(mcp,tmp_path,failed):
    root = tmp_path/'.evolving-profile/audit/memory-route-receipts'; root.mkdir()
    target = root/(hashlib.sha256(b'c1').hexdigest()+'.json')
    original = json.dumps({'check_id':'c1','prompt_binding':{'session_id':'OTHER','turn_id':'other','hook_invocation_id':'c1'},'tool_events':[]})
    target.write_text(original)
    if failed: failed_reply(mcp)
    else: invoke(mcp,'read_source',{'memory':{'id':'m1'},'source':{'text':'original'}})
    assert target.read_text() == original
    assert sorted(p.name for p in root.glob('*.json')) == [target.name]


def test_failed_check_id_cannot_escape_receipt_root(mcp,tmp_path):
    ingress = tmp_path/'.evolving-profile/audit/prompt-ingress.jsonl'
    row = json.loads(ingress.read_text()); row['hook_invocation_id'] = '../escaped'
    ingress.write_text(json.dumps(row)+'\n')
    failed_reply(mcp,'../escaped')
    root = tmp_path/'.evolving-profile/audit/memory-route-receipts'
    assert [p.name for p in root.glob('*.json')] == [hashlib.sha256(b'../escaped').hexdigest()+'.json']
    assert not (root.parent/'escaped.json').exists()


@pytest.mark.parametrize('failed',[True,False])
def test_binding_at_dispatch_survives_later_prompt_before_reply(mcp,tmp_path,failed):
    call = mcp._begin_tool_call({'name':'read_source','arguments':{'check_id':'c1'}},7,
                               started_at='2026-10-01T02:00:00Z')
    ingress = tmp_path/'.evolving-profile/audit/prompt-ingress.jsonl'
    with ingress.open('a') as stream:
        stream.write(json.dumps({'at':'2026-10-01T03:00:00Z','session_id':'s1',
            'turn_id':'t2','hook_invocation_id':'c2'})+'\n')
    mcp.CURRENT_TOOL_CALL = call
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mcp.reply(7,error={'code':-32000,'message':'failed after dispatch'}) if failed else mcp.reply(7,
                {'content':[{'type':'text','text':json.dumps({'memory':{'id':'m'},'source':{'text':'original'}})}],'isError':False})
    finally:mcp.CURRENT_TOOL_CALL=None
    trace = json.loads((tmp_path/'process.json').read_text())['records'][0]
    assert trace['primary_context']['turn_id'] == 't1'
    assert trace['binding_state'] == 'prompt_bound'
    event = json.loads((tmp_path/'.evolving-profile/audit/mcp-tool-activity.jsonl').read_text())
    assert event['turn_id'] == 't1'
    assert event['invocation_started_at'] == '2026-10-01T02:00:00Z'
    root = tmp_path/'.evolving-profile/audit/memory-route-receipts'
    receipt = json.loads((root/(hashlib.sha256(b'c1').hexdigest()+'.json')).read_text())
    assert receipt['prompt_binding']['turn_id'] == 't1'
    assert not (root/(hashlib.sha256(b'c2').hexdigest()+'.json')).exists()


def test_dispatch_does_not_admit_already_stale_binding(mcp,tmp_path):
    ingress = tmp_path/'.evolving-profile/audit/prompt-ingress.jsonl'
    with ingress.open('a') as stream:
        stream.write(json.dumps({'at':'2026-10-01T03:00:00Z','session_id':'s1',
            'turn_id':'t2','hook_invocation_id':'c2'})+'\n')
    call = mcp._begin_tool_call({'name':'read_source','arguments':{'check_id':'c1'}},7,
                               started_at='2026-10-01T04:00:00Z')
    assert call['binding_at_start']['state'] == 'stale_prompt_binding'


@pytest.mark.parametrize('body',[{'status':'returned','source':'external_rag','results':[{'id':'r'}]},
                                 {'status':'disabled','source':'external_rag','results':[]}])
def test_rag_reply_and_activity_share_external_plane_without_provider_calls(mcp,tmp_path,body):
    mcp.CURRENT_TOOL_CALL={'name':'rag_search','arguments':{'check_id':'c1'},'tool_call_id':'rag-call'}
    output=io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            mcp.reply(1,{'content':[{'type':'text','text':json.dumps(body)}],'isError':False})
    finally:mcp.CURRENT_TOOL_CALL=None
    value=json.loads(json.loads(output.getvalue())['result']['content'][0]['text'])
    event=json.loads((tmp_path/'.evolving-profile/audit/mcp-tool-activity.jsonl').read_text())
    assert value['plane'] == 'external_rag'
    assert event['plane'] == 'external_rag'
    assert value['stage'] == event['stage'] == 'retrieve'


def test_error_body_exposes_same_occurrence_binding_and_keeps_recovery_data(mcp,tmp_path):
    mcp.CURRENT_TOOL_CALL=mcp._begin_tool_call({'name':'read_source','arguments':{'check_id':'c1'}},7,
        started_at='2026-10-01T02:00:00Z')
    output=io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            mcp.reply(7,error={'code':-32000,'message':'unavailable','data':{'recovery':{'state':'attempted'}}})
    finally:mcp.CURRENT_TOOL_CALL=None
    data=json.loads(output.getvalue())['error']['data']
    event=json.loads((tmp_path/'.evolving-profile/audit/mcp-tool-activity.jsonl').read_text())
    assert data['tool_call_id'] == event['tool_call_id']
    assert data['observability_binding']['turn_id'] == 't1'
    assert data['recovery'] == {'state':'attempted'}
