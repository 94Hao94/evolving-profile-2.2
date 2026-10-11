"""Replay the real repeated 206/190-character failure, without a provider."""
import io
import json
import pytest
from lib.scenario_model import request_session_state_draft
from lib.scenario_state_v3 import validate_state_draft


def fixture():
    return {'thread_id':'test-session','status':'complete','source_revision':'revision',
        'source_files':[],'messages':[
            {'role':'user','text':'Prepare an image quality audit.','evidence_id':'u1'},
            {'role':'assistant','text':'I report checks, not external verification.','evidence_id':'a1'}]}


def state(long=True):
    def claim(text,mid):return {'text':text,'message_ids':[mid]}
    return {'subject':claim('Image quality','u1'),'goal':claim('x'*206 if long else 'Audit images','u1'),
        'phase':'assistant_reported','constraints':[],'corrections':[],
        'assistant_reports':[claim('y'*190 if long else 'Reported checks; unverified','a1')],'unresolved':[]}


def test_retry_identifies_exact_overlong_fields_instead_of_repeating_generic_error():
    calls=[]
    def opener(request,**_):
        body=json.loads(request.data);prompt=body['messages'][0]['content'];calls.append(prompt)
        diagnosed=all(word in prompt for word in ['text_over_limit','goal/text','assistant_reports/0/text','206','190'])
        result={'source_revision':'revision','state':state(long=not diagnosed)}
        return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]}).encode())
    draft=request_session_state_draft(fixture(),base_url='https://fixture.invalid',api_key='test-not-a-key',model='fixture',opener=opener)
    assert len(calls)==2
    assert draft['state']['goal']['text']=='Audit images'
    assert len(draft['state']['assistant_reports'][0]['text'])<180


def test_invalid_state_preserves_code_and_reports_only_controlled_safe_diagnostics():
    with pytest.raises(ValueError,match='^scenario_state_invalid$') as caught:
        validate_state_draft(fixture(),state(),model='fixture')
    detail=caught.value.safe_detail()
    assert detail['reason']=='state_contract_violations'
    assert detail['fields']==['goal/text','assistant_reports/0/text']
    assert detail['violations']==[
        {'field':'goal/text','reason':'text_over_limit','actual':206,'maximum':180},
        {'field':'assistant_reports/0/text','reason':'text_over_limit','actual':190,'maximum':180}]
    assert 'xxx' not in json.dumps(detail) and 'yyy' not in json.dumps(detail)


def test_structure_diagnostics_do_not_allow_role_or_source_forgery():
    bad=state(False);bad['goal']['message_ids']=['a1']
    with pytest.raises(ValueError,match='scenario_state_role_invalid'):
        validate_state_draft(fixture(),bad,model='fixture')
    bad['goal']['message_ids']=['missing']
    with pytest.raises(ValueError,match='scenario_evidence_id_invalid'):
        validate_state_draft(fixture(),bad,model='fixture')


def test_safe_detail_rejects_arbitrary_exception_attributes_and_nested_values():
    from lib.scenario_state_v3 import StateValidationError
    error=StateValidationError([
        {'field':'goal/text','reason':'text_over_limit','actual':206,'maximum':180,'secret':'not-to-be-stored'},
        {'field':'apikey_secret','reason':'text_over_limit','actual':999},
        {'field':'constraints/0/text','reason':{'secret':'must-not-leak'}},
        {'field':'subject/text','reason':'text_invalid','actual':{'secret':'nested'}},
    ])
    error.raw_response='credential-do-not-store'
    detail=error.safe_detail()
    assert detail['fields']==['goal/text','subject/text']
    assert all(text not in json.dumps(detail) for text in ['secret','credential','nested','not-to-be-stored'])


def test_worker_persists_contract_diagnostics_and_retains_attempts(tmp_path):
    from lib.context_incremental import ContextIncremental
    from lib.context_summary import read_context_index
    from test_context_incremental import rollout
    import uuid
    sid=str(uuid.uuid4());rollout(tmp_path/'sessions',sid)
    p=ContextIncremental(tmp_path/'state',tmp_path/'sessions');p.discover(now=100)
    def invalid(*_):validate_state_draft(fixture(),state(),model='fixture')
    for tick in (200,1000,2000):result=p.run_once({},now=tick,processor=invalid)
    assert result['status']=='failed' and result['error_code']=='scenario_state_invalid'
    assert result['failure_detail']['violations'][0]['actual']==206
    with p.db() as db:
        assert db.execute('SELECT attempts FROM jobs WHERE session_id=?',(sid,)).fetchone()[0]==3
    row=read_context_index(p.index)['sessions'][0]
    assert row['summary_failure_detail']==result['failure_detail']


def test_feedback_covers_every_violation_of_maximum_legal_state_shape():
    bad=state(False)
    claim={'text':'x'*5001,'message_ids':[]}
    bad['subject']=dict(claim);bad['goal']=dict(claim)
    for field in ['constraints','corrections','assistant_reports','unresolved']:
        bad[field]=[dict(claim) for _ in range(8)]
    with pytest.raises(ValueError,match='scenario_state_invalid') as caught:
        validate_state_draft(fixture(),bad,model='fixture')
    detail=caught.value.safe_detail()
    assert len(detail['violations'])==68
    assert detail['total_violations']==68 and detail['truncated'] is False


def test_more_than_eight_explicit_conditions_in_one_turn_are_not_forced_to_disappear():
    from lib.scenario_state_v3 import USER_CLAIM_PROTOCOL,STATE_LIMITS
    clauses=[f'必须保留第{i}条原始限制。' for i in range(1,15)]
    source=fixture();source['messages'][0]['text']=''.join(clauses)
    candidate=state(False);candidate['constraints']=[{'text':c,'message_ids':['u1']} for c in clauses]
    draft=validate_state_draft(source,candidate,model='fixture',user_claim_protocol=USER_CLAIM_PROTOCOL)
    assert len(draft['state']['constraints'])==14
    assert all(c in draft['summaries']['full'] for c in clauses)
    assert STATE_LIMITS['claims_max_per_array']>=14


def test_english_sentence_boundaries_keep_conditions_without_splitting_urls_versions_or_numbers():
    from lib.scenario_state_v3 import whole_user_clauses
    text='Do not publish keys. Use version 3.7 with endpoint https://example.com/v1. Retry only after checking the 1.5 second timeout.'
    clauses=whole_user_clauses(text)
    assert 'Do not publish keys.' in clauses
    assert 'Use version 3.7 with endpoint https://example.com/v1.' in clauses
    assert 'Retry only after checking the 1.5 second timeout.' in clauses
    assert all(c not in clauses for c in ['7 with endpoint https://example.com/v1.','com/v1.','5 second timeout.'])


@pytest.mark.parametrize('abbreviation',['U.S.','U.K.','E.U.','J.','e.g.'])
def test_english_abbreviations_cannot_turn_negated_condition_into_positive_fragment(abbreviation):
    from lib.scenario_state_v3 import whole_user_clauses,USER_CLAIM_PROTOCOL
    text=f'Do not use {abbreviation} Government infrastructure.'
    assert 'Government infrastructure.' not in whole_user_clauses(text)
    src=fixture();src['messages'][0]['text']=text
    candidate=state(False);candidate['constraints']=[{'text':'Government infrastructure.','message_ids':['u1']}]
    with pytest.raises(ValueError,match='scenario_user_claim_not_verbatim'):
        validate_state_draft(src,candidate,model='fixture',user_claim_protocol=USER_CLAIM_PROTOCOL)


def test_exact_long_condition_uses_source_clause_budget_not_paraphrase_budget():
    from lib.scenario_state_v3 import USER_CLAIM_PROTOCOL,user_clause_catalog
    text='Only after a valid source check, '+('keep the original scope and evidence; '*5)+'never publish a claimed delivery as independently verified.'
    src=fixture();src['messages'][0]['text']=text
    candidate=state(False);candidate['constraints']=[{'text':text,'message_ids':['u1']}]
    assert len(text)>180
    draft=validate_state_draft(src,candidate,model='fixture',user_claim_protocol=USER_CLAIM_PROTOCOL)
    assert text in draft['summaries']['full']
    assert user_clause_catalog(src)[0]['clauses'][0]['eligible'] is True
    candidate['assistant_reports'][0]['text']=text
    with pytest.raises(ValueError,match='scenario_state_invalid'):
        validate_state_draft(src,candidate,model='fixture',user_claim_protocol=USER_CLAIM_PROTOCOL)


def test_many_short_same_turn_constraints_keep_all_conditions_within_tier_budget():
    from lib.scenario_state_v3 import USER_CLAIM_PROTOCOL
    conditions=[f'Keep condition {i}.' for i in range(20)]
    src=fixture();src['messages'][0]['text']=' '.join(conditions)
    candidate=state(False);candidate['constraints']=[{'text':c,'message_ids':['u1']} for c in conditions]
    draft=validate_state_draft(src,candidate,model='fixture',user_claim_protocol=USER_CLAIM_PROTOCOL)
    assert len(draft['state']['constraints'])==20 and all(c in draft['summaries']['full'] for c in conditions)


def test_code_comparison_is_not_certified_as_separate_incomplete_condition():
    from lib.scenario_state_v3 import whole_user_clauses
    text='Do not accept code!=0 as success. Keep the rejection evidence.'
    assert 'Do not accept code!=0 as success.' in whole_user_clauses(text)
    assert 'Do not accept code!' not in whole_user_clauses(text)
