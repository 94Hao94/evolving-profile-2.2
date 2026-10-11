import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class InstructionProjectionTest(unittest.TestCase):
    def test_route_projection_never_exposes_catalog_fact_body(self):
        spec=importlib.util.spec_from_file_location('route_safety_status_test', Path(__file__).with_name('evolving_profile_status_server.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        value=module._sanitize_route_receipt({
            'catalog_probe':{'matched_entities':['小黛']},
            'catalog_hints':[{'memory_id':'m1','type':'experience','topic':'SECRET FACT BODY','mentioned_at':'2026-08-01T00:00:00Z'}],
        })
        self.assertNotIn('SECRET FACT BODY',json.dumps(value,ensure_ascii=False))
        self.assertEqual(value['catalog_hints'][0]['topic'],'小黛相关经历记录')

    def test_history_without_receipt_is_unknown_not_not_needed(self):
        spec=importlib.util.spec_from_file_location('history_decision_status_test', Path(__file__).with_name('evolving_profile_status_server.py'))
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ingress={'at':'2026-09-17T06:00:00+00:00','session_id':'session','turn_id':'turn','hook_invocation_id':'hook','prompt_preview':'一个没有历史回执的问题','prompt_fingerprint':'fp','origin_kind':'human','origin_status':'verified'}
        with tempfile.TemporaryDirectory() as root:
            with patch.object(module,'STATE_ROOT',Path(root)), patch.object(module,'_prompt_ingress_rows',return_value=[ingress]), patch.object(module,'_hook_output_rows',return_value=[]), patch.object(module,'guidance_delivery_list',return_value={}), patch.object(module,'research_snapshot',return_value={}):
                row=module.guidance_prompt_list()['items'][0]
        self.assertEqual(row['routes']['historical_memory'],'unknown')
        self.assertEqual(row['history_decision'],'unknown')
        self.assertEqual(row['history_decision_evidence'],'missing_history_receipt')

    def test_history_keeps_its_recorded_manual_and_only_rendered_preferences(self):
        spec=importlib.util.spec_from_file_location('instruction_status_test', Path(__file__).with_name('evolving_profile_status_server.py'))
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manual={'instruction_version':'recorded-v1','content_sha256':'hash','core_text':'当时提供的说明','source_file':'/source.py','stage':'hook_context_prepared','model_context_visibility':'not_measured'}
        ingress={'at':'2026-09-17T06:00:00+00:00','session_id':'session','turn_id':'turn','hook_invocation_id':'hook','prompt_preview':'解释一下机制','prompt_fingerprint':'fp','origin_kind':'human','origin_status':'verified'}
        receipt={**ingress,'instruction':manual,'coverage':'partial','entry_context_included_count':1,'model_section_count':0,'deferred_count':1,'result':{'included':[{'id':'a','text':'已输出'},{'id':'b','text':'未输出'}]},'rendered_guidance':{'included':[{'id':'a','text':'已输出'}],'model_sections':[],'deferred':[{'id':'b'}]}}
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'audit/guidance-entry-receipts'
            target.mkdir(parents=True)
            (target/'receipt.json').write_text(json.dumps(receipt))
            with patch.object(module,'STATE_ROOT',Path(root)), patch.object(module,'_prompt_ingress_rows',return_value=[ingress]), patch.object(module,'_hook_output_rows',return_value=[]), patch.object(module,'guidance_delivery_list',return_value={}), patch.object(module,'research_snapshot',return_value={}):
                rows=module.guidance_prompt_list()['items']
                detail=module.guidance_prompt_detail(rows[0]['prompt_id'])
        self.assertEqual(detail['instruction_receipt'],manual)
        self.assertEqual([row['id'] for row in detail['guidance_receipt']['guidance_items']],['a'])
        self.assertEqual(detail['guidance_receipt']['host_state'],'hook_context_prepared')

if __name__=='__main__': unittest.main()
