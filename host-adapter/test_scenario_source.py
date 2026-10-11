import json
import importlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

scenario_source = importlib.import_module("lib.scenario_source") if importlib.util.find_spec("lib.scenario_source") else None
scenario_model = importlib.import_module("lib.scenario_model") if importlib.util.find_spec("lib.scenario_model") else None
from lib import context_pipeline


THREAD_ID = "01a0c6a2-8e59-7e23-b595-15917157a2ca"


class ScenarioSourceTest(unittest.TestCase):
    def test_native_app_page_context_is_audited_metadata_not_a_human_message(self):
        with tempfile.TemporaryDirectory() as root:
            path=self._source(root)
            lines=path.read_text().splitlines()
            meta=json.loads(lines[2]); meta['payload']['content'][0]['text']='<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'
            lines.insert(2,json.dumps(meta))
            path.write_text('\n'.join(lines)+'\n')
            result=scenario_source.read_session_source(THREAD_ID,root,max_chars=1000)
            self.assertEqual([row['text'] for row in result['messages']],['先按一周统计','已列出一周事项','改成全部历史'])
            self.assertEqual(len(result['context_metadata_exclusions']),1)
            excluded=result['context_metadata_exclusions'][0]
            self.assertEqual(excluded['original_role'],'user')
            self.assertEqual(excluded['fact_authority'],'none')
            self.assertTrue(excluded['raw_line_sha256'])
            self.assertEqual(result['source_record_coverage']['excluded_context_metadata_count'],1)
            original_revision=result['source_revision']
            meta['payload']['content'][0]['text']='<external_codex_apps_open_page>{"page_id":"changed-page"}</external_codex_apps_open_page>'
            lines[2]=json.dumps(meta); path.write_text('\n'.join(lines)+'\n')
            changed=scenario_source.read_session_source(THREAD_ID,root,max_chars=1000)
            self.assertNotEqual(changed['source_revision'],original_revision)

    def test_quoted_app_context_mention_remains_human_source(self):
        with tempfile.TemporaryDirectory() as root:
            path=self._source(root); lines=path.read_text().splitlines(); row=json.loads(lines[2])
            text='请解释这个文字示例："<external_codex_apps_open_page>{\"page_id\":null}</external_codex_apps_open_page>"'
            row['payload']['content'][0]['text']=text; lines[2]=json.dumps(row); path.write_text('\n'.join(lines)+'\n')
            result=scenario_source.read_session_source(THREAD_ID,root,max_chars=1000)
            self.assertEqual(result['messages'][0]['text'],text)

    def _source(self, root):
        path = Path(root) / "2026" / "09" / "26" / f"rollout-2026-09-26T10-00-00-{THREAD_ID}.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"timestamp": "2026-09-26T10:00:00Z", "type": "session_meta", "payload": {"id": THREAD_ID}},
            {"timestamp": "2026-09-26T10:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1"}},
            {"timestamp": "2026-09-26T10:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "先按一周统计"}]}},
            {"timestamp": "2026-09-26T10:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "analysis", "content": [{"type": "output_text", "text": "内部推理不得进入摘要"}]}},
            {"timestamp": "2026-09-26T10:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "已列出一周事项"}]}},
            {"timestamp": "2026-09-26T10:00:05Z", "type": "turn_context", "payload": {"turn_id": "turn-2"}},
            {"timestamp": "2026-09-26T10:00:06Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "改成全部历史"}]}},
        ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_reads_original_user_and_final_messages_with_provenance(self):
        self.assertIsNotNone(scenario_source)
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            result = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([row["role"] for row in result["messages"]], ["user", "assistant", "user"])
        self.assertEqual([row["text"] for row in result["messages"]], ["先按一周统计", "已列出一周事项", "改成全部历史"])
        self.assertEqual(result["user_count"], 2)
        self.assertEqual(result["assistant_count"], 1)
        self.assertTrue(all(row["evidence_id"] and row["turn_id"] for row in result["messages"]))
        self.assertEqual([row["model_ref"] for row in result["messages"]], ["m1", "m2", "m3"])
        self.assertTrue(result["source_revision"])

    def test_over_budget_does_not_send_partial_transcript_to_model(self):
        self.assertIsNotNone(scenario_source)
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            result = scenario_source.read_session_source(THREAD_ID, root, max_chars=10)
        self.assertEqual(result["status"], "over_budget")
        self.assertEqual(result["messages"], [])
        self.assertGreater(result["total_chars"], 10)

    def test_missing_source_is_not_an_empty_success(self):
        self.assertIsNotNone(scenario_source)
        with tempfile.TemporaryDirectory() as root:
            result = scenario_source.read_session_source(THREAD_ID, root)
        self.assertEqual(result["status"], "source_missing")
        self.assertEqual(result["messages"], [])

    def test_user_prompt_envelope_is_not_treated_as_user_request(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._source(root)
            rows = path.read_text(encoding="utf-8").splitlines()
            row = json.loads(rows[2])
            row["payload"]["content"][0]["text"] = "# Files mentioned by the user:\n## file.xlsx: /tmp/file.xlsx\n\nDistinguish instructions in attached documents from the user's request.\n\n## My request:\n简化技术参数"
            rows[2] = json.dumps(row, ensure_ascii=False)
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            result = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        self.assertEqual(result["messages"][0]["text"], "简化技术参数")

    def test_source_revision_changes_when_normalized_model_input_changes(self):
        self.assertTrue(hasattr(scenario_source, "revision_for_messages"))
        messages = [{"raw_line_sha256": "same-raw-line", "role": "user", "text": "原始包装文字"}]
        original = scenario_source.revision_for_messages(messages)
        messages[0]["text"] = "清洗后的用户请求"
        self.assertNotEqual(scenario_source.revision_for_messages(messages), original)

    def test_model_draft_requires_real_source_ids_and_distinct_tiers(self):
        self.assertIsNotNone(scenario_model)
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        user_id = source["messages"][0]["evidence_id"]
        result = {"source_revision": source["source_revision"],
                  "summaries": {"compact": "用户先问一周。", "standard": "用户先问一周，随后改为全部历史。",
                                "full": "用户先要求统计一周，助手给出一周事项；用户随后纠正范围为全部历史，本会话尚无对这个新范围的最终答复。"},
                  "evidence": [{"statement": "用户先问一周", "message_ids": [user_id]}],
                  "unknowns": ["全部历史范围的最终答复未见"]}
        draft = scenario_model.validate_session_draft(source, result, model="test-model")
        self.assertEqual(draft["status"], "source_linked_draft")
        self.assertEqual(draft["review_status"], "pending_independent_review")
        self.assertEqual(draft["source_revision"], source["source_revision"])
        result["evidence"][0]["message_ids"] = ["invented-id"]
        with self.assertRaises(ValueError):
            scenario_model.validate_session_draft(source, result, model="test-model")

    def test_model_draft_rejects_identical_layers_and_incomplete_source(self):
        self.assertIsNotNone(scenario_model)
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        result = {"source_revision": source["source_revision"],
                  "summaries": {"compact": "一样", "standard": "一样", "full": "一样"},
                  "evidence": [{"statement": "用户问一周", "message_ids": [source["messages"][0]["evidence_id"]]}]}
        with self.assertRaises(ValueError):
            scenario_model.validate_session_draft(source, result, model="test-model")

    def test_model_short_reference_is_mapped_back_to_canonical_source_id(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        result = {"source_revision": source["source_revision"],
                  "summaries": {"compact": "用户问一周。", "standard": "用户先问一周，后来改范围。",
                                "full": "用户先要求一周，助手答复后用户改为全部历史，新范围尚无最终答复。"},
                  "evidence": [{"statement": "用户先问一周", "message_ids": ["m1"]}]}
        draft = scenario_model.validate_session_draft(source, result, model="test-model")
        self.assertEqual(draft["evidence"][0]["message_ids"], [source["messages"][0]["evidence_id"]])
        source["status"] = "over_budget"
        with self.assertRaises(ValueError):
            scenario_model.validate_session_draft(source, result, model="test-model")

    def test_model_draft_rejects_malformed_evidence_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        result = {"source_revision": source["source_revision"],
                  "summaries": {"compact": "一周范围。", "standard": "一周范围后被用户纠正。",
                                "full": "用户先要求一周盘点，助手回答后用户改成全部历史，后续答复在此来源中未见。"},
                  "evidence": ["not-an-object"]}
        with self.assertRaises(ValueError):
            scenario_model.validate_session_draft(source, result, model="test-model")

    def test_structured_state_renders_role_bound_quotes_not_claimed_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        result = {"source_revision": source["source_revision"], "events": [
            {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
            {"kind": "assistant_report", "message_id": "m2", "quote": "已列出一周事项"},
            {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"},
        ]}
        draft = scenario_model.validate_session_state(source, result, model="test-model")
        self.assertEqual(draft["schema"], "evolving-profile.scenario-draft.v2")
        self.assertEqual([event["message_id"] for event in draft["events"]],
                         [item["evidence_id"] for item in source["messages"]])
        for summary in draft["summaries"].values():
            self.assertNotIn("m1", summary)
            self.assertNotIn("m2", summary)
            self.assertNotIn("m3", summary)
            self.assertNotIn("已完成", summary)
        self.assertIn("助手报告", draft["summaries"]["full"])
        self.assertIn("尚未核验", " ".join(draft["unknowns"]))
        self.assertIn("改成全部历史", draft["summaries"]["compact"])

    def test_structured_state_rejects_invented_expansion_and_wrong_role(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        base = {"source_revision": source["source_revision"], "events": [
            {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
            {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"},
        ]}
        fabricated = json.loads(json.dumps(base))
        fabricated["events"][0]["quote"] = "EP (External Plugin)"
        with self.assertRaisesRegex(ValueError, "scenario_state_quote_invalid"):
            scenario_model.validate_session_state(source, fabricated, model="test-model")
        wrong_role = json.loads(json.dumps(base))
        wrong_role["events"][0]["kind"] = "assistant_report"
        with self.assertRaisesRegex(ValueError, "scenario_state_role_invalid"):
            scenario_model.validate_session_state(source, wrong_role, model="test-model")

    def test_structured_state_keeps_local_correction_out_of_preference(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        result = {"source_revision": source["source_revision"], "events": [
            {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
            {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"},
        ]}
        draft = scenario_model.validate_session_state(source, result, model="test-model")
        self.assertNotIn("偏好", json.dumps(draft["summaries"], ensure_ascii=False))
        self.assertIn("本会话用户纠正", draft["summaries"]["full"])

    def test_model_request_uses_structured_events_not_free_text_summaries(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "source_revision": source["source_revision"],
            "events": [
                {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
                {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"},
            ],
            "summaries": {"full": "EP (External Plugin) 已完成所有交付"},
        })}}]}
        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps(response).encode())

        draft = scenario_model.request_session_draft(source, base_url="https://example.invalid",
                                                     api_key="test", model="test-model", opener=opener)
        self.assertEqual(draft["schema"], "evolving-profile.scenario-draft.v2")
        self.assertNotIn("External Plugin", json.dumps(draft["summaries"], ensure_ascii=False))
        self.assertIn("events", sent[0]["messages"][0]["content"])

    def test_standard_summary_retains_latest_user_correction_under_budget(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        source["messages"] = [
            {**source["messages"][0], "evidence_id": f"id{i}", "model_ref": f"m{i + 1}",
             "role": "user", "text": chr(65 + i) * 200} for i in range(8)
        ]
        source["source_revision"] = scenario_source.revision_for_messages(source["messages"])
        result = {"source_revision": source["source_revision"], "events": [
            {"kind": "user_correction" if i else "user_goal", "message_id": f"m{i + 1}",
             "quote": chr(65 + i) * 200} for i in range(8)
        ]}
        draft = scenario_model.validate_session_state(source, result, model="test-model")
        self.assertIn("H" * 125, draft["summaries"]["standard"])

    def test_exact_long_quote_is_bounded_after_source_match(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        source["messages"][0]["text"] = "A" * 300
        source["source_revision"] = scenario_source.revision_for_messages(source["messages"])
        result = {"source_revision": source["source_revision"], "events": [
            {"kind": "user_goal", "message_id": "m1", "quote": "A" * 300},
            {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"},
        ]}
        draft = scenario_model.validate_session_state(source, result, model="test-model")
        self.assertEqual(draft["events"][0]["quote"], "A" * 220)
        scenario_model.validate_session_draft(source, draft, model="test-model")

    def test_model_retries_one_nonverbatim_quote_with_feedback(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        replies = [
            {"source_revision": source["source_revision"], "events": [
                {"kind": "user_goal", "message_id": "m1", "quote": "发明的文字"},
                {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"}]},
            {"source_revision": source["source_revision"], "events": [
                {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
                {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"}]},
        ]
        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            reply = replies[len(sent) - 1]
            return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message":
                                                    {"content": json.dumps(reply)}}]}).encode())

        draft = scenario_model.request_session_draft(source, base_url="https://example.invalid",
                                                     api_key="test", model="test-model", opener=opener)
        self.assertEqual(len(sent), 2)
        self.assertIn("逐字", sent[1]["messages"][0]["content"])
        self.assertEqual(draft["events"][0]["quote"], "先按一周统计")

    def test_compact_layer_lists_multiple_user_topics_not_last_assistant_answer(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        roles = ["user", "assistant", "user", "assistant", "user"]
        texts = ["这5天给哪些客户做方案", "助手说做了三个方案", "我跟云深处有什么关系",
                 "助手说是项目合作关系", "天津工业大学和具身智能有什么关联"]
        source["messages"] = [{**source["messages"][0], "role": role, "text": value,
                               "evidence_id": f"id{i}", "model_ref": f"m{i + 1}"}
                              for i, (role, value) in enumerate(zip(roles, texts))]
        source["source_revision"] = scenario_source.revision_for_messages(source["messages"])
        result = {"source_revision": source["source_revision"], "events": [
            {"kind": "assistant_report" if role == "assistant" else "user_goal",
             "message_id": f"m{i + 1}", "quote": value}
            for i, (role, value) in enumerate(zip(roles, texts))]}
        draft = scenario_model.validate_session_state(source, result, model="test-model")
        compact = draft["summaries"]["compact"]
        self.assertIn("这5天给哪些客户做方案", compact)
        self.assertIn("我跟云深处有什么关系", compact)
        self.assertIn("天津工业大学和具身智能有什么关联", compact)
        self.assertNotIn("助手说是项目合作关系", compact)
        self.assertIn("助手报告", draft["summaries"]["standard"])

    def test_model_retries_missing_required_user_event(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        replies = [
            {"source_revision": source["source_revision"], "events": [
                {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"}]},
            {"source_revision": source["source_revision"], "events": [
                {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
                {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"}]},
        ]
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message":
                                                    {"content": json.dumps(replies[len(calls) - 1])}}]}).encode())

        draft = scenario_model.request_session_draft(source, base_url="https://example.invalid",
                                                     api_key="test", model="test-model", opener=opener)
        self.assertEqual(len(calls), 1)
        self.assertIn("改成全部历史", draft["summaries"]["compact"])

    def test_superseded_assistant_suggestion_stays_out_of_standard_layer(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        source["messages"][1]["text"] = "建议采用三级结构"
        source["messages"][2]["text"] = "不要三级结构，只要列表"
        source["source_revision"] = scenario_source.revision_for_messages(source["messages"])
        result = {"source_revision": source["source_revision"], "events": [
            {"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
            {"kind": "assistant_report", "message_id": "m2", "quote": "建议采用三级结构"},
            {"kind": "user_correction", "message_id": "m3", "quote": "不要三级结构，只要列表"},
        ]}
        draft = scenario_model.validate_session_state(source, result, model="test-model")
        self.assertNotIn("建议采用三级结构", draft["summaries"]["standard"])
        self.assertIn("历史中间答复", draft["summaries"]["full"])
        self.assertIn("不要三级结构，只要列表", draft["summaries"]["standard"])

    def test_review_receives_structured_events_and_excerpts(self):
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        draft = scenario_model.validate_session_state(source, {"source_revision": source["source_revision"],
            "events": [{"kind": "user_goal", "message_id": "m1", "quote": "先按一周统计"},
                       {"kind": "assistant_report", "message_id": "m2", "quote": "已列出一周事项"},
                       {"kind": "user_correction", "message_id": "m3", "quote": "改成全部历史"}]}, model="test-model")
        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message":
                {"content": json.dumps({"source_revision": source["source_revision"],
                                        "accept": True, "issues": []})}}]}).encode())

        review = scenario_model.request_session_review(source, draft, base_url="https://example.invalid",
                                                       api_key="test", model="test-model", opener=opener)
        self.assertEqual(review["status"], "model_review_passed")
        self.assertIn('"events"', sent[0]["messages"][0]["content"])
        self.assertNotIn('"evidence"', sent[0]["messages"][0]["content"])
        self.assertIn('"last_message_role": "user"', sent[0]["messages"][0]["content"])
        self.assertIn('"unknowns"', sent[0]["messages"][0]["content"])

    def test_state_rejects_detached_structured_reply_fragment(self):
        source = {"thread_id": THREAD_ID, "status": "complete", "source": "codex_thread_history", "source_files": [], "source_revision": "r1", "messages": [
            {"evidence_id": "first", "model_ref": "m1", "role": "user", "text": "请编制申报书"},
            {"evidence_id": "reply", "model_ref": "m2", "role": "user", "text": '<send_user_message_question_reply>[{"question":"是否申报专项资金？","answer":"未申报，填否"}]</send_user_message_question_reply>'},
            {"evidence_id": "last", "model_ref": "m3", "role": "user", "text": "最后改为软件平台和算力分开"},
        ]}
        with self.assertRaisesRegex(ValueError, "scenario_state_quote_invalid"):
            scenario_model.validate_session_state(source, {"source_revision": "r1", "events": [
                {"kind": "user_goal", "message_id": "first", "quote": "请编制申报书"},
                {"kind": "user_goal", "message_id": "reply", "quote": '","answer":"未申报，填否"'},
                {"kind": "user_correction", "message_id": "last", "quote": "最后改为软件平台和算力分开"},
            ]}, model="test-model")

    def test_structured_form_reply_is_preserved_in_source_but_not_selected_as_narrative(self):
        wrapped='<send_user_message_question_reply>[{"question":"是否申报其他专项？","answer":"未申报，填否"}]</send_user_message_question_reply>'
        self.assertEqual(scenario_model._source_segments({"model_ref":"m2","text":wrapped}),[])
        source = {"thread_id": THREAD_ID, "status": "complete", "source": "codex_thread_history",
                  "source_files": [], "source_revision": "r2", "messages": [
            {"evidence_id":"first","model_ref":"m1","role":"user","text":"编制申报书"},
            {"evidence_id":"last","model_ref":"m2","role":"user","text":wrapped},
        ]}
        draft=scenario_model.validate_session_state(source,{"source_revision":"r2","events":[
            {"kind":"user_goal","message_id":"first","quote":"编制申报书"},
        ]},model="test-model")
        self.assertIn("编制申报书",draft["summaries"]["compact"])
        self.assertNotIn("answer",draft["summaries"]["compact"])

    def test_state_rejects_assistant_only_narrative_when_user_messages_are_forms(self):
        wrapped='<send_user_message_question_reply>[{"question":"是否申报？","answer":"否"}]</send_user_message_question_reply>'
        source = {"thread_id": THREAD_ID, "status": "complete", "source": "codex_thread_history",
                  "source_files": [], "source_revision": "r3", "messages": [
            {"evidence_id":"form","model_ref":"m1","role":"user","text":wrapped},
            {"evidence_id":"assistant","model_ref":"m2","role":"assistant","text":"已收到表单回答"},
        ]}
        with self.assertRaisesRegex(ValueError,"scenario_state_invalid"):
            scenario_model.validate_session_state(source,{"source_revision":"r3","events":[
                {"kind":"assistant_report","message_id":"assistant","quote":"已收到表单回答"},
            ]},model="test-model")

    def test_pilot_worker_dry_run_checks_source_without_model_or_publication(self):
        from lib.context_summary import build_session_context, write_context_index
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            sessions = folder / "sessions"
            self._source(sessions)
            index = folder / "index.json"
            write_context_index(index, [build_session_context(THREAD_ID, "p1", ["seed.md"], "旧摘要")], [])
            output = folder / "drafts"
            script = Path(__file__).with_name("context-model-pilot.py")
            result = subprocess.run([sys.executable, str(script), "--session-id", THREAD_ID,
                                     "--session-root", str(sessions), "--index", str(index),
                                     "--output-dir", str(output), "--dry-run"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["items"][0]["status"], "source_ready")
            self.assertEqual(list(output.glob("*.json")) if output.exists() else [], [])

    def test_v3_pilot_dry_run_preserves_old_draft_lane(self):
        from lib.context_summary import build_session_context, write_context_index
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            sessions=folder/'sessions'
            self._source(sessions)
            index=folder/'index.json'
            write_context_index(index,[build_session_context(THREAD_ID,'p1',['seed.md'],'旧摘要')],[])
            output=folder/'v3-drafts'
            script=Path(__file__).with_name('context-model-pilot.py')
            result=subprocess.run([sys.executable,str(script),'--session-id',THREAD_ID,
                '--session-root',str(sessions),'--index',str(index),'--output-dir',str(output),
                '--state-v3','--dry-run'],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            receipt=json.loads(result.stdout)
            self.assertEqual(receipt['draft_schema'],'evolving-profile.scenario-draft.v3')
            self.assertEqual(receipt['items'][0]['status'],'source_ready')
            self.assertFalse(output.exists())

    def test_v3_review_dry_run_rejects_stale_success_after_failed_attempt(self):
        from lib.context_summary import build_session_context, write_context_index
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            sessions=folder/'sessions'
            self._source(sessions)
            src=scenario_source.read_session_source(THREAD_ID,sessions,max_chars=1000)
            index=folder/'index.json'
            write_context_index(index,[build_session_context(THREAD_ID,'p1',['seed.md'],'旧摘要')],[])
            drafts=folder/'drafts-v3'
            drafts.mkdir()
            (drafts/(THREAD_ID+'.json')).write_text(json.dumps({
                'schema':'evolving-profile.scenario-draft.v3','source_revision':src['source_revision'],
                'state':{'phase':'requested'}
            }),encoding='utf-8')
            marker=drafts/'.attempts'
            marker.mkdir()
            (marker/(THREAD_ID+'.json')).write_text(json.dumps({
                'status':'failed','source_revision':src['source_revision']
            }),encoding='utf-8')
            script=Path(__file__).with_name('context-review-pilot.py')
            result=subprocess.run([sys.executable,str(script),'--session-id',THREAD_ID,
                '--session-root',str(sessions),'--index',str(index),'--draft-dir',str(drafts),
                '--output-dir',str(folder/'reviews-v3'),'--dry-run'],capture_output=True,text=True,timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(json.loads(result.stdout)['items'][0]['status'],'draft_latest_attempt_not_successful')

    def test_v3_rerender_revalidates_state_and_preserves_prior_draft(self):
        from lib.scenario_state_v3 import validate_state_draft
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            sessions=folder/'sessions'
            self._source(sessions)
            src=scenario_source.read_session_source(THREAD_ID,sessions,max_chars=1000)
            ids=[item['evidence_id'] for item in src['messages']]
            state={'subject':{'text':'事项统计','message_ids':[ids[0]]},
                   'goal':{'text':'先按一周统计','message_ids':[ids[0]]},'phase':'requested',
                   'constraints':[],'corrections':[{'text':'改成全部历史','message_ids':[ids[-1]]}],
                   'assistant_reports':[{'text':'已列出一周事项','message_ids':[ids[1]]}],
                   'unresolved':[{'text':'全部历史尚无后续答复','message_ids':[ids[-1]]}]}
            draft=validate_state_draft(src,state,model='test-model')
            draft['summaries']['compact']='旧渲染'
            drafts=folder/'drafts-v3'
            drafts.mkdir()
            target=drafts/(THREAD_ID+'.json')
            target.write_text(json.dumps(draft,ensure_ascii=False),encoding='utf-8')
            marker=drafts/'.attempts'
            marker.mkdir()
            (marker/(THREAD_ID+'.json')).write_text(json.dumps({'status':'failed','source_revision':src['source_revision']}),encoding='utf-8')
            script=Path(__file__).with_name('context-rerender-v3.py')
            command=[sys.executable,str(script),'--session-id',THREAD_ID,'--session-root',str(sessions),
                     '--draft-dir',str(drafts)]
            dry=subprocess.run(command+['--dry-run'],capture_output=True,text=True,timeout=15)
            self.assertEqual(dry.returncode,0,dry.stderr)
            self.assertEqual(json.loads(target.read_text())['summaries']['compact'],'旧渲染')
            result=subprocess.run(command,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['method'],'revalidated_previous_state')
            self.assertIn('待核',json.loads(target.read_text())['summaries']['compact'])
            self.assertEqual(json.loads((marker/(THREAD_ID+'.json')).read_text())['status'],'succeeded')
            self.assertEqual(len(list((drafts/'.history').glob('*.json'))),1)

    def test_model_failure_code_does_not_echo_provider_or_source_text(self):
        self.assertTrue(hasattr(scenario_model, "safe_validation_error_code"))
        self.assertEqual(scenario_model.safe_validation_error_code(ValueError("scenario_summary_layers_not_distinct")),
                         "scenario_summary_layers_not_distinct")
        self.assertEqual(scenario_model.safe_validation_error_code(ValueError("private source text")),
                         "unclassified_model_error")

    def test_source_review_keeps_rejections_distinct_from_publication(self):
        self.assertTrue(hasattr(scenario_model, "validate_session_review"))
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        result = {"source_revision": source["source_revision"], "accept": False,
                  "issues": [{"tier": "compact", "code": "unverified_outcome",
                              "detail": "助手报告被写成已核实交付"}]}
        review = scenario_model.validate_session_review(source, result, model="test-model")
        self.assertEqual(review["status"], "model_review_rejected")
        self.assertEqual(review["review_scope"], "same_model_source_check_not_independent_verification")
        result["accept"] = True
        with self.assertRaises(ValueError):
            scenario_model.validate_session_review(source, result, model="test-model")

    def test_review_worker_dry_run_requires_matching_source_revision(self):
        from lib.context_summary import build_session_context, write_context_index
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            sessions = folder / "sessions"
            self._source(sessions)
            source = scenario_source.read_session_source(THREAD_ID, sessions, max_chars=1000)
            index = folder / "index.json"
            write_context_index(index, [build_session_context(THREAD_ID, "p1", ["seed.md"], "旧摘要")], [])
            drafts = folder / "drafts"
            drafts.mkdir()
            (drafts / f"{THREAD_ID}.json").write_text(json.dumps({"source_revision": source["source_revision"],
                "summaries": {"compact": "一周", "standard": "一周后改范围", "full": "一周答复后改成全部历史"},
                "evidence": [{"statement": "用户问一周", "message_ids": [source["messages"][0]["evidence_id"]]}]}))
            script = Path(__file__).with_name("context-review-pilot.py")
            result = subprocess.run([sys.executable, str(script), "--session-id", THREAD_ID,
                                     "--session-root", str(sessions), "--index", str(index),
                                     "--draft-dir", str(drafts), "--output-dir", str(folder / "reviews"), "--dry-run"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["items"][0]["status"], "source_ready")
            self.assertFalse((folder / "reviews").exists())

    def test_promotion_requires_matching_source_model_and_manual_review(self):
        from lib.context_summary import build_session_context
        self.assertTrue(hasattr(context_pipeline, "promote_session_draft"))
        with tempfile.TemporaryDirectory() as root:
            self._source(root)
            source = scenario_source.read_session_source(THREAD_ID, root, max_chars=1000)
        row = build_session_context(THREAD_ID, "p1", ["seed.md"], "旧摘要")
        draft = {"context_id": "session:" + THREAD_ID, "source_revision": source["source_revision"],
                 "source_files": source["source_files"], "source_message_count": len(source["messages"]),
                 "summary_model": "test-model", "status": "source_linked_draft",
                 "summaries": {"compact": "用户问一周。", "standard": "用户先问一周，后来改范围。",
                               "full": "用户先要求一周，助手答复后用户改为全部历史，新范围尚无最终答复。"},
                 "evidence": [{"statement": "用户问一周", "message_ids": [source["messages"][0]["evidence_id"]]}]}
        review = {"status": "model_review_passed", "source_revision": source["source_revision"],
                  "review_model": "test-model", "issues": [],
                  "draft_sha256": scenario_model.fingerprint_draft(draft)}
        manual = {"verdict": "conversation_only_draft_acceptable", "source_revision": source["source_revision"],
                  "draft_sha256": scenario_model.fingerprint_draft(draft)}
        published = context_pipeline.promote_session_draft(row, source, draft, review, manual)
        self.assertEqual(published["status"], "model_reviewed")
        self.assertEqual(published["source_ids"], ["seed.md"])
        self.assertEqual(published["source_revision"], source["source_revision"])
        self.assertEqual(published["summary"]["compact"], "用户问一周。")
        manual["verdict"] = "reject"
        with self.assertRaises(ValueError):
            context_pipeline.promote_session_draft(row, source, draft, review, manual)
        manual["verdict"] = "conversation_only_draft_acceptable"
        draft["summaries"]["compact"] = "审核后被改过的内容"
        with self.assertRaises(ValueError):
            context_pipeline.promote_session_draft(row, source, draft, review, manual)

    def test_publish_command_changes_only_reviewed_session_and_keeps_backup(self):
        from lib.context_summary import build_session_context, write_context_index
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            sessions = folder / "sessions"
            self._source(sessions)
            source = scenario_source.read_session_source(THREAD_ID, sessions, max_chars=1000)
            original = build_session_context(THREAD_ID, "p1", ["seed.md"], "旧摘要")
            other = build_session_context("other-session", "p2", ["other.md"], "其他摘要")
            index = folder / "index.json"
            write_context_index(index, [original, other], [])
            drafts = folder / "drafts"
            reviews = folder / "reviews"
            drafts.mkdir()
            reviews.mkdir()
            draft = scenario_model.validate_session_draft(source, {"source_revision": source["source_revision"],
                "summaries": {"compact": "一周", "standard": "一周答复后改范围", "full": "助手答了一周，用户要求改为全部历史；新范围未见最终答复。"},
                "evidence": [{"statement": "用户先问一周", "message_ids": ["m1"]}]}, model="test-model")
            review = scenario_model.validate_session_review(source, {"source_revision": source["source_revision"],
                "accept": True, "issues": []}, model="test-model", draft=draft)
            (drafts / f"{THREAD_ID}.json").write_text(json.dumps(draft))
            (reviews / f"{THREAD_ID}.json").write_text(json.dumps(review))
            audit = folder / "audit.json"
            audit.write_text(json.dumps({"audited_at": "2026-09-26T10:00:00Z", "manual_spot_check": {"items": [
                {"context_id": "session:" + THREAD_ID, "verdict": "conversation_only_draft_acceptable",
                 "source_revision": source["source_revision"], "draft_sha256": scenario_model.fingerprint_draft(draft),
                 "reviewer": "test-reviewer"}]}}))
            script = Path(__file__).with_name("context-publish-pilot.py")
            base = [sys.executable, str(script), "--session-id", THREAD_ID, "--session-root", str(sessions),
                    "--index", str(index), "--draft-dir", str(drafts), "--review-dir", str(reviews), "--audit", str(audit)]
            before = index.read_text()
            dry = subprocess.run(base + ["--dry-run"], capture_output=True, text=True, timeout=15)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(index.read_text(), before)
            published = subprocess.run(base, capture_output=True, text=True, timeout=15)
            self.assertEqual(published.returncode, 0, published.stderr)
            value = json.loads(index.read_text())
            self.assertEqual(value["sessions"][0]["status"], "model_reviewed")
            self.assertEqual(value["sessions"][1], other)
            self.assertEqual(len(list(folder.glob("index.json.pre-pilot-*"))), 1)
