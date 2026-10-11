import copy
import json
import uuid

import pytest

from lib.memory_recovery_scenario import request_source_coverage_review, publish_automated_session
from lib.context_summary import build_session_context
from lib.scenario_source import read_session_source
from lib.scenario_episodes import partition_source, validate_episode_bundle
from lib.scenario_state_v3 import validate_state_draft
from lib.scenario_model import fingerprint_draft, fingerprint_episode_bundle


def fixture(tmp_path, prefix=''):
    sid=str(uuid.uuid4()); root=tmp_path/'sessions'; root.mkdir()
    rows=[{'type':'session_meta','payload':{'id':sid,'cwd':'/project'}}]
    for i,(role,text) in enumerate([('user','请编制项目甲方案'),('assistant','我报告项目甲方案完成，但未核验'),('user','另一个任务比较项目乙预算'),('assistant','我报告已经比较，仍需核验')]):
        if i == 0: text = prefix + text
        rows.append({'type':'response_item','timestamp':f'2026-10-01T00:0{i}:00Z','payload':{'type':'message','role':role,'phase':'final_answer','content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}})
    path=root/f'rollout-test-{sid}.jsonl'; path.write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n')
    source=read_session_source(sid,root,max_chars=60000); ids=[r['evidence_id'] for r in source['messages']]
    episodes=[]
    for partition in partition_source(source,[ids[2]]):
        sub={**source,'messages':partition['_messages'],'source_revision':partition['source_revision']}; u,a=sub['messages']
        draft=validate_state_draft(sub,{'subject':{'text':u['text'],'message_ids':[u['evidence_id']]},'goal':{'text':u['text'],'message_ids':[u['evidence_id']]},'phase':'assistant_reported','constraints':[],'corrections':[], 'assistant_reports':[{'text':a['text'],'message_ids':[a['evidence_id']]}],'unresolved':[]},model='fake-generator')
        episodes.append({**{k:v for k,v in partition.items() if k!='_messages'},'draft':draft,'title':draft['state']['subject']['text'],'title_authority':'navigation_label_not_verified_fact'})
    bundle=validate_episode_bundle(source,{'schema':'evolving-profile.scenario-episode-bundle.v1','status':'source_linked_episode_draft','thread_id':sid,'parent_source_revision':source['source_revision'],'chunk_count':1,'boundary_decisions':[{'message_id':ids[2],'decision':'new_episode','method':'model_boundary_review'}],'unresolved_boundary_ids':[],'episodes':episodes})
    review={'schema':'evolving-profile.scenario-episode-review.v1','status':'model_review_passed','thread_id':sid,'parent_source_revision':source['source_revision'],'bundle_sha256':fingerprint_episode_bundle(bundle),'reviewed_episode_ids':[e['episode_id'] for e in episodes],'issues':[],'review_model':'fake-reviewer',
            'episode_reviews':[{'episode_id':e['episode_id'],'status':'model_review_passed','source_revision':e['source_revision'],'draft_sha256':fingerprint_draft(e['draft']),'issues':[]} for e in episodes]}
    return root,path,source,bundle,review


def coverage(source,bundle):
    return {'reviewer_kind':'automated_source_coverage','source_revision':source['source_revision'],'bundle_sha256':fingerprint_episode_bundle(bundle),
            'reviewed_message_ids':[m['evidence_id'] for m in source['messages']], 'reviewed_episode_ids':[e['episode_id'] for e in bundle['episodes']],
            'whole_source_topics_covered':True,'corrections_preserved':True,'assistant_claims_labeled':True,'partition_exact':True,'accept':True,'issues':[],'no_human_confirmation_claim':True}


def test_automated_gate_publishes_exact_navigation_without_human_claim(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path)
    result=publish_automated_session(build_session_context(source['thread_id'],'project',[],''),source,bundle,review,coverage(source,bundle),root)
    assert result['status']=='episode_directory_ready'
    assert result['reviewer_kind']=='automated_source_coverage'
    assert result['no_human_confirmation_claim'] is True
    assert 'manual_source_coverage' not in result and 'manual_reviewer' not in result
    assert len(result['episodes'])==2 and '人工' not in result['summary']['compact']


def test_successful_recovery_removes_active_failure_label_without_resetting_origin(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path)
    row={'status':'raw_available_summary_failed','error_code':'automated_source_coverage_incomplete','summary_kind':'not_yet_generated',
         'summary_failure_detail':{'reason':'semantic_coverage_rejected'}}
    result=publish_automated_session(row,source,bundle,review,coverage(source,bundle),root)
    assert result['status']=='episode_directory_ready'
    assert 'error_code' not in result and 'summary_failure_detail' not in result
    assert result['summary_kind']=='canonical_model_summary'
    assert row['status']=='raw_available_summary_failed'
    assert row['error_code']=='automated_source_coverage_incomplete'


def test_missing_message_or_source_edit_or_partial_review_blocks_publication(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path); audit=coverage(source,bundle); audit['reviewed_message_ids'].pop()
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete'):
        publish_automated_session({},source,bundle,review,audit,root)
    broken=copy.deepcopy(review); broken['episode_reviews'][0]['review_coverage']={'source_chunk_count':2,'reviewed_chunk_count':1,'all_chunks_accepted':True}
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete'):
        publish_automated_session({},source,bundle,broken,coverage(source,bundle),root)
    path.write_text(path.read_text().replace('请编制项目甲方案','已修改原文'))
    with pytest.raises(ValueError,match='automated_source_revision_changed'):
        publish_automated_session({},source,bundle,review,coverage(source,bundle),root)


def test_provider_coverage_contract_is_exact_not_boolean_only(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path)
    class Response:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def read(self): return json.dumps({'choices':[{'message':{'content':json.dumps({'accept':True,'issues':[]})}}]}).encode()
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete'):
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=lambda *_args,**_kw:Response())


def reference_response(**changes):
    return {'reviewed_message_refs':['m1','m2','m3','m4'], 'reviewed_episode_refs':['e1','e2'],
            'whole_source_topics_covered':True, 'corrections_preserved':True,
            'assistant_claims_labeled':True, 'partition_exact':True, 'accept':True, 'issues':[], **changes}


def intent_manifest(source,bundle):
    entries=[]
    for i,m in enumerate(source['messages'],1):
        if m['role']!='user':continue
        episode=next(e for e in bundle['episodes'] if m['evidence_id'] in e['message_ids'])
        eindex=bundle['episodes'].index(episode)+1
        entries.append({'message_ref':f'm{i}','intents':[{'kind':'request','source_quote':m['text'],
            'disposition':'covered','field_links':[{'episode_ref':f'e{eindex}','state_path':'goal/text',
                'field_quote':episode['draft']['state']['goal']['text'],'summary_paths':['standard','full']}]}]})
    return entries


def response_with_intents(source,bundle,**changes):
    return reference_response(user_intent_coverage=intent_manifest(source,bundle),**changes)


def test_answered_manifest_accepts_real_goal_context_only_with_primary_reply(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path)
    result=response_with_intents(source,bundle);intent=result['user_intent_coverage'][0]['intents'][0]
    intent.update(disposition='answered',answer_message_ref='m2')
    intent['field_links'].append({'episode_ref':'e1','state_path':'assistant_reports/0/text',
         'field_quote':bundle['episodes'][0]['draft']['state']['assistant_reports'][0]['text'],'summary_paths':['standard','full']})
    audit=request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=response_opener(result))
    assert audit['user_intent_coverage'][0]['intents'][0]['field_links']==intent['field_links']
    witness=audit['model_review_receipt']['primary_context_witness'][0]
    assert witness['primary_link_indexes']==[1] and witness['context_link_indexes']==[0]
    intent['field_links'].pop()
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete'):
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=response_opener(result))


def test_full_source_quote_with_unmatched_leading_quote_is_valid(tmp_path):
    from lib.scenario_state_v3 import whole_user_clauses
    raw='“前述建议并不完整。"不要公开密钥，只有本机使用'
    assert raw in whole_user_clauses(raw)
    assert '公开密钥，只有本机使用' not in whole_user_clauses(raw)


def test_per_user_context_manifest_is_required_even_with_all_refs_and_green_flags(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path)
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete') as caught:
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',
                                       opener=response_opener(reference_response()))
    assert caught.value.reason=='user_intent_coverage_missing'


@pytest.mark.parametrize('mutation',[
    'missing_user','duplicate_user','assistant_as_user','empty_source_quote','fabricated_source_quote',
    'empty_field_quote','fabricated_field_quote','unsafe_path','constraint_to_generic_goal','correction_to_generic_goal',
    'wrong_episode','non_substantive_request','unexplained_omission','superseded_by_earlier','superseded_by_assistant',
    'nested_kind','nested_episode_ref','nested_summary_path',
])
def test_per_user_context_manifest_rejects_unproven_or_unsafe_mapping(tmp_path,mutation):
    root,path,source,bundle,review=fixture(tmp_path)
    result=response_with_intents(source,bundle);entries=result['user_intent_coverage'];intent=entries[0]['intents'][0];link=intent['field_links'][0]
    if mutation=='missing_user':entries.pop()
    elif mutation=='duplicate_user':entries[1]=copy.deepcopy(entries[0])
    elif mutation=='assistant_as_user':entries[0]['message_ref']='m2'
    elif mutation=='empty_source_quote':intent['source_quote']=''
    elif mutation=='fabricated_source_quote':intent['source_quote']='not in original source'
    elif mutation=='empty_field_quote':link['field_quote']=''
    elif mutation=='fabricated_field_quote':link['field_quote']='not in actual state'
    elif mutation=='unsafe_path':link['state_path']='__class__/__dict__'
    elif mutation=='constraint_to_generic_goal':intent['kind']='constraint'
    elif mutation=='correction_to_generic_goal':intent['kind']='correction'
    elif mutation=='wrong_episode':link['episode_ref']='e2'
    elif mutation=='non_substantive_request':intent.update(disposition='non_substantive',field_links=[])
    elif mutation=='unexplained_omission':intent.update(disposition='omitted',field_links=[])
    elif mutation=='superseded_by_earlier':intent.update(disposition='superseded',superseded_by_ref='m1',superseding_quote=source['messages'][0]['text'],field_links=[])
    elif mutation=='superseded_by_assistant':intent.update(disposition='superseded',superseded_by_ref='m2',superseding_quote=source['messages'][1]['text'],field_links=[])
    elif mutation=='nested_kind':intent['kind']={'api_key':'must-not-be-used'}
    elif mutation=='nested_episode_ref':link['episode_ref']={'api_key':'must-not-be-used'}
    elif mutation=='nested_summary_path':link['summary_paths']=[{'api_key':'must-not-be-used'}]
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete'):
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=response_opener(result))


def response_opener(result, *, finish_reason='stop', seen=None):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def read(self):
            return json.dumps({'choices':[{'finish_reason':finish_reason,'message':{'content':json.dumps(result)}}]}).encode()
    def open_request(request, **kwargs):
        if seen is not None: seen.append(json.loads(request.data))
        return Response()
    return open_request


def test_short_reference_coverage_binds_exact_sources_without_model_hash_copy(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path); seen=[]
    audit=request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',
                                       opener=response_opener(response_with_intents(source,bundle),seen=seen))
    assert audit['reviewed_message_ids']==[m['evidence_id'] for m in source['messages']]
    assert audit['reviewed_episode_ids']==[e['episode_id'] for e in bundle['episodes']]
    receipt=audit['model_review_receipt']
    assert receipt['schema']=='evolving-profile.source-coverage-receipt.v2'
    assert receipt['reviewed_message_refs']==['m1','m2','m3','m4']
    assert receipt['reviewed_episode_refs']==['e1','e2']
    assert len(receipt['request_sha256'])==len(receipt['response_sha256'])==64
    prompt=seen[0]['messages'][0]['content']
    assert source['source_revision'] not in prompt
    assert all(e['episode_id'] not in prompt for e in bundle['episodes'])
    published=publish_automated_session({},source,bundle,review,audit,root)
    assert published['status']=='episode_directory_ready'


@pytest.mark.parametrize('changes,reason',[
    ({'reviewed_message_refs':['m1','m2','m3']},'message_reference_coverage_mismatch'),
    ({'reviewed_message_refs':['m2','m1','m3','m4']},'message_reference_coverage_mismatch'),
    ({'reviewed_message_refs':['m1','m2','m3','m3']},'message_reference_coverage_mismatch'),
    ({'reviewed_message_refs':['m1','m2','m3','m4','m5']},'message_reference_coverage_mismatch'),
    ({'reviewed_message_refs':'m1,m2,m3,m4'},'message_reference_coverage_mismatch'),
    ({'reviewed_episode_refs':['e1']},'episode_reference_coverage_mismatch'),
    ({'reviewed_episode_refs':['e1','e1']},'episode_reference_coverage_mismatch'),
    ({'reviewed_episode_refs':['e1','e2','e3']},'episode_reference_coverage_mismatch'),
    ({'corrections_preserved':False},'semantic_coverage_rejected'),
    ({'assistant_claims_labeled':False},'semantic_coverage_rejected'),
    ({'issues':[{'code':'missing_topic','detail':'topic omitted'}]},'semantic_coverage_rejected'),
])
def test_short_reference_coverage_rejects_missing_scope_or_semantic_failure(tmp_path,changes,reason):
    root,path,source,bundle,review=fixture(tmp_path)
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete') as caught:
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',
                                       opener=response_opener(reference_response(**changes)))
    assert caught.value.reason==reason


def test_source_coverage_reports_output_truncation_without_accepting_partial_json(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path)
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete') as caught:
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',
                                       opener=response_opener(reference_response(),finish_reason='length'))
    assert caught.value.reason=='model_output_truncated'


def test_host_bound_receipt_cannot_be_reused_for_changed_bundle(tmp_path):
    from lib.memory_recovery_scenario import validate_coverage
    root,path,source,bundle,review=fixture(tmp_path)
    audit=request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',
                                       opener=response_opener(response_with_intents(source,bundle)))
    audit['model_review_receipt']['bundle_sha256']='0'*64
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete') as caught:
        validate_coverage(source,bundle,audit)
    assert caught.value.reason=='receipt_binding_mismatch'


def test_accepted_source_reuse_requires_unchanged_host_receipt_binding(tmp_path):
    from lib.memory_recovery_scenario import accepted_session_source
    root,path,source,bundle,review=fixture(tmp_path)
    audit=request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',
                                       opener=response_opener(response_with_intents(source,bundle)))
    published=publish_automated_session({},source,bundle,review,audit,root)
    assert accepted_session_source(published,source) is True
    published['automated_source_coverage']['model_review_receipt']['source_revision']='0'*64
    assert accepted_session_source(published,source) is False


def test_serialized_coverage_budget_includes_all_draft_layers_before_provider_call(tmp_path,monkeypatch):
    root,path,source,bundle,review=fixture(tmp_path)
    # Raw source fits. The complete serialized state/evidence/three layers do not.
    monkeypatch.setattr('lib.memory_recovery_scenario.MAX_COVERAGE_INPUT_CHARS',source['total_chars']+100)
    def forbidden(*_args,**_kwargs): raise AssertionError('over-budget input reached provider')
    with pytest.raises(ValueError,match='automated_source_coverage_budget_exceeded') as caught:
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=forbidden)
    assert caught.value.reason=='serialized_input_budget_exceeded'


def test_reference_response_budget_is_checked_before_provider_call(tmp_path,monkeypatch):
    root,path,source,bundle,review=fixture(tmp_path)
    monkeypatch.setattr('lib.memory_recovery_scenario.MAX_COVERAGE_OUTPUT_TOKENS',1024)
    def forbidden(*_args,**_kwargs): raise AssertionError('over-budget refs reached provider')
    with pytest.raises(ValueError,match='automated_source_coverage_budget_exceeded') as caught:
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=forbidden)
    assert caught.value.reason=='reference_output_budget_exceeded'


def test_non_completed_finish_reason_cannot_provide_accepted_coverage(tmp_path):
    root,path,source,bundle,review=fixture(tmp_path)
    with pytest.raises(ValueError,match='automated_source_coverage_incomplete') as caught:
        request_source_coverage_review(source,bundle,base_url='https://fake.invalid',api_key='test-private',model='fake',
                                       opener=response_opener(reference_response(),finish_reason='content_filter'))
    assert caught.value.reason=='model_output_not_completed'


def test_real_coordinator_publishes_and_skips_completed_source(tmp_path,monkeypatch):
    from test_memory_recovery import engine,start
    from lib.context_summary import read_context_index
    root,path,source,bundle,review=fixture(tmp_path)
    e=engine(tmp_path); e.settings={'providers':{'primary':{'base_url':'https://fake.invalid','api_key':'test-private','model':'fake'}}}
    e.queue.capture(source['thread_id'],1,'bank-a','/project','original user',{})
    calls=[]
    def generate(*_,**__): calls.append('generate'); return bundle
    monkeypatch.setattr('lib.scenario_model.request_episode_bundle',generate)
    monkeypatch.setattr('lib.scenario_model.request_episode_bundle_review',lambda *_,**__:review)
    monkeypatch.setattr('lib.memory_recovery_scenario.request_source_coverage_review',lambda *_,**__:coverage(source,bundle))
    result=e.run_job(start(e,['session_summaries']))['job']
    assert result['status']=='complete'
    published=read_context_index(tmp_path/'context/context-index.json')['sessions'][0]
    assert published['bank_id']=='bank-a' and published['reviewer_kind']=='automated_source_coverage'
    e.run_job(start(e,['session_summaries']))
    assert calls==['generate']


def test_valid_legacy_accepted_same_revision_skips_all_provider_calls(tmp_path,monkeypatch):
    from test_memory_recovery import engine,start
    from lib.context_pipeline import promote_episode_bundle
    from lib.context_summary import write_context_index
    root,path,source,bundle,review=fixture(tmp_path)
    manual={'verdict':'conversation_only_draft_acceptable','source_revision':source['source_revision'],'draft_sha256':fingerprint_episode_bundle(bundle),
        'scope_verdict':'whole_session_scope_acceptable','reviewed_source_message_count':len(source['messages']),'episode_scope_verdict':'multiple_topics',
        'episode_partition_verdict':'exact_contiguous_partition_acceptable','reviewed_episode_ids':[r['episode_id'] for r in bundle['episodes']], 'reviewer':'existing-real-authority'}
    legacy=promote_episode_bundle(build_session_context(source['thread_id'],'project',[],''),source,bundle,review,manual)
    legacy['bank_id']='bank-a'; write_context_index(tmp_path/'context/context-index.json',[legacy],[])
    e=engine(tmp_path); e.queue.capture(source['thread_id'],1,'bank-a','/project','original',{})
    def forbidden(*_,**__): raise AssertionError('valid legacy same-source replay called provider')
    monkeypatch.setattr('lib.scenario_model.request_episode_bundle',forbidden)
    monkeypatch.setattr('lib.scenario_model.request_episode_bundle_review',forbidden)
    result=e.run_job(start(e,['session_summaries']))['job']
    assert result['status']=='complete'


def test_verified_project_complete_sources_auto_publish_and_missing_proof_holds(tmp_path,monkeypatch):
    from test_memory_recovery import engine,start
    from lib.context_summary import write_context_index,read_context_index
    from lib.memory_recovery_project import source_material
    root,path,source,bundle,review=fixture(tmp_path)
    session=publish_automated_session(build_session_context(source['thread_id'],'project-a',[],''),source,bundle,review,coverage(source,bundle),root)
    session['bank_id']='bank-a'
    identity={'source_session_id':source['thread_id'],'source_revision':source['source_revision'],'message_ids':[source['messages'][0]['evidence_id']], 'quote':source['messages'][0]['text']}
    project={'context_id':'project:project-a','context_type':'project','bank_id':'bank-a','project_key':'project-a','identity_status':'verified_project','identity_evidence':identity,'session_ids':[source['thread_id']]}
    write_context_index(tmp_path/'context/context-index.json',[session],[project])
    material=source_material(project,[session],[source]); calls=[]
    draft={'project_key':'project-a','session_ids':material['session_ids'],'source_revisions':material['source_revisions'],
        'summaries':{'compact':'项目甲方案和项目乙预算的导航。','standard':'本项目会话包含两个分段任务，助手均报告已答复但未独立核验。','full':'项目甲方案和项目乙预算是此会话的两个独立任务段。助手报告已进行答复，原始会话没有外部成果的独立验证。后续需要按对应原始消息回读核验。'},
        'evidence_refs':[{'session_id':source['thread_id'],'message_id':source['messages'][0]['evidence_id'],'role':'user','quote':source['messages'][0]['text']}], 'summary_model':'fake-generator'}
    audit={**{k:material[k] for k in ('project_key','session_ids','source_revisions')},
        'reviewed_message_ids':{source['thread_id']:[m['evidence_id'] for m in source['messages']]},
        'whole_source_topics_covered':True,'corrections_preserved':True,'assistant_claims_labeled':True,'identity_scope_preserved':True,'accept':True,'issues':[],'review_model':'fake-reviewer'}
    def model(*_args,**_kwargs): calls.append('generate'); return draft
    monkeypatch.setattr('lib.memory_recovery_project.request_project_draft',model)
    monkeypatch.setattr('lib.memory_recovery_project.request_project_review',lambda *_args,**_kwargs:audit)
    e=engine(tmp_path); e.queue.capture(source['thread_id'],1,'bank-a','/project','original user',{})
    e.settings={'providers':{'primary':{'base_url':'https://fake.invalid','api_key':'test-private','model':'fake'}}}
    result=e.run_job(start(e,['project_summaries']))['job']; assert result['status']=='complete'
    published=read_context_index(tmp_path/'context/context-index.json')['projects'][0]
    assert published['status']=='model_reviewed' and published['reviewer_kind']=='automated_project_source_coverage' and published['no_human_confirmation_claim'] is True
    e.run_job(start(e,['project_summaries'])); assert calls==['generate']
    # No missing member/source can be filled by the already accepted boolean.
    project['session_ids'].append('missing-session'); write_context_index(tmp_path/'context/context-index.json',[session],[project])
    assert e.run_job(start(e,['project_summaries']))['job']['status']=='review_pending'


def test_project_membership_change_during_review_is_held_without_overwriting_latest(tmp_path,monkeypatch):
    from test_memory_recovery import engine,start
    from lib.context_summary import write_context_index,read_context_index,update_context_index
    from lib.memory_recovery_project import source_material
    root,path,source,bundle,review=fixture(tmp_path)
    session=publish_automated_session(build_session_context(source['thread_id'],'project-a',[],''),source,bundle,review,coverage(source,bundle),root)
    session['bank_id']='bank-a'
    project={'context_id':'project:project-a','context_type':'project','bank_id':'bank-a','project_key':'project-a', 'identity_status':'verified_project',
        'identity_evidence':{'source_session_id':source['thread_id'],'source_revision':source['source_revision'],
                            'message_ids':[source['messages'][0]['evidence_id']],'quote':source['messages'][0]['text']},
        'session_ids':[source['thread_id']]}
    index_path=tmp_path/'context/context-index.json'; write_context_index(index_path,[session],[project])
    material=source_material(project,[session],[source])
    draft={'project_key':'project-a','session_ids':material['session_ids'],'source_revisions':material['source_revisions'],
        'summaries':{'compact':'项目甲方案导航。','standard':'本项目会话含方案任务与预算任务，助手报告尚未独立核验。',
                     'full':'项目甲方案与项目乙预算是两个来源任务段。助手报告已答复，但没有外部成果的独立验证；必须保留各段边界与来源回读。'},
        'evidence_refs':[{'session_id':source['thread_id'],'message_id':source['messages'][0]['evidence_id'],'role':'user','quote':source['messages'][0]['text']}],
        'summary_model':'fake-generator'}
    audit={**{k:material[k] for k in ('project_key','session_ids','source_revisions')},
        'reviewed_message_ids':{source['thread_id']:[m['evidence_id'] for m in source['messages']]},
        'whole_source_topics_covered':True,'corrections_preserved':True,'assistant_claims_labeled':True,'identity_scope_preserved':True,'accept':True,'issues':[],'review_model':'fake-reviewer'}
    def concurrent_review(*_args,**_kwargs):
        def add_member(index):
            index['projects'][0]['session_ids'].append('new-verified-member')
            index['sessions'].append({'context_id':'session:new-verified-member','session_id':'new-verified-member','project_key':'project-a',
                                      'bank_id':'bank-a','source_revision':'new-original-revision','status':'model_reviewed'})
            return index
        update_context_index(index_path,add_member)
        return audit
    monkeypatch.setattr('lib.memory_recovery_project.request_project_draft',lambda *_args,**_kwargs:draft)
    monkeypatch.setattr('lib.memory_recovery_project.request_project_review',concurrent_review)
    e=engine(tmp_path); e.queue.capture(source['thread_id'],1,'bank-a','/project','original user',{})
    e.settings={'providers':{'primary':{'base_url':'https://fake.invalid','api_key':'test-private','model':'fake'}}}
    result=e.run_job(start(e,['project_summaries']))['job']
    latest=read_context_index(index_path)
    assert result['status']=='review_pending'
    assert latest['projects'][0]['session_ids']==[source['thread_id'],'new-verified-member']
    assert len(latest['sessions'])==2
    assert latest['projects'][0].get('reviewer_kind')!='automated_project_source_coverage'
