import os
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ham-os"))

from ham.adapter import event_from_hook
from host_receipt_projection import TOOLS, resolve_tool_response, response_records, response_navigation, read_turn_contributions


class HostReceiptProjectionTest(unittest.TestCase):
    def test_host_capture_navigation_does_not_become_missing_body_or_answer_adoption(self):
        navigation = {'memory_id':'nav','document_id':'doc','chunk_id':'chunk','subject_relation':'unverified'}
        response = {'content':[{'type':'text','text':json.dumps({'mode':'official_discovery_evidence_only','memories':[],
            'source_navigation':[navigation],'source_navigation_reference_count':191})}]}
        with tempfile.TemporaryDirectory() as root:
            checks, capture = Path(root)/'checks.sqlite3', Path(root)/'capture.sqlite3'
            with sqlite3.connect(checks) as db:
                db.execute('CREATE TABLE observations (id INTEGER, session TEXT, turn TEXT, kind TEXT, payload TEXT)')
                db.execute('INSERT INTO observations VALUES (1,?,?,?,?)', ('s','t','tool',json.dumps({'tool':'mcp__evolving_profile_controller__user_recall','call_id':'call','response_recognized':True,'record_ids':[]})))
            with sqlite3.connect(capture) as db:
                db.execute('CREATE TABLE captures (capture_order INTEGER, envelope_json TEXT)')
                event = {'event_id':'event','provenance':{'hook':'PostToolUse'},'source_payload':{'session_id':'s','turn_id':'t',
                    'tool_name':'mcp__evolving_profile_controller__user_recall','tool_use_id':'call','tool_response':response}}
                db.execute('INSERT INTO captures VALUES (1,?)',(json.dumps(event),))
            entry = read_turn_contributions([{'session_id':'s','turn_id':'t'}], checks, capture)['turns'][('s','t')]
        self.assertEqual(entry['mcp']['record_count'], 0)
        self.assertEqual(entry['mcp']['record_ids'], [])
        self.assertEqual(entry['mcp']['missing_content_ids'], [])
        self.assertEqual(entry['mcp']['source_navigation_returned_count'], 1)
        self.assertEqual(entry['mcp']['source_navigation_returned_ids'], ['nav'])
        self.assertEqual(entry['answer']['cited_mcp_ids'], [])
        self.assertEqual(entry['answer']['causal_benefit'], 'not_measured')

    def test_source_navigation_is_a_bodyless_separate_receipt(self):
        navigation = {'memory_id':'nav','document_id':'doc','chunk_id':'chunk','source_revision':'revision',
                      'subject_relation':'unverified','claim_verification':'not_performed','authority':'unverified_source_claim',
                      'next_action':{'tool':'read_source','arguments':{'memory_id':'nav','scope':'chunk'}}}
        response = {'content':[{'type':'text','text':json.dumps({'mode':'official_discovery_evidence_only',
            'memories':[],'source_navigation':[dict(navigation,text='unrelated body'),dict(navigation,memory_id='denied',permission_status='denied')]})}]}
        self.assertEqual(response_records(response), [])
        self.assertEqual(response_navigation(response), [navigation])
        self.assertEqual(response_navigation(dict(response,isError=True)), [])

    def test_recognizes_current_and_legacy_controller_tool_names(self):
        self.assertIn("mcp__hindsight_controller__recall", TOOLS)
        self.assertIn("mcp__hindsight_controller__research", TOOLS)
        self.assertIn("mcp__evolving_profile_controller__recall", TOOLS)
        self.assertIn("mcp__evolving_profile_controller__get_preference", TOOLS)

    def test_resolves_large_archived_tool_response(self):
        """A compact capture envelope must not make the audit projection silently lose an observed MCP response."""
        response = {"content": [{"type": "text", "text": "x" * 40_000}]}
        with tempfile.TemporaryDirectory() as root:
            previous = os.environ.get("HAM_CAPTURE_STATE_DIR")
            os.environ["HAM_CAPTURE_STATE_DIR"] = root
            try:
                envelope = event_from_hook(
                    "PostToolUse",
                    {"session_id": "session", "cwd": "/project", "turn_id": "turn", "tool_response": response},
                    "tool_result",
                    "tool",
                )
            finally:
                if previous is None:
                    os.environ.pop("HAM_CAPTURE_STATE_DIR", None)
                else:
                    os.environ["HAM_CAPTURE_STATE_DIR"] = previous

            resolved = resolve_tool_response(envelope["source_payload"], Path(root) / "capture.sqlite3")

        self.assertEqual(resolved, response)


if __name__ == "__main__":
    unittest.main()
