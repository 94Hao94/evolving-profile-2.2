import unittest

from lib.scenario_episodes import (episode_id_for, partition_source,
                                   deterministic_size_boundaries,
                                   revalidate_persisted_episode_source, validate_episode_bundle)
from lib.scenario_state_v3 import validate_state_draft


def source(messages):
    rows = []
    for index, (role, text, turn_id) in enumerate(messages, 1):
        rows.append({"evidence_id": f"id{index}", "model_ref": f"m{index}",
                     "role": role, "text": text, "turn_id": turn_id,
                     "at": f"2026-09-27T00:{index:02d}:00Z",
                     "raw_line_sha256": f"hash-{index}"})
    return {"thread_id": "01a0aad3-d9dd-7400-b1e7-a636388cab3b",
            "source": "codex_thread_history", "source_files": ["rollout.jsonl"],
            "source_revision": "parent-r1", "status": "complete", "messages": rows}


class ScenarioEpisodePartitionTests(unittest.TestCase):
    def test_partition_covers_all_messages_once_and_keeps_assistant_with_prior_prompt(self):
        original = source([
            ("user", "请整理项目甲方案", "turn-1"),
            ("assistant", "已整理项目甲方案", "turn-1"),
            ("user", "另一个任务：比较项目乙预算", "turn-2"),
            ("assistant", "项目乙预算比较", "turn-2"),
        ])

        episodes = partition_source(original, ["id3"])

        self.assertEqual([row["message_ids"] for row in episodes],
                         [["id1", "id2"], ["id3", "id4"]])
        self.assertEqual([row["start_message_id"] for row in episodes], ["id1", "id3"])
        self.assertEqual([row["end_message_id"] for row in episodes], ["id2", "id4"])
        self.assertTrue(all(row["source_revision"] != "parent-r1" for row in episodes))

    def test_one_episode_is_a_complete_partition(self):
        original = source([("user", "一个任务", "turn-1"),
                           ("assistant", "答复", "turn-1")])

        episodes = partition_source(original, [])

        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["message_ids"], ["id1", "id2"])

    def test_long_session_gets_deterministic_source_boundaries_without_splitting_turns(self):
        original = source([
            ("user", "任务一", "turn-1"), ("assistant", "答复一", "turn-1"),
            ("user", "任务二", "turn-2"), ("assistant", "答复二", "turn-2"),
            ("user", "任务三", "turn-3"), ("assistant", "答复三", "turn-3"),
            ("user", "任务四", "turn-4"), ("assistant", "答复四", "turn-4"),
            ("user", "任务五", "turn-5"), ("assistant", "答复五", "turn-5"),
        ])
        self.assertEqual(deterministic_size_boundaries(original, max_user_messages=4), ["id9"])

    def test_size_boundaries_defer_when_adjacent_user_messages_share_turn(self):
        original = source([
            ("user", "任务一", "turn-1"), ("assistant", "答复一", "turn-1"),
            ("user", "同轮补充", "turn-1"), ("assistant", "补充答复", "turn-1"),
            ("user", "任务二", "turn-2"), ("assistant", "答复二", "turn-2"),
            ("user", "任务三", "turn-3"), ("assistant", "答复三", "turn-3"),
            ("user", "任务四", "turn-4"), ("assistant", "答复四", "turn-4"),
        ])
        self.assertEqual(deterministic_size_boundaries(original, max_user_messages=3), ["id7"])

    def test_unknown_duplicate_or_out_of_order_boundaries_are_rejected(self):
        original = source([("user", "任务一", "turn-1"), ("assistant", "答复一", "turn-1"),
                           ("user", "任务二", "turn-2"), ("assistant", "答复二", "turn-2"),
                           ("user", "任务三", "turn-3")])

        for starts in (["missing"], ["id3", "id3"], ["id5", "id3"]):
            with self.subTest(starts=starts), self.assertRaisesRegex(ValueError, "scenario_episode_boundary_invalid"):
                partition_source(original, starts)

    def test_boundary_must_start_at_user_message_and_not_split_one_turn(self):
        non_user = source([("user", "任务一", "turn-1"), ("assistant", "答复一", "turn-1"),
                           ("user", "任务二", "turn-2")])
        same_turn = source([("user", "任务一", "turn-1"), ("assistant", "答复一", "turn-1"),
                            ("user", "同轮补充", "turn-1")])

        with self.assertRaisesRegex(ValueError, "scenario_episode_boundary_invalid"):
            partition_source(non_user, ["id2"])
        with self.assertRaisesRegex(ValueError, "scenario_episode_turn_split"):
            partition_source(same_turn, ["id3"])

    def test_first_episode_cannot_silently_begin_with_an_assistant_message(self):
        original = source([("assistant", "没有可归属的前置回答", "turn-1"),
                           ("user", "新任务", "turn-2")])

        with self.assertRaisesRegex(ValueError, "scenario_episode_first_message_not_user"):
            partition_source(original, [])

    def test_consecutive_user_boundary_without_turn_ids_is_unverifiable(self):
        original = source([("user", "任务一", None), ("user", "任务二", None)])

        with self.assertRaisesRegex(ValueError, "scenario_episode_turn_boundary_unverified"):
            partition_source(original, ["id2"])

    def test_episode_identity_is_derived_from_session_and_start_message(self):
        expected = "episode:thread-a:id9"
        self.assertEqual(episode_id_for("thread-a", "id9"), expected)
        self.assertNotEqual(episode_id_for("thread-b", "id9"), expected)

    def test_edit_to_a_nonadjacent_episode_invalidates_selected_episode_snapshot(self):
        original = source([("user", "任务甲", "turn-1"), ("assistant", "甲已完成", "turn-1"),
                           ("user", "任务乙", "turn-2"), ("assistant", "乙已完成", "turn-2"),
                           ("user", "任务丙", "turn-3"), ("assistant", "丙已完成", "turn-3")])
        partitions = partition_source(original, ["id3", "id5"])
        stored_episodes = [{key: part[key] for key in (
            "episode_id", "parent_session_id", "parent_source_revision", "source_revision",
            "start_message_id", "end_message_id", "message_ids")}
            for part in partitions]
        parent = {"session_id": original["thread_id"], "source_revision": original["source_revision"],
                  "episodes": stored_episodes}
        changed_elsewhere = {**original, "messages": [dict(row) for row in original["messages"]],
                             "source_revision": "parent-r2"}
        changed_elsewhere["messages"][5].update(evidence_id="id6-edited", raw_line_sha256="hash-6-edited",
                                                  text="丙的修订版答复")

        result = revalidate_persisted_episode_source(changed_elsewhere, parent,
                                                     stored_episodes[0]["episode_id"])

        self.assertEqual(result["status"], "stale_source_changed")

    def test_incomplete_source_cannot_be_partitioned(self):
        original = source([("user", "任务", "turn-1")])
        original["status"] = "over_budget"

        with self.assertRaisesRegex(ValueError, "scenario_source_incomplete"):
            partition_source(original, [])

    def test_bundle_keeps_each_v3_draft_inside_its_exact_episode_source(self):
        original = source([("user", "请整理项目甲方案", "turn-1"),
                           ("assistant", "已整理项目甲方案", "turn-1"),
                           ("user", "另一个任务：比较项目乙预算", "turn-2"),
                           ("assistant", "项目乙预算比较", "turn-2")])
        partitions = partition_source(original, ["id3"])
        rows = []
        for part in partitions:
            episode_source = {**original, "messages": part["_messages"],
                              "source_revision": part["source_revision"]}
            user_id = next(item["evidence_id"] for item in part["_messages"] if item["role"] == "user")
            assistant_id = next(item["evidence_id"] for item in part["_messages"] if item["role"] == "assistant")
            state = {"subject": {"text": "该段任务", "message_ids": [user_id]},
                     "goal": {"text": "完成该段工作", "message_ids": [user_id]},
                     "phase": "assistant_reported", "constraints": [], "corrections": [],
                     "assistant_reports": [{"text": "助手报告完成", "message_ids": [assistant_id]}],
                     "unresolved": []}
            draft = validate_state_draft(episode_source, state, model="test-model")
            rows.append({**{key: part[key] for key in (
                "episode_id", "start_message_id", "start_user_message_id", "end_message_id",
                "message_ids", "source_message_count", "source_revision")}, "draft": draft})
        bundle = {"schema": "evolving-profile.scenario-episode-bundle.v1",
                  "status": "source_linked_episode_draft", "thread_id": original["thread_id"],
                  "parent_source_revision": original["source_revision"],
                  "boundary_decisions": [{"message_id": "id3", "decision": "new_episode",
                                          "method": "model_boundary_review"}],
                  "unresolved_boundary_ids": [], "episodes": rows}

        validated = validate_episode_bundle(original, bundle)

        self.assertEqual([row["message_ids"] for row in validated["episodes"]],
                         [["id1", "id2"], ["id3", "id4"]])
        import json
        self.assertNotIn("请整理项目甲方案", json.dumps(validated, ensure_ascii=False))
        malformed = json.loads(json.dumps(bundle, ensure_ascii=False))
        malformed["boundary_decisions"][0]["decision"] = []
        with self.assertRaisesRegex(ValueError, "scenario_episode_decision_invalid"):
            validate_episode_bundle(original, malformed)
        mismatched_title = json.loads(json.dumps(bundle, ensure_ascii=False))
        mismatched_title["episodes"][0]["title"] = "项目乙预算"
        with self.assertRaisesRegex(ValueError, "scenario_episode_title_mismatch"):
            validate_episode_bundle(original, mismatched_title)

    def test_bundle_without_one_decision_per_later_user_message_is_rejected(self):
        original = source([("user", "任务甲", "turn-1"), ("assistant", "完成", "turn-1"),
                           ("user", "任务乙", "turn-2"), ("assistant", "完成", "turn-2")])
        partitions = partition_source(original, ["id3"])
        rows = []
        for part in partitions:
            episode_source = {**original, "messages": part["_messages"],
                              "source_revision": part["source_revision"]}
            user_id = next(item["evidence_id"] for item in part["_messages"] if item["role"] == "user")
            assistant_id = next(item["evidence_id"] for item in part["_messages"] if item["role"] == "assistant")
            state = {"subject": {"text": "该段任务", "message_ids": [user_id]},
                     "goal": {"text": "完成该段工作", "message_ids": [user_id]},
                     "phase": "assistant_reported", "constraints": [], "corrections": [],
                     "assistant_reports": [{"text": "助手报告完成", "message_ids": [assistant_id]}],
                     "unresolved": []}
            draft = validate_state_draft(episode_source, state, model="test-model")
            rows.append({**{key: part[key] for key in (
                "episode_id", "start_message_id", "start_user_message_id", "end_message_id",
                "message_ids", "source_message_count", "source_revision")}, "draft": draft})
        bundle = {"schema": "evolving-profile.scenario-episode-bundle.v1",
                  "status": "source_linked_episode_draft", "thread_id": original["thread_id"],
                  "parent_source_revision": original["source_revision"],
                  "boundary_decisions": [], "unresolved_boundary_ids": [], "episodes": rows}

        with self.assertRaisesRegex(ValueError, "scenario_episode_decision_coverage_invalid"):
            validate_episode_bundle(original, bundle)

    def test_bundle_from_an_older_source_revision_is_rejected(self):
        original = source([("user", "任务", "turn-1"), ("assistant", "答复", "turn-1")])
        bundle = {"schema": "evolving-profile.scenario-episode-bundle.v1",
                  "status": "source_linked_episode_draft", "thread_id": original["thread_id"],
                  "parent_source_revision": "older-revision", "episodes": []}

        with self.assertRaisesRegex(ValueError, "scenario_episode_bundle_stale_or_invalid"):
            validate_episode_bundle(original, bundle)


if __name__ == "__main__":
    unittest.main()
