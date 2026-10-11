import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.context_summary import BUDGETS
from lib.context_pipeline import promote_session_draft
from lib.context_summary import build_session_context
from lib import scenario_model
from lib.scenario_state_v3 import parse_form_reply, project_source_messages, correction_review_hints, validate_state_draft


def source(messages):
    rows=[]
    for index,(role,text) in enumerate(messages,1):
        rows.append({'evidence_id':f'id{index}','model_ref':f'm{index}','role':role,
                     'text':text,'turn_id':f't{index}','at':f'2026-09-27T0{index}:00:00Z'})
    return {'thread_id':'01a0aad3-d9dd-7400-b1e7-a636388cab3b','source':'codex_thread_history',
            'source_files':['rollout.jsonl'],'source_revision':'revision-1','status':'complete','messages':rows}


def claim(text,mid):
    return {'text':text,'message_ids':[mid]}


class ScenarioStateV3Tests(unittest.TestCase):
    def test_new_user_clause_protocol_rejects_negation_condition_and_question_loss(self):
        for raw,bad in [('不要公开密钥','公开密钥'),('不指名道姓','指名道姓'),
                        ('不应优先SearXNG','应优先SearXNG'),('是否支持批量抓取？','支持批量抓取'),
                        ('如果额度不足，才调用后备引擎','调用后备引擎'),
                        ('readme还是中英两版','readme还是中英两版，顶部互相切换')]:
            with self.subTest(raw=raw):
                original=source([('user',raw),('assistant','已答复，尚未核验')])
                state={'subject':claim('项目要求','id1'),'goal':claim('处理请求','id1'),'phase':'assistant_reported',
                       'constraints':[claim(bad,'id1')],'corrections':[],
                       'assistant_reports':[claim('已答复，尚未核验','id2')],'unresolved':[]}
                with self.assertRaisesRegex(ValueError,'scenario_user_claim_not_verbatim'):
                    validate_state_draft(original,state,model='test',user_claim_protocol='user_constraints_corrections_whole_source_clause.v1')

    def test_new_user_clause_protocol_preserves_whole_clause_and_legacy_paraphrase_mode(self):
        original=source([('user','不要公开密钥，只有本机使用；README中英两版。'),('assistant','已答复')])
        state={'subject':claim('项目要求','id1'),'goal':claim('处理请求','id1'),'phase':'assistant_reported',
               'constraints':[claim('不要公开密钥，只有本机使用；','id1'),claim('README中英两版。','id1')],
               'corrections':[],'assistant_reports':[claim('已答复','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test',user_claim_protocol='user_constraints_corrections_whole_source_clause.v1')
        self.assertEqual(draft['state_claim_protocol'],'user_constraints_corrections_whole_source_clause.v1')
        state['constraints']=[claim('禁止公开凭据','id1')]
        legacy=validate_state_draft(original,state,model='test')
        self.assertNotIn('state_claim_protocol',legacy)

    def test_field_catalog_exposes_only_real_paths_roles_source_refs_and_summary_tiers(self):
        from lib.scenario_state_v3 import state_field_catalog
        original=source([('user','编制方案'),('assistant','旧报告尚待核验'),('user','说明实际路径'),('assistant','实际未采用EP线索')])
        state={'subject':claim('方案','id1'),'goal':claim('说明路径','id3'),'phase':'assistant_reported',
               'constraints':[],'corrections':[],'assistant_reports':[claim('旧报告尚待核验','id2'),claim('实际未采用EP线索','id4')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        catalog=state_field_catalog(original,draft)
        prior=next(r for r in catalog if r['state_path']=='assistant_reports/0/text')
        self.assertEqual(prior['role'],'assistant')
        self.assertEqual(prior['supported_message_refs'],['id2'])
        self.assertEqual(prior['field_text'],'旧报告尚待核验')
        self.assertEqual(prior['summary_paths'],['full'])

    def test_model_receives_claim_cardinality_limits_without_weakening_source_guard(self):
        original=source([('user',f'第{i}项项目条件') for i in range(1,6)]+[('assistant','已答复，结果未独立核验')])
        def reply(request,**_kwargs):
            body=json.loads(request.data)
            payload=json.loads(body['messages'][0]['content'].split('输入：',1)[1].split('\n上次输出',1)[0])
            ids=payload['allowed_message_ids_by_role']['user']
            limit=(payload.get('state_limits') or {}).get('message_ids_max_per_claim',len(ids))
            state={'subject':{'text':'项目条件','message_ids':ids[:limit]},'goal':claim('讨论项目条件','id1'),
                   'phase':'assistant_reported','constraints':[],'corrections':[],
                   'assistant_reports':[claim('已答复，结果未独立核验','id6')],'unresolved':[]}
            return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'source_revision':'revision-1','state':state})}}]}).encode())
        draft=scenario_model.request_session_state_draft(original,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=reply)
        self.assertEqual(draft['state']['subject']['message_ids'],['id1','id2','id3','id4'])
        self.assertEqual(draft['state']['phase'],'assistant_reported')
        invalid=dict(draft['state']);invalid['subject']={'text':'项目条件','message_ids':['id1','id2','id3','id4','id5']}
        with self.assertRaisesRegex(ValueError,'scenario_state_invalid'):
            validate_state_draft(original,invalid,model='test')

    def test_generation_and_validator_share_claim_limits(self):
        original=source([('user',f'第{i}项项目条件') for i in range(1,4)]+[('assistant','已答复')])
        def reply(request,**_kwargs):
            payload=json.loads(json.loads(request.data)['messages'][0]['content'].split('输入：',1)[1].split('\n上次输出',1)[0])
            count=payload['state_limits']['message_ids_max_per_claim']
            state={'subject':{'text':'项目条件','message_ids':['id1','id2','id3'][:count]},'goal':claim('讨论条件','id1'),
                   'phase':'assistant_reported','constraints':[],'corrections':[],
                   'assistant_reports':[claim('已答复','id4')],'unresolved':[]}
            return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'source_revision':'revision-1','state':state})}}]}).encode())
        limits={'text_max_chars_per_claim':180,'message_ids_min_per_claim':1,'message_ids_max_per_claim':2,'claims_max_per_array':8}
        with patch('lib.scenario_state_v3.STATE_LIMITS',limits,create=True):
            draft=scenario_model.request_session_state_draft(original,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=reply)
            self.assertEqual(draft['state']['subject']['message_ids'],['id1','id2'])
            state=dict(draft['state']);state['subject']={'text':'项目条件','message_ids':['id1','id2','id3']}
            with self.assertRaisesRegex(ValueError,'scenario_state_invalid'):validate_state_draft(original,state,model='test')

    def test_episode_review_receives_validator_roles_and_limits(self):
        original=source([('user','编制项目方案'),('assistant','已提供答复但尚未核验')])
        state={'subject':claim('项目方案','id1'),'goal':claim('编制方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[],'assistant_reports':[claim('已提供答复但尚未核验','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        def reply(request,**_kwargs):
            payload=json.loads(json.loads(request.data)['messages'][0]['content'].split('输入：',1)[1])
            roles=payload.get('allowed_roles_by_field') or {};limits=payload.get('state_limits') or {}
            accepted=roles.get('subject')==roles.get('corrections')=='user' and roles.get('assistant_reports')=='assistant' and limits.get('message_ids_max_per_claim')==4
            issues=[] if accepted else [{'tier':'evidence','code':'role_contract_missing','detail':'Explicit role or limit contract absent'}]
            return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'source_revision':'revision-1','accept':accepted,'issues':issues})}}]}).encode())
        review=scenario_model.request_session_review(original,draft,base_url='https://fake.invalid',api_key='test-private',model='fake',opener=reply)
        self.assertEqual(review['status'],'model_review_passed')

    def test_long_source_uses_smaller_default_state_chunks_without_splitting_messages(self):
        original=source([('user','甲'*4000),('assistant','乙'*3000)]*5)
        self.assertEqual(scenario_model.adaptive_state_chunk_chars(original,30000),8000)
        original['messages'][0]['text']='甲'*12000
        self.assertEqual(scenario_model.adaptive_state_chunk_chars(original,30000),12000)
        self.assertEqual(scenario_model.adaptive_state_chunk_chars(source([('user','短问题')]),30000),30000)

    def test_review_uses_the_chunk_limit_recorded_by_the_draft(self):
        self.assertEqual(scenario_model.review_chunk_limit_for_draft(
            {'schema':'evolving-profile.scenario-draft.v3','source_chunk_char_limit':9000},30000),9000)
        self.assertEqual(scenario_model.review_chunk_limit_for_draft(
            {'schema':'evolving-profile.scenario-draft.v2','selection_coverage':{'chunk_char_limit':12000}},30000),12000)
        self.assertEqual(scenario_model.review_chunk_limit_for_draft({'schema':'legacy'},30000),30000)
        with self.assertRaisesRegex(ValueError,'scenario_source_chunk_too_large'):
            scenario_model.review_chunk_limit_for_draft(
                {'schema':'evolving-profile.scenario-draft.v3','source_chunk_char_limit':None,
                 'selection_coverage':{'chunk_char_limit':9000}},30000)

    def test_correction_hints_keep_later_user_name_correction_as_navigation_only(self):
        original=source([('user','请做天津大学方案'),('assistant','草稿称施艳超院长'),
                         ('user','不要写施艳超，院长名字叫师燕超'),('assistant','已收到')])
        hints=correction_review_hints(original)
        self.assertEqual([row['message_id'] for row in hints],['id3'])
        self.assertIn('师燕超',hints[0]['excerpt'])
        self.assertEqual(hints[0]['evidence_role'],'review_hint_not_verified_claim')

    def test_explicit_user_correction_misclassified_as_constraint_is_recategorized(self):
        original=source([('user','不要把采购月份写死，保留后续安排空间。'),('assistant','已记录，尚未核验。')])
        state={'subject':claim('采购前方案','id1'),'goal':claim('整理方案','id1'),'phase':'assistant_reported',
               'constraints':[claim('不要把采购月份写死，保留后续安排空间。','id1')],
               'corrections':[],'assistant_reports':[claim('已记录，尚未核验。','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test',
                                   user_claim_protocol='user_constraints_corrections_whole_source_clause.v1')
        self.assertEqual(draft['state']['constraints'],[])
        self.assertEqual(draft['state']['corrections'],[claim('不要把采购月份写死，保留后续安排空间。','id1')])

    def test_missing_later_user_correction_is_added_from_exact_source_clause(self):
        original=source([('user','先做方案。'),('assistant','已回复。'),
                         ('user','不要把标题写成采购清单，要保留合作方案语气。'),('assistant','已修改。')])
        state={'subject':claim('方案','id1'),'goal':claim('修改方案','id3'),'phase':'assistant_reported',
               'constraints':[],'corrections':[],'assistant_reports':[claim('已回复。','id2'),claim('已修改。','id4')],
               'unresolved':[]}
        draft=validate_state_draft(original,state,model='test',
                                   user_claim_protocol='user_constraints_corrections_whole_source_clause.v1')
        self.assertEqual(draft['state']['corrections'],[
            claim('不要把标题写成采购清单，要保留合作方案语气。','id3')])

    def test_correction_hints_mask_credentials_before_model_exposure(self):
        secret='sk-'+'A'*32
        original=source([('user','不要再写旧配置，密钥 '+secret)])
        hints=correction_review_hints(original)
        self.assertEqual(len(hints),1)
        self.assertNotIn(secret,hints[0]['excerpt'])

    def test_latest_v3_attempt_blocks_stale_draft_after_failed_regeneration(self):
        draft={'schema':'evolving-profile.scenario-draft.v3','source_revision':'r1','state':{'phase':'requested'}}
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            marker=path/'.attempts'/'session-a.json'
            marker.parent.mkdir()
            marker.write_text(json.dumps({'status':'failed','source_revision':'r1'}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'scenario_v3_latest_attempt_not_successful'):
                scenario_model.validate_latest_v3_attempt(path,'session-a',draft,'r1')
            marker.write_text(json.dumps({'status':'succeeded','source_revision':'r1',
                'draft_sha256':scenario_model.fingerprint_draft(draft)}),encoding='utf-8')
            scenario_model.validate_latest_v3_attempt(path,'session-a',draft,'r1')
            draft['state']['phase']='unknown'
            with self.assertRaisesRegex(ValueError,'scenario_v3_latest_attempt_not_successful'):
                scenario_model.validate_latest_v3_attempt(path,'session-a',draft,'r1')

    def test_model_request_sends_parsed_form_and_returns_v3_state_draft(self):
        raw='<send_user_message_question_reply>[{"question":"是否申报其他专项？","answer":"否"}]</send_user_message_question_reply>'
        original=source([('user','为天津财经大学商学院编制人工智能实训平台申报书'),
                         ('user',raw),('assistant','已完成旧稿'),('user','现在软件平台与硬件算力分开')])
        state={'subject':claim('天津财经大学商学院人工智能实训平台申报书','id1'),
               'goal':claim('编制申报书','id1'),'phase':'requested',
               'constraints':[claim('问题：是否申报其他专项？ 回答：否','id2')],
               'corrections':[claim('现在软件平台与硬件算力分开','id4')],
               'assistant_reports':[claim('已完成旧稿','id3')],
               'unresolved':[claim('新要求尚无完成答复','id4')]}
        sent=[]
        def opener(request,timeout):
            sent.append(json.loads(request.data))
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':'revision-1','state':state},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        draft=scenario_model.request_session_state_draft(original,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener)
        self.assertEqual(draft['schema'],'evolving-profile.scenario-draft.v3')
        prompt=sent[0]['messages'][0]['content']
        self.assertIn('结构化表单',prompt)
        self.assertIn('是否申报其他专项？',prompt)
        self.assertNotIn('<send_user_message_question_reply>',prompt)
        self.assertEqual(draft['state']['constraints'][0]['message_ids'],['id2'])

    def test_model_request_repairs_wrong_role_once_without_relaxing_validation(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','不要写具体机器人型号')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'requested',
               'constraints':[],'corrections':[claim('不要写具体机器人型号','id3')],
               'assistant_reports':[claim('旧稿已完成','id1')],
               'unresolved':[claim('新要求尚无完成答复','id3')]}
        sent=[]
        def opener(request,timeout):
            sent.append(json.loads(request.data))
            if len(sent)==2:
                state['assistant_reports']=[claim('旧稿已完成','id2')]
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':'revision-1','state':state},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        draft=scenario_model.request_session_state_draft(original,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener)
        self.assertEqual(len(sent),2)
        self.assertEqual(draft['state']['assistant_reports'][0]['message_ids'],['id2'])
        self.assertIn('scenario_state_role_invalid',sent[1]['messages'][0]['content'])
        self.assertIn('"correction_review_hints"',sent[0]['messages'][0]['content'])

    def test_model_request_stops_after_three_invalid_role_attempts(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','不要写具体机器人型号')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'requested',
               'constraints':[],'corrections':[],
               'assistant_reports':[claim('旧稿已完成','id1')],
               'unresolved':[claim('新要求尚无完成答复','id3')]}
        sent=[]
        def opener(request,timeout):
            sent.append(json.loads(request.data))
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':'revision-1','state':state},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        with self.assertRaisesRegex(ValueError,'scenario_state_role_invalid'):
            scenario_model.request_session_state_draft(original,base_url='https://example.invalid',
                api_key='test',model='test-model',opener=opener)
        self.assertEqual(len(sent),3)
        self.assertIn('assistant_reports 只能引用 assistant',sent[1]['messages'][0]['content'])

    def test_long_session_chunks_then_merges_source_linked_state(self):
        original=source([('user','天津大学墙体方案'),('assistant','旧稿已完成'),
                         ('user','改成背负式喷洒'),('assistant','新版已形成')])
        states=[
            {'subject':claim('天津大学墙体方案','id1'),'goal':claim('编制墙体方案','id1'),
             'phase':'assistant_reported','constraints':[],'corrections':[],
             'assistant_reports':[claim('旧稿已完成','id2')],'unresolved':[]},
            {'subject':claim('背负式喷洒方案','id3'),'goal':claim('改成背负式喷洒','id3'),
             'phase':'assistant_reported','constraints':[],'corrections':[],
             'assistant_reports':[claim('新版已形成','id4')],'unresolved':[]},
            {'subject':claim('天津大学墙体方案','id1'),'goal':claim('编制墙体方案','id1'),
             'phase':'assistant_reported','constraints':[],
             'corrections':[claim('改成背负式喷洒','id3')],
             'assistant_reports':[claim('新版已形成','id4')],'unresolved':[]},
        ]
        sent=[]
        def opener(request,timeout):
            sent.append(json.loads(request.data))
            state=states[len(sent)-1]
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':'revision-1','state':state},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        draft=scenario_model.request_session_state_draft(original,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener,max_input_chars=13)
        self.assertEqual(len(sent),3)
        self.assertIn('source_linked_state_candidate',sent[2]['messages'][0]['content'])
        self.assertEqual(draft['state']['corrections'][0]['message_ids'],['id3'])
        self.assertEqual(draft['selection_coverage']['source_chunk_count'],2)
        self.assertEqual(draft['selection_coverage']['chunk_char_limit'],13)
        self.assertEqual(draft['selection_coverage']['source_message_count'],4)
        self.assertFalse(draft['selection_coverage']['semantic_completeness_proven'])
        self.assertTrue(draft['selection_coverage']['merge_projection_reduced'])
        validated=scenario_model.validate_session_draft(original,draft,model='test-model')
        self.assertEqual(validated['selection_coverage']['source_chunk_count'],2)
        draft['selection_coverage']['source_chunk_count']=0
        with self.assertRaisesRegex(ValueError,'scenario_state_invalid'):
            scenario_model.validate_session_draft(original,draft,model='test-model')

    def test_multichunk_v3_source_rejects_missing_selection_coverage(self):
        original=source([('user','甲'*16000),('assistant','乙'*16000),('user','丙')])
        state={'subject':claim('项目','id1'),'goal':claim('处理项目','id1'),
               'phase':'requested','constraints':[],'corrections':[],
               'assistant_reports':[claim('正在处理','id2')],
               'unresolved':[claim('最后一条请求待回应','id3')]}
        draft=validate_state_draft(original,state,model='test')
        with self.assertRaisesRegex(ValueError,'scenario_state_invalid'):
            scenario_model.validate_session_draft(original,draft,model='test')

    def test_v3_selection_coverage_chunk_count_must_match_source(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','改成背负式喷洒'),('assistant','新版已形成')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('改成背负式喷洒','id3')],
               'assistant_reports':[claim('新版已形成','id4')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        draft['selection_coverage']={'source_revision':'revision-1','source_message_count':4,
            'source_chunk_count':3,'candidate_state_count':3,'chunk_char_limit':20,
            'semantic_completeness_proven':False}
        with self.assertRaisesRegex(ValueError,'scenario_state_invalid'):
            scenario_model.validate_session_draft(original,draft,model='test')

    def test_single_long_session_preserves_custom_chunk_limit_for_validation(self):
        original=source([('user','甲'*20000),('assistant','乙'*20000)])
        state={'subject':claim('长会话项目','id1'),'goal':claim('整理项目状态','id1'),
               'phase':'assistant_reported','constraints':[],'corrections':[],
               'assistant_reports':[claim('已整理项目状态','id2')],'unresolved':[]}
        def opener(request,timeout):
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':'revision-1','state':state},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        draft=scenario_model.request_session_state_draft(original,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener,max_input_chars=50000)
        validated=scenario_model.validate_session_draft(original,draft,model='test-model')
        self.assertEqual(draft['source_chunk_char_limit'],50000)
        self.assertEqual(validated['source_chunk_char_limit'],50000)

    def test_explicit_null_chunk_limit_is_rejected(self):
        original=source([('user','整理项目状态'),('assistant','已整理项目状态')])
        state={'subject':claim('项目','id1'),'goal':claim('整理项目状态','id1'),
               'phase':'assistant_reported','constraints':[],'corrections':[],
               'assistant_reports':[claim('已整理项目状态','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        draft['source_chunk_char_limit']=None
        with self.assertRaisesRegex(ValueError,'scenario_state_invalid'):
            scenario_model.validate_session_draft(original,draft,model='test')

    def test_v3_review_receives_state_and_cannot_hide_final_user_boundary(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','不要写具体机器人型号')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'requested',
               'constraints':[],'corrections':[claim('不要写具体机器人型号','id3')],
               'assistant_reports':[claim('旧稿已完成','id2')],
               'unresolved':[claim('新要求尚无完成答复','id3')]}
        draft=validate_state_draft(original,state,model='test-model')
        sent=[]
        def opener(request,timeout):
            sent.append(json.loads(request.data))
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':'revision-1','accept':True,'issues':[]},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        reviewed=scenario_model.request_session_review(original,draft,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener)
        prompt=sent[0]['messages'][0]['content']
        self.assertIn('"state"',prompt)
        self.assertIn('"last_message_role": "user"',prompt)
        self.assertIn('"correction_review_hints"',prompt)
        self.assertEqual(reviewed['status'],'model_review_passed')
        self.assertEqual(scenario_model.validate_session_draft(original,draft,model='test-model')['schema'],
                         'evolving-profile.scenario-draft.v3')

    def test_chunked_v3_review_preserves_whole_session_boundary(self):
        original=source([('user','天津大学方案'),('user','改成新方案'),('assistant','新版已形成')])
        state={'subject':claim('天津大学方案','id1'),'goal':claim('编制方案','id1'),
               'phase':'assistant_reported','constraints':[],
               'corrections':[claim('改成新方案','id2')],
               'assistant_reports':[claim('新版已形成','id3')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test-model')
        sent=[]
        def opener(request,timeout):
            sent.append(json.loads(request.data))
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':'revision-1','accept':True,'issues':[]},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        review=scenario_model.request_session_review(original,draft,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener,max_input_chars=12)
        self.assertGreater(len(sent),1)
        self.assertEqual(review['status'],'model_review_passed')
        first=sent[0]['messages'][0]['content']
        self.assertIn('"full_session_last_role": "assistant"',first)
        self.assertIn('不能仅因草稿引用其他分段',first)
        self.assertIn('"claims_in_chunk"',first)
        self.assertNotIn('"summaries"',first)

    def test_chunked_review_counts_claims_spanning_multiple_source_chunks(self):
        original=source([('user','天津大学方案'),('user','改成旧方案'),
                         ('user','只保留天津大学新方案'),('assistant','新版已形成')])
        state={'subject':{'text':'天津大学新方案','message_ids':['id1','id3']},
               'goal':claim('编制方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('改成旧方案','id2')],
               'assistant_reports':[claim('新版已形成','id4')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test-model')
        sent=[]
        def opener(request,timeout):
            payload=json.loads(request.data)
            prompt=payload['messages'][0]['content']
            body=json.loads(prompt.rsplit('输入：',1)[1])
            sent.append(body)
            if body.get('request_type')=='cross_chunk_claim_review':
                result={'source_revision':body['source_revision'],'claim_reviews':[
                    {'claim_id':claim['claim_id'],'accept':True,'issues':[]}
                    for claim in body['claims']]}
            else:
                result={'source_revision':body['source_revision'],'accept':True,'issues':[]}
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        review=scenario_model.request_session_review(original,draft,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener,max_input_chars=12)
        self.assertEqual(review['status'],'model_review_passed')
        self.assertEqual(review['review_coverage']['cross_chunk_claim_count'],1)
        self.assertEqual(review['review_coverage']['cross_chunk_claim_reviewed_count'],1)
        self.assertEqual(review['review_coverage']['unreviewed_cross_chunk_claim_count'],0)
        self.assertEqual(review['review_coverage']['locally_reviewable_claim_count'],3)
        cross=next(row for row in sent if row.get('request_type')=='cross_chunk_claim_review')
        self.assertEqual([row['message_id'] for row in cross['messages']],['id1','id2','id3'])
        self.assertEqual(cross['claims'][0]['message_ids'],['id1','id3'])
        self.assertEqual(cross['correction_review_hints'][0]['message_id'],'id2')

    def test_chunked_cross_claim_review_rejection_rejects_the_session_review(self):
        original=source([('user','天津大学方案'),('user','改成新方案'),('assistant','新版已形成')])
        state={'subject':{'text':'天津大学新方案','message_ids':['id1','id2']},
               'goal':claim('编制方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('改成新方案','id2')],
               'assistant_reports':[claim('新版已形成','id3')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test-model')

        def opener(request,timeout):
            prompt=json.loads(request.data)['messages'][0]['content']
            body=json.loads(prompt.rsplit('输入：',1)[1])
            if body.get('request_type')=='cross_chunk_claim_review':
                results=[{'claim_id':item['claim_id'],'accept':False,
                          'issues':[{'tier':'evidence','code':'unsupported_claim','detail':'引用原文不足以支持合并对象'}]}
                         for item in body['claims']]
                value={'source_revision':body['source_revision'],'claim_reviews':results}
            else:
                value={'source_revision':body['source_revision'],'accept':True,'issues':[]}
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())

        review=scenario_model.request_session_review(original,draft,base_url='https://example.invalid',
            api_key='test',model='test-model',opener=opener,max_input_chars=8)

        self.assertEqual(review['status'],'model_review_rejected')
        self.assertEqual(review['review_coverage']['cross_chunk_claim_reviewed_count'],1)
        self.assertEqual(review['review_coverage']['unreviewed_cross_chunk_claim_count'],0)
        self.assertTrue(review['review_coverage']['all_chunks_accepted'])
        self.assertEqual(review['issues'][0]['code'],'unsupported_claim')

    def test_cross_claim_review_budget_overflow_remains_explicitly_unreviewed(self):
        source_value={'source_revision':'r1','messages':[
            {'evidence_id':'m1','role':'user','text':'"'*1650},
            {'evidence_id':'m2','role':'user','text':'"'*1650},
        ]}
        claim_value={'statement':'合并主张','field':'subject','message_ids':['m1','m2']}
        def never_call(*args,**kwargs):
            raise AssertionError('oversized citations must not be sent after truncation')

        result=scenario_model._request_cross_chunk_claim_reviews(
            source_value,[(1,claim_value)],base_url='https://example.invalid',api_key='test',
            model='test-model',opener=never_call,max_chars=4000)

        self.assertEqual(result['reviewed_claim_count'],0)
        self.assertEqual(result['unreviewed_claim_count'],1)
        self.assertEqual(result['oversized_claim_ids'],['claim-1'])

    def test_unreviewed_cross_chunk_claim_changes_single_session_review_to_rejected(self):
        original=source([('user','项目甲'),('user','更正为项目乙'),('assistant','已处理项目乙')])
        state={'subject':{'text':'当前项目乙','message_ids':['id1','id2']},
               'goal':claim('处理项目','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('更正为项目乙','id2')],
               'assistant_reports':[claim('已处理项目乙','id3')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test-model')
        def opener(request,timeout):
            payload=json.loads(request.data)
            body=json.loads(payload['messages'][0]['content'].rsplit('输入：',1)[1])
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'source_revision':body['source_revision'],'accept':True,'issues':[]},ensure_ascii=False)}}]}
            return io.BytesIO(json.dumps(response,ensure_ascii=False).encode())
        unreviewed={'reviewed_claim_count':0,'unreviewed_claim_count':1,
                    'unreviewed_claim_ids':['claim-1'],'oversized_claim_ids':['claim-1'],
                    'correction_context_unreviewed_claim_ids':[],'review_batch_count':0,'issues':[]}
        with patch('lib.scenario_model._request_cross_chunk_claim_reviews',return_value=unreviewed):
            review=scenario_model.request_session_review(original,draft,base_url='https://example.invalid',
                api_key='test',model='test-model',opener=opener,max_input_chars=8)

        self.assertEqual(review['status'],'model_review_rejected')
        self.assertGreater(review['review_coverage']['unreviewed_cross_chunk_claim_count'],0)

    def test_cross_claim_review_does_not_swallow_correction_hint_failure(self):
        source_value={'source_revision':'r1','messages':[
            {'evidence_id':'m1','role':'user','text':'旧方案'},
            {'evidence_id':'m2','role':'user','text':'更正为新方案'}]}
        claim_value={'statement':'项目方案','field':'subject','message_ids':['m1','m2']}
        with patch('lib.scenario_state_v3.correction_review_hints',side_effect=RuntimeError('hint_error')):
            with self.assertRaisesRegex(RuntimeError,'hint_error'):
                scenario_model._request_cross_chunk_claim_reviews(
                    source_value,[(1,claim_value)],base_url='https://example.invalid',api_key='test',
                    model='test-model',opener=lambda *_args,**_kwargs: self.fail('must fail closed'),max_chars=4000)

    def test_batched_cross_claim_source_messages_stay_in_chronological_order(self):
        source_value={'source_revision':'r1','messages':[
            {'evidence_id':'m1','role':'user','text':'方案早期要求'},
            {'evidence_id':'m2','role':'user','text':'中间继续细化'},
            {'evidence_id':'m3','role':'user','text':'最终要求保持原方案'},
        ]}
        seen=[]
        def opener(request,timeout):
            prompt=json.loads(request.data)['messages'][0]['content']
            body=json.loads(prompt.rsplit('输入：',1)[1]);seen.append(body)
            result={'source_revision':'r1','claim_reviews':[
                {'claim_id':row['claim_id'],'accept':True,'issues':[]} for row in body['claims']]}
            return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{
                'content':json.dumps(result,ensure_ascii=False)}}]},ensure_ascii=False).encode())

        scenario_model._request_cross_chunk_claim_reviews(source_value,[
            (1,{'statement':'后期说法','field':'goal','message_ids':['m3']}),
            (2,{'statement':'早期说法','field':'subject','message_ids':['m1']}),
        ],base_url='https://example.invalid',api_key='test',model='test-model',opener=opener,max_chars=4000)

        self.assertEqual([row['message_id'] for row in seen[0]['messages']],['m1','m3'])

    def test_cross_claim_review_keeps_older_corrections_or_marks_context_incomplete(self):
        rows=[{'evidence_id':f'm{index}','role':'user','text':f'更正项目阶段记录{index}'}
              for index in range(1,14)]
        source_value={'source_revision':'r1','messages':rows}
        seen=[]
        def opener(request,timeout):
            prompt=json.loads(request.data)['messages'][0]['content']
            body=json.loads(prompt.rsplit('输入：',1)[1]);seen.append(body)
            result={'source_revision':'r1','claim_reviews':[
                {'claim_id':row['claim_id'],'accept':True,'issues':[]} for row in body['claims']]}
            return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{
                'content':json.dumps(result,ensure_ascii=False)}}]},ensure_ascii=False).encode())

        result=scenario_model._request_cross_chunk_claim_reviews(source_value,[
            (1,{'statement':'旧决定是否仍有效','field':'subject','message_ids':['m1','m13']})
        ],base_url='https://example.invalid',api_key='test',model='test-model',opener=opener,max_chars=10000)

        self.assertEqual(result['unreviewed_claim_count'],0)
        self.assertEqual([row['message_id'] for row in seen[0]['correction_review_hints']],
                         [f'm{index}' for index in range(1,14)])
        self.assertEqual([row['message_id'] for row in seen[0]['messages']],
                         [f'm{index}' for index in range(1,14)])

    def test_correction_context_beyond_review_cap_blocks_cross_claim_instead_of_dropping_oldest(self):
        source_value={'source_revision':'r1','messages':[
            {'evidence_id':f'm{index}','role':'user','text':f'更正项目阶段记录{index}'}
            for index in range(1,202)]}
        claim_value={'statement':'旧决定是否仍有效','field':'subject','message_ids':['m1','m201']}
        def never_call(*args,**kwargs):
            raise AssertionError('incomplete correction context must not be sent as a complete review')

        result=scenario_model._request_cross_chunk_claim_reviews(source_value,[(1,claim_value)],
            base_url='https://example.invalid',api_key='test',model='test-model',
            opener=never_call,max_chars=60000)

        self.assertEqual(result['unreviewed_claim_count'],1)
        self.assertEqual(result['correction_context_unreviewed_claim_ids'],['claim-1'])

    def test_v3_fingerprint_changes_with_state_but_v2_fingerprint_does_not(self):
        v2={'schema':'evolving-profile.scenario-draft.v2','context_id':'session:x',
            'source_revision':'r1','events':[],'summaries':{},'evidence':[],'unknowns':[]}
        stable=scenario_model.fingerprint_draft(v2)
        self.assertEqual(stable,scenario_model.fingerprint_draft({**v2,'unused_metadata':'not hashed'}))
        v3={**v2,'schema':'evolving-profile.scenario-draft.v3','state':{'phase':'requested'}}
        self.assertNotEqual(scenario_model.fingerprint_draft(v3),
                            scenario_model.fingerprint_draft({**v3,'state':{'phase':'unknown'}}))

    def test_promotion_keeps_v3_state_only_after_source_model_and_manual_gates(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','不要写具体机器人型号')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'requested',
               'constraints':[],'corrections':[claim('不要写具体机器人型号','id3')],
               'assistant_reports':[claim('旧稿已完成','id2')],
               'unresolved':[claim('新要求尚无完成答复','id3')]}
        draft=validate_state_draft(original,state,model='test-model')
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test-model'}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual'}
        with self.assertRaisesRegex(ValueError,'scenario_promotion_coverage_unresolved'):
            promote_session_draft(row,original,draft,review,manual)
        manual.update(scope_verdict='whole_session_scope_acceptable',reviewed_source_message_count=3,
                      episode_scope_verdict='single_coherent_task')
        published=promote_session_draft(row,original,draft,review,manual)
        self.assertEqual(published['scenario_state']['phase'],'requested')
        self.assertEqual(published['status'],'model_reviewed')
        review['status']='model_review_rejected'
        with self.assertRaisesRegex(ValueError,'scenario_promotion_gate_failed'):
            promote_session_draft(row,original,draft,review,manual)

    def test_v2_promotion_remains_compatible_without_episode_scope_verdict(self):
        original=source([('user','编制项目方案'),('assistant','方案已形成')])
        draft=scenario_model.validate_session_state(original,{
            'source_revision':'revision-1','events':[
                {'kind':'user_goal','message_id':'id1','quote':'编制项目方案'},
                {'kind':'assistant_report','message_id':'id2','quote':'方案已形成'}]},model='test-model')
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test-model'}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual'}

        published=promote_session_draft(row,original,draft,review,manual)

        self.assertEqual(published['status'],'model_reviewed')
        self.assertNotIn('manual_source_coverage',published)

    def test_multichunk_promotion_requires_explicit_whole_session_manual_coverage(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','改成背负式喷洒'),('assistant','新版已形成')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('改成背负式喷洒','id3')],
               'assistant_reports':[claim('新版已形成','id4')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        draft['selection_coverage']={'source_revision':'revision-1','source_message_count':4,
            'source_chunk_count':2,'candidate_state_count':2,'chunk_char_limit':20,
            'semantic_completeness_proven':False}
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test'}
        review['review_coverage']={'source_chunk_count':2,'reviewed_chunk_count':2,
            'source_chunk_char_limit':20,
            'all_chunks_accepted':True,'cross_chunk_claim_count':0,
            'cross_chunk_claim_reviewed_count':0,'unreviewed_cross_chunk_claim_count':0}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual',
                'scope_verdict':'whole_session_scope_acceptable','reviewed_source_message_count':2}
        with self.assertRaisesRegex(ValueError,'scenario_promotion_coverage_unresolved'):
            promote_session_draft(row,original,draft,review,manual)
        manual.update(scope_verdict='whole_session_scope_acceptable',reviewed_source_message_count=4)
        with self.assertRaisesRegex(ValueError,'scenario_promotion_episode_scope_unresolved'):
            promote_session_draft(row,original,draft,review,manual)
        manual['episode_scope_verdict']='single-task'
        with self.assertRaisesRegex(ValueError,'scenario_promotion_episode_scope_invalid'):
            promote_session_draft(row,original,draft,review,manual)
        manual['episode_scope_verdict']=[]
        with self.assertRaisesRegex(ValueError,'scenario_promotion_episode_scope_invalid'):
            promote_session_draft(row,original,draft,review,manual)
        manual['episode_scope_verdict']='multiple_topics'
        with self.assertRaisesRegex(ValueError,'scenario_promotion_episode_split_required'):
            promote_session_draft(row,original,draft,review,manual)
        manual['episode_scope_verdict']='single_coherent_task'
        review['review_coverage']['source_chunk_char_limit']=30000
        with self.assertRaisesRegex(ValueError,'scenario_promotion_review_coverage_incomplete'):
            promote_session_draft(row,original,draft,review,manual)
        review['review_coverage']['source_chunk_char_limit']=20
        review['review_coverage']['cross_chunk_claim_count']=1
        with self.assertRaisesRegex(ValueError,'scenario_promotion_review_coverage_incomplete'):
            promote_session_draft(row,original,draft,review,manual)
        review['review_coverage']['cross_chunk_claim_count']=0
        published=promote_session_draft(row,original,draft,review,manual)
        self.assertEqual(published['manual_source_coverage']['reviewed_source_message_count'],4)
        self.assertEqual(published['manual_source_coverage']['episode_scope_verdict'],'single_coherent_task')

    def test_multichunk_promotion_validates_chunk_count_type_before_branching(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','改成背负式喷洒'),('assistant','新版已形成')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('改成背负式喷洒','id3')],
               'assistant_reports':[claim('新版已形成','id4')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        draft['selection_coverage']={'source_revision':'revision-1','source_message_count':4,
            'source_chunk_count':[],'candidate_state_count':[],'chunk_char_limit':20,
            'semantic_completeness_proven':False}
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test'}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual',
                'scope_verdict':'whole_session_scope_acceptable','reviewed_source_message_count':4,
                'episode_scope_verdict':'single_coherent_task'}
        with self.assertRaisesRegex(ValueError,'scenario_state_invalid'):
            promote_session_draft(row,original,draft,review,manual)

    def test_single_chunk_multitopic_promotion_requires_episode_split(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','改成背负式喷洒'),('assistant','新版已形成')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('改成背负式喷洒','id3')],
               'assistant_reports':[claim('新版已形成','id4')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test'}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual',
                'scope_verdict':'whole_session_scope_acceptable','reviewed_source_message_count':4,
                'episode_scope_verdict':'multiple_topics'}
        with self.assertRaisesRegex(ValueError,'scenario_promotion_episode_split_required'):
            promote_session_draft(row,original,draft,review,manual)

    def test_scope_reviewed_message_count_rejects_boolean(self):
        original=source([('user','编制项目方案')])
        state={'subject':claim('项目方案','id1'),'goal':claim('编制项目方案','id1'),
               'phase':'requested','constraints':[claim('只处理本项目','id1')],
               'corrections':[],'assistant_reports':[],
               'unresolved':[claim('用户请求待回应','id1')]}
        draft=validate_state_draft(original,state,model='test')
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test'}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual',
                'scope_verdict':'whole_session_scope_acceptable',
                'reviewed_source_message_count':True,
                'episode_scope_verdict':'single_coherent_task'}
        with self.assertRaisesRegex(ValueError,'scenario_promotion_coverage_unresolved'):
            promote_session_draft(row,original,draft,review,manual)

    def test_v3_promotion_keeps_three_useful_tiers_without_extra_constraints(self):
        original=source([('user','编制方案'),('assistant','方案已形成')])
        state={'subject':claim('方案','id1'),'goal':claim('编制方案','id1'),
               'phase':'assistant_reported','constraints':[],'corrections':[],
               'assistant_reports':[claim('方案已形成','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        self.assertNotEqual(draft['summaries']['compact'],draft['summaries']['standard'])
        self.assertIn('助手报告（未独立核验）',draft['summaries']['standard'])
        self.assertNotEqual(draft['summaries']['standard'],draft['summaries']['full'])
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test'}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual',
                'scope_verdict':'whole_session_scope_acceptable','reviewed_source_message_count':2,
                'episode_scope_verdict':'single_coherent_task'}
        published=promote_session_draft(row,original,draft,review,manual)
        self.assertEqual(published['status'],'model_reviewed')

    def test_v3_promotion_allows_standard_equal_to_compact_when_no_extra_evidence_exists(self):
        original=source([('user','为天津大学编制巡检方案')])
        state={'subject':claim('天津大学巡检方案','id1'),'goal':claim('编制巡检方案','id1'),
               'phase':'requested','constraints':[],'corrections':[],'assistant_reports':[],
               'unresolved':[claim('为天津大学编制巡检方案','id1')]}
        draft=validate_state_draft(original,state,model='test')
        self.assertEqual(draft['summaries']['compact'],draft['summaries']['standard'])
        self.assertNotEqual(draft['summaries']['compact'],draft['summaries']['full'])
        digest=scenario_model.fingerprint_draft(draft)
        row=build_session_context(original['thread_id'],'workspace',['seed.md'],'旧导航')
        review={'status':'model_review_passed','source_revision':'revision-1','issues':[],
                'draft_sha256':digest,'review_model':'test'}
        manual={'verdict':'conversation_only_draft_acceptable','source_revision':'revision-1',
                'draft_sha256':digest,'reviewer':'manual','scope_verdict':'whole_session_scope_acceptable',
                'reviewed_source_message_count':1,'episode_scope_verdict':'single_coherent_task'}

        published=promote_session_draft(row,original,draft,review,manual)

        self.assertEqual(published['status'],'model_reviewed')

    def test_form_reply_projects_question_and_answer_without_losing_raw_source(self):
        raw='<send_user_message_question_reply>[{"questionItemId":"q1","question":"是否申报其他专项资金？","answer":"未申报，填否"}]</send_user_message_question_reply>'
        self.assertEqual(parse_form_reply(raw),[{'question':'是否申报其他专项资金？','answer':'未申报，填否'}])
        original=source([('user',raw)])
        projected=project_source_messages(original)
        self.assertEqual(projected[0]['provenance_kind'],'structured_user_answer')
        self.assertIn('是否申报其他专项资金？',projected[0]['text'])
        self.assertIn('未申报，填否',projected[0]['text'])
        self.assertEqual(original['messages'][0]['text'],raw)
        self.assertEqual(projected[0]['message_id'],'id1')

    def test_malformed_form_reply_stays_opaque_not_a_user_claim(self):
        raw='<send_user_message_question_reply>{broken}</send_user_message_question_reply>'
        projected=project_source_messages(source([('user',raw)]))
        self.assertEqual(projected[0]['provenance_kind'],'opaque_structured_reply')
        self.assertNotIn('{broken}',projected[0]['text'])

    def test_final_user_request_cannot_be_marked_completed(self):
        original=source([('user','为天津财经大学商学院编制人工智能实训平台申报书'),
                         ('assistant','已完成旧版申报书'),
                         ('user','现在改为软件平台和硬件算力分开')])
        state={'subject':claim('天津财经大学商学院人工智能实训平台申报书','id1'),
               'goal':claim('编制申报书','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('软件平台和硬件算力分开','id3')],
               'assistant_reports':[claim('已完成旧版申报书','id2')],
               'unresolved':[claim('新要求尚无完成答复','id3')]}
        with self.assertRaisesRegex(ValueError,'scenario_state_phase_invalid'):
            validate_state_draft(original,state,model='test')

    def test_final_structured_user_answer_cannot_inherit_old_assistant_completion(self):
        raw='<send_user_message_question_reply>[{"question":"是否申报其他专项？","answer":"否"}]</send_user_message_question_reply>'
        original=source([('user','编制项目申报书'),('assistant','旧稿已完成'),('user',raw)])
        state={'subject':claim('项目申报书','id1'),'goal':claim('编制申报书','id1'),
               'phase':'assistant_reported','constraints':[],
               'corrections':[],'assistant_reports':[claim('旧稿已完成','id2')],
               'unresolved':[]}
        with self.assertRaisesRegex(ValueError,'scenario_state_phase_invalid'):
            validate_state_draft(original,state,model='test')

    def test_three_tiers_preserve_object_latest_request_and_assistant_boundary(self):
        original=source([('user','为天津财经大学商学院编制人工智能实训平台申报书'),
                         ('assistant','已完成旧版申报书'),
                         ('user','现在改为软件平台和硬件算力分开')])
        state={'subject':claim('天津财经大学商学院人工智能实训平台申报书','id1'),
               'goal':claim('编制申报书','id1'),'phase':'requested',
               'constraints':[],'corrections':[claim('软件平台和硬件算力分开','id3')],
               'assistant_reports':[claim('已完成旧版申报书','id2')],
               'unresolved':[claim('新要求尚无完成答复','id3')]}
        draft=validate_state_draft(original,state,model='test')
        self.assertEqual(draft['schema'],'evolving-profile.scenario-draft.v3')
        compact=draft['summaries']['compact']
        self.assertIn('天津财经大学商学院',compact)
        self.assertIn('编制申报书',compact)
        self.assertIn('待处理',compact)
        self.assertIn('新要求尚无完成答复',compact)
        self.assertIn('软件平台和硬件算力分开',draft['summaries']['standard'])
        self.assertIn('助手报告（未独立核验）',draft['summaries']['full'])
        self.assertIn('id2',draft['summaries']['full'])
        self.assertEqual(draft['summaries']['full'].count('助手报告（未独立核验）：已完成旧版申报书'),1)
        for tier,text in draft['summaries'].items():
            self.assertLessEqual(len(text),BUDGETS['session'][tier]['max_chars'])
        self.assertIn('最后一条用户请求后未见助手最终答复。',draft['unknowns'])

    def test_completed_phase_must_cite_last_assistant_and_compact_names_progress(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','旧稿已完成'),
                         ('user','改成不写机器人型号'),('assistant','新版Word已改好')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),
               'goal':claim('编制巡检方案','id1'),'phase':'assistant_reported',
               'constraints':[],'corrections':[claim('不写机器人型号','id3')],
               'assistant_reports':[claim('旧稿已完成','id2')],'unresolved':[]}
        with self.assertRaisesRegex(ValueError,'scenario_state_phase_invalid'):
            validate_state_draft(original,state,model='test')
        state['assistant_reports'].append(claim('新版Word已改好','id4'))
        draft=validate_state_draft(original,state,model='test')
        self.assertIn('最近进展',draft['summaries']['compact'])
        self.assertIn('未独立核验',draft['summaries']['compact'])
        self.assertIn('已答复',draft['summaries']['compact'])
        self.assertIn('最近进展（助手报告，未独立核验）：新版Word已改好',draft['summaries']['compact'])
        self.assertIn('新版Word已改好',draft['summaries']['standard'])
        self.assertEqual(draft['summaries']['standard'].count('新版Word已改好'),1)
        self.assertEqual(draft['summaries']['full'].count('新版Word已改好'),1)
        self.assertIn('对象：天津大学墙体巡检方案 [来源:id1]',draft['summaries']['full'])
        self.assertIn('纠正：不写机器人型号 [来源:id3]',draft['summaries']['full'])

    def test_answered_request_with_unknown_external_result_is_not_an_unanswered_request(self):
        original=source([('user','把报告上传到公开仓库'),('assistant','已上传公开仓库，尚无独立验收')])
        state={'subject':claim('报告公开仓库上传','id1'),'goal':claim('上传报告','id1'),
               'phase':'assistant_reported','constraints':[],'corrections':[],
               'assistant_reports':[claim('已上传公开仓库，尚无独立验收','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        self.assertEqual(draft['state']['unresolved'],[])
        self.assertEqual(draft['state']['phase'],'assistant_reported')
        self.assertIn('最近进展（助手报告，未独立核验）：已上传公开仓库，尚无独立验收',draft['summaries']['compact'])
        self.assertNotIn('待处理',draft['summaries']['compact'])
        self.assertNotIn('verified',draft['summaries']['compact'])

    def test_compact_report_excerpt_preserves_attribution_and_full_report_in_standard(self):
        report='助手自述已生成报告；'+'尚需外部核验。'*18
        original=source([('user','编制报告'),('assistant',report)])
        state={'subject':claim('报告','id1'),'goal':claim('编制报告','id1'),
               'phase':'assistant_reported','constraints':[],'corrections':[],
               'assistant_reports':[claim(report,'id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        self.assertIn('最近进展（助手报告，未独立核验）：助手自述已生成报告',draft['summaries']['compact'])
        self.assertIn('…',draft['summaries']['compact'])
        self.assertIn(report,draft['summaries']['standard'])
        for tier,text in draft['summaries'].items():
            self.assertLessEqual(len(text),BUDGETS['session'][tier]['max_chars'])

    def test_recent_report_follows_source_order_even_when_model_array_is_reversed(self):
        original=source([('user','EP里的线索是否有作用'),('assistant','尚未核对EP记录，不能断言无效'),
                         ('user','刚才实际用了EP吗'),('assistant','实际查找未采用EP线索，但不证明记录不存在')])
        state={'subject':claim('EP定位线索','id1'),'goal':claim('核对实际检索路径','id3'),
               'phase':'assistant_reported','constraints':[],'corrections':[],
               'assistant_reports':[claim('实际查找未采用EP线索，但不证明记录不存在','id4'),
                                    claim('尚未核对EP记录，不能断言无效','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        self.assertEqual([r['message_ids'] for r in draft['state']['assistant_reports']],[['id2'],['id4']])
        self.assertIn('最近进展（助手报告，未独立核验）：实际查找未采用EP线索',draft['summaries']['compact'])
        self.assertIn('尚未核对EP记录，不能断言无效',draft['summaries']['full'])
        self.assertEqual(draft['state']['unresolved'],[])

    def test_state_rejects_claim_from_wrong_role_or_unknown_source(self):
        original=source([('user','天津大学墙体巡检方案'),('assistant','已完成方案')])
        state={'subject':claim('天津大学墙体巡检方案','id1'),'goal':claim('编制方案','id1'),
               'phase':'assistant_reported','constraints':[],'corrections':[],
               'assistant_reports':[claim('已完成方案','id1')],'unresolved':[]}
        with self.assertRaisesRegex(ValueError,'scenario_state_role_invalid'):
            validate_state_draft(original,state,model='test')
        state['assistant_reports']=[claim('已完成方案','missing')]
        with self.assertRaisesRegex(ValueError,'scenario_evidence_id_invalid'):
            validate_state_draft(original,state,model='test')


if __name__=='__main__':
    unittest.main()
