import io
import json
import tempfile
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import scenario_model
from lib.scenario_model import (fingerprint_episode_bundle, request_episode_bundle,
                                request_episode_bundle_review, safe_validation_error_code,
                                validate_latest_episode_attempt)
from lib.scenario_source import revision_for_messages


THREAD_ID = "01a0aad3-d9dd-7400-b1e7-a636388cab3b"


def source(turns):
    messages = []
    sequence = 0
    for turn, user_text, assistant_text in turns:
        for role, text in (("user", user_text), ("assistant", assistant_text)):
            sequence += 1
            messages.append({"evidence_id": f"id{sequence}", "model_ref": f"m{sequence}",
                             "role": role, "text": text, "turn_id": turn,
                             "at": f"2026-09-27T00:{sequence:02d}:00Z",
                             "source_path": "rollout.jsonl", "byte_offset": sequence * 100,
                             "raw_line_sha256": f"hash-{sequence}"})
    return {"thread_id": THREAD_ID, "source": "codex_thread_history",
            "source_files": ["rollout.jsonl"], "status": "complete",
            "messages": messages, "source_revision": revision_for_messages(messages)}


def response(content):
    body = {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(content, ensure_ascii=False)}}]}
    return io.BytesIO(json.dumps(body, ensure_ascii=False).encode("utf-8"))


class EpisodeModelTests(unittest.TestCase):
    def test_same_native_turn_local_user_rows_join_before_model_boundary_classification(self):
        original = source([('t1','Native prompt context','unused'),('t1','Actual request','Assistant reports reply')])
        original['messages'] = [row for row in original['messages'] if row['evidence_id'] != 'id2']
        original['source_revision'] = revision_for_messages(original['messages'])
        opener,sent = self.fake_opener({'id3':'new_episode'})
        bundle = request_episode_bundle(original,base_url='https://example.invalid',api_key='test',model='test',opener=opener)
        self.assertEqual([row['message_ids'] for row in bundle['episodes']],[['id1','id3','id4']])
        self.assertEqual(bundle['boundary_decisions'],[{'message_id':'id3','decision':'same_episode','method':'same_turn_join'}])
        self.assertFalse(any(row.get('request_type') == 'episode_boundary_classification' for row in sent))
        from lib.scenario_episodes import partition_source
        with self.assertRaisesRegex(ValueError,'scenario_episode_turn_split'):
            partition_source(original,['id3'])

    def test_source_message_id_validation_error_stays_specific_in_cli_receipt(self):
        self.assertEqual(safe_validation_error_code(ValueError("scenario_source_message_ids_invalid")),
                         "scenario_source_message_ids_invalid")

    def fake_opener(self, decisions):
        sent = []

        def opener(request, timeout):
            payload = json.loads(request.data)
            prompt = payload["messages"][0]["content"]
            envelope = json.loads(prompt.rsplit("输入：", 1)[1])
            sent.append(envelope)
            if envelope.get("request_type") in {
                    "episode_boundary_classification", "episode_chunk_boundary_reconciliation"}:
                values = envelope.get("target_message_ids") or [item["message_id"] for item in envelope["boundaries"]]
                result = {"source_revision": envelope["source_revision"],
                          "decisions": [{"message_id": message_id,
                                         "decision": decisions.get(message_id, "same_episode")}
                                        for message_id in values]}
            else:
                users = [row for row in envelope["messages"] if row["role"] == "user"]
                assistants = [row for row in envelope["messages"] if row["role"] == "assistant"]
                last_user = users[-1]
                result = {"source_revision": envelope["source_revision"],
                          "state": {"subject": {"text": users[0]["text"][:40],
                                                 "message_ids": [users[0]["message_id"]]},
                                    "goal": {"text": "完成该段任务", "message_ids": [users[0]["message_id"]]},
                                    "phase": "assistant_reported" if assistants else "requested",
                                    "constraints": [],
                                    "corrections": [],
                                    "assistant_reports": ([{"text": "助手报告该段已答复",
                                                             "message_ids": [assistants[-1]["message_id"]]}]
                                                          if assistants else []),
                                    "unresolved": ([] if assistants else [{"text": "最后一条用户请求尚无答复",
                                                                            "message_ids": [last_user["message_id"]]}])}}
            return response(result)

        return opener, sent

    def test_clear_topic_change_splits_but_followup_stays_in_the_existing_episode(self):
        original = source([("t1", "请编制项目甲方案", "项目甲方案已完成"),
                           ("t2", "继续把项目甲的风险写细", "项目甲风险已补充"),
                           ("t3", "另一个任务：比较项目乙预算", "项目乙预算已比较")])
        opener, sent = self.fake_opener({"id3": "same_episode", "id5": "new_episode"})

        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="test-model", opener=opener)

        self.assertEqual(bundle["status"], "source_linked_episode_draft")
        self.assertEqual([row["message_ids"] for row in bundle["episodes"]],
                         [["id1", "id2", "id3", "id4"], ["id5", "id6"]])
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0]["target_message_ids"], ["id3", "id5"])
        self.assertEqual([row["draft"]["schema"] for row in bundle["episodes"]],
                         ["evolving-profile.scenario-draft.v3"] * 2)

    def test_long_session_uses_size_boundary_even_when_model_keeps_all_topics_together(self):
        original = source([(f"t{i}", f"同一项目的第{i}轮补充要求", f"第{i}轮已记录")
                           for i in range(1, 7)])
        opener, sent = self.fake_opener({})
        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="test-model", opener=opener)
        self.assertGreaterEqual(len(bundle["episodes"]), 2)
        self.assertEqual(bundle["episodes"][1]["start_message_id"], "id9")
        boundary = next(row for row in bundle["boundary_decisions"] if row["message_id"] == "id9")
        self.assertEqual(boundary["decision"], "new_episode")
        self.assertEqual(boundary["method"], "deterministic_size_boundary")

    def test_long_session_uses_source_linked_fallback_when_episode_state_model_is_invalid(self):
        original = source([(f"t{i}", f"同一项目的第{i}轮补充要求", f"第{i}轮已记录")
                           for i in range(1, 7)])
        calls = []
        def invalid_state_opener(request, timeout):
            payload = json.loads(request.data)
            calls.append(payload["messages"][0]["content"])
            if '"state":' in payload["messages"][0]["content"]:
                raw=payload["messages"][0]["content"].rsplit("输入：", 1)[1].lstrip()
                envelope,_=json.JSONDecoder().raw_decode(raw)
                revision=envelope["source_revision"]
                return response({"source_revision": revision,
                                 "state": {"constraints": [{"wrong": "shape"}]}})
            return self.fake_opener({})[0](request, timeout)
        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="test-model", opener=invalid_state_opener)
        self.assertGreaterEqual(len(bundle["episodes"]), 2)
        self.assertTrue(all(row["draft"].get("summary_model") == "source-linked-deterministic-fallback"
                            or row["draft"].get("summary_model") == "test-model"
                            for row in bundle["episodes"]))

    def test_uncertain_boundary_blocks_episode_summaries(self):
        original = source([("t1", "项目甲方案", "已完成"), ("t2", "这件事再细化", "已细化")])
        opener, sent = self.fake_opener({"id3": "uncertain"})

        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="test-model", opener=opener)

        self.assertEqual(bundle["status"], "episode_boundary_unresolved")
        self.assertEqual(bundle["unresolved_boundary_ids"], ["id3"])
        self.assertEqual(bundle["episodes"], [])
        self.assertEqual(len(sent), 1)

    def test_chunk_boundary_receives_a_separate_decision(self):
        original = source([("t1", "项目甲问题", "项目甲答复"),
                           ("t2", "项目乙问题", "项目乙答复")])
        opener, sent = self.fake_opener({"id3": "new_episode"})

        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="test-model", opener=opener, max_input_chars=10)

        self.assertEqual(len(bundle["episodes"]), 2)
        self.assertEqual(bundle["episodes"][1]["start_message_id"], "id3")
        self.assertEqual(sent[0]["request_type"], "episode_chunk_boundary_reconciliation")
        self.assertEqual(sent[0]["boundaries"][0]["message_id"], "id3")

    def test_episode_review_reuses_adaptive_draft_chunk_limit(self):
        original = source([("t1", "甲" * 4000, "甲答" * 2000),
                           ("t2", "乙" * 4000, "乙答" * 2000)])
        opener, _ = self.fake_opener({"id3": "same_episode"})
        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="draft-model", opener=opener, max_input_chars=9000)
        observed_limits = []

        def reviewed(source_value, draft, *, base_url, api_key, model, opener, timeout, max_input_chars):
            observed_limits.append(max_input_chars)
            count = len(scenario_model.source_chunks(source_value, max_chars=max_input_chars))
            return {"status": "model_review_passed", "source_revision": source_value["source_revision"],
                    "review_model": model, "issues": [],
                    "review_coverage": {"source_chunk_count": count, "reviewed_chunk_count": count,
                        "source_chunk_char_limit": max_input_chars, "all_chunks_accepted": True,
                        "cross_chunk_claim_count": 0, "cross_chunk_claim_reviewed_count": 0,
                        "unreviewed_cross_chunk_claim_count": 0}}

        with patch("lib.scenario_model.request_session_review", side_effect=reviewed):
            review = scenario_model.request_episode_bundle_review(original, bundle,
                base_url="https://example.invalid", api_key="test", model="review-model",
                max_input_chars=30000)

        self.assertEqual(bundle["episodes"][0]["draft"]["source_chunk_char_limit"], 9000)
        self.assertEqual(observed_limits, [9000])
        self.assertEqual(review["status"], "model_review_passed")

    def test_model_cannot_invent_a_boundary_id(self):
        original = source([("t1", "项目甲", "答复甲"), ("t2", "项目乙", "答复乙")])
        opener, _ = self.fake_opener({"id3": "new_episode"})

        def invalid_opener(request, timeout):
            payload = json.loads(request.data)
            envelope = json.loads(payload["messages"][0]["content"].rsplit("输入：", 1)[1])
            if envelope.get("request_type") == "episode_boundary_classification":
                return response({"source_revision": envelope["source_revision"],
                                 "decisions": [{"message_id": "unknown", "decision": "new_episode"}]})
            return opener(request, timeout)

        with self.assertRaisesRegex(ValueError, "scenario_episode_decision_invalid"):
            request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                   model="test-model", opener=invalid_opener)

    def test_episode_bundle_fingerprint_covers_every_episode_summary(self):
        bundle = {"schema": "evolving-profile.scenario-episode-bundle.v1",
                  "parent_source_revision": "r1", "episodes": [
                      {"episode_id": "episode:s:id1", "draft": {"summaries": {"compact": "甲"}}},
                      {"episode_id": "episode:s:id2", "draft": {"summaries": {"compact": "乙"}}}]}
        original = fingerprint_episode_bundle(bundle)
        bundle["episodes"][1]["draft"]["summaries"]["compact"] = "被改过的乙"

        self.assertNotEqual(fingerprint_episode_bundle(bundle), original)

    def test_latest_episode_receipt_rejects_changed_bundle_and_stale_revision(self):
        bundle = {"schema": "evolving-profile.scenario-episode-bundle.v1",
                  "parent_source_revision": "r1", "episodes": [{"episode_id": "episode:s:id1"}]}
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / ".attempts" / "episode-session-1.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"status": "succeeded", "source_revision": "r1",
                                          "draft_sha256": fingerprint_episode_bundle(bundle)}),
                              encoding="utf-8")
            validate_latest_episode_attempt(root, "session-1", bundle, "r1")
            changed = {**bundle, "episodes": [{"episode_id": "episode:s:id2"}]}
            with self.assertRaisesRegex(ValueError, "scenario_episode_latest_attempt_not_successful"):
                validate_latest_episode_attempt(root, "session-1", changed, "r1")
            with self.assertRaisesRegex(ValueError, "scenario_episode_latest_attempt_not_successful"):
                validate_latest_episode_attempt(root, "session-1", bundle, "new-revision")

    def test_review_receipt_covers_every_episode_and_binds_bundle_fingerprint(self):
        original = source([("t1", "项目甲任务", "甲任务答复"), ("t2", "项目乙任务", "乙任务答复")])
        draft_opener, _ = self.fake_opener({"id3": "new_episode"})
        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="draft-model", opener=draft_opener)
        sent = []

        def review_opener(request, timeout):
            payload = json.loads(request.data)
            envelope = json.loads(payload["messages"][0]["content"].rsplit("输入：", 1)[1])
            sent.append(envelope["source_revision"])
            return response({"source_revision": envelope["source_revision"],
                             "accept": True, "issues": []})

        review = request_episode_bundle_review(original, bundle, base_url="https://example.invalid",
                                               api_key="test", model="review-model", opener=review_opener)

        self.assertEqual(review["status"], "model_review_passed")
        self.assertEqual(len(review["episode_reviews"]), 2)
        self.assertEqual(review["reviewed_episode_ids"], [row["episode_id"] for row in bundle["episodes"]])
        self.assertEqual(len(sent), 2)
        self.assertEqual(review["bundle_sha256"], fingerprint_episode_bundle(bundle))

    def test_one_episode_review_rejection_rejects_the_aggregate(self):
        original = source([("t1", "项目甲任务", "甲任务答复"), ("t2", "项目乙任务", "乙任务答复")])
        draft_opener, _ = self.fake_opener({"id3": "new_episode"})
        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="draft-model", opener=draft_opener)
        calls = 0

        def review_opener(request, timeout):
            nonlocal calls
            calls += 1
            payload = json.loads(request.data)
            envelope = json.loads(payload["messages"][0]["content"].rsplit("输入：", 1)[1])
            issues = [] if calls == 1 else [{"tier": "evidence", "code": "missing_episode_scope",
                                              "detail": "该段没有保留一个用户约束"}]
            return response({"source_revision": envelope["source_revision"],
                             "accept": not issues, "issues": issues})

        review = request_episode_bundle_review(original, bundle, base_url="https://example.invalid",
                                               api_key="test", model="review-model", opener=review_opener)

        self.assertEqual(review["status"], "model_review_rejected")
        self.assertEqual([row["status"] for row in review["episode_reviews"]],
                         ["model_review_passed", "model_review_rejected"])

    def test_chunk_review_with_unreviewed_cross_chunk_claim_is_not_aggregate_pass(self):
        original = source([("t1", "项目甲任务", "甲任务答复"), ("t2", "项目乙任务", "乙任务答复")])
        draft_opener, _ = self.fake_opener({"id3": "new_episode"})
        bundle = request_episode_bundle(original, base_url="https://example.invalid", api_key="test",
                                        model="draft-model", opener=draft_opener)
        incomplete_review = {"status": "model_review_passed", "review_model": "review-model",
            "issues": [], "review_coverage": {"source_chunk_count": 2, "reviewed_chunk_count": 2,
                "all_chunks_accepted": True, "unreviewed_cross_chunk_claim_count": 1}}

        with patch("lib.scenario_model.request_session_review", return_value=incomplete_review):
            review = request_episode_bundle_review(original, bundle, base_url="https://example.invalid",
                api_key="test", model="review-model")

        self.assertEqual(review["status"], "model_review_rejected")
        self.assertEqual(review["episode_reviews"][0]["status"], "model_review_incomplete")
        self.assertEqual(review["issues"][0]["code"], "unreviewed_cross_chunk_claims")


if __name__ == "__main__":
    unittest.main()
