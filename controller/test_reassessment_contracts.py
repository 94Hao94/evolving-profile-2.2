"""Cross-boundary regressions from the September 5 architecture review."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '/Users/apple/.hindsight/custom-codex/scripts')
import recall
from lib.memory_packet import build_memory_packet
from fast_bank import FastMemoryBank


class ReassessmentContracts(unittest.TestCase):
    def resolve(self, result):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({'choices': [{'message': {'content': json.dumps(result)}}]}).encode()
        with patch.object(recall, '_load_dotenv_values', return_value={
            'HINDSIGHT_API_LLM_BASE_URL': 'https://test.invalid', 'HINDSIGHT_API_LLM_API_KEY': 'test',
        }), patch.object(recall.urllib.request, 'urlopen', return_value=Response()) as call:
            value = recall.qwen_full_prompt_fallback('继续', [
                {'role': 'user', 'content': '继续核对 ICU 论文，保留数据可得性与论文出处。'},
                {'role': 'assistant', 'content': '下一步检查候选论文。'},
            ])
        return value, json.loads(call.call_args.args[0].data)

    def test_resolver_does_not_impose_memory_infrastructure(self):
        _, request = self.resolve({'full_prompt': '继续核对 ICU 论文。', 'confidence': .99})
        text = request['messages'][0]['content']
        self.assertNotIn('Hook、Controller、Bank、Packet', text)

    def test_resolver_rejects_untraceable_expansion_even_with_high_confidence(self):
        value, _ = self.resolve({'full_prompt': '继续修复支付系统。', 'confidence': 1.0,
                                 'context_evidence': [{'message_index': 0, 'quote': '支付系统'}]})
        self.assertFalse(value['ok'])

    def test_resolver_preserves_valid_source_evidence(self):
        value, _ = self.resolve({'full_prompt': '继续核对 ICU 论文，保留数据可得性与论文出处。',
                                 'confidence': .99, 'unresolved': [],
                                 'context_evidence': [{'message_index': 0, 'quote': 'ICU 论文'}]})
        self.assertTrue(value['ok'])
        self.assertEqual(value.get('context_evidence', [{}])[0].get('quote'), 'ICU 论文')

    def test_missing_task_data_does_not_discard_grounded_question(self):
        value, _ = self.resolve({'full_prompt':'继续核对 ICU 论文；哪些论文尚未处理仍待查证。',
            'confidence':.98, 'context_evidence':[{'message_index':0,'quote':'ICU 论文'}],
            'unresolved':['尚未处理的论文清单']})
        self.assertTrue(value['ok'])
        self.assertEqual(value['resolution_state'], 'partial_with_unresolved')
        self.assertEqual(value['unresolved'], ['尚未处理的论文清单'])

    def test_default_bank_record_is_not_verified_by_rendering(self):
        packet = build_memory_packet(full_prompt='当前状态？', items=[
            {'id': 'a', 'type': 'world', 'text': '助手认为任务已完成。'}])
        self.assertNotIn('核验事实', packet['rendered_context'])

    def test_observation_is_guidance_not_execution_authority(self):
        packet = build_memory_packet(full_prompt='给出建议', items=[
            {'id': 'o', 'type': 'observation', 'text': '用户可能偏好简短解释。'}])
        self.assertNotIn('执行约束', packet['rendered_context'])

    def test_superseded_observation_keeps_warning(self):
        packet = build_memory_packet(full_prompt='回顾过去的偏好', items=[
            {'id':'o','type':'observation','text':'旧偏好','metadata':{'time_status':'superseded'}}])
        self.assertEqual(packet['bundles'][0]['role'], 'conflict')

    def test_rendering_does_not_claim_host_confirmation(self):
        packet = build_memory_packet(full_prompt='当前状态', items=[{'id':'a','text':'状态待核实'}])
        self.assertEqual(packet.get('rendered_record_ids'), ['a'])
        self.assertEqual(packet.get('delivery_evidence'), 'rendered_only_host_visibility_unknown')

    def test_distinct_timeline_events_keep_each_statement(self):
        packet = build_memory_packet(full_prompt='请回顾系统更新履历', items=[
            {'id': 'v1', 'text': '8月1日官方安装后无法写入。'},
            {'id': 'v2', 'text': '8月9日官方安装版本更换并完成修复。'},
        ])
        self.assertIn('8月1日官方安装后无法写入', packet['rendered_context'])
        self.assertIn('8月9日官方安装版本更换并完成修复', packet['rendered_context'])

    def cache(self, root):
        for name, content in [('snapshot.jsonl', '{"id":"s","text":"baseline"}\n'),
                              ('common.json', '{"candidates":[]}'),
                              ('policies.json', '{"policies":[]}'),
                              ('trace.json', '{"entries":{}}')]:
            (root/name).write_text(content)
        return FastMemoryBank(root/'snapshot.jsonl', root/'index.sqlite3',
                              common_path=root/'common.json', policies_path=root/'policies.json',
                              trace_index_path=root/'trace.json')

    def test_cache_updates_metadata_with_identical_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bank = self.cache(root)
            def save(revision):
                (root/'common.json').write_text(json.dumps({'candidates': [
                    {'id':'a', 'text':'same text', 'metadata': {'revision':revision}}]}))
            save(1); self.assertTrue(bank.ensure_ready())
            save(200); bank._refresh_runtime_if_needed(force=True)
            row = bank._conn.execute("select metadata_json from memory_units where id='candidate:a'").fetchone()
            self.assertEqual(json.loads(row[0])['revision'], 200)

    def test_withdrawn_policy_is_removed_without_process_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bank = self.cache(root)
            p = root/'policies.json'
            p.write_text(json.dumps({'policies':[{'policy_id':'p','policy':'old policy','status':'active_provisional'}]}))
            self.assertTrue(bank.ensure_ready())
            p.write_text('{"policies":[]}'); bank._refresh_runtime_if_needed(force=True)
            self.assertEqual(bank._conn.execute("select count(*) from memory_units where id like 'direct-policy:%'").fetchone()[0], 0)

    def test_selected_trace_is_not_a_new_memory_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bank = self.cache(root)
            (root/'trace.json').write_text(json.dumps({'entries':{'x':{'at':'2026-09-05','selected_results':[
                {'id':'x','text':'selected does not mean independently verified'}]}}}))
            self.assertTrue(bank.ensure_ready())
            self.assertEqual(bank._conn.execute("select count(*) from memory_units where id like 'trace:%'").fetchone()[0], 0)

    def test_corrupt_policy_file_does_not_withdraw_good_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bank = self.cache(root)
            p = root/'policies.json'
            p.write_text(json.dumps({'policies':[{'policy_id':'p','policy':'keep policy','status':'active'}]}))
            self.assertTrue(bank.ensure_ready())
            p.write_text('{'); bank._refresh_runtime_if_needed(force=True)
            self.assertEqual(bank._conn.execute("select count(*) from memory_units where id='direct-policy:p'").fetchone()[0], 1)
            self.assertTrue(bank._runtime_refresh_error)

    def test_new_canonical_snapshot_is_loaded_without_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bank = self.cache(root)
            self.assertTrue(bank.ensure_ready())
            (root/'snapshot.jsonl').write_text('{"id":"new","text":"new canonical evidence"}\n')
            self.assertTrue(bank.ensure_ready())
            self.assertEqual(bank._conn.execute("select id from memory_units where id='new'").fetchone()[0], 'new')
            self.assertIsNone(bank._conn.execute("select id from memory_units where id='s'").fetchone())

    def test_missing_snapshot_does_not_destroy_last_good_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bank = self.cache(root)
            self.assertTrue(bank.ensure_ready())
            (root/'snapshot.jsonl').unlink()
            self.assertTrue(bank.ensure_ready())
            self.assertIsNotNone(bank._conn.execute("select id from memory_units where id='s'").fetchone())
            self.assertTrue(bank._runtime_refresh_error)

if __name__ == '__main__': unittest.main()
