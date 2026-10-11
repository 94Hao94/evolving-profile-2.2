import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class McpToolActivityTests(unittest.TestCase):
    def test_user_return_body_preview_is_kept_without_claiming_host_delivery(self):
        with tempfile.TemporaryDirectory() as home, patch.object(self.module, 'project_tool_review', return_value={'status':'disabled'}):
            self._invoke_reply(home,'user_recall',{}, {'content':[{'type':'text','text':json.dumps({'memories':[{'id':'m1','text':'这次真实返回的候选正文'}], 'delivery':{'host_visibility':'unknown','answer_use':'not_measured'}})}]})
            event=json.loads((Path(home)/'.evolving-profile/audit/mcp-tool-activity.jsonl').read_text().splitlines()[-1])
        self.assertEqual(event['mapping_items'][0]['id'],'m1')
        self.assertEqual(event['mapping_items'][0]['text'],'这次真实返回的候选正文')
        self.assertEqual(event['delivery']['host_visibility'],'unknown')

    def test_preference_preview_preserves_returned_conditions_and_not_deferred_units(self):
        value={'included':[{'id':'p1','text':'偏好正文','applies_when':['页面交付'],'exceptions':['纯文本不适用']}],
               'stable_profile':[{'id':'stable','text':'稳定参考'}],
               'deferred':[{'id':'later','text':'未返回正文'}]}
        items=self.module._process_mapping_items(value)
        self.assertEqual([item['id'] for item in items],['p1','stable'])
        self.assertEqual(items[0]['applies_when'],['页面交付'])
        self.assertEqual(items[0]['exceptions'],['纯文本不适用'])

    def test_returned_source_navigation_stays_separate_from_body_and_delivery(self):
        navigation = {'memory_id':'nav','document_id':'doc','chunk_id':'chunk','source_revision':'revision',
            'subject_relation':'unverified','claim_verification':'not_performed','authority':'unverified_source_claim',
            'next_action':{'tool':'read_source','arguments':{'memory_id':'nav','scope':'chunk'}}}
        with tempfile.TemporaryDirectory() as home, patch.object(self.module, 'project_tool_review', return_value={'status':'disabled'}):
            result = {'content':[{'type':'text','text':json.dumps({'memories':[],
                'source_navigation':[navigation],'source_navigation_reference_count':191,'source_navigation_returned_count':1,
                'delivery':{'host_visibility':'unknown','answer_use':'not_measured'}})}]}
            self._invoke_reply(home, 'user_recall', {}, result)
            event = json.loads((Path(home)/'.evolving-profile/audit/mcp-tool-activity.jsonl').read_text().splitlines()[-1])
        self.assertEqual(event['returned_count'], 0)
        self.assertEqual(event['memory_ids'], [])
        self.assertEqual(event['mapping_items'], [])
        self.assertEqual(event['source_navigation_returned_count'], 1)
        self.assertEqual(event['source_navigation_returned_ids'], ['nav'])
        self.assertEqual(event['source_navigation'], [navigation])
        self.assertEqual(event['delivery'], {'host_visibility':'unknown','answer_use':'not_measured'})
        route = self.module._route_receipt_counts({'tool_events':[event]})
        self.assertEqual(route['delivery_state'], 'source_navigation_returned')
        self.assertEqual(route['returned_count'], 0)
        self.assertEqual(route['source_navigation_returned_count'], 1)

    def test_agent_activity_keeps_query_and_workspace_for_pool_deduplication(self):
        with tempfile.TemporaryDirectory() as home, patch.object(self.module, 'project_tool_review', return_value={'status': 'disabled'}):
            result = {'content': [{'type': 'text', 'text': json.dumps({'source': 'agent_process_memory',
                'workspace_id': 'shared-pool', 'candidate_count': 144, 'returned_count': 1, 'records': []})}]}
            self._invoke_reply(home, 'agent_recall', {'query': '同一个历史查询'}, result)
            event = json.loads((Path(home) / '.evolving-profile/audit/mcp-tool-activity.jsonl').read_text().splitlines()[-1])
        self.assertEqual(event['workspace_id'], 'shared-pool')
        self.assertEqual(event['query'], '同一个历史查询')

    def test_process_candidates_are_available_in_receipt_preview_without_fake_delivery(self):
        with tempfile.TemporaryDirectory() as home, patch.object(self.module, 'project_tool_review', return_value={'status': 'disabled'}):
            result = {'content': [{'type': 'text', 'text': json.dumps({'source': 'agent_process_memory', 'records': [
                {'process_memory_id': 'pm_trace_test', 'text': '实际候选正文', 'readback_tool': 'read_agent_process_memory'}], 'returned_count': 1, 'candidate_count': 80})}]}
            self._invoke_reply(home, 'agent_recall', {}, result)
            event = json.loads((Path(home) / '.evolving-profile/audit/mcp-tool-activity.jsonl').read_text().splitlines()[-1])
        self.assertEqual(event['mapping_items'][0]['text'], '实际候选正文')
        self.assertEqual(event['mapping_items'][0]['id'], 'pm_trace_test')
        self.assertNotEqual(event.get('delivery', {}).get('host_visibility'), 'observed')

    def test_process_read_reply_reports_record_and_original_message_counts_separately(self):
        with tempfile.TemporaryDirectory() as home, patch.object(self.module, 'project_tool_review', return_value={'status': 'disabled'}):
            payload = self._invoke_reply(home, 'read_agent_process_memory', {}, {'content': [{'type': 'text', 'text': json.dumps({
                'record': {'process_memory_id': 'pm_trace_test'}, 'source': 'agent_process_memory',
                'raw_source': {'text': 'original messages'}, 'raw_source_status': 'source_read',
            })}]})
        value = json.loads(payload['content'][0]['text'])
        self.assertEqual(value['returned_count'], 1)
        self.assertEqual(value['original_message_readback_count'], 1)

    def test_agent_result_count_reads_records_instead_of_reporting_zero(self):
        self.assertEqual(self.module._returned_count({"returned_count": 6, "records": [{"process_memory_id": "p"}]}, "agent_recall"), 6)
        self.assertEqual(self.module._returned_count({"record": {"process_memory_id": "p"}}, "read_agent_process_memory"), 1)

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module('evolving_profile_controller_mcp')

    def _invoke_reply(self, home, tool_name, arguments, result):
        output=io.StringIO()
        with patch.object(self.module.Path,'home',return_value=Path(home)), \
             patch.object(self.module,'CURRENT_TOOL_CALL',{'name':tool_name,'arguments':arguments}), \
             contextlib.redirect_stdout(output):
            self.module.reply('id-1',result=result)
        return json.loads(output.getvalue())['result']

    def test_recall_result_reports_discovered_and_returned_counts_separately(self):
        with tempfile.TemporaryDirectory() as home:
            result={'content':[{'type':'text','text':json.dumps({
                'research_id':'research-1','discovered_reference_count':26,
                'memories':[{'id':'m1','text':'first'},{'id':'m2','text':'second'}],
                'delivery':{'transport':'mcp_tool_result','host_visibility':'unknown','answer_use':'not_measured'},
            })}]}
            payload=self._invoke_reply(home,'recall',{},result)
            value=json.loads(payload['content'][0]['text'])
            self.assertEqual(value['discovered_reference_count'],26)
            self.assertIn('returned_count',value)
            self.assertEqual(value['returned_count'],2)

    def test_tool_receipt_identifies_the_loaded_adapter_build(self):
        with tempfile.TemporaryDirectory() as home:
            result = {'content': [{'type': 'text', 'text': json.dumps({'memories': []})}]}
            payload = self._invoke_reply(home, 'recall', {}, result)
        value = json.loads(payload['content'][0]['text'])
        digest = hashlib.sha256(Path(self.module.__file__).read_bytes()).hexdigest()
        self.assertEqual(value.get('adapter_build_sha256'), digest)

    def test_internal_judge_receipt_is_delivered_and_recorded_without_deleting_candidates(self):
        with tempfile.TemporaryDirectory() as home, patch.object(self.module, 'project_tool_review', return_value={'status': 'ok', 'mode': 'assist', 'answers': {'evidence': {'choice': 'needs_source'}}, 'calls': 1}):
            result = {'content': [{'type': 'text', 'text': json.dumps({'memories': [{'id': 'm1', 'text': 'unchanged candidate'}], 'returned_count': 1})}]}
            payload = self._invoke_reply(home, 'user_recall', {}, result)
            value = json.loads(payload['content'][0]['text'])
            event = json.loads((Path(home) / '.evolving-profile/audit/mcp-tool-activity.jsonl').read_text().splitlines()[-1])
        self.assertEqual(value['memories'], [{'id': 'm1', 'text': 'unchanged candidate'}])
        self.assertEqual(value['jev_review']['answers']['evidence']['choice'], 'needs_source')
        self.assertEqual(event['jev_review']['calls'], 1)

    def test_scenario_read_reports_summary_count_not_original_source_reads(self):
        with tempfile.TemporaryDirectory() as home:
            result = {'content': [{'type': 'text', 'text': json.dumps({
                'source': 'scenario_summary_index', 'items': [{'scenario_id': 'session:s', 'summary': '摘要'}]})}]}
            payload = self._invoke_reply(home, 'read_scenario_summary', {}, result)
            value = json.loads(payload['content'][0]['text'])
            activity = json.loads((Path(home) / '.evolving-profile/audit/mcp-tool-activity.jsonl').read_text().splitlines()[-1])
        self.assertEqual(value.get('returned_count'), 1)
        self.assertEqual(activity['source_read_count'], 0)
        self.assertEqual(activity['scenario_ids'], ['session:s'])
        self.assertNotIn('scenario_episode_count', activity)

    def test_episode_directory_receipt_lists_locators_without_copying_episode_bodies(self):
        with tempfile.TemporaryDirectory() as home:
            result = {'content': [{'type': 'text', 'text': json.dumps({
                'source': 'scenario_summary_index', 'scenario_type': 'session', 'tier': 'compact',
                'items': [{'scenario_id': 'session:s', 'summary': '本会话含两个 episode',
                           'episodes_total': 2, 'episodes': [
                               {'episode_id': 'episode:s:m1', 'title': '项目甲方案', 'status': 'model_reviewed'},
                               {'episode_id': 'episode:s:m2', 'title': '项目乙预算', 'status': 'model_reviewed'}]}]})}]}
            self._invoke_reply(home, 'read_scenario_summary', {}, result)
            activity = json.loads((Path(home) / '.evolving-profile/audit/mcp-tool-activity.jsonl')
                                  .read_text().splitlines()[-1])

        self.assertEqual(activity['scenario_episode_count'], 2)
        self.assertEqual([row['episode_id'] for row in activity['scenario_episodes']],
                         ['episode:s:m1', 'episode:s:m2'])
        self.assertNotIn('summary', activity['scenario_episodes'][0])
        self.assertEqual(activity['scenario_summary_text'], '本会话含两个 episode')

    def test_single_episode_read_receipt_keeps_the_returned_tier_body(self):
        with tempfile.TemporaryDirectory() as home:
            result = {'content': [{'type': 'text', 'text': json.dumps({
                'source': 'scenario_summary_index', 'scenario_type': 'episode', 'tier': 'standard',
                'status': 'current', 'items': [{'scenario_id': 'episode:s:m2', 'scenario_type': 'episode',
                    'episode_id': 'episode:s:m2', 'title': '项目乙预算', 'status': 'current',
                    'summary': '标准摘要：项目乙预算已比较；来源范围已核验。'}]})}]}
            self._invoke_reply(home, 'read_scenario_summary', {'episode_id': 'episode:s:m2'}, result)
            activity = json.loads((Path(home) / '.evolving-profile/audit/mcp-tool-activity.jsonl')
                                  .read_text().splitlines()[-1])

        self.assertEqual(activity['scenario_episode_id'], 'episode:s:m2')
        self.assertEqual(activity['scenario_tier'], 'standard')
        self.assertEqual(activity['scenario_summary_status'], 'current')
        self.assertIn('项目乙预算已比较', activity['scenario_summary_text'])

    def test_preference_result_count_includes_visible_guidance_not_deferred_items(self):
        with tempfile.TemporaryDirectory() as home:
            result={'content':[{'type':'text','text':json.dumps({
                'included':[{'id':'p1'},{'id':'p2'}],
                'stable_profile':[{'id':'p3'}],
                'model_sections':[{'section_id':'model-1'}],
                'deferred':[{'id':'not-delivered'}],
            })}]}
            payload=self._invoke_reply(home,'get_preference',{},result)
            value=json.loads(payload['content'][0]['text'])
            self.assertIn('returned_count',value)
            self.assertEqual(value['returned_count'],4)

    def test_stale_check_id_is_not_written_into_an_older_prompt_receipt(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)/'.evolving-profile'/'audit'
            receipt_root=root/'memory-route-receipts'
            receipt_root.mkdir(parents=True)
            check_id='prompt-old'
            target=receipt_root/(hashlib.sha256(check_id.encode()).hexdigest()+'.json')
            target.write_text(json.dumps({
                'check_id':check_id,
                'prompt_binding':{'session_id':'session-a','turn_id':'turn-old','hook_invocation_id':check_id},
                'tool_events':[],
            }),encoding='utf-8')
            now=datetime.now(timezone.utc)
            ingress=root/'prompt-ingress.jsonl'
            ingress.write_text('\n'.join(json.dumps(row) for row in [
                {'at':(now-timedelta(minutes=3)).isoformat(),'session_id':'session-a','turn_id':'turn-old','hook_invocation_id':check_id},
                {'at':(now-timedelta(minutes=1)).isoformat(),'session_id':'session-a','turn_id':'turn-new','hook_invocation_id':'prompt-new'},
            ])+'\n',encoding='utf-8')
            result={'content':[{'type':'text','text':json.dumps({'memories':[{'id':'m1'}]})}]}
            with patch.dict(os.environ,{'EVOLVING_PROFILE_ROUTE_RECEIPT_ROOT':str(receipt_root)}):
                payload=self._invoke_reply(home,'recall',{'check_id':check_id},result)
            value=json.loads(payload['content'][0]['text'])
            self.assertIn('observability_binding',value)
            self.assertEqual(value['observability_binding']['state'],'stale_prompt_binding')
            receipt=json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(receipt['tool_events'],[])
            activity=json.loads((root/'mcp-tool-activity.jsonl').read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual(activity['binding_state'],'stale_prompt_binding')
            self.assertEqual(activity['session_id'],'session-a')
            self.assertEqual(activity['latest_hook_invocation_id'],'prompt-new')

    def test_recall_flags_new_budget_details_without_blocking_retrieval(self):
        with tempfile.TemporaryDirectory() as home:
            audit=Path(home)/'.evolving-profile/audit'
            audit.mkdir(parents=True)
            audit.joinpath('prompt-ingress.jsonl').write_text(json.dumps({
                'at':(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat(),
                'session_id':'session-a','turn_id':'turn-a','hook_invocation_id':'prompt-a',
                'prompt_preview':'一个项目有45万元、50万元和55万元三个版本，分别属于什么阶段？',
            })+'\n',encoding='utf-8')
            result={'content':[{'type':'text','text':json.dumps({'memories':[{'id':'m1'}]})}]}
            payload=self._invoke_reply(home,'recall',{
                'check_id':'prompt-a',
                'query':'45万元、50万元、55万元：45万是28+14+3早期开发版，55万是46+5+4入库版',
            },result)
            value=json.loads(payload['content'][0]['text'])
            self.assertEqual(value['returned_count'],1)
            self.assertEqual(value['query_scope_audit']['status'],'review_added_anchors')
            self.assertIn('28+14+3',value['query_scope_audit']['added_budget_splits'])
            self.assertIn('not proof of fabrication',value['query_scope_audit']['boundary'])

    def test_recall_keeps_prompt_supported_budget_details_clear(self):
        with tempfile.TemporaryDirectory() as home:
            audit=Path(home)/'.evolving-profile/audit'
            audit.mkdir(parents=True)
            audit.joinpath('prompt-ingress.jsonl').write_text(json.dumps({
                'at':(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat(),
                'session_id':'session-a','turn_id':'turn-a','hook_invocation_id':'prompt-a',
                'prompt_preview':'核对45万元28+14+3方案与55万元46+5+4方案。',
            })+'\n',encoding='utf-8')
            result={'content':[{'type':'text','text':json.dumps({'memories':[]})}]}
            payload=self._invoke_reply(home,'recall',{
                'check_id':'prompt-a','query':'核对45万元28+14+3与55万元46+5+4两个方案',
            },result)
            value=json.loads(payload['content'][0]['text'])
            self.assertEqual(value['query_scope_audit']['status'],'no_added_anchors')

    def test_history_results_distinguish_candidates_from_direct_sources(self):
        for tool_name in ('recall','research'):
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as home:
                result={'content':[{'type':'text','text':json.dumps({'memories':[{'id':'m1'}]})}]}
                payload=self._invoke_reply(home,tool_name,{'query':'查找项目历史'},result)
                value=json.loads(payload['content'][0]['text'])
                contract=value['evidence_contract']
                self.assertEqual(contract['candidate_role'],'unverified_memory_preview')
                self.assertEqual(contract['navigation_role'],'locator_only')
                self.assertEqual(contract['absence_claim'],'not_supported_by_one_result_page')
                self.assertIn('requested slots',contract['stop_rule'])


if __name__=='__main__':
    unittest.main()
