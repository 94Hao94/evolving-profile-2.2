import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.scenario_model import fingerprint_draft, request_session_state_draft
from lib.scenario_state_v3 import validate_state_draft


THREAD_ID = "01a0aad3-d9dd-7400-b1e7-a636388cab3b"
SCRIPT = Path(__file__).with_name("context-rerender-v3.py")
SPEC = importlib.util.spec_from_file_location("context_rerender_v3", SCRIPT)
RERENDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RERENDER)


def source(messages):
    rows = []
    for index, (role, text) in enumerate(messages, 1):
        rows.append({"evidence_id": f"id{index}", "model_ref": f"m{index}", "role": role,
                     "text": text, "turn_id": f"t{index}", "at": f"2026-09-27T0{index}:00:00Z"})
    return {"thread_id": THREAD_ID, "source": "codex_thread_history", "source_files": ["rollout.jsonl"],
            "source_revision": "revision-1", "status": "complete", "messages": rows}


def claim(text, message_id):
    return {"text": text, "message_ids": [message_id]}


class ContextRerenderV3Tests(unittest.TestCase):
    def test_rerender_preserves_new_user_quote_protocol(self):
        original=source([('user','不要公开密钥，只有本机使用'),('assistant','已答复')])
        state={'subject':claim('项目要求','id1'),'goal':claim('处理请求','id1'),'phase':'assistant_reported',
               'constraints':[claim('不要公开密钥，只有本机使用','id1')],'corrections':[],
               'assistant_reports':[claim('已答复','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test',user_claim_protocol='user_constraints_corrections_whole_source_clause.v1')
        rebuilt=RERENDER.rebuild_rerender_draft(original,draft)
        self.assertEqual(rebuilt['state_claim_protocol'],'user_constraints_corrections_whole_source_clause.v1')
        self.assertEqual(rebuilt['state']['constraints'][0]['text'],'不要公开密钥，只有本机使用')

    def test_rerender_cannot_drop_protocol_to_accept_negation_loss(self):
        original=source([('user','不要公开密钥'),('assistant','已答复')])
        state={'subject':claim('项目要求','id1'),'goal':claim('处理请求','id1'),'phase':'assistant_reported',
               'constraints':[claim('不要公开密钥','id1')],'corrections':[],
               'assistant_reports':[claim('已答复','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test',user_claim_protocol='user_constraints_corrections_whole_source_clause.v1')
        draft['state']['constraints'][0]['text']='公开密钥'
        with self.assertRaisesRegex(ValueError,'scenario_user_claim_not_verbatim'):
            RERENDER.rebuild_rerender_draft(original,draft)

    def test_rerender_keeps_legacy_paraphrase_mode_when_no_protocol_exists(self):
        original=source([('user','不要公开密钥'),('assistant','已答复')])
        state={'subject':claim('项目要求','id1'),'goal':claim('处理请求','id1'),'phase':'assistant_reported',
               'constraints':[claim('凭据应保密','id1')],'corrections':[],
               'assistant_reports':[claim('已答复','id2')],'unresolved':[]}
        draft=validate_state_draft(original,state,model='test')
        rebuilt=RERENDER.rebuild_rerender_draft(original,draft)
        self.assertNotIn('state_claim_protocol',rebuilt)

    def test_rerender_preserves_custom_limit_and_validates_rebuilt_draft(self):
        original = source([("user", "甲" * 20000), ("assistant", "乙" * 20000)])
        state = {"subject": claim("长会话项目", "id1"), "goal": claim("整理项目状态", "id1"),
                 "phase": "assistant_reported", "constraints": [], "corrections": [],
                 "assistant_reports": [claim("已整理项目状态", "id2")], "unresolved": []}

        def opener(request, timeout):
            result = {"source_revision": "revision-1", "state": state}
            return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(result, ensure_ascii=False)}}]}).encode())

        draft = request_session_state_draft(original, base_url="https://example.invalid", api_key="test",
            model="test-model", opener=opener, max_input_chars=50000)
        rebuilt = RERENDER.rebuild_rerender_draft(original, draft)
        self.assertEqual(rebuilt["source_chunk_char_limit"], 50000)
        self.assertEqual(rebuilt["source_revision"], original["source_revision"])

    def test_invalid_chunk_coverage_does_not_write_successful_rerender(self):
        original = source([("user", "天津大学墙体巡检方案"), ("assistant", "旧稿已完成"),
                           ("user", "改成背负式喷洒"), ("assistant", "新版已形成")])
        state = {"subject": claim("天津大学墙体巡检方案", "id1"),
                 "goal": claim("编制巡检方案", "id1"), "phase": "assistant_reported",
                 "constraints": [], "corrections": [claim("改成背负式喷洒", "id3")],
                 "assistant_reports": [claim("新版已形成", "id4")], "unresolved": []}
        draft = validate_state_draft(original, state, model="test-model")
        draft.update(source_chunk_char_limit=20, selection_coverage={
            "source_revision": "revision-1", "source_message_count": 4,
            "source_chunk_count": 3, "candidate_state_count": 3,
            "chunk_char_limit": 20, "semantic_completeness_proven": False,
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft_dir = root / "drafts"
            draft_dir.mkdir()
            draft_path = draft_dir / f"{THREAD_ID}.json"
            before = json.dumps(draft, ensure_ascii=False, indent=2) + "\n"
            draft_path.write_text(before, encoding="utf-8")
            marker = draft_dir / ".attempts" / f"{THREAD_ID}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"status": "succeeded", "source_revision": "revision-1",
                "draft_sha256": fingerprint_draft(draft)}), encoding="utf-8")
            args = ["context-rerender-v3.py", "--session-id", THREAD_ID,
                    "--session-root", str(root), "--draft-dir", str(draft_dir)]
            with patch("sys.argv", args), patch.object(RERENDER, "read_session_source", return_value=original):
                with self.assertRaisesRegex(ValueError, "scenario_state_invalid"):
                    RERENDER.main()
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["status"], "pending")
            self.assertEqual(draft_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
