import json
import unittest
from unittest.mock import patch
from system_probe import plan_history, run_probe, count_tokens
from recall import route_requires_ep_history


class SystemProbeTests(unittest.TestCase):
    def setUp(self):
        settings = patch('system_probe._load_probe_runtime_settings', return_value={})
        settings.start()
        self.addCleanup(settings.stop)

    def test_historical_reference_requires_real_ep_history_route(self):
        self.assertTrue(route_requires_ep_history('河北工业大学采购计算机那个项目需求书，你找到了吗'))
        self.assertTrue(route_requires_ep_history('我上次给你的那段说明'))
        self.assertFalse(route_requires_ep_history('把这句话翻译成英文'))
        self.assertFalse(route_requires_ep_history('请翻译以下内容，不要结合历史'))

    def test_probe_saves_excluded_candidates_without_injecting_their_body(self):
        plan=plan_history('鹏飞学校呢？')
        output, receipt=run_probe(plan, {'auto_probe': True, 'probe_max_tokens': 1200},
            lambda *args, **kwargs: {'results': [
                {'id': 'good', 'state': 'valid', 'text': '鹏飞学校方案'},
                {'id': 'uncertain', 'state': 'valid', 'text': '另一个高校的方案'},
                {'id': 'withdrawn', 'state': 'invalidated', 'text': '撤回正文'},
            ]}, 'bank')
        self.assertEqual(len(receipt['candidate_audit']), 3)
        self.assertEqual(receipt['candidate_audit'][0]['delivery'], 'text_returned')
        self.assertEqual(receipt['candidate_audit'][1]['delivery'], 'not_returned')
        self.assertEqual(receipt['candidate_audit'][2]['text'], '')
        self.assertNotIn('另一个高校的方案', output)
        self.assertEqual(receipt['text_returned_count'], 1)

    def test_unresolved_document_ordinal_is_not_a_literal_history_anchor(self):
        plan = plan_history('我让你重写第三份材料，第一份和第二份材料目前是合格的')
        self.assertEqual(plan['minimum_action'], 'needs_context')
        output, receipt = run_probe(plan, {'auto_probe': True},
                                   lambda *args, **kwargs: self.fail('unresolved references must not auto-search'), 'bank')
        self.assertEqual(receipt['calls'], 0)

    def test_monthly_inventory_and_paraphrase_choose_research_without_keywords(self):
        for text in ('我最近一个月都干什么了，分几类','请回顾我过去一个月的工作，按类别列出来','过去30天忙了些什么？'):
            with self.subTest(text=text):
                self.assertEqual(plan_history(text)['recommended_route'],'research')

    def test_explicit_boundaries_and_self_contained_tasks_do_not_search(self):
        for text in ('只根据下面材料回答。','不要使用任何记忆。','解释一下 recall 和 research 的区别','把“上次做错了”翻译成英文。','17乘以23等于多少？'):
            self.assertEqual(plan_history(text)['minimum_action'],'skip')

    def test_unknown_is_not_a_negative_memory_judgment(self):
        plan=plan_history('帮我看看这个方案')
        self.assertEqual(plan['history_dependency'],'possible')
        self.assertEqual(plan['minimum_action'],'recall_probe')

    def test_current_turn_audit_does_not_query_historical_bank(self):
        plan=plan_history('检验最新prompt有没有调用 recall research，看实际回执')
        self.assertEqual(plan['recommended_route'],'live_audit')
        self.assertEqual(plan['minimum_action'],'live_audit')
        self.assertEqual(plan['suggested_tools'],[])

    def test_defect_report_about_missing_history_calls_is_live_audit(self):
        prompts = (
            '应该调用 recall、research，但它没有调用；把测试做好后再停工。',
            '必须使用 get_preference，结果却没使用，请检查链路和回执。',
            '这条 Prompt 本应调用 EP 历史工具但没有调用，继续测试直到确认。',
            '刚才翻译的问题，我说是应该调用 recall、research，但它没有调用，请检查。',
            'codex://threads/01a0c2d2-9f05-7fc3-b913-ee631658e6a7 不是翻译任务，检查里面为什么没调用 recall。',
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                plan = plan_history(prompt)
                self.assertEqual(plan['recommended_route'], 'live_audit')
                self.assertEqual(plan['minimum_action'], 'live_audit')
                self.assertEqual(plan['suggested_tools'], [])

    def test_explicit_no_memory_boundary_still_wins_over_audit_words(self):
        plan = plan_history('不要读取任何记忆，只根据这段文字解释为什么没调用 recall。')
        self.assertEqual(plan['recommended_route'], 'skip')

    def test_quoted_translation_or_rewrite_does_not_trigger_live_audit(self):
        for prompt in (
            '把“应该调用 recall 但没有调用”翻译成英文。',
            '请润色这一句：“应该调用 recall 但没有调用，请检查。”',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(plan_history(prompt)['recommended_route'], 'skip')

    def test_live_audit_and_broad_inventory_skip_probe_and_use_explicit_route(self):
        audit=plan_history('codex://threads/x 看刚才几条对话有没有问题')
        inventory=plan_history('最近三天关于客户的事情都有哪些')
        self.assertEqual(audit['minimum_action'],'live_audit')
        self.assertEqual(inventory['recommended_route'],'research')
        self.assertEqual(inventory['minimum_action'],'agent_query')
        self.assertEqual(inventory['fallback_route'],None)

    def test_codex_thread_audit_is_live_audit_and_excludes_infrastructure_tokens(self):
        plan=plan_history('codex://threads/01a0c9c2-8952-7430-89a1-9df9cc0f3511你看看这里面对话找找问题先，先别修复，把问题找明白')
        self.assertEqual(plan['recommended_route'],'live_audit')
        self.assertEqual(plan['minimum_action'],'live_audit')
        self.assertEqual(plan['suggested_tools'],[])
        self.assertNotIn('codex',plan['focus_terms'])
        self.assertNotIn('threads',plan['focus_terms'])
        self.assertNotIn('a0c9c2-8952-7430-89a1-9df9cc0f3511',plan['focus_terms'])

    def test_multi_entity_mapping_and_previous_turn_audit_escalate(self):
        mapping=plan_history('跨多个学校把方案和客户对应起来')
        previous_audit=plan_history('上一轮对话链路有没有问题')
        self.assertEqual(mapping['recommended_route'],'research')
        self.assertEqual(mapping['minimum_action'],'agent_query')
        self.assertEqual(previous_audit['recommended_route'],'live_audit')
        self.assertEqual(previous_audit['minimum_action'],'live_audit')

    def test_multi_object_relation_comparison_uses_research(self):
        plan=plan_history('比较我在天津农学院、天职师大、河工大三件事之间的关系。')
        self.assertEqual(plan['recommended_route'],'research')
        self.assertEqual(plan['minimum_action'],'agent_query')

    def test_explicit_personal_format_history_routes_to_get_preference(self):
        plan=plan_history('我说公文和方案里面的格式，你这根据我习惯和要求记录的都有哪几种？')
        self.assertEqual(plan['recommended_route'],'get_preference')
        self.assertEqual(plan['minimum_action'],'agent_query')
        self.assertEqual(plan['suggested_tools'],['get_preference'])
        self.assertEqual(plan['required_ep_tool'],'mcp__evolving_profile_controller__user_preference')
        def forbidden(*_args,**_kwargs):
            raise AssertionError('preference lookup must not be approximated by a generic Bank probe')
        _output,receipt=run_probe(plan,{'auto_probe':True,'probe_max_tokens':500},forbidden,'bank')
        self.assertEqual(receipt['calls'],0)

    def test_time_bounded_generated_asset_inventory_uses_research(self):
        plan=plan_history('上周我写的方案有哪些生图了？')
        self.assertEqual(plan['recommended_route'],'research')
        self.assertEqual(plan['minimum_action'],'agent_query')

    def test_preference_opt_out_keeps_requested_historical_fact_route(self):
        plan=plan_history('不要用我的偏好，请查上次备份的结果。')
        self.assertEqual(plan['recommended_route'],'recall')

    def test_self_contained_code_conversion_skips_history(self):
        plan=plan_history('请把这段代码改成 TypeScript：function add(a,b){return a+b}')
        self.assertEqual(plan['minimum_action'],'skip')

    def test_version_evolution_and_test_recurrence_use_research(self):
        for text in (
            '把备份设置从最初版本到现在版本的变化、原因和未解决问题串起来。',
            '找出过去几次 Recall/Research 测试中反复出现的共同问题，并说明哪些已经修复。',
            '天津农学院和天职师大分别对应哪些项目、联系人和结果？',
        ):
            with self.subTest(text=text):
                self.assertEqual(plan_history(text)['recommended_route'],'research')

    def test_previous_test_question_is_recall_not_live_audit(self):
        plan=plan_history('上一轮测试中第六题具体出了什么问题？')
        self.assertEqual(plan['recommended_route'],'recall')
        self.assertEqual(plan['minimum_action'],'recall_probe')

    def test_general_explanation_with_explicit_personal_exclusion_skips_history(self):
        plan=plan_history('解释一下什么是向量数据库，不要结合我的项目。')
        self.assertEqual(plan['minimum_action'],'skip')

    def test_narrow_probe_admits_only_entity_overlapping_candidates(self):
        plan=plan_history('鹏飞学校呢？')
        self.assertEqual(plan['recommended_route'],'recall')
        calls=[]
        def api(path,body,timeout):
            calls.append(body)
            return {'results':[
                {'id':'candidate-good','text':'鹏飞学校 AI 方案记录'},
                {'id':'candidate-bad','text':'天职师大毛老师方案待办'},
            ]}
        output,receipt=run_probe(plan,{'auto_probe':True,'probe_max_tokens':500},api,'bank')
        self.assertEqual(len(calls),1)
        self.assertEqual(receipt['returned_count'],1)
        self.assertEqual(receipt['admission']['rejected_count'],1)
        self.assertIn('candidate-good',output)
        self.assertNotIn('candidate-bad',output)
        self.assertEqual(receipt['admission']['mode'],'positive_anchor_overlap')

    def test_generic_time_or_document_followup_does_not_run_automatic_probe(self):
        for prompt in ('一分钟是不是太少了容易观测到啊，2分钟呢','让你重新写个word这么麻烦呢'):
            with self.subTest(prompt=prompt):
                plan=plan_history(prompt)
                self.assertEqual(plan['focus_terms'],[])
                self.assertEqual(plan['minimum_action'],'agent_query')
                calls=[]
                _output,receipt=run_probe(
                    plan,{'auto_probe':True,'probe_max_tokens':500},
                    lambda *args:calls.append(args) or {'results':[{'id':'noise','text':'分钟或Word模板的旧记录'}]},
                    'bank')
                self.assertEqual(calls,[])
                self.assertEqual(receipt['calls'],0)
                self.assertEqual(receipt['returned_count'],0)

    def test_negated_entity_is_not_an_admission_anchor(self):
        plan=plan_history('跟Tailscale没有关系吧，现在只是SonoBus连不上服务器吧？')
        self.assertEqual(plan['focus_terms'],['sonobus'])
        calls=[]
        output,receipt=run_probe(
            plan,{'auto_probe':True,'probe_max_tokens':500},
            lambda *args,**kwargs:calls.append((args,kwargs)) or {'results':[
                {'id':'sonobus-router','text':'SonoBus UDP 12000 转发到 Mac mini 的网络排障记录'},
                {'id':'tailscale-iphone','text':'Tailscale 与 Shadowrocket 在 iPhone 上不能同时运行'},
            ]},
            'bank')
        self.assertEqual(len(calls),1)
        self.assertEqual(receipt['returned_count'],1)
        self.assertEqual(receipt['admission']['rejected_count'],1)
        self.assertIn('sonobus-router',output)
        self.assertNotIn('tailscale-iphone',output)

    def test_multi_anchor_query_rejects_same_entity_wrong_task_memory(self):
        plan={'recommended_route':'recall','minimum_action':'recall_probe','query':'河北工业大学未来学习中心与云深处具身智能项目式教学如何结合',
              'focus_terms':['河北工业大学','云深处','具身智能'],'candidate_policy':'positive_anchor_overlap'}
        output,receipt=run_probe(
            plan,{'auto_probe':True,'probe_max_tokens':500},
            lambda *args,**kwargs:{'results':[
                {'id':'hebut-hr','text':'河北工业大学人事处AI员工服务方案'},
                {'id':'hebut-embodied','text':'河北工业大学云深处具身智能项目式教学方案'},
            ]},
            'bank')
        self.assertEqual(receipt['returned_count'],1)
        self.assertEqual(receipt['admission']['rejected_count'],1)
        self.assertNotIn('hebut-hr',output)
        self.assertIn('hebut-embodied',output)

    def test_recall_probe_declares_research_fallback(self):
        plan=plan_history('鹏飞学校呢？')
        def api(path,body,timeout):
            return {'results':[]}
        output,receipt=run_probe(plan,{'auto_probe':True,'probe_max_tokens':500},api,'bank')
        self.assertEqual(receipt['state'],'empty')
        self.assertEqual(receipt['returned_count'],0)
        self.assertIn('research',output)
        self.assertIn('recall_empty_or_scope_insufficient',output)

    def test_followup_query_keeps_task_but_current_prohibition_wins(self):
        task={'continuation':True,'context_summary':'我最近一个月都干什么了，分几类'}
        self.assertEqual(plan_history('那你用啊，每一类把事情列出来',task)['recommended_route'],'research')
        self.assertEqual(plan_history('不要查历史，只根据下面材料回答',task)['minimum_action'],'skip')

    def test_probe_never_calls_denied_recall(self):
        plan=plan_history('不用 recall，使用 research 回顾上月工作')
        def forbidden(*args,**kwargs):raise AssertionError('forbidden API call')
        output,receipt=run_probe(plan,{'auto_probe':True,'probe_max_tokens':500},forbidden,'bank')
        self.assertEqual(receipt['calls'],0)

    def test_oversized_response_is_bounded_and_receipt_matches_delivered_items(self):
        calls=[]
        def api(path,body,timeout):
            calls.append(body)
            return {'results':[{'id':f'00000000-0000-0000-0000-{i:012d}','text':'鹏飞学校相关的来源摘要。'*500} for i in range(12)]}
        output,receipt=run_probe(plan_history('鹏飞学校呢？'),{'auto_probe':True,'probe_max_tokens':500},api,'bank')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['max_tokens'],500)
        self.assertEqual(calls[0]['budget'],'low')
        self.assertLessEqual(count_tokens(output)[0],500)
        self.assertLessEqual(receipt['returned_count'],3)
        self.assertGreater(receipt['returned_count'],0)
        for row in receipt['items']:self.assertIn(row['id'],output)

    def test_failure_is_not_empty_success(self):
        def fail(*args,**kwargs):raise TimeoutError('test timeout')
        output,receipt=run_probe(plan_history('鹏飞学校呢？'),{'auto_probe':True,'probe_max_tokens':500},fail,'bank')
        self.assertEqual(receipt['state'],'unavailable')
        self.assertIsNone(receipt['candidate_count'])
        self.assertIn('unavailable',output)
