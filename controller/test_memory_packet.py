#!/usr/bin/env python3
"""Contract tests for the Hook-owned Memory Packet boundary."""
from __future__ import annotations

import sys
import unittest

SCRIPTS = "/Users/apple/.hindsight/custom-codex/scripts"
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from lib.memory_packet import build_memory_packet  # type: ignore[import-not-found]


class MemoryPacketTests(unittest.TestCase):
    def _item(self, record_id: str, text: str, **metadata):
        return {
            "id": record_id,
            "text": text,
            "type": metadata.pop("type", "world_fact"),
            "metadata": metadata,
        }

    def test_packet_makes_user_full_prompt_higher_priority_than_memory(self):
        packet = build_memory_packet(
            full_prompt="把本轮用户提出的方案改为三段，并只基于当前附件。",
            items=[self._item("a", "旧记忆要求沿用五段模板", claim_id="old-template")],
            required_slots=["current_request"],
        )
        self.assertEqual(packet["priority"]["user_full_prompt"], "highest")
        self.assertIn("不得覆盖用户本轮原始 Prompt", packet["rendered_context"])
        self.assertIn("只基于当前附件", packet["rendered_context"])

    def test_low_lexical_bridge_is_kept_when_it_has_explained_graph_path(self):
        item = self._item(
            "bridge-1", "岗位三变更会同步影响后续职责页与验收页。",
            claim_id="role-three-closure",
            _ccy_graph_evidence={
                "path": ["岗位三", "影响", "职责页", "影响", "验收页"],
                "endpoint": "official_memory_graph",
                "fills_slots": ["related_impacts"],
            },
        )
        packet = build_memory_packet(
            full_prompt="在汇报页新增第三个岗位。",
            items=[item],
            required_slots=["related_impacts"],
        )
        bundle = packet["bundles"][0]
        self.assertEqual(bundle["role"], "bridge")
        self.assertEqual(bundle["delivery_state"], "rendered")
        self.assertEqual(bundle["relation_path"], ["岗位三", "影响", "职责页", "影响", "验收页"])

    def test_guidance_is_not_flattened_into_project_fact(self):
        item = self._item(
            "guidance-1", "涉及关联修改时必须检查全链路影响面。",
            type="observation",
            claim_id="closure-quality-rule",
            stable_guidance_sidecar=True,
            guidance_decision="apply",
        )
        packet = build_memory_packet(
            full_prompt="在PPT中新增第三个岗位。",
            items=[item],
            required_slots=["guidance"],
        )
        self.assertEqual(packet["bundles"][0]["role"], "guidance")
        self.assertIn("观察/偏好参考（适用性待判断）", packet["rendered_context"])
        self.assertNotIn("已证实当前事实", packet["rendered_context"])

    def test_same_claim_is_merged_but_all_evidence_is_preserved(self):
        items = [
            self._item("v1", "8月1日：方案采用A路径", claim_id="architecture-choice", occurred_at="2026-08-01"),
            self._item("v2", "8月20日：方案仍采用A路径，但增加回退", claim_id="architecture-choice", occurred_at="2026-08-20"),
        ]
        packet = build_memory_packet(
            full_prompt="方案现在是什么，演进里增加了什么？",
            items=items,
            required_slots=["current", "history"],
        )
        self.assertEqual(len(packet["bundles"]), 1)
        self.assertEqual(packet["bundles"][0]["evidence_ids"], ["v1", "v2"])
        self.assertEqual(packet["bundles"][0]["time_values"], ["2026-08-01", "2026-08-20"])

    def test_transport_budget_defers_without_silent_claim_loss(self):
        items = [
            self._item("must", "当前有效结论", claim_id="current", required_slots=["current"]),
            self._item("optional", "很长的补充证据 " * 30, claim_id="history", required_slots=["history"]),
        ]
        packet = build_memory_packet(
            full_prompt="请核对当前状态与历史。",
            items=items,
            required_slots=["current", "history"],
            max_rendered_chars=420,
        )
        self.assertIn("must", packet["transport_confirmed_record_ids"])
        self.assertIn("history", packet["deferred_claim_ids"])
        self.assertTrue(packet["deferred_claims"][0]["reason"].startswith("transport_budget"))

    def test_validated_controller_coverage_is_carried_to_legacy_packet_rows(self):
        # Official Hindsight rows often have no per-record slot metadata.  A
        # completed Controller receipt must therefore make the rendered
        # Packet auditable instead of displaying every slot as missing.
        packet = build_memory_packet(
            full_prompt="请解释三个组件的职责和上下游关系。",
            items=[self._item("legacy", "组件A负责编排，组件B负责存储。")],
            required_slots=["system_definitions", "role_relations"],
            validated_coverage={
                "required": ["system_definitions", "role_relations"],
                "covered": ["system_definitions", "role_relations"],
                "complete": True,
            },
        )
        self.assertEqual(packet["coverage"]["missing_slots"], [])
        self.assertTrue(packet["coverage"]["controller_complete"])
        self.assertTrue(packet["coverage"]["transport_complete"])

    def test_incomplete_controller_coverage_is_not_promoted_at_packet_boundary(self):
        packet = build_memory_packet(
            full_prompt="请解释一个组件。",
            items=[self._item("partial", "组件A负责编排。")],
            required_slots=["system_definitions", "alias_relations"],
            validated_coverage={
                "required": ["system_definitions", "alias_relations"],
                "covered": ["system_definitions"],
                "complete": False,
            },
        )
        self.assertIn("alias_relations", packet["coverage"]["missing_slots"])


if __name__ == "__main__":
    unittest.main()
