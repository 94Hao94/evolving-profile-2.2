import unittest

from lib.context_coordination import build_contextual_intent_envelope, build_full_prompt_context_slice, build_profile, build_contextual_recall_query


PAGE = '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'
ENV = '<environment_context><cwd>/project</cwd><shell>zsh</shell></environment_context>'


class TaskContextMetadataTest(unittest.TestCase):
    def test_real_message_array_does_not_replace_prior_task_with_page_metadata(self):
        messages = [{'role':'user','content':'修复 Evolving Profile 的来源核验并验收'},
                    {'role':'assistant','content':'已经定位问题，下一步核对测试。'},
                    {'role':'user','content':PAGE}]
        envelope = build_contextual_intent_envelope('继续处理', messages, {})
        self.assertIn('前一项活动任务：修复 Evolving Profile 的来源核验并验收',envelope['full_prompt'])
        self.assertNotIn('external_codex_apps_open_page',envelope['full_prompt'])
        self.assertEqual(envelope['current_user_message'],'继续处理')
        profile=build_profile('继续处理',messages,{})
        self.assertEqual(profile['recent'],['修复 Evolving Profile 的来源核验并验收'])
        sliced,receipt=build_full_prompt_context_slice('继续处理',messages)
        self.assertEqual(sliced,messages[:2])
        self.assertEqual(receipt['source_message_count'],2)

    def test_structural_metadata_preceding_human_prose_preserves_directive(self):
        messages=[{'role':'user','content':ENV+'\n'+PAGE+'\n继续核验原生来源，不要部署'}]
        envelope=build_contextual_intent_envelope('更细一点',messages,{})
        self.assertIn('前一项活动任务：继续核验原生来源，不要部署',envelope['full_prompt'])
        self.assertEqual(build_profile('再细一点',messages,{})['recent'],['继续核验原生来源，不要部署'])

    def test_metadata_only_input_cannot_create_task_or_continuation_anchors(self):
        for block in [PAGE,ENV,ENV+'\n'+PAGE]:
            with self.subTest(block=block):
                envelope=build_contextual_intent_envelope('继续', [{'role':'user','content':block}], {})
                self.assertEqual(envelope['intent_mode'],'current_utterance')
                self.assertEqual(envelope['context_items'],[])
                self.assertEqual(envelope['full_prompt'],'')

    def test_quoted_xml_and_malformed_or_unrecognized_page_data_remain_user_content(self):
        for text in ['请解释这个模板：'+PAGE,'```xml\n'+PAGE+'\n```',
                     '请解释环境示例：'+ENV,'"'+ENV+'"',
                     '<external_codex_apps_open_page>{bad json}</external_codex_apps_open_page>',
                     '<external_codex_apps_open_page>{"note":"user XML example"}</external_codex_apps_open_page>',
                     '<environment_context>用户自己写的示例文字</environment_context>',
                     '<environment_context><cwd>missing end</environment_context>']:
            with self.subTest(text=text):
                self.assertEqual(build_profile('解释一下',[{'role':'user','content':text}],{})['recent'],[text])

    def test_existing_source_normalization_protocol_is_not_changed_by_task_helper(self):
        from lib.content import extract_user_request
        self.assertEqual(extract_user_request(PAGE),PAGE)
        self.assertEqual(extract_user_request('例子：'+ENV),'例子：')

    def test_voice_attachment_and_current_user_text_remain_authoritative(self):
        messages=[{'role':'user','content':PAGE+'\n## My request for Codex:\n附件：review.pdf\n按这份材料继续，不要扩展范围'}]
        envelope=build_contextual_intent_envelope(ENV+'\n继续处理',messages,{})
        self.assertEqual(envelope['current_user_message'],'继续处理')
        self.assertIn('附件：review.pdf 按这份材料继续，不要扩展范围',envelope['full_prompt'])

    def test_metadata_only_current_input_cannot_become_a_fallback_recall_query(self):
        envelope=build_contextual_intent_envelope(PAGE,[],{})
        self.assertEqual(build_contextual_recall_query(PAGE,envelope),'')
        mixed=PAGE+'\n解释附件中的采购条件'
        self.assertEqual(build_contextual_recall_query(mixed,build_contextual_intent_envelope(mixed,[],{})),'解释附件中的采购条件')

    def test_native_tag_with_unknown_fields_or_meaningful_xml_text_preserves_constraints(self):
        from lib.content import extract_task_user_request
        from task_state import TaskStateStore
        import tempfile
        from pathlib import Path
        texts=[
            '<environment_context><cwd>/project</cwd>Do not deploy.</environment_context>',
            '<environment_context>Do not deploy.<cwd>/project</cwd></environment_context>',
            '<environment_context><cwd>Do not deploy.</cwd></environment_context>',
            '<environment_context><cwd>/project</cwd><shell>Do not deploy.</shell></environment_context>',
            '<environment_context><filesystem><workspace_roots><root>/project</root>Do not deploy.</workspace_roots></filesystem></environment_context>',
            '<environment_context><filesystem><request>Do not deploy.</request></filesystem></environment_context>',
            '<environment_context><!-- Do not deploy. --><cwd>/project</cwd></environment_context>',
            '<external_codex_apps_open_page>{"page_id":null,"request":"Do not deploy."}</external_codex_apps_open_page>',
            '<external_codex_apps_open_page>{"page_id":{"request":"Do not deploy."}}</external_codex_apps_open_page>',
            '<external_codex_apps_open_page>{"page_id":"Do not deploy.","page_id":null}</external_codex_apps_open_page>',
            '<in-app-browser-context>{"request":"Do not deploy."}</in-app-browser-context>',
            '<heartbeat>{"request":"Do not deploy."}</heartbeat>',
            '<evolving_profile_checkpoint>{"request":"Do not deploy."}</evolving_profile_checkpoint>',
        ]
        for text in texts:
            with self.subTest(text=text),tempfile.TemporaryDirectory() as folder:
                self.assertEqual(extract_task_user_request(text),text)
                self.assertEqual(build_profile('继续',[{'role':'user','content':text}],{})['recent'],[text])
                envelope=build_contextual_intent_envelope('继续',[{'role':'user','content':text}],{})
                self.assertIn('Do not deploy.',envelope['full_prompt'])
                state=TaskStateStore(Path(folder)).record('s',text,'t','h',continuation=False)
                self.assertEqual(state['current_objective'],text)

    def test_actual_complete_environment_filesystem_schema_is_metadata_only(self):
        from lib.content import extract_task_user_request
        text='<environment_context>\n<cwd>/Users/apple/Documents/Codex</cwd>\n<shell>zsh</shell>\n<current_date>2026-10-09</current_date>\n<timezone>Asia/Shanghai</timezone>\n<filesystem><workspace_roots><root>/project</root></workspace_roots><permission_profile type="disabled"><file_system type="unrestricted" /></permission_profile></filesystem>\n</environment_context>'
        self.assertEqual(extract_task_user_request(text),'')
        self.assertEqual(extract_task_user_request(text+'\nDo not deploy.'),'Do not deploy.')


if __name__=='__main__':unittest.main()
