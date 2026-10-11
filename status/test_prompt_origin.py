import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PromptOriginTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('prompt_origin_status_test', Path(__file__).with_name('evolving_profile_status_server.py'))
        self.status = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.status)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        (self.root / 'audit').mkdir()

    def ingress(self, name, source='vscode', thread_source='user', prompt='继续解释机制', parent=None):
        path = self.sessions / f'rollout-day-{name}.jsonl'
        payload = {'id': name, 'session_id': parent or name, 'source': source, 'thread_source': thread_source}
        if parent:
            payload['parent_thread_id'] = parent
        message={'type':'response_item','payload':{'type':'message','role':'user','id':'msg-'+name,
                 'internal_chat_message_metadata_passthrough':{'turn_id':name,'content_item_kinds':['user.text']},
                 'content':[{'type':'input_text','text':prompt}]}}
        path.write_text(json.dumps({'type': 'session_meta', 'payload': payload}) + '\n'+json.dumps(message)+'\n')
        return {'at': '2026-10-09T00:00:00Z', 'source': 'codex-userpromptsubmit', 'host_id': 'codex',
                'prompt_preview': prompt, 'prompt_fingerprint': name, 'prompt_origin': 'user_direct',
                'session_id': parent or name, 'turn_id': name, 'hook_invocation_id': name,
                'transcript_path': str(path)}

    def write_ingress(self, rows):
        (self.root / 'audit/prompt-ingress.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n')

    def collection(self, **kwargs):
        with patch.object(self.status, 'STATE_ROOT', self.root), \
             patch.object(self.status, 'CODEX_SESSION_ROOTS', [self.sessions], create=True), \
             patch.object(self.status, '_hook_output_rows', return_value=[]), \
             patch.object(self.status, 'guidance_delivery_list', return_value={}), \
             patch.object(self.status, 'research_snapshot', return_value={}):
            return self.status.guidance_prompt_list(**kwargs)

    def test_audit_retains_unknown_and_human_quoted_background_words(self):
        quoted = self.ingress('human', prompt='请解释 "Memory Writing Agent: Phase 2" 和 codex://threads/，这是用户引用的模板')
        missing = {**quoted, 'session_id': 'missing', 'hook_invocation_id': 'missing', 'transcript_path': None}
        self.write_ingress([quoted, missing])
        with patch.object(self.status, 'STATE_ROOT', self.root), patch.object(self.status, 'CODEX_SESSION_ROOTS', [self.sessions], create=True):
            rows = self.status._prompt_ingress_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['origin_kind'] for row in rows], ['human', 'unknown'])

    def test_default_natural_filter_and_counts_use_one_population(self):
        human = self.ingress('human')
        voice = self.ingress('voice', thread_source='realtime_voice', prompt='嗯，然后呢？')
        child = self.ingress('child', source={'subagent': {'thread_spawn': {'parent_thread_id': 'parent', 'depth': 1}}}, thread_source='subagent', parent='parent')
        auto = self.ingress('auto', thread_source='automation')
        maintenance = self.ingress('memory', source={'subagent': 'memory_consolidation'}, thread_source='subagent')
        unknown = {**human, 'session_id': 'missing', 'hook_invocation_id': 'missing', 'prompt_fingerprint': 'missing', 'transcript_path': None}
        self.write_ingress([human, voice, child, auto, maintenance, unknown])
        value = self.collection(limit=1)
        self.assertEqual(value['total'], 2)
        self.assertEqual(value['statistics_denominator'], 2)
        self.assertEqual(value['audit_total'], 6)
        self.assertEqual(value['source_counts'], {'human': 2, 'subagent': 1, 'automation': 1, 'memory-maintenance': 1, 'unknown': 1})
        self.assertEqual(value['next_cursor'], '1')
        second = self.collection(limit=1, cursor=value['next_cursor'])
        self.assertEqual(second['items'][0]['origin_kind'], 'human')
        self.assertIsNone(second['next_cursor'])
        audit = self.collection(prompt_source='all')
        self.assertEqual(audit['total'], 6)
        self.assertEqual(self.collection(prompt_source='unknown')['total'], 1)
        self.assertEqual(self.collection(prompt_source='subagent')['total'], 1)
        self.assertEqual(self.collection(cursor='9999')['items'], [])
        self.assertEqual(self.collection(prompt_source='bogus')['source_filter'], 'natural')

    def test_identity_mismatch_is_unknown_even_if_ingress_claims_user_direct(self):
        row = self.ingress('actual')
        row['session_id'] = 'unrelated'
        self.write_ingress([row])
        value = self.collection(prompt_source='all')
        self.assertEqual(value['items'][0]['origin_kind'], 'unknown')
        self.assertEqual(value['items'][0]['origin_evidence']['reason'], 'session_identity_mismatch')

    def test_changed_header_origin_is_not_hidden_by_cache(self):
        row = self.ingress('changed')
        self.write_ingress([row])
        first = self.collection()
        self.assertEqual(first['total'], 1)
        path = Path(row['transcript_path'])
        lines=path.read_text().splitlines()
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'changed', 'source': 'vscode', 'thread_source': 'automation'}}) + '\n'+lines[1]+'\n')
        second = self.collection(prompt_source='all')
        self.assertEqual(second['items'][0]['origin_kind'], 'automation')
        self.assertNotEqual(first['items'][0]['origin_evidence']['source_revision'], second['items'][0]['origin_evidence']['source_revision'])

    def test_shared_native_metadata_cases(self):
        from prompt_origin import classify_prompt_origin
        fixtures = json.loads((Path(__file__).resolve().parents[1] / 'guidance/prompt-origin-fixtures.json').read_text())
        for case in fixtures:
            with self.subTest(case=case['name']):
                self.assertEqual(classify_prompt_origin(case['row'], case['metadata'])['origin_kind'], case['expected'])

    def test_malformed_audit_lines_and_native_headers_do_not_drop_other_prompts(self):
        human = self.ingress('human')
        malformed = self.ingress('malformed')
        Path(malformed['transcript_path']).write_text('null\n')
        self.write_ingress([human, malformed, None, []])
        value = self.collection(prompt_source='all', limit='invalid')
        self.assertEqual(value['total'], 2)
        self.assertEqual(value['items'][1]['origin_kind'], 'unknown')
        self.assertEqual(self.collection(query_text='解释')['natural_total'], 1)
        self.write_ingress([])
        self.assertEqual(self.collection()['source_counts'], {'human': 0, 'subagent': 0, 'automation': 0, 'memory-maintenance': 0, 'unknown': 0})

    def test_detail_keeps_background_audit_access_and_duplicate_log_is_one_occurrence(self):
        child = self.ingress('child', source={'subagent': {'thread_spawn': {'parent_thread_id': 'parent', 'depth': 1}}}, thread_source='subagent', parent='parent')
        self.write_ingress([child, dict(child)])
        self.assertEqual(self.collection()['total'], 0)
        audit = self.collection(prompt_source='all')
        self.assertEqual(audit['total'], 1)
        self.assertEqual(self.collection(detail_id=audit['items'][0]['prompt_id'])['items'][0]['origin_kind'], 'subagent')
        self.assertEqual(len((self.root / 'audit/prompt-ingress.jsonl').read_text().splitlines()), 2)

    def test_missing_path_locates_actual_child_turn_instead_of_borrowing_parent_human_header(self):
        parent=self.ingress('parent',prompt='actual human wording')
        child=self.ingress('child',source={'subagent':{'thread_spawn':{'parent_thread_id':'parent','depth':1}}},thread_source='subagent',parent='parent',prompt='a dispatched prompt')
        self.write_ingress([{**child,'transcript_path':None}])
        value=self.collection(prompt_source='all')
        self.assertEqual(value['items'][0]['origin_kind'],'subagent')
        self.assertEqual(value['items'][0]['origin_evidence']['native_session_id'],'child')
        self.assertEqual(value['natural_total'],0)

    def test_matching_only_parent_tool_arguments_is_not_a_human_prompt(self):
        parent=self.ingress('parent',prompt='actual human wording')
        path=Path(parent['transcript_path'])
        with path.open('a') as stream:
            stream.write(json.dumps({'type':'event_msg','payload':{'type':'item_completed','turn_id':'other','item':{'type':'CollabAgentToolCall','tool':'spawn_agent','prompt':'a dispatched prompt'}}})+'\n')
        self.write_ingress([{**parent,'turn_id':'missing','prompt_preview':'a dispatched prompt'}])
        value=self.collection(prompt_source='all')
        self.assertEqual(value['items'][0]['origin_kind'],'unknown')
        self.assertEqual(value['items'][0]['origin_evidence']['reason'],'native_prompt_message_not_found')

    def test_incremental_cursor_crosses_large_non_user_record_and_append_keeps_existing_witness(self):
        import prompt_origin as origin
        row=self.ingress('human');path=Path(row['transcript_path'])
        with path.open('a') as stream:stream.write(json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'x'*1200000}})+'\n')
        first=origin.PromptOriginResolver([self.sessions]);self.assertEqual(first.resolve(row)['origin_kind'],'human')
        with path.open('a') as stream:stream.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'human'}})+'\n')
        warm=origin.PromptOriginResolver([self.sessions]);self.assertEqual(warm.resolve(row)['origin_kind'],'human')
        self.assertGreater(origin.SCAN_BYTE_BUDGET-warm.bytes_left,0)
        self.assertLess(origin.SCAN_BYTE_BUDGET-warm.bytes_left,4096)
        pending={**row,'turn_id':'missing'}
        last=None
        for _ in range(4):last=origin.PromptOriginResolver([self.sessions]).resolve(pending)
        self.assertEqual(last['origin_evidence']['reason'],'native_prompt_message_not_found')
        self.assertEqual(last['origin_evidence']['verification_progress']['complete_files'],1)

    def test_rewrite_existing_message_then_grow_file_cannot_reuse_old_witness(self):
        import hashlib
        row=self.ingress('human',prompt='original request')
        row['prompt_fingerprint']=hashlib.sha256(b'original request').hexdigest()[:16]
        self.write_ingress([row]);initial=self.collection(prompt_source='all')['items'][0]
        self.assertEqual(initial['origin_kind'],'human')
        path=Path(row['transcript_path']);lines=path.read_text().splitlines();message=json.loads(lines[1])
        message['payload']['content'][0]['text']='different request'
        path.write_text(lines[0]+'\n'+json.dumps(message)+'\n'+json.dumps({'type':'event_msg','payload':{'type':'token_count'}})+'\n')
        changed=self.collection(prompt_source='all')['items'][0]
        self.assertEqual(changed['origin_kind'],'unknown')
        self.assertNotEqual(changed['origin_evidence'].get('message_sha256'),initial['origin_evidence']['message_sha256'])

    def test_truncated_long_prompt_requires_valid_matching_full_fingerprint(self):
        import hashlib
        text='x'*600+' native suffix';base=self.ingress('human',prompt=text)
        for fingerprint,want in [(None,'unknown'),('legacy','unknown'),('0'*16,'unknown'),(hashlib.sha256(text.encode()).hexdigest()[:16],'human')]:
            with self.subTest(fingerprint=fingerprint):
                self.write_ingress([{**base,'prompt_preview':'x'*600,'prompt_fingerprint':fingerprint}])
                row=self.collection(prompt_source='all')['items'][0]
                self.assertEqual(row['origin_kind'],want)

    def test_window_index_keeps_advancing_after_budget_exhausted_rows(self):
        import prompt_origin as origin
        row=self.ingress('big',prompt='target native request');path=Path(row['transcript_path']);lines=path.read_text().splitlines()
        filler=json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'x'*2000}})+'\n'
        path.write_text(lines[0]+'\n'+filler*700+lines[1]+'\n'+filler*700)
        other=[self.ingress('other-'+str(i),prompt='different question') for i in range(140)]
        found=False;prefixes=[]
        for _ in range(8):
            resolver=origin.PromptOriginResolver([self.sessions]);resolver.bytes_left=512*1024
            result=resolver.resolve_rows([row,*other])[0]
            self.assertLessEqual(512*1024-resolver.bytes_left,512*1024)
            if result['origin_kind']=='human':found=True;break
            prefixes.append(result['origin_evidence']['verification_progress']['indexed_prefix_bytes'])
        self.assertTrue(found,'a bounded request must retain its cursor across the full window, not clear it at source 129')
        self.assertGreater(max(prefixes),min(prefixes))

    def test_proven_locator_stays_verified_before_unresolved_rows_spend_shared_budget(self):
        import prompt_origin as origin
        proven=self.ingress('proven',prompt='already verified')
        self.assertEqual(origin.PromptOriginResolver([self.sessions]).resolve(proven)['origin_kind'],'human')
        blocked=self.ingress('blocked',prompt='not yet indexed');path=Path(blocked['transcript_path']);lines=path.read_text().splitlines()
        filler=json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'x'*2000}})+'\n'
        path.write_text(lines[0]+'\n'+filler*700+lines[1]+'\n'+filler*700)
        for _ in range(3):
            resolver=origin.PromptOriginResolver([self.sessions]);resolver.bytes_left=4096
            result=resolver.resolve_rows([blocked,proven])
            self.assertEqual(result[1]['origin_kind'],'human')
            self.assertLessEqual(4096-resolver.bytes_left,4096)

    def test_current_page_gets_lookup_budget_before_an_unresolved_earlier_occurrence(self):
        import prompt_origin as origin
        blocked=self.ingress('blocked',prompt='not on current page');path=Path(blocked['transcript_path']);lines=path.read_text().splitlines()
        filler=json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'x'*2000}})+'\n'
        path.write_text(lines[0]+'\n'+filler*700+lines[1]+'\n'+filler*700)
        page=self.ingress('page',prompt='current page user')
        self.write_ingress([blocked,page])
        with patch.object(origin,'SCAN_BYTE_BUDGET',4096):
            result=self.collection(prompt_source='all',cursor='1',limit=1)
        self.assertEqual(result['items'][0]['origin_kind'],'human')

    def test_known_page_proof_also_advances_its_unfinished_source_once_per_window(self):
        import prompt_origin as origin
        row=self.ingress('first',prompt='first page proof');path=Path(row['transcript_path']);lines=path.read_text().splitlines()
        filler=json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'x'*2000}})+'\n'
        later=json.loads(lines[1]);later['payload']['id']='msg-later';later['payload']['internal_chat_message_metadata_passthrough']['turn_id']='later';later['payload']['content'][0]['text']='later page proof'
        path.write_text(lines[0]+'\n'+lines[1]+'\n'+filler*700+json.dumps(later)+'\n'+filler*700)
        for _ in range(4):
            result=origin.PromptOriginResolver([self.sessions]).resolve_rows([row])[0]
            self.assertEqual(result['origin_kind'],'human')
        lookup=origin.PromptOriginResolver([self.sessions]);lookup.bytes_left=4096
        later_row={**row,'turn_id':'later','prompt_preview':'later page proof','prompt_fingerprint':None}
        self.assertEqual(lookup.resolve(later_row)['origin_kind'],'human','a page made entirely of cached proofs must not freeze indexing forever')


if __name__ == '__main__':
    unittest.main()
