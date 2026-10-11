import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from lib.returned_content import archive_returned_items,returned_items,preview_items

class ReturnedContentTests(unittest.TestCase):
    def test_incomplete_binding_cannot_create_an_exact_prompt_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            result=archive_returned_items(root,'user_recall',returned_items({'memories':[{'id':'m','text':'actual'}]}),{'state':'prompt_bound'}, {'tool_call_id':'call'})
            self.assertIsNone(result)
            self.assertFalse((Path(root)/'.evolving-profile/audit/returned-content').exists())

    def test_exact_snapshot_keeps_full_long_return_and_private_reasoning_is_not_archived(self):
        text='实际已返回😀'*1000
        items=returned_items({'memories':[{'id':'m','text':text}], 'private_reasoning':'must-not-store','source_navigation':[{'memory_id':'nav','text':'not-a-body'}]})
        previews=preview_items(items)
        self.assertTrue(previews[0]['text_truncated'])
        binding={'state':'prompt_bound','hook_invocation_id':'c','session_id':'s','turn_id':'t'}
        with tempfile.TemporaryDirectory() as root:
            snapshot=archive_returned_items(root,'user_recall',items,binding,{'tool_call_id':'call'})
            path=Path(root)/'.evolving-profile/audit/returned-content'/f"{snapshot['ref']}.json"
            raw=path.read_bytes();body=json.loads(raw)
            self.assertEqual(body['items'][0]['text'],text)
            self.assertEqual(hashlib.sha256(raw).hexdigest(),snapshot['ref'])
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertNotIn('must-not-store',raw.decode())
            self.assertNotIn('not-a-body',raw.decode())

    def test_source_readback_is_original_text_not_extracted_memory_summary(self):
        items=returned_items({'memory':{'id':'m','text':'提取摘要'},'source':{'text':'原文文本'}})
        self.assertEqual(items[0]['text'],'原文文本')
        self.assertEqual(items[0]['source_role'],'original_source')

    def test_explicitly_denied_original_source_and_root_cannot_bypass_record_guard(self):
        self.assertEqual(returned_items({'memory':{'id':'m'},'source':{'text':'denied','permission_status':'denied','hard_scope_match':False}}),[])
        self.assertEqual(returned_items({'permission_status':'denied','memories':[{'id':'m','text':'root denied'}]}),[])
        for flags in ({'scope_verification':{'status':'mismatch'}},{'scope_verification':'invalid'}):
            self.assertEqual(returned_items({**flags,'memories':[{'id':'m','text':'blocked envelope'}]}),[])

    def test_unauthorized_and_malformed_scopes_cannot_break_valid_return_preview(self):
        rows=[{'id':'m','text':'valid'},{'id':'bad','text':'blocked','permission_status':'denied'},{'id':'malformed','text':'invalid','scope_verification':'invalid'}]
        self.assertEqual([row['id'] for row in returned_items({'memories':rows})],['m'])

if __name__=='__main__': unittest.main()
