#!/usr/bin/env python3
"""Regression coverage for long-term evolution recall planning.

These tests intentionally use generic language rather than the name of one
product.  A question that asks how *anything* changed from an origin to the
present needs temporal stages and relationship discovery before it can claim
coverage complete.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
import json
import urllib.parse
import threading

import controller
from lib.governance import (
    is_backup_runtime_query,
    is_current_runtime_query,
    is_current_version_query,
    is_wps_sync_current_query,
    requires_historical_bank_recall,
)
from lib.relevance import (
    admission_decision,
    current_operation_scope_alignment,
    continuity_task_alignment,
    document_change_scope_alignment,
    injection_audit_alignment,
    operational_audit_alignment,
    operational_audit_query,
    procurement_scope_alignment,
    recent_activity_alignment,
    system_comparison_alignment,
)
from lib.context_coordination import build_contextual_intent_envelope, build_contextual_recall_query, build_full_prompt_context_slice
from lib.memory_router_v4 import build_payload as build_memory_router_payload
from recall import (
    admit_recall_results,
    allow_shadow_fixture_transcript,
    build_claim_delivery_receipt,
    filter_specific_personal_attribute,
    filter_timeline_evidence,
    specific_personal_attribute_anchor,
    split_mental_model_sections,
)


POLICY = {
    "roles": {
        "codex": {
            "allowedLevels": ["core", "facts", "events"],
            "initialLevels": ["core"],
        }
    },
    "sharedBank": "test-bank",
}


class EvolutionRecallPlanningTests(unittest.TestCase):
    def test_zero_injection_diagnostic_with_current_correction_keeps_bank_history(self):
        prompt = "那不对把，那么多都注入0条？历史的世界事实、经历、观察、心智模型、实体那么多，一个该召回的都没有？"
        self.assertTrue(requires_historical_bank_recall(prompt))
        plan = controller.build_plan(
            prompt, "codex", POLICY, {},
            runtime_context={"user_correction": True, "source_authority": False},
        )
        self.assertNotEqual(plan["memory_action"], "source_first")
        self.assertGreater(plan["query_count"], 0)

    def test_procedure_diagnosis_does_not_become_system_taxonomy_from_locate_word(self):
        """A process question mentioning ``定位`` must not require aliases."""
        prompt = (
            "请总结一次 Hindsight 真实召回/注入问题的完整处理流程："
            "如何发现、如何定位是召回还是准入还是投递、如何修复、"
            "如何做同题复测和相邻变体复测，以及失败时如何回退。"
        )
        self.assertNotIn("system_map", controller.detect_shapes(prompt))
        self.assertTrue(controller.comprehensive_procedure_requested(prompt, controller.detect_shapes(prompt)))

    def test_procedure_audit_does_not_require_alias_relations(self):
        """Contextual audit forcing keeps audit slots but not unrelated aliases."""
        prompt = (
            "请总结一次 Hindsight 真实召回/注入问题的完整处理流程："
            "如何发现、如何定位是召回还是准入还是投递、如何修复、"
            "如何做同题复测和相邻变体复测，以及失败时如何回退。"
        )
        plan = controller.build_plan(
            prompt,
            "codex",
            POLICY,
            {},
            runtime_context={
                "full_prompt": prompt,
                "full_prompt_source": "qwen3.7-plus_context_resolution",
                "contextual_intent": {
                    "used_context": True,
                    "routing_hints": {"force_shapes": ["audit"], "suppress_shapes": ["timeline"]},
                },
            },
        )
        self.assertIn("audit", plan["matched_shapes"])
        self.assertNotIn("alias_relations", plan["coverage_dimensions"])

    def test_current_working_set_keeps_explicit_long_term_guidance_check(self):
        plan = controller.build_plan(
            "刷新了，还是这样啊", "codex", POLICY, {},
            runtime_context={"full_prompt": "在当前项目中继续把上一轮指出的这一处改好，并核对长期相关约束。"},
        )
        self.assertNotEqual(plan["memory_action"], "noop_long_term")
        self.assertEqual(plan["queries"], [])
        self.assertTrue(plan["memory_needs"]["guidance"]["needed"])
        self.assertEqual(plan["working_set"]["decision"], "augment_current_working_set")

    def test_plan_keeps_raw_and_full_prompt_separate_for_audit(self):
        plan = controller.build_plan(
            "那就按这个改",
            "codex",
            POLICY,
            {},
            runtime_context={
                "full_prompt": "在当前 Hindsight 状态页中，把 Full Prompt 与上下文消解节点的详情补充为原始用户输入和完整语义问题。",
                "full_prompt_source": "agent_contract",
            },
        )
        self.assertEqual(plan["raw_user_prompt"], "那就按这个改")
        self.assertIn("原始用户输入", plan["full_prompt"])
        self.assertEqual(plan["full_prompt_source"], "agent_contract")

    def test_full_prompt_preserves_all_semantic_paragraphs_and_is_not_reduced_to_latest_line(self):
        """Full Prompt is already resolved; markers inside it are semantic content."""
        full = (
            "Prior context:\n"
            "此前已经比较过采购价格、功耗和部署边界。\n\n"
            "Latest user message:\n"
            "请补充长期运行、噪音和散热条件，并给出结论。"
        )
        plan = controller.build_plan(
            "请补充长期运行、噪音和散热条件，并给出结论。",
            "codex", POLICY, {},
            runtime_context={
                "raw_user_prompt": "那几个条件呢？",
                "full_prompt": full,
                "full_prompt_source": "qwen3.7-plus_context_resolution",
            },
        )
        self.assertEqual(plan["full_prompt"], full)
        self.assertEqual(plan["input_query"], full)
        self.assertIn("此前已经比较过采购价格", plan["full_prompt"])
        self.assertIn("请补充长期运行", plan["full_prompt"])

    def test_every_full_prompt_gets_guidance_probe_and_coverage_driven_graph_plan(self):
        plan = controller.build_plan(
            "新增第三个岗位", "codex", POLICY, {},
            runtime_context={"full_prompt": "在当前PPT新增第三个岗位，并检查所有关联职责页和验收页。"},
        )
        self.assertTrue(plan["guidance_sidecar"]["enabled"])
        self.assertTrue(plan["graph_route"]["enabled"])
        self.assertEqual(plan["graph_route"]["max_hops"], 2)
        self.assertLessEqual(plan["graph_route"]["max_nodes"], 120)
        self.assertIn("coverage_gap_stop", plan["graph_route"])

    def test_router_payload_carries_bounded_contextual_intent_not_full_transcript(self):
        envelope = {
            "schema": "hindsight.contextual_intent.v1",
            "used_context": True,
            "intent_mode": "contextual_mechanism_audit",
            "context_items": [{"role": "user", "content": "最近一条目标"}],
        }
        payload = build_memory_router_payload(
            "codex", "这条显示合格吗？", "codex-hook", "test-bank", envelope
        )
        self.assertEqual(payload["runtime_context"]["contextual_intent"]["intent_mode"], "contextual_mechanism_audit")
        self.assertNotIn("transcript_path", payload["runtime_context"])

    def test_runtime_headers_restore_contextual_intent_envelope(self):
        envelope = {"schema": "hindsight.contextual_intent.v1", "used_context": True, "intent_mode": "contextual_mechanism_audit"}
        runtime = controller.runtime_context_from_headers({
            "X-Memory-Context-Intent": urllib.parse.quote(json.dumps(envelope, ensure_ascii=False)),
        })
        self.assertEqual(runtime["contextual_intent"], envelope)

    def test_runtime_headers_preserve_explicit_agent_full_prompt(self):
        runtime = controller.runtime_context_from_headers({
            "X-Memory-Full-Prompt": urllib.parse.quote("结合当前任务上下文后的完整问题", safe=""),
            "X-Memory-Full-Prompt-Source": "agent_contract",
        })
        self.assertEqual(runtime["full_prompt"], "结合当前任务上下文后的完整问题")
        self.assertEqual(runtime["full_prompt_source"], "agent_contract")

    def test_runtime_headers_preserve_hook_admission_revision(self):
        runtime = controller.runtime_context_from_headers({
            "X-Memory-Admission-Revision": "hindsight.admission-contract.v1:abc123",
        })
        self.assertEqual(runtime["admission_revision"], "hindsight.admission-contract.v1:abc123")

    def test_runtime_headers_preserve_immutable_hook_invocation_identity(self):
        """Every controller trace must be joinable to one Hook turn.

        The status page previously had to infer this relation from timestamps:
        the Hook emitted an invocation ID, but the Controller discarded it.
        """
        runtime = controller.runtime_context_from_headers({
            "X-Memory-Session-Id": "ui-thread-123",
            "X-Memory-Invocation-Id": "hook-turn-456",
            "X-Memory-User-Prompt-Fingerprint": "prompt-789",
        })
        self.assertEqual(runtime["session_id"], "ui-thread-123")
        self.assertEqual(runtime["invocation_id"], "hook-turn-456")
        self.assertEqual(runtime["user_prompt_fingerprint"], "prompt-789")

    def test_controller_prequalification_cannot_bypass_final_topic_proof(self):
        """Regression for a real UI turn about quote data vanishing.

        An unrelated Hindsight maintenance record shared only the generic span
        ``的Bug``.  The Controller marked it qualified, and the Hook then
        trusted that stale mark without rechecking whether it proved the quote
        scenario/persistence problem.  A cross-layer cache receipt is never a
        permission to inject without a present-turn subject or relation proof.
        """
        prompt = (
            "我怀疑你并没有真正修复多情景切换导致报价丢失的Bug。"
            "在情况一填写报价后，切换到情况二再切回情况一时报价消失；"
            "必须动态复现、找根因并修复。"
        )
        unrelated = {
            "text": "修复了小黛卡片在飞书接口失败时一直显示正在推演的Bug。",
            "metadata": {"_ccy_admission": {"policy": "v6_relation_fallback_admission", "decision": "qualified"}},
        }
        decision = admission_decision(prompt, unrelated, preserve_controller_decision=True)
        self.assertEqual(decision["decision"], "rejected")
        self.assertIn("独立主题锚点", decision["reason"])

    def test_controller_prequalification_keeps_a_real_quote_persistence_proof(self):
        """The repair must reject only the generic overlap, not related history."""
        prompt = (
            "我怀疑你并没有真正修复多情景切换导致报价丢失的Bug。"
            "在情况一填写报价后，切换到情况二再切回情况一时报价消失；"
            "必须动态复现、找根因并修复。"
        )
        related = {
            "text": "多情景报价切换时，情况一和情况二的报价必须各自持久化，切回情况一不能丢失报价。",
            "metadata": {
                "_ccy_admission": {"policy": "v6_relation_fallback_admission", "decision": "qualified"},
                "semantic_relevance_score": 0.92,
            },
        }
        decision = admission_decision(prompt, related, preserve_controller_decision=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertIn("报价", decision["strong_hits"])

    def test_plan_payload_carries_full_prompt_before_controller_routing(self):
        payload = build_memory_router_payload(
            "codex", "能更细吗？", "codex-hook", "test-bank",
            {"schema": "hindsight.contextual_intent.v1", "used_context": True},
            full_prompt="前一项：Hindsight 从官方到现在的演进。\n本轮：分阶段展开。",
            full_prompt_source="contextual_followup_resolution",
        )
        runtime = payload["runtime_context"]
        self.assertEqual(runtime["full_prompt_source"], "contextual_followup_resolution")
        self.assertIn("分阶段展开", runtime["full_prompt"])

    def test_contextual_intent_resolves_audit_followup_without_false_timeline(self):
        prompt = (
            "我认为，如果想理解一句话的语义，不能只看这句话，必须结合上下文。"
            "在记忆最开始进行语义分析时是否做到了？检查状态页，这条显示合格吗？"
        )
        messages = [
            {"role": "user", "content": "汇报一下现在的新机制"},
            {"role": "assistant", "content": "目前通过 Query Contract、检索、注入回执和状态页来工作。"},
            {"role": "user", "content": prompt},
        ]
        envelope = build_contextual_intent_envelope(prompt, messages, {"contextMemoryCoordination": {}})
        self.assertTrue(envelope["used_context"])
        self.assertEqual(envelope["intent_mode"], "contextual_mechanism_audit")
        self.assertIn("状态页", envelope["resolved_subjects"])
        plan = controller.build_plan(
            prompt, "codex", POLICY, {}, runtime_context={"contextual_intent": envelope}
        )
        self.assertEqual(plan["primary_shape"], "audit")
        self.assertNotIn("timeline", plan["matched_shapes"])
        self.assertTrue(plan["contextual_intent"]["used_context"])

    def test_contextual_audit_uses_bounded_context_for_bank_query_not_answer_instruction(self):
        prompt = "这条状态页显示合格吗？语义分析是否结合上下文？"
        envelope = build_contextual_intent_envelope(prompt, [
            {"role": "assistant", "content": "目前链路包含 Query Contract、Hindsight 检索、Hook 实际注入和 9998 回执。"},
            {"role": "user", "content": prompt},
        ], {"contextMemoryCoordination": {}})
        query = build_contextual_recall_query(prompt, envelope, max_added_chars=120)
        self.assertTrue(query.startswith(prompt))
        self.assertIn("受控上下文检索提示", query)
        self.assertIn("Hindsight", query)
        self.assertLessEqual(len(query) - len(prompt), 220)

    def test_zero_injection_status_followup_is_a_contextual_audit_not_a_bare_point(self):
        """“这个注入是 0 条” must keep the visible status object in scope.

        The original production trace marked this turn ``used_context=True``
        but then treated it as an independent point query.  Its generic query
        admitted no candidates despite the Bank returning audit, observation
        and mental-model candidates.  The public planning seam must instead
        expose a contextual audit hint, so the Full-Prompt resolver can retain
        the status/packet subject before Controller admission.
        """
        prompt = "这个注入是0条，正常吗？"
        messages = [
            {"role": "user", "content": "请检查状态页里 Full Prompt、实际注入和真实链路是否一致。"},
            {"role": "assistant", "content": "我会核对 Hook 回执、Memory Packet 和历史审计记录。"},
            {"role": "user", "content": "那前半它能够获取到每个对话的上下文吧？现在测试还有问题吗？"},
            {"role": "assistant", "content": "Qwen 兜底会基于可访问会话形成 Full Prompt；状态页需显示真实回执。"},
            {"role": "user", "content": prompt},
        ]
        envelope = build_contextual_intent_envelope(prompt, messages, {"contextMemoryCoordination": {}})
        self.assertTrue(envelope["used_context"])
        self.assertEqual(envelope["intent_mode"], "contextual_mechanism_audit")
        self.assertIn("注入", envelope["resolved_subjects"])
        query = build_contextual_recall_query(prompt, envelope, max_added_chars=180)
        self.assertIn("受控上下文检索提示", query)
        self.assertIn("状态页", query)
        runtime = {
            "contextual_intent": envelope,
            "source_authority": True,
            "source_kind": "attachment",
            "source_label": "status.png",
        }
        plan = controller.build_plan(query, "codex", POLICY, {}, runtime_context=runtime)
        self.assertNotEqual(plan["memory_action"], "source_first")

    def test_short_verification_followup_inherits_active_memory_audit_scope(self):
        """A short “现在怎么样，能测试出来吗” is not a self-contained task."""
        prompt = "现在你觉得怎么样，能测试出来吗"
        messages = [
            {"role": "user", "content": "把 Full Prompt、实际注入、真实链路和状态页逐项核对。"},
            {"role": "assistant", "content": "会用真实 Hook 回执和状态页验证，不能只看模拟。"},
            {"role": "user", "content": prompt},
        ]
        envelope = build_contextual_intent_envelope(prompt, messages, {"contextMemoryCoordination": {}})
        self.assertTrue(envelope["used_context"])
        self.assertEqual(envelope["intent_mode"], "contextual_mechanism_audit")
        self.assertIn("注入", envelope["resolved_subjects"])

    def test_contextual_continuation_skips_fact_probe_and_checks_guidance(self):
        """A recovered "继续优化" task must not become a misleading zero recall."""
        prompt = "继续优化"
        messages = [
            {"role": "assistant", "content": "当前 Hindsight 真实输入、Hook、Controller、Packet 和状态页仍在核对。"},
            {"role": "user", "content": "给我继续优化"},
        ]
        envelope = build_contextual_intent_envelope(prompt, messages, {"contextMemoryCoordination": {}})
        self.assertTrue(envelope["used_context"])
        self.assertEqual(envelope["intent_mode"], "contextual_followup")
        plan = controller.build_plan(
            prompt, "codex", POLICY, {},
            runtime_context={"contextual_intent": envelope,
                             "full_prompt": envelope["full_prompt"],
                             "full_prompt_source": "contextual_followup_resolution",
                             "raw_user_prompt": prompt},
        )
        self.assertTrue(plan["working_set"]["sufficient"])
        self.assertEqual(plan["memory_action"], "guidance_only")
        self.assertEqual(plan["queries"], [])
        self.assertFalse(plan["memory_needs"]["facts"]["needed"])
        self.assertTrue(plan["memory_needs"]["guidance"]["needed"])
        self.assertEqual(plan["working_set"]["decision"], "use_current_working_set")

    def test_test_vs_real_comparison_resolves_the_preceding_test_protocol(self):
        """“你测试的真实吗” needs its omitted test object."""
        prompt = "你测试的真实吗"
        messages = [
            {"role": "user", "content": "你再测 5 条随机的。"},
            {"role": "assistant", "content": "会跑生产等价回放，核对 Hook、Controller、Bank、Packet 与 9998 回执。"},
            {"role": "user", "content": prompt},
        ]
        envelope = build_contextual_intent_envelope(prompt, messages, {"contextMemoryCoordination": {}})
        self.assertTrue(envelope["used_context"])
        self.assertEqual(envelope["intent_mode"], "contextual_mechanism_audit")
        self.assertIn("回执", envelope["resolved_subjects"])

    def test_full_prompt_slice_handles_a_production_sized_transcript(self):
        """A giant real transcript must not make Full-Prompt resolution bail out."""
        prompt = "你测试的真实吗"
        messages = [{"role": "user", "content": "无关历史" + ("甲" * 6000)} for _ in range(30)]
        messages.extend([
            {"role": "user", "content": "你再测 5 条随机的。"},
            {"role": "assistant", "content": "会跑生产等价回放，核对 Hook、Controller、Bank、Packet 与 9998 回执。"},
            {"role": "user", "content": prompt},
        ])
        selected, receipt = build_full_prompt_context_slice(prompt, messages)
        encoded = json.dumps(selected, ensure_ascii=False).encode("utf-8")
        self.assertGreater(receipt["source_bytes"], 48000)
        self.assertLessEqual(len(encoded), 44000)
        self.assertIn("生产等价回放", "\n".join(row["content"] for row in selected))

    def test_detail_followup_inherits_previous_evolution_workload(self):
        """“更细” is a continuation even without a pronoun such as “这条”."""
        previous = "你现在告诉我，Hindsight 自下载官方以来都经历了哪些比较大的变迁？"
        prompt = "但是我实际上我的改动特别频繁。你能给我捋出更细的吗？"
        envelope = build_contextual_intent_envelope(prompt, [
            {"role": "user", "content": previous},
            {"role": "assistant", "content": "Hindsight 从官方基础版演进到当前的 Agent Memory OS。"},
            {"role": "user", "content": prompt},
        ], {"contextMemoryCoordination": {}})
        plan = controller.build_plan(
            prompt, "codex", POLICY, {}, runtime_context={"contextual_intent": envelope}
        )

        self.assertTrue(envelope["used_context"])
        self.assertEqual(envelope["intent_mode"], "contextual_followup")
        self.assertTrue(controller.evolution_question(envelope["full_prompt"]))
        self.assertIn("timeline", plan["matched_shapes"])
        self.assertIn("stage_coverage", plan["coverage_dimensions"])

    def test_current_runtime_gate_does_not_swallow_architecture_comparison(self):
        self.assertFalse(is_current_runtime_query(
            "请比较官方 Hindsight、我们后续接入 Hook/Controller 后的变化，以及现在 Agent Memory OS 还缺什么。"
        ))
        self.assertFalse(is_current_runtime_query(
            "现在 Hindsight 的官方 Bank、Controller、Hook、9998 和 9999 分别是什么关系？哪些是正式路径？"
        ))
        self.assertTrue(is_current_runtime_query("现在 Hindsight 的 Hook 服务是否启用、端口是否监听？"))

    def test_origin_to_present_evolution_with_current_state_keeps_bank_history(self):
        """Generic '关键版本/当前状态' wording is not live-authority-only."""
        prompt = (
            "从 Hindsight 官方下载安装到当前定制 Agent Memory OS，架构经历了哪些阶段变化？"
            "请说明关键版本、转折、失败边界和当前状态。请只做文字回答。"
        )
        self.assertTrue(requires_historical_bank_recall(prompt))
        self.assertFalse(is_current_version_query(prompt))
        self.assertFalse(is_current_runtime_query(prompt))
        # A stale project-state source must not change the decision when this
        # invocation did not carry a current attachment/open-file authority.
        plan = controller.build_plan(
            prompt, "codex", POLICY, {},
            runtime_context={
                "source_authority": False,
                "source_present": False,
                "project_state": {
                    "active_source": {"kind": "attachment", "label": "old.png"},
                    "authority_mode": "source_first",
                },
            },
        )
        self.assertNotEqual(plan["memory_action"], "source_first")
        self.assertGreater(plan["query_count"], 0)

    def test_source_presence_bit_blocks_reused_attachment_header(self):
        """An old Source-Kind header cannot authorize a text-only turn."""
        context = controller.runtime_context_from_headers({
            "X-Memory-Source-Kind": "attachment",
            "X-Memory-Source-Label": "old.png",
            "X-Memory-Source-Present": "0",
        })
        self.assertFalse(context["source_authority"])
        self.assertFalse(context["source_present"])

    def test_mixed_current_version_query_keeps_bank_history_and_live_authority(self):
        prompt = (
            "之前讨论过 Hindsight 旧版本和当前版本，旧版召回不足；现在到底采用哪个版本？"
            "请区分当前生产状态与历史证据，说明历史过程。"
        )
        self.assertTrue(is_current_version_query(prompt))
        # The mixed prompt still needs the durable timeline; the recognizer
        # only re-enables the live authority prefix instead of short-circuiting
        # Bank recall.
        self.assertTrue(requires_historical_bank_recall(prompt))
        self.assertTrue(is_current_runtime_query(prompt))

    def test_context_resolved_generic_version_query_recovers_hindsight_runtime_domain(self):
        """A terse user turn must still receive live authority when its
        validated context envelope identifies the Hindsight system."""
        intent = {
            "schema": "hindsight.contextual_intent.v1",
            "used_context": True,
            "intent_mode": "association_closure",
            "routing_hints": {"force_shapes": ["system_map", "synthesis"]},
            "context_items": [
                {"role": "user", "content": "当前版本加入图谱/星座和 Claim Bundle，需以最新运行状态为准。"},
                {"role": "user", "content": "Full Prompt 驱动的 Hindsight 记忆链路。"},
            ],
            "current_user_message": "那当前到底采用哪个版本？",
        }
        self.assertTrue(is_current_version_query("当前系统到底采用哪个版本？", contextual_intent=intent))
        self.assertTrue(is_current_runtime_query("当前系统到底采用哪个版本？", contextual_intent=intent))
        # The same generic sentence without a validated Hindsight context must
        # remain neutral and must not leak this machine's runtime snapshot.
        self.assertFalse(is_current_version_query("当前系统到底采用哪个版本？"))

    def test_long_term_migration_scope_overrides_current_status_words(self):
        """A handoff question must query durable history before live status."""
        prompt = (
            "小黛之前做过 Codex 文件夹迁移任务。请仅基于长期记忆回答："
            "具体迁移了什么、当前接管时要核对哪些状态、验证与回退要求是什么？"
            "请区分已完成事实、待验证事项和计划。"
        )
        self.assertTrue(requires_historical_bank_recall(prompt))
        self.assertFalse(is_current_runtime_query(prompt))

    def test_historical_scope_blocks_stale_hook_authority_only_header(self):
        """A stale Hook authority hint cannot turn history recall into zero queries."""
        prompt = (
            "小黛之前做过 Codex 文件夹迁移任务。请仅基于长期记忆回答："
            "具体迁移了什么、当前接管时要核对哪些状态、验证与回退要求是什么？"
            "请区分已完成事实、待验证事项和计划。"
        )
        service = controller.MemoryQueryController.__new__(controller.MemoryQueryController)
        service.config = {}
        service.status = unittest.mock.Mock()
        service.audit_path = Path("/tmp/hindsight-history-authority-override.jsonl")
        # The plan seam is enough to prove the policy boundary without making
        # this unit test depend on a live Bank response.
        plan = controller.build_plan(prompt, "codex", POLICY, {}, runtime_context={"full_prompt": prompt})
        self.assertNotEqual(plan["memory_action"], "source_first")
        self.assertTrue(requires_historical_bank_recall(plan["full_prompt"]))

    def test_current_runtime_gate_does_not_swallow_delivery_acceptance_model(self):
        self.assertFalse(is_current_runtime_query(
            "怎样区分脚本测试通过、Hook 已投递和模型实际可用这三个验收层次？"
        ))
        self.assertFalse(is_backup_runtime_query(
            "Hindsight 备份上传云盘时，加密、恢复演练和来源核验有哪些已确认规则？"
        ))
        self.assertTrue(is_backup_runtime_query("现在 Hindsight 备份的加密算法和最近文件路径是什么？"))
        self.assertFalse(is_wps_sync_current_query(
            "Hindsight 备份上传 WPS 云盘时，云端保留、加密、恢复演练和来源核验有什么已确认规则？"
        ))
        self.assertTrue(is_wps_sync_current_query("现在 WPS 云同步是否已经完成，最新同步目录在哪里？"))

    def test_version_retention_explanation_keeps_bank_history(self):
        """Version lineage/rollback guidance must not become authority-only."""
        prompt = (
            "请解释为什么同一功能的不同时间版本不能简单去重成一条，"
            "以及什么时候应保留旧版本、失败版本和回退版本；"
            "请用 Hindsight 召回/注入的实际场景说明。"
        )
        self.assertTrue(requires_historical_bank_recall(prompt))
        self.assertFalse(is_current_runtime_query(prompt))
        self.assertFalse(is_current_version_query(prompt))

    def test_version_retention_family_admits_historical_lifecycle_evidence(self):
        """Version-lineage evidence is admitted without a project-name allowlist."""
        prompt = (
            "请解释为什么同一功能的不同时间版本不能简单去重成一条，"
            "以及什么时候应保留旧版本、失败版本和回退版本；"
            "请用 Hindsight 召回/注入的实际场景说明。"
        )
        item = {
            "type": "experience",
            "text": (
                "Hindsight 升级时，不同时间版本不能简单删除；"
                "要保留旧版本、失败版本、回退记录和生效时间，"
                "用于召回、审计与必要时的版本切换。"
            ),
        }
        decision = admission_decision(prompt, item, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertIn("版本沿革保留", decision["matched_concepts"])

    def test_generic_version_note_is_not_admitted_by_lineage_family(self):
        """A bare release note must not pass merely because it says Hindsight."""
        prompt = (
            "请解释为什么同一功能的不同时间版本不能简单去重成一条，"
            "以及什么时候应保留旧版本、失败版本和回退版本。"
        )
        item = {"type": "world", "text": "Hindsight 的版本更新记录。"}
        decision = admission_decision(prompt, item, deep=True)
        self.assertNotEqual(decision["decision"], "qualified")

    def test_shadow_replay_uses_only_explicit_safe_fixture_transcript(self):
        fixture = "/Users/apple/Documents/Codex/2026-07-11/ag/hindsight-memory-os/tests/fixtures/hindsight-replay/followup.jsonl"
        with patch.dict("os.environ", {"HINDSIGHT_EXECUTION_MODE": "shadow_replay"}, clear=False):
            self.assertFalse(allow_shadow_fixture_transcript(fixture))
        with patch.dict(
            "os.environ",
            {
                "HINDSIGHT_EXECUTION_MODE": "shadow_replay",
                "HINDSIGHT_SHADOW_REPLAY_WITH_TRANSCRIPT": "1",
            },
            clear=False,
        ):
            self.assertTrue(allow_shadow_fixture_transcript(fixture))
            self.assertFalse(allow_shadow_fixture_transcript("/tmp/real-user-transcript.jsonl"))

    def test_origin_to_present_transition_question_requires_timeline_and_graph(self):
        query = "从官方版本最开始到现在，这个系统经历了多少次明显变迁？"
        plan = controller.build_plan(query, "codex", POLICY, {})

        self.assertIn("timeline", plan["matched_shapes"])
        self.assertIn("transitions", plan["coverage_dimensions"])
        self.assertTrue(plan["relation_closure"]["required"])
        self.assertTrue(plan["graph_route"]["enabled"])
        self.assertGreaterEqual(plan["planned_query_count"], 4)
        self.assertTrue(any("阶段检索" in item for item in [*plan["queries"], *plan["escalation_queries"]]))
        self.assertTrue(plan["explicit_deep_recall"])
        self.assertTrue(plan["foreground_escalation_allowed"])

    def test_origin_since_official_download_is_an_evolution_question(self):
        """A natural Chinese origin-to-now question must not degrade to inventory."""
        query = "H-I-N-D-S-I-G-H-T 自下载官方以来都经历了哪些比较大的变迁？"
        plan = controller.build_plan(query, "codex", POLICY, {})

        self.assertIn("timeline", plan["matched_shapes"])
        self.assertIn("stage_coverage", plan["coverage_dimensions"])
        self.assertTrue(plan["explicit_deep_recall"])

    def test_evolution_plan_opens_version_counter_and_decision_facets(self):
        query = "Hindsight 从最开始官方到现在经历了哪些比较大的变迁？"
        plan = controller.build_plan(query, "codex", POLICY, {})
        self.assertTrue(all(
            dimension in plan["coverage_dimensions"]
            for dimension in ("version", "counter_evidence", "related_decisions")
        ))
        queries = [*plan["queries"], *plan["escalation_queries"]]
        self.assertTrue(any("版本" in item for item in queries))
        self.assertTrue(any("反例" in item for item in queries))
        self.assertTrue(any("决定" in item for item in queries))

    def test_semantic_review_cannot_erase_required_evolution_timeline(self):
        query = "从官方版本最开始到现在，这个系统经历了多少次明显变迁？"
        plan = controller.build_plan(
            query, "codex", POLICY, {},
            semantic_plan={
                "source": "qwen3.7-plus", "shape": "inventory", "confidence": 0.92,
                "coverage_dimensions": ["sources", "time_ranges"],
            },
        )
        self.assertIn("timeline", plan["matched_shapes"])
        self.assertTrue(any("阶段检索" in item for item in [*plan["queries"], *plan["escalation_queries"]]))

    def test_semantic_review_cannot_add_unsupported_system_taxonomy_to_method_question(self):
        """An advisory planner must not turn a continuation policy into a map."""
        query = (
            "在长任务场景中，当用户发出‘继续、不要停、把新的补上’等续办指令时，"
            "为什么不能仅依赖最近三轮对话上下文？请说明如何结合远距离上下文、"
            "活动任务证据、Full Prompt 和判断逻辑，避免误判普通续写。"
        )
        plan = controller.build_plan(
            query,
            "codex",
            POLICY,
            {"complexDeadlineMs": 18000},
            runtime_context={"full_prompt": query, "full_prompt_source": "qwen3.7-plus_context_resolution"},
            semantic_plan={
                "source": "qwen3.7-plus",
                "shape": "system_map",
                "confidence": 0.95,
                "coverage_dimensions": [],
                "reason": "组件关系",
            },
        )
        self.assertNotIn("system_map", plan["matched_shapes"])
        self.assertEqual(plan["semantic_plan_review"]["accepted"], False)
        self.assertIn("unsupported_shape", plan["semantic_plan_review"]["reason"])

    def test_explicit_named_system_question_still_accepts_system_taxonomy_review(self):
        query = "Hook、Controller 和 Hindsight Bank 分别是什么，角色、上下游和边界如何区分？"
        plan = controller.build_plan(
            query,
            "codex",
            POLICY,
            {"complexDeadlineMs": 18000},
            semantic_plan={
                "source": "qwen3.7-plus",
                "shape": "system_map",
                "confidence": 0.95,
                "coverage_dimensions": [],
                "reason": "命名组件关系",
            },
        )
        self.assertIn("system_map", plan["matched_shapes"])
        self.assertTrue(plan["semantic_plan_review"]["accepted"])

    def test_negative_dependency_phrase_does_not_open_graph_closure(self):
        query = "为什么不能仅依赖最近三轮对话上下文？请说明如何结合远距离上下文。"
        plan = controller.build_plan(query, "codex", POLICY, {"complexDeadlineMs": 18000})
        self.assertFalse(plan["relation_closure"]["required"])

    def test_continuation_policy_uses_method_and_synthesis_routes_not_incidental_status_audit(self):
        query = (
            "在长任务中，‘继续、不要停、把新的补上’为什么不能只依赖最近三轮？"
            "请说明如何结合远距离上下文、活动任务证据和 Full Prompt，避免误判普通续写。"
        )
        self.assertTrue(controller.continuation_policy_question(query))
        plan = controller.build_plan(query, "codex", POLICY, {"complexDeadlineMs": 18000})
        self.assertNotIn("current", plan["matched_shapes"])
        self.assertNotIn("audit", plan["matched_shapes"])
        self.assertIn("synthesis", plan["matched_shapes"])
        self.assertIn("procedure", plan["matched_shapes"])

    def test_continuation_policy_skips_second_semantic_planner(self):
        """The deterministic long-context route must not pay a second Qwen hop."""
        query = (
            "在长任务中，用户说“继续、不要停、把新的补上”时，为什么不能只读取最近三轮？"
            "请说明应如何使用远距离上下文、活动任务证据和 Full Prompt，同时避免把普通续写误判成同一任务。"
        )
        decision = controller.semantic_planner_needed(query)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["reason"], "deterministic_continuation_policy_route")

    def test_continuation_policy_admits_method_guidance_without_project_anchor(self):
        """Long-task guidance must survive the Hook gate without a file name."""
        query = (
            "在长任务中，用户说‘继续、不要停、把新的补上’时，为什么不能只读取最近三轮？"
            "请说明应如何使用远距离上下文、活动任务证据和 Full Prompt，同时避免把普通续写误判成同一任务。"
        )
        item = {
            "type": "world",
            "text": (
                "正确的长任务上下文使用方式是分层处理：Full Prompt 定范围，"
                "活动任务证据定状态，远距离上下文补意图，最近三轮只解决语言指代；"
                "需结合任务锚点避免把普通续写误判为同一任务。"
            ),
        }
        decision = admission_decision(query, item, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertTrue(decision["continuation_policy_alignment"]["qualified"])

    def test_continuation_policy_does_not_admit_context_heading_alone(self):
        """A same-topic heading is weak background, not continuation proof."""
        query = (
            "在长任务中，用户说‘继续、不要停、把新的补上’时，为什么不能只读取最近三轮？"
            "请说明应如何使用远距离上下文、活动任务证据和 Full Prompt，同时避免把普通续写误判成同一任务。"
        )
        item = {"type": "world", "text": "Full Prompt 与上下文。"}
        decision = admission_decision(query, item, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertEqual(decision["relevance_strength"], "weak")
        self.assertFalse(decision["continuation_policy_alignment"]["qualified"])
        self.assertFalse(decision["authority_verified"])

    def test_continuation_policy_coverage_uses_structural_evidence(self):
        """Coverage must recognize task scope/method/outcome markers, not only labels."""
        query = (
            "在长任务中，用户说‘继续、不要停、把新的补上’时，为什么不能只读取最近三轮？"
            "请说明应如何使用远距离上下文、活动任务证据和 Full Prompt，同时避免把普通续写误判成同一任务。"
        )
        plan = {
            "full_prompt": query,
            "input_query": query,
            "coverage_dimensions": ["supporting_evidence", "counter_evidence", "scope", "steps", "outcomes", "failure_modes"],
            "relation_closure": {"required": False},
        }
        results = [
            {"type": "world", "text": "Full Prompt 的历史问题包括短续问未正确合成；修复方案为统一构造并强制经过 Controller。"},
            {"type": "experience", "text": "Full Prompt 消解与远距离上下文检索并行，验收需区分缺失、短路和 Bank 超时。"},
            {"type": "world", "text": "需结合任务锚点判断是否同一任务，避免把普通续写误判为恢复历史任务。"},
        ]
        receipt = controller.evaluate_coverage(results, plan)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["missing"], [])
        self.assertTrue(receipt["evidence"]["continuation_policy_evidence"]["enabled"])

    def test_evolution_stage_admission_requires_both_subject_and_stage_evidence(self):
        query = "Hindsight 从最开始到现在经历了哪些变迁？"
        self.assertTrue(controller.is_evolution_stage_candidate(
            query, "Hindsight 完成了从 AgentMemory 的迁移，并启用新的 Hook 接入。"
        ))
        self.assertFalse(controller.is_evolution_stage_candidate(
            query, "另一套系统完成了迁移和升级。"
        ))
        self.assertFalse(controller.is_evolution_stage_candidate(
            query, "Hindsight 是一个记忆系统。"
        ))

    def test_evolution_scope_keeps_stage_evidence_but_rejects_topic_only_records(self):
        query = "Hindsight 自下载官方以来都经历了哪些比较大的变迁？"
        self.assertTrue(controller.evolution_evidence_alignment(
            query, "Hindsight 最初以官方 Retain、Recall、Reflect 为基础架构。"
        )["qualified"])
        self.assertTrue(controller.evolution_evidence_alignment(
            query, "Hindsight 后续引入 Query Controller、Hook 和多路线召回治理。"
        )["qualified"])
        self.assertTrue(controller.evolution_evidence_alignment(
            query, "当前 Hindsight 已作为 Agent Memory OS 的记忆底座运行。"
        )["qualified"])
        self.assertFalse(controller.evolution_evidence_alignment(
            query, "用户偏好在工程判断中优先使用 Python 和 TypeScript。"
        )["qualified"])
        self.assertFalse(controller.evolution_evidence_alignment(
            query, "用户偏好最小化改动方案，仅修改 Hindsight 数据库及其本地接入层。"
        )["qualified"])
        self.assertFalse(controller.evolution_evidence_alignment(
            query, "Hindsight 支持 HTTP、SDK、MCP 和 Wrapper 多种接入方式。"
        )["qualified"])
        self.assertFalse(controller.evolution_evidence_alignment(
            query, "助手解释官方 Hindsight 的写入机制和检索逻辑。"
        )["qualified"])
        self.assertFalse(controller.evolution_evidence_alignment(
            query, "示例用户询问 Hindsight 自下载官方以来的主要变迁历程。"
        )["qualified"])

    def test_evolution_scope_rejects_unrealized_request_but_keeps_completed_change(self):
        """A requested change is planning evidence, not a delivered timeline stage."""
        query = "Hindsight 自下载官方以来都经历了哪些比较大的变迁？"
        requested = controller.evolution_evidence_alignment(
            query, "用户指出之前的更新未实现在 Hindsight 基础上改造，且仍缺少启动开关。"
        )
        completed = controller.evolution_evidence_alignment(
            query, "Hindsight 已完成图谱接入和状态页改造，生产路径正式启用。"
        )
        self.assertFalse(requested["qualified"])
        self.assertEqual(requested["evidence_state"], "requested_not_realized")
        self.assertTrue(completed["qualified"])
        self.assertEqual(completed["evidence_state"], "realized_change")

    def test_live_merged_rows_use_the_same_evolution_admission_as_ranked_rows(self):
        """The post-merge production shape is a plain memory row, not {item: …}."""
        query = "Hindsight 自下载官方以来都经历了哪些比较大的变迁？"
        plan = controller.build_plan(query, "codex", POLICY, {})
        admitted, rejected = controller.admit_controller_results(query, [
            {"id": "request", "text": "用户要求在 Hindsight 中增加图谱和状态页改动。"},
            {"id": "done", "text": "Hindsight 已完成图谱接入和状态页改造，生产路径正式启用。"},
        ], plan)
        self.assertEqual([item["id"] for item in admitted], ["done"])
        self.assertEqual([item["id"] for item in rejected], ["request"])

    def test_continuation_cache_never_reuses_a_packet_across_sessions(self):
        instance = object.__new__(controller.MemoryQueryController)
        body = {"query": "Hindsight 从官方下载安装到现在经历了哪些关键变迁？", "types": ["world"], "budget": "high", "max_tokens": 1200}
        common = {"X-Memory-Project-Cwd": "/tmp/same-project"}
        first = instance._recall_key("/v1/default/banks/demo/memories/recall", body, {**common, "X-Memory-Session-Id": "session-a"}, "demo")
        second = instance._recall_key("/v1/default/banks/demo/memories/recall", body, {**common, "X-Memory-Session-Id": "session-b"}, "demo")
        self.assertNotEqual(first, second)

    def test_foreground_incomplete_recall_delivers_deep_result_before_hook_return(self):
        """A coverage helper must not be the only place where deep evidence exists.

        This is the seam behind the user's real-window symptom: the foreground
        Controller response can be incomplete while a later continuation is
        complete, but the Hook has already built its Packet from the first
        response.  The foreground owner must therefore synchronously adopt a
        completed deep result whenever the normal Hook request is still alive.
        """
        instance = object.__new__(controller.MemoryQueryController)
        instance.config = {
            "backgroundContinuation": {"enabled": True},
            "foregroundCompletionEnabled": True,
        }
        instance.inflight_lock = threading.Lock()
        instance.inflight_requests = {}
        instance._recall_key = lambda *args: "same-key"
        instance._foreground_reuse_key = lambda *args: "foreground-key"
        instance._foreground_reuse_get = lambda *args: None
        instance._foreground_reuse_put = lambda *args: None
        instance._continuation_cache_get = lambda *args: None
        instance._schedule_continuation = lambda *args: self.fail(
            "complete foreground continuation should not fall back to background"
        )
        initial = {
            "results": [{"id": "focused"}],
            "query_controller": {
                "version": controller.VERSION,
                "execution_id": "focused-exec",
                "query_id": "focused-exec",
                "execution_complete": True,
                "coverage_complete": False,
                "coverage_required": True,
                "result_count": 1,
                "continuation_status": "foreground_pending",
            },
        }
        deep = {
            "results": [{"id": "deep-a"}, {"id": "deep-b"}],
            "query_controller": {
                "version": controller.VERSION,
                "execution_id": "deep-exec",
                "query_id": "deep-exec",
                "execution_complete": True,
                "coverage_complete": True,
                "coverage_required": True,
                "result_count": 2,
                "continuation_mode": True,
            },
        }
        calls = []

        def fake_uncached(path, body, headers, bank_id):
            calls.append(dict(headers))
            return deep if headers.get("X-Memory-Background-Continuation") == "1" else initial

        instance._execute_recall_uncached = fake_uncached
        response = instance.execute_recall(
            "/v1/default/banks/demo/memories/recall",
            {"query": "系统因果链", "types": ["world"]},
            {"X-Memory-Invocation-Id": "inv"},
            "demo",
        )
        self.assertIs(response, deep)
        self.assertEqual(len(calls), 2)
        self.assertEqual(response["query_controller"]["execution_id"], "deep-exec")
        self.assertTrue(calls[1].get("X-Memory-Foreground-Continuation"))

    def test_graph_seed_queries_prioritize_explicit_anchor_over_generic_chinese_fragments(self):
        seeds = controller.build_graph_seed_queries(
            "那你告诉我 Hindsight 从最开始官方到现在经历了多少次明显变迁"
        )
        self.assertEqual(seeds[0], "Hindsight")
        self.assertIn("Hindsight", seeds[:3])

    def test_graph_seed_queries_drop_full_prompt_framing_and_keep_handoff_artifact(self):
        full = (
            "Prior context:\nHindsight 状态页曾记录 Codex 文件夹迁移任务，"
            "当前需要核对交接与回退证据。\n\n"
            "Latest user message:\n请说明迁移任务的验证与回退要求。"
        )
        seeds = controller.build_graph_seed_queries(full)
        self.assertIn("文件夹迁移任务", seeds[:3])
        self.assertNotIn("状态页曾", seeds)
        self.assertNotIn("需要核", seeds)

    def test_graph_seed_queries_do_not_spend_literal_probes_on_conversational_fragments(self):
        handoff = "用户要接管小黛之前做的 Codex 文件夹迁移任务，你接管一下"
        evolution = "那你告诉我 Hindsight 从最开始官方到现在经历了多少次明显变迁"
        handoff_seeds = controller.build_graph_seed_queries(handoff)
        evolution_seeds = controller.build_graph_seed_queries(evolution)
        self.assertEqual(handoff_seeds[:2], ["Codex", "文件夹迁移任务"])
        self.assertNotIn("用户要接管小黛之前做", handoff_seeds)
        self.assertNotIn("你接管一下", handoff_seeds)
        self.assertEqual(evolution_seeds[0], "Hindsight")
        self.assertNotIn("或者准确说有多少明显", evolution_seeds)
        self.assertNotIn("经历了", evolution_seeds)

    def test_graph_seed_queries_keep_chinese_alias_and_file_anchors(self):
        seeds = controller.build_graph_seed_queries(
            "当同一项目在不同记录里使用简称、文件名和客户别名时，Hindsight 如何通过实体归一化、图谱/星座关系和范围边界避免不同主体串线？"
        )
        self.assertEqual(seeds[:3], ["Hindsight", "同一项目", "文件名"])
        self.assertNotIn("当同一项目", seeds)
        self.assertNotIn("录里使用简称", seeds)

    def test_claim_receipt_keeps_record_evidence_and_never_pretends_hook_delivery(self):
        plan = controller.build_plan(
            "Hindsight 从官方最开始到现在经历了哪些变迁？", "codex", POLICY, {}
        )
        receipt = controller.build_claim_receipt(
            "Hindsight 从官方最开始到现在经历了哪些变迁？",
            [
                {"id": "origin", "text": "官方 Hindsight 初始版本提供基础长期记忆。"},
                {"id": "hook", "text": "Hindsight 后续接入 Hook 和 Controller 进行治理。"},
                {"id": "now", "text": "当前 Agent Memory OS 采用 Hindsight Bank 与图谱。"},
            ],
            plan,
        )
        self.assertEqual(receipt["schema"], "ham.controller_claim_receipt.v1")
        self.assertEqual(receipt["actual_hook_injected_claim_ids"], [])
        self.assertEqual(receipt["candidate_record_count"], 3)
        self.assertGreaterEqual(receipt["admitted_claim_count"], 3)
        self.assertTrue(any("origin" in row["evidence_ids"] for row in receipt["claims"]))

    def test_attachment_diagnostic_does_not_suppress_historical_memory(self):
        runtime = {
            "source_authority": True,
            "source_kind": "attachment",
            "source_label": "status.png",
        }
        plan = controller.build_plan(
            "刷新了还是这样啊，为什么这次实际链路不全？", "codex", POLICY, {}, runtime_context=runtime
        )
        self.assertNotEqual(plan["memory_action"], "source_first")

    def test_non_policy_source_first_receipt_does_not_reference_unset_policy_flag(self):
        """A current-file authority trace still needs a complete controller receipt.

        This is the production shape behind a user saying “only check this
        attached file; do not use old history”.  It is source-first, but it is
        not one of the Hook's stable policy authorities.
        """
        service = controller.MemoryQueryController.__new__(controller.MemoryQueryController)
        service.config = {}
        service.status = unittest.mock.Mock()
        service.audit_path = Path("/tmp/hindsight-source-first-regression.jsonl")
        plan = {
            "query_id": "q-source-first", "execution_id": "e-source-first",
            "query_fingerprint": "fp-source-first", "memory_action": "source_first",
            "primary_shape": "current", "matched_shapes": ["current"],
            "strategies": ["attachment_authority"], "route_decisions": [],
            "working_set": {}, "planner": {}, "value_of_information": {},
            "input_query_tokens": 5, "project_state": None,
            "source_guard": {"active": True, "kind": "attachment"},
        }
        with patch.object(service, "plan", return_value=plan):
            response = service._execute_recall_uncached(
                "/v1/default/banks/test-bank/memories/recall",
                {"query": "只根据当前附件核对金额"}, {}, "test-bank",
            )
        receipt = response["query_controller"]
        self.assertEqual(receipt["memory_action"], "source_first")
        self.assertEqual(receipt["scope_claim"], "current_source_inspection_required")

    def test_hook_delivery_receipt_marks_only_records_that_crossed_context_boundary(self):
        controller_receipt = {
            "claim_receipt": {
                "schema": "ham.controller_claim_receipt.v1",
                "claims": [
                    {"claim_id": "claim-origin", "evidence_ids": ["origin", "other"]},
                    {"claim_id": "claim-current", "evidence_ids": ["current"]},
                ],
            }
        }
        delivery = build_claim_delivery_receipt(
            controller_receipt,
            injected_items=[{"id": "origin"}, {"id": "authority:runtime"}],
            candidate_items=[{"id": "origin"}, {"id": "current"}],
        )
        self.assertEqual(delivery["schema"], "ham.claim_delivery_receipt.v1")
        self.assertEqual(delivery["actual_hook_injected_claim_ids"], ["claim-origin"])
        self.assertEqual(delivery["non_claim_injection_ids"], ["authority:runtime"])
        self.assertEqual(delivery["retrieved_claim_count"], 2)

    def test_status_join_preserves_delivery_receipt_from_the_real_hook_event(self):
        joined = controller.join_effectiveness(
            [{"execution_id": "exec-1", "query_id": "exec-1", "selected_results": []}],
            [{
                "execution_id": "exec-1", "stage": "injection", "injected_ids": ["record-1"],
                "claim_delivery": {"schema": "ham.claim_delivery_receipt.v1", "actual_hook_injected_claim_ids": ["claim-1"]},
            }],
        )
        self.assertEqual(joined[0]["memory_effectiveness"]["claim_delivery"]["actual_hook_injected_claim_ids"], ["claim-1"])

    def test_receipt_index_preserves_visible_injection_after_large_ledger_tail_rolls(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "receipts.json"
            event = {
                "at": "2026-08-30T18:00:00Z", "query_id": "visible-exec",
                "execution_id": "visible-exec", "stage": "injection",
                "injected_ids": ["memory-a"], "injected_items": [{"id": "memory-a", "text_preview": "实际注入"}],
            }
            controller.upsert_receipt_index(index, event, max_executions=3)
            restored = controller.receipt_index_events(index, {"visible-exec"})
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["injected_ids"], ["memory-a"])

    def test_receipt_index_preserves_hook_and_packet_stage_ledgers(self):
        """A status click must not collapse real Hook/Packet stages to zero.

        The bounded receipt index stores the effectiveness event used after
        the append-only audit grows beyond its tail window.  Its projection
        must retain the stage-3/4 counts; otherwise 9998 displays a false
        ``Hook input 0 / Packet delivered 0`` beside a non-zero injection.
        """
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "receipts.json"
            event = {
                "execution_id": "stage-exec", "stage": "injection",
                "injected_ids": ["memory-a"],
                "pipeline_stages": {
                    "controller_admission": {"input_count": 3, "qualified_count": 2, "rejected_count": 1},
                    "hook_postprocessing": {"input_count": 2, "admitted_count": 1, "rejected_count": 1, "deferred_count": 0},
                    "packet_delivery": {"input_count": 1, "delivered_count": 1, "not_delivered_count": 0},
                },
            }
            controller.upsert_receipt_index(index, event, max_executions=3)
            restored = controller.receipt_index_events(index, {"stage-exec"})
        self.assertEqual(len(restored), 1)
        stages = restored[0].get("pipeline_stages") or {}
        self.assertEqual(stages["hook_postprocessing"]["admitted_count"], 1)
        self.assertEqual(stages["packet_delivery"]["delivered_count"], 1)

    def test_trace_index_preserves_recent_broad_trace_after_tail_window_rolls(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "traces.json"
            event = {
                "at": "2026-08-30T18:00:00Z", "event": "recall",
                "query_id": "broad-exec", "execution_id": "broad-exec",
                "query_preview": "从最开始到现在的完整演进", "execution_complete": True,
                "selected_results": [{"id": "memory-a", "content": "large but inspectable claim ledger"}],
            }
            controller.upsert_trace_index(index, event, max_executions=3)
            restored = controller.trace_index_rows(index, limit=3)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["execution_id"], "broad-exec")
        self.assertTrue(restored[0]["execution_complete"])

    def test_implicit_evolution_plan_widens_the_hook_timeout_before_transport(self):
        plan = controller.build_plan(
            "Hindsight 从最开始官方到现在经历多少次明显变迁？", "codex", POLICY, {}
        )
        settings = controller  # keep this test's controller/Hook boundary explicit
        from recall import reconcile_recall_settings_with_plan
        resolved = reconcile_recall_settings_with_plan(
            {"profile": "precise", "budget": "low", "max_tokens": 700, "timeout": 15},
            plan,
            {"deepRecallTimeout": 185, "recallTransportReserveSeconds": 2},
        )
        self.assertEqual(resolved["controller_profile"], "deep")
        self.assertGreaterEqual(resolved["timeout"], 177)

    def test_cosmetic_record_is_not_an_evolution_stage(self):
        query = "请告诉我 Hindsight 从官方最开始到现在经历了哪些重要变迁、为什么改、现在还差什么？"
        cosmetic = "用户要求将界面左上角的 Hindsight 品牌文字修改为 ccy Hindsight，并保留蓝色图标。"
        self.assertFalse(controller.is_evolution_stage_candidate(query, cosmetic))

    def test_recurrence_subject_guard_does_not_treat_failure_as_the_subject(self):
        query = "周报想办法预防再次出问题，应该复用哪些历史机制和失败教训？"
        weekly = controller.recurrence_prevention_alignment(query, "周报自动触发失败，已经修复合并算法。")
        morning = controller.recurrence_prevention_alignment(query, "晨报曾经失败，现已增加重试。")
        self.assertTrue(weekly["qualified"])
        self.assertFalse(morning["qualified"])

    def test_recurrence_subject_guard_does_not_treat_mechanism_as_the_subject(self):
        query = "周报也想办法预防再出问题。应该复用哪些历史故障原因、修复和验收机制？"
        morning = controller.recurrence_prevention_alignment(query, "晨报图片故障自愈机制：自动重新匹配并复验图片。")
        weekly = controller.recurrence_prevention_alignment(query, "周报故障的合并算法已修复，并加入验收与幂等重试机制。")
        self.assertFalse(morning["qualified"])
        self.assertTrue(weekly["qualified"])

    def test_recurrence_request_rejects_generic_validation_from_another_workflow(self):
        decision = admission_decision(
            "周报也想办法预防再出问题。应该复用哪些历史故障原因、修复和验收机制？",
            {"type": "world", "text": "验收写法应采用主项、证据、结果三层结构。"},
            deep=True,
        )
        self.assertNotEqual(decision["decision"], "qualified")

    def test_delivery_policy_needs_delivery_context_not_only_xiaodai_name(self):
        item = {
            "type": "direct_policy",
            "text": "完成后通过飞书或小黛发送文件，优先使用小黛发送。",
            "metadata": {
                "direct_policy_gate": True, "policy_status": "active_provisional",
                "direct_policy_keywords": ["飞书", "小黛", "文件发送", "优先小黛", "交付通道"],
            },
        }
        self.assertFalse(controller.qualify_direct_policy("小黛之前做了 Codex 文件夹迁移任务吗？接管一下", item)[0])
        self.assertTrue(controller.qualify_direct_policy("完成后用小黛发送文件给我", item)[0])

    def test_direct_policy_scope_must_intersect_full_prompt(self):
        """A broad memory word cannot authorize a policy from another workflow."""
        migration = {
            "type": "direct_policy",
            "text": (
                "助手仅基于长期记忆回答 Codex 文件夹迁移，"
                "并保留来源和回退边界。"
            ),
            "metadata": {
                "direct_policy_gate": True,
                "policy_status": "active_provisional",
                "direct_policy_scope": "Codex 文件夹迁移",
                "direct_policy_keywords": ["长期记忆", "Codex迁移", "来源", "回退"],
            },
        }
        conflict_query = "那这种情况下，旧结论和历史经历分别怎么处理？如果证据不足，应该怎么办？"
        allowed_query = "请接管 Codex 文件夹迁移，并核对来源和回退边界。"

        rejected, rejection = controller.qualify_direct_policy(conflict_query, migration)
        admitted, admission = controller.qualify_direct_policy(allowed_query, migration)

        self.assertFalse(rejected)
        self.assertEqual(rejection["scope_anchor_hits"], [])
        self.assertIn("适用范围没有命中", rejection["reason"])
        self.assertTrue(admitted)
        self.assertTrue(admission["scope_anchor_hits"])

    def test_direct_policy_scope_can_be_read_from_sidecar_text(self):
        """Legacy sidecars without structured metadata still use their scope."""
        item = {
            "type": "direct_policy",
            "text": (
                "规则：迁移时保留原目录。适用范围：Codex 文件夹迁移；"
                "边界：不适用于普通 Hindsight 冲突问答。"
            ),
            "metadata": {
                "direct_policy_gate": True,
                "policy_status": "active_provisional",
                "direct_policy_keywords": ["规则", "迁移", "长期记忆"],
            },
        }
        ok, details = controller.qualify_direct_policy(
            "请核对 Codex 文件夹迁移的规则", item
        )
        self.assertTrue(ok)
        self.assertIn(
            "codex文件夹迁移",
            details["declared_scope"].casefold().replace(" ", ""),
        )
        self.assertTrue(details["scope_anchor_hits"])

    def test_schema_policy_does_not_leak_from_generic_evidence_word(self):
        item = {
            "type": "direct_policy",
            "text": "记忆模型必须包含证据、非空内容、刷新时间和失效条件。",
            "metadata": {
                "direct_policy_gate": True, "policy_status": "active_provisional",
                "direct_policy_keywords": ["记忆模型", "证据", "非空内容", "刷新时间", "失效条件"],
            },
        }
        query = "用户要求不要相信单测，要看真实链路；测试报告应怎样呈现证据？"
        self.assertFalse(controller.qualify_direct_policy(query, item)[0])

    def test_direct_policy_scope_ignores_file_format_for_named_audience(self):
        """Word/document overlap alone cannot leak a school-only format rule."""
        item = {
            "type": "direct_policy",
            "text": "记住特定格式。适用范围：校领导内部汇报材料的Word文档制作。边界：不适用于其他材料。",
            "metadata": {
                "direct_policy_gate": True,
                "policy_status": "active_provisional",
                "direct_policy_scope": "校领导内部汇报材料的Word文档制作",
                "direct_policy_keywords": ["Word文档", "格式规范", "校领导", "内部汇报"],
            },
        }
        unrelated = "针对智慧教学督导系统.docx，恢复表格格式并保留原有内容。"
        specific = "请制作校领导内部汇报的Word文档，按格式规范处理。"
        self.assertFalse(controller.qualify_direct_policy(unrelated, item)[0])
        self.assertTrue(controller.qualify_direct_policy(specific, item)[0])

    def test_teaching_policy_requires_explanation_request(self):
        """A project name containing 教学 is not a knowledge-explanation request."""
        item = {
            "type": "direct_policy",
            "text": "解释复杂专业知识时必须举例和打比方。适用范围：所有知识解释与教学内容输出。",
            "metadata": {
                "direct_policy_gate": True,
                "policy_status": "active_provisional",
                "direct_policy_scope": "所有知识解释与教学内容输出",
                "direct_policy_keywords": ["举例", "打比方", "知识解释", "教学"],
            },
        }
        document_query = "针对智慧教学督导系统.docx，恢复表格格式。"
        explanation_query = "请解释复杂专业知识，举例并打比方。"
        self.assertFalse(controller.qualify_direct_policy(document_query, item)[0])
        self.assertTrue(controller.qualify_direct_policy(explanation_query, item)[0])

    def test_real_completion_mental_model_supports_end_to_end_acceptance(self):
        item = {
            "type": "mental_model",
            "text": "【心智模型｜协作、沟通与交付标准｜真实完成的定义】真实完成不等于助手认为做完了；要有日志、测试结果或运行时行为等硬证据，同类问题扫描、根因修复、端到端回归验证和可用交付物。",
            "metadata": {"is_stale": False, "mental_model_section_gate": True},
        }
        query = "用户要求不要相信单测，要看真实链路；测试报告应怎样呈现证据？"
        self.assertTrue(controller.qualify_stable_mental_model(query, item)[0])

    def test_hook_admission_keeps_controller_selected_real_completion_section(self):
        item = {
            "id": "mental-real-completion",
            "type": "mental_model",
            "text": "真实完成不是助手认为做完了：必须有运行时行为或测试结果的硬证据、根因修复与端到端回归验证。",
            "metadata": {"mental_model_section_gate": True},
        }
        query = "用户要求不要相信单测，要看真实链路；测试报告应怎样呈现证据？"
        self.assertEqual(admission_decision(query, item, deep=True)["decision"], "qualified")

    def test_claim_receipt_maps_controller_mental_parent_to_delivered_section(self):
        parent = "mental-model:collaboration-delivery"
        delivered = {
            "id": parent + ":section:2",
            "type": "mental_model",
            "admission": {"mental_model_parent_id": parent, "mental_model_section": 2},
        }
        receipt = build_claim_delivery_receipt(
            {"claim_receipt": {"claims": [{"claim_id": "real-completion", "evidence_ids": [parent]}]}},
            injected_items=[delivered], candidate_items=[delivered],
        )
        self.assertEqual(receipt["actual_hook_injected_claim_ids"], ["real-completion"])
        self.assertEqual(receipt["not_delivered_claim_ids"], [])

    def test_injection_audit_does_not_admit_other_agent_adapter_only_on_receipt_words(self):
        query = "如何区分检索候选、Memory Packet 和实际注入，避免把未投递候选说成已进入对话？"
        unrelated = {
            "type": "experience",
            "text": "为 Hermes 增加同构适配器：发送 session_id 和 Full Prompt 至 Controller，同步写入注入回执并在状态页展示。",
        }
        self.assertNotEqual(admission_decision(query, unrelated, deep=True)["decision"], "qualified")

    def test_real_visible_dialogue_evidence_survives_status_and_transport_audit(self):
        """A real-window test method is evidence, not generic plumbing.

        This is taken from an actual recent turn where the user contrasted
        visible dialogue input with Computer Use.  The old gate discarded the
        matching experience because it only looked for status/Packet nouns.
        """
        query = (
            "之前在新的对话里实际输入内容来测试，不用 computer use；"
            "窗口中要有真实交互和真实回复，后续按这种方式测试。"
        )
        item = {
            "type": "experience",
            "text": (
                "助手承认此前测试不真实，原因是将附件路径与用户问题混为一段查询，"
                "而真实Hook会将二者分开（附件仅生成来源头，语义查询仅为文本），"
                "导致未覆盖真实触发路径。"
            ),
        }
        decision = admission_decision(query, item, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertTrue(decision["real_dialogue_alignment"]["qualified"])

    def test_visible_dialogue_policy_is_admitted_for_another_dialogue_prompt(self):
        """A concise cross-dialogue instruction needs the same reusable lane."""
        query = "在另一个对话框里输入内容，看到真实回复，用这种方式测试。"
        item = {
            "type": "world",
            "text": "用户强调后续测试必须使用窗口中实际显示且有交互的真实对话数据，并要求综合分析现有数据找出问题根源。",
        }
        decision = admission_decision(query, item, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertGreaterEqual(len(decision["real_dialogue_alignment"]["candidate_channel_markers"]), 2)

    def test_status_audit_keeps_explicit_non_infrastructure_subject_evidence(self):
        """A multi-topic Full Prompt must not turn subject facts into zero.

        The query also audits Bank/Hook/Controller/Packet/9998, but explicitly
        asks for PCB, Doubao history ingestion, and Hindsight architecture. A
        matching subject proposition must pass even if it does not describe the
        delivery receipt itself.
        """
        query = (
            "先验证PCB开发流程、豆包历史数据接入Hindsight现状，以及Hindsight v2与官方架构的区别；"
            "核对它们是否进入Bank并在状态页可见。"
        )
        item = {
            "type": "world",
            "text": (
                "用户纠正豆包关于Hindsight架构的错误解释：真实链路由程序控制召回，"
                "模型为gpt-5.6-sol，Stop每轮触发但第10轮才retain，并区分写入与回答。"
            ),
        }
        decision = admission_decision(query, item, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertTrue(decision["subject_evidence_alignment"]["qualified"])

    def test_subject_evidence_lane_does_not_bypass_pure_chain_audit(self):
        """Related migration background cannot become direct chain evidence."""
        query = (
            "我要做Hindsight真实验收，给出从真实输入、检查Hook/Controller/Bank/Memory Packet，"
            "到状态页复核的最短完整流程，并说明异常时如何回退和复测。"
        )
        item = {
            "type": "world",
            "text": (
                "Hindsight v2迁移自动化任务规则：每10分钟读取本机9998状态页，完成长文重提炼、"
                "时间线去重、观察重建、Bank切换、备份恢复演练和验收；遇到429时回退并发后重试。"
            ),
        }
        decision = admission_decision(query, item, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertEqual(decision["relevance_strength"], "weak")
        self.assertFalse(decision["subject_evidence_alignment"]["qualified"])
        self.assertFalse(decision["proposition_alignment"]["relation_support"])
        self.assertFalse(decision["authority_verified"])

    def test_memory_taxonomy_admits_four_layer_boundary_model(self):
        query = "跨任务继续处理某个历史项目时，怎样判断该优先调用具体经历、当前状态事实、观察还是心智模型？请说明各类记忆的边界，避免把旧结论当成当前事实。"
        item = {
            "type": "mental_model",
            "text": "Hindsight 四层模型：世界事实可随新证据更新；经历是不可改写的历史事件；观察是带范围与置信度的归纳；心智模型是可版本化刷新但不覆盖底层证据的决策框架。",
        }
        self.assertEqual(admission_decision(query, item, deep=True)["decision"], "qualified")

    def test_evidence_standard_admits_real_completion_model_without_literal_chain_word(self):
        query = "用户多次要求不要只报测试通过，要给可核对的真实证据；这应形成哪类稳定观察，并如何影响验收？"
        item = {
            "type": "mental_model",
            "text": "真实完成必须有日志、测试结果或运行时行为等硬证据，并完成根因修复、回归验证和可用交付。",
        }
        self.assertEqual(admission_decision(query, item, deep=True)["decision"], "qualified")

    def test_explicit_observation_question_admits_memory_architecture_observation(self):
        """Naming the observation/model layer must open its broad guidance lane.

        This reproduces the real Q13 regression: the Bank observation was
        directly about the four-layer Hindsight model, but the old gate only
        counted a literal Hindsight/记忆/召回 token as ``memory_anchor`` and
        rejected it for the concise question “准入标准是什么”.
        """
        query = (
            "基于观察和心智模型的泛化验收框架，请给出准入标准："
            "通过门槛、拒绝条件、降级处理和回归验证。"
        )
        item = {
            "type": "observation",
            "text": (
                "用户在个人AI记忆系统设计中偏好云端记忆总账＋本地节点＋远程MCP服务，"
                "采用Hindsight四层模型（世界事实、经历、观察、心智模型）严格单向派生，"
                "并按意图区分精准检索、时间线联合召回、开放盘点、溯源审计四种召回策略。"
            ),
        }
        ok, detail = controller.qualify_stable_guidance_observation(query, item)
        self.assertTrue(ok, detail)
        self.assertIn("观察", detail["specific_stable_relations"])

    def test_named_entity_fact_admits_concrete_alias_record_without_graph_vocabulary(self):
        """A concise named migration fact need not repeat ``图谱``/``覆盖``.

        The production Q15 alias variant found the right Alpha/Beta records in
        Bank, but the structural association gate discarded them because the
        summaries did not restate graph terminology.  Entity/action/result
        proof is sufficient for the concrete fact lane; graph nodes still use
        the stricter closure gate.
        """
        query = (
            "对于“Alpha 文件夹迁移”和“Beta Cloud Files”这类简称或别名相近的记录，"
            "如何通过实体归一化、图谱/星座关系、来源与范围边界确认是否同一项目并避免串线？"
        )
        record = {
            "type": "experience",
            "text": "Beta Cloud Files 的 Alpha 文件夹迁移已完成，复制到新目录并保留旧路径作为回退。",
        }
        decision = admission_decision(query, record, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertTrue(decision["named_entity_fact_alignment"]["qualified"])
        self.assertIn("alpha", " ".join(decision["named_entity_fact_alignment"]["shared_anchors"]))

    def test_named_entity_fact_does_not_admit_unrelated_alias_and_keeps_graph_evidence(self):
        query = (
            "对于“Alpha 文件夹迁移”和“Beta Cloud Files”这类简称或别名相近的记录，"
            "如何通过实体归一化、图谱/星座关系、来源与范围边界确认是否同一项目并避免串线？"
        )
        unrelated = {
            "type": "experience",
            "text": "Gamma Cloud Files 的普通目录整理已完成，未涉及 Alpha 或 Beta 项目。",
        }
        graph_only = {
            "type": "observation",
            "text": "实体图谱与星座关系用于准入，必须检查命题覆盖和来源证据。",
        }
        self.assertNotEqual(admission_decision(query, unrelated, deep=True)["decision"], "qualified")
        # A graph-only mechanism row is valid closure evidence and remains
        # governed by the structural association gate; it is not a concrete
        # migration fact, but it must not be lost merely because the new
        # direct-fact lane exists.
        self.assertEqual(admission_decision(query, graph_only, deep=True)["decision"], "qualified")

    def test_named_service_issue_admits_concise_historical_failure_evidence(self):
        """A named service incident needs its own lane, without graph words."""
        query = "Trainer怎么回事啊，Trainer刚才他回复有问题啊。"
        record = {
            "type": "world",
            "text": "Trainer存在历史Bug：用户回答仅做文字反馈未写入复习状态，导致后台认为尚未回答；后来已修复并恢复状态。",
        }
        decision = admission_decision(query, record, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertTrue(decision["named_service_issue_alignment"]["qualified"])

    def test_named_service_issue_rejects_bare_named_service_status(self):
        """A name plus a stable status is not an incident answer by itself."""
        query = "Trainer怎么回事啊，Trainer刚才他回复有问题啊。"
        unrelated = {
            "type": "world",
            "text": "Trainer迁移完成，当前状态已稳定，日常复习由独立服务运行。",
        }
        self.assertNotEqual(admission_decision(query, unrelated, deep=True)["decision"], "qualified")

    def test_explicit_mental_model_question_can_admit_fresh_model_without_hindsight_word(self):
        query = "心智模型的准入标准是什么？请说明证据、范围和反例。"
        item = {
            "type": "mental_model",
            "text": "心智模型是带证据范围、适用边界和反例的可版本化解释，不覆盖底层事实。",
            "metadata": {"is_stale": False, "mental_model_section_gate": True},
        }
        ok, detail = controller.qualify_stable_mental_model(query, item)
        self.assertTrue(ok, detail)

    def test_named_handoff_requires_the_named_system_not_only_lifecycle_words(self):
        query = "小黛之前不是做了 Codex 文件夹的迁移任务吗？你接管一下，告诉我当前状态和回退办法。"
        correct = continuity_task_alignment(query, "Codex 迁移完成，原目录保留作回退，当前状态已核验。")
        unrelated = continuity_task_alignment(query, "Codex 的迁移诊断显示当前状态仍需补充，已有回退方案。")
        self.assertTrue(correct["passes"])
        self.assertFalse(unrelated["passes"])

    def test_document_revision_with_old_content_is_not_misclassified_as_handoff(self):
        """Old content/format wording must stay in the document-change lane.

        A real table-restoration prompt mentioned previously整理好的内容,
        an old .doc and a project requirement book.  The former historical
        detector treated those generic words plus ``恢复`` as a cross-task
        handoff and rejected the relevant table memories before the document
        scope gate could evaluate them.
        """
        query = (
            "针对文件《招标文件技术参数初稿.docx》，恢复表格格式；将之前已整理好的"
            "技术参数重新放入表格，保留表格容器，删除占位内容，完成旧版 .doc 回转和逐页检查。"
        )
        alignment = continuity_task_alignment(query, "助手已恢复需求书中的六列表格，填入入库参数并保留表头跨页及颜色格式。")
        self.assertFalse(alignment["required"])
        document = {
            "type": "experience",
            "text": "助手已恢复需求书中的六列表格，填入入库参数并保留表头跨页及颜色格式。",
        }
        decision = admission_decision(query, document, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertNotIn("具体跨任务交接", decision["reason"])

    def test_document_scope_pass_is_an_admission_lane_for_table_restore(self):
        """A passing document-scope check must not fall through to rejection.

        This is the production-shaped Full Prompt behind the earlier false
        zero: the Bank row carries a six-column table, parameters, fill/retain
        actions and cross-page formatting.  It should be admitted as a
        document-change fact, while a same-project row with no document scope
        remains rejected.
        """
        query = (
            "针对文件《招标文件技术参数初稿_智慧教学督导系统.docx》，恢复表格格式；"
            "将技术参数重新放入Word文档的表格结构中，保留表格容器，删除占位内容，"
            "替换为实际参数，确保子系统、子模块标题与表格内容对应，完成旧版 .doc 回转和逐页检查。"
        )
        table_restore = {
            "type": "experience",
            "text": "助手此前已恢复需求书中的六列表格，填入入库参数并保留表头跨页及颜色格式，原文件已备份。",
        }
        unrelated = {
            "type": "experience",
            "text": "助手已完成同一项目的网络拓扑预算核算，服务已恢复，未涉及需求书表格。",
        }
        self.assertTrue(document_change_scope_alignment(query, table_restore["text"])["passes"])
        self.assertEqual(admission_decision(query, table_restore, deep=True)["decision"], "qualified")
        self.assertNotEqual(admission_decision(query, unrelated, deep=True)["decision"], "qualified")

    def test_recent_entry_review_uses_bounded_inventory_lane(self):
        query = "你现在就查看最近10几条我主动正常的条目，看看有没有问题"
        record = {
            "type": "world",
            "text": "示例用户要求助手查看其最近10几条主动正常的条目以检查是否存在问题 | When: 2026-09-04 | Involving: 示例用户, 助手",
        }
        unrelated = {
            "type": "experience",
            "text": "最近服务已恢复，检查接口延迟正常，但没有记录任何用户条目。",
        }
        self.assertIn("inventory", controller.detect_shapes(query))
        self.assertTrue(recent_activity_alignment(query, record["text"])["qualified"])
        self.assertEqual(admission_decision(query, record, deep=True)["decision"], "qualified")
        self.assertFalse(recent_activity_alignment(query, unrelated["text"])["qualified"])

    def test_recent_entry_review_preserves_query_qualifiers(self):
        """A recent service-status row must not stand in for active user entries."""
        query = "你现在就查看最近10几条我主动正常的条目，看看有没有问题"
        service_status = {
            "type": "experience",
            "text": "最近小黛飞书服务已恢复，用户反馈接口检查正常，但没有用户条目记录。",
        }
        alignment = recent_activity_alignment(query, service_status["text"])
        self.assertIn("主动", alignment["candidate_qualifier_mismatch"])
        self.assertFalse(alignment["qualified"])
        self.assertNotEqual(admission_decision(query, service_status, deep=True)["decision"], "qualified")

    def test_colloquial_table_restore_requires_specific_scope_overlap(self):
        """表格/参数恢复 should not admit a generic document policy."""
        query = "表格怎么没了？我是让你把那些参数放到需求书的表格里啊，表格里原有内容删了，不代表让你把表格也删了"
        relevant = {
            "type": "experience",
            "text": "需求书六列表格已恢复，填入参数并保留表头，原有内容未删除。",
        }
        generic = {
            "type": "world",
            "text": "五千字报告的内容需要保留，标题和结构按之前方案生成。",
        }
        self.assertTrue(document_change_scope_alignment(query, relevant["text"])["passes"])
        self.assertEqual(admission_decision(query, relevant, deep=True)["decision"], "qualified")
        self.assertFalse(document_change_scope_alignment(query, generic["text"])["passes"])
        self.assertNotEqual(admission_decision(query, generic, deep=True)["decision"], "qualified")

    def test_document_formats_do_not_activate_system_comparison_lane(self):
        query = (
            "针对文件《招标文件技术参数初稿.docx》，恢复表格格式；删除表格内占位内容，"
            "替换为实际参数，并完成旧版 .doc 回转。"
        )
        alignment = system_comparison_alignment(query, "网络拓扑由服务器、交换机与安全边界组成。")
        self.assertFalse(alignment["requested"])

    def test_concrete_handoff_probes_guidance_without_replacing_factual_handoff(self):
        plan = controller.build_plan(
            "小黛之前做了 Codex 文件夹迁移，接管后给我当前状态、待办与回退办法。",
            "codex", POLICY, {}
        )
        self.assertEqual(plan["mental_model_policy"], "suppress")
        self.assertTrue(plan["guidance_sidecar"]["enabled"])
        self.assertIn("不能替代", plan["guidance_sidecar"]["reason"])
        self.assertEqual(len(plan["queries"]), 3)

    def test_explicit_guidance_request_keeps_mental_model_sidecar_with_audit_cues(self):
        # The Full Prompt can legitimately mention current facts, evidence and
        # original wording while asking for the observation/model admission
        # rules.  Those audit cues must not suppress the requested guidance
        # sidecar; only a continuity handoff remains a hard suppression case.
        query = (
            "请核对当前事实与原话证据，并说明观察和心智模型的准入标准、"
            "反例和泛化边界。"
        )
        plan = controller.build_plan(query, "codex", POLICY, {})
        self.assertIn("audit", plan["matched_shapes"])
        self.assertIn("current", plan["matched_shapes"])
        self.assertEqual(plan["mental_model_policy"], "allow")
        self.assertTrue(plan["guidance_sidecar"]["mental_model"])

    def test_hook_preserves_controller_top_level_admission_receipt(self):
        item = {
            "id": "controller-approved",
            "text": "Codex 文件夹迁移已完成，旧路径保留回退。",
            "admission": {"decision": "qualified", "policy": "v5_coverage_proposition_admission"},
        }
        admitted, rejected = admit_recall_results("接管 Codex 文件夹迁移并说明回退", [item], {})
        self.assertEqual([row["id"] for row in admitted], ["controller-approved"])
        self.assertFalse(rejected)
        self.assertEqual(admitted[0]["metadata"]["_ccy_admission"]["decision"], "qualified")

    def test_status_injection_audit_rejects_unrelated_observation_plumbing(self):
        """A bare “observation” term cannot smuggle unrelated API plumbing.

        This is the exact false positive exposed by the status-followup replay:
        the candidate said only that ``expandIds`` needs ``sessionId|obsId``
        and happened to contain “观察/0 条”.  It has no status, Packet, Hook,
        Full Prompt, candidate-admission or delivery proposition.
        """
        query = (
            "检查状态页中 Full Prompt、实际注入和真实链路一致性；"
            "实际注入为 0 条是否正常，不能混淆候选、注入和回答使用。"
        )
        unrelated = {
            "type": "world",
            "text": "expandIds 参数必须使用 sessionId|obsId 格式才能正确定位观察记录，纯 obsId 会返回 0 条结果。",
            "metadata": {"semantic_relevance_score": 0.904003},
        }
        decision = admission_decision(query, unrelated, deep=True)
        self.assertEqual(decision["decision"], "rejected")
        self.assertIn("注入审计", decision["reason"])
        queue_noise = {
            "type": "world",
            "text": "当前状态：Qwen 画像候选 690 条，Observation pending 为 0，按两小时串行处理。",
            "metadata": {"semantic_relevance_score": 0.91},
        }
        decision = admission_decision(query, queue_noise, deep=True)
        self.assertEqual(decision["decision"], "rejected")

    def test_evolution_timeline_listing_does_not_become_injection_audit(self):
        """A timeline may name injection governance and the status page as stages.

        Merely co-occurring those nouns used to activate the narrow delivery
        audit gate, so an origin-to-present evolution query rejected nearly all
        stage evidence with a misleading status/Packet reason.  The audit gate
        should activate only for an explicit delivery predicate; the timeline
        must remain governed by its evolution-stage rules.
        """
        query = (
            "请按时间顺序梳理 Hindsight 从官方下载安装到现在的主要变迁，至少覆盖原始架构、"
            "Query Controller、Full Prompt、实际注入治理、状态页和当前未完成边界；不要只给最近几条记录。"
        )
        alignment = injection_audit_alignment(query, "Hindsight 在后期完善实际注入治理，并建设 9998 状态页。")
        self.assertFalse(alignment["required"])
        item = {
            "type": "world",
            "text": (
                "Hindsight 从官方 Bank 起步，随后引入 Query Controller，后来以 Full Prompt 驱动检索，"
                "并将实际注入治理与 9998 状态页纳入端到端验收。"
            ),
        }
        decision = admission_decision(query, item, deep=True)
        self.assertNotEqual(decision["reason"], "当前问题是状态页实际注入审计；候选只共享观察、数字或泛词，没有同时解释候选、投递和实际注入之间的区分关系，不能注入。")

    def test_procedure_recall_does_not_admit_a_migration_plan_that_lacks_the_named_chain(self):
        """A generic migration plan must not leak into a short chain audit.

        The production replay exposed this exact false positive: both texts
        mentioned Bank/status/validation, but the candidate had no Hook →
        Controller → Packet evidence.  Two generic endpoint words are not a
        sufficient substitute for the user-named execution chain.
        """
        query = (
            "我要做 Hindsight 的真实验收，给出从真实输入、检查 "
            "Hook/Controller/Bank/Memory Packet、到状态页复核的最短完整流程；"
            "同时说明发现异常时应如何回退和复测。"
        )
        migration_noise = {
            "type": "world",
            "text": (
                "Hindsight v2 迁移自动化任务规则：每10分钟读取本机9998状态页，"
                "完成长文重提炼、时间线去重、观察重建、Bank 切换、备份恢复演练和验收；"
                "遇到429时回退并发后重试。"
            ),
        }
        decision = admission_decision(query, migration_noise, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertEqual(decision["relevance_strength"], "weak")
        self.assertFalse(decision["proposition_alignment"]["relation_support"])
        self.assertFalse(decision["authority_verified"])

    def test_stable_guidance_expands_real_chain_acceptance_to_end_to_end_completion(self):
        """A durable acceptance preference need not repeat the model heading.

        The production failure used “不要相信单测，要看真实链路”.  The
        applicable live mental-model section calls the same standard
        “真实完成 / 端到端回归”; section selection must bridge those
        generic acceptance expressions before normal relevance admission.
        """
        terms = controller.guidance_query_terms(
            "用户要求不要相信单测，要看真实链路；测试报告应怎样呈现证据？"
        )
        self.assertIn("端到端", terms)
        self.assertIn("真实完成", terms)

    def test_effectiveness_projection_keeps_packet_and_transport_separate(self):
        trace = {"execution_id": "packet-test-1", "query_id": "packet-test-1", "selected_results": []}
        event = {
            "execution_id": "packet-test-1", "stage": "injection", "injected_ids": ["r1"],
            "injected_items": [{"id": "r1", "text_preview": "当前事实"}],
            "memory_packet": {"schema": "ham.memory_packet.v1", "rendered_bundle_count": 1},
            "packet_delivery": {"state": "transport_confirmed"},
            "full_prompt_resolution": {"source": "qwen3.7-plus_context_resolution", "ok": True, "confidence": 0.95},
        }
        joined = controller.join_effectiveness([trace], [event])[0]["memory_effectiveness"]
        self.assertEqual(joined["memory_packet"]["rendered_bundle_count"], 1)
        self.assertEqual(joined["packet_delivery"]["state"], "transport_confirmed")
        self.assertEqual(joined["full_prompt_resolution"]["source"], "qwen3.7-plus_context_resolution")
        self.assertEqual(joined["injected_count"], 1)

    def test_mental_model_bold_labels_are_independent_sections(self):
        sections = split_mental_model_sections(
            "# 风险提示\n- **经营状况**：现金流需关注。\n- **Hindsight系统优化**：只注入相关记忆。\n- **视频制作规范**：烧录字幕。"
        )
        self.assertEqual(len(sections), 4)
        self.assertTrue(any("Hindsight系统优化" in section for section in sections))

    def test_duplicate_progress_records_do_not_complete_stage_coverage(self):
        plan = {
            "coverage_dimensions": ["earlier", "transitions", "latest", "stage_coverage"],
            "relation_closure": {"required": True},
        }
        results = [
            {"id": "m1", "type": "experience", "text": "迁移第一阶段完成 60 批", "mentioned_at": "2026-08-01T00:00:00Z", "document_id": "migration-a"},
            {"id": "m2", "type": "experience", "text": "迁移第二阶段完成 120 批", "mentioned_at": "2026-08-02T00:00:00Z", "document_id": "migration-b"},
        ]

        receipt = controller.evaluate_coverage(results, plan)

        self.assertFalse(receipt["complete"])
        self.assertIn("latest", receipt["missing"])
        self.assertIn("stage_coverage", receipt["missing"])

    def test_explicit_original_architecture_anchor_completes_evolution_stage_coverage(self):
        """Do not flag a complete timeline as missing its origin stage."""
        plan = {
            "coverage_dimensions": ["earlier", "transitions", "latest", "stage_coverage"],
            "relation_closure": {"required": True},
        }
        results = [
            {"id": "origin", "type": "world", "text": "官方下载安装到现在的原始架构演进", "mentioned_at": "2026-07-01T00:00:00Z", "document_id": "origin"},
            {"id": "current", "type": "experience", "text": "后来引入 Query Controller，当前状态已完成升级", "mentioned_at": "2026-08-31T00:00:00Z", "document_id": "current"},
            {"id": "graph", "type": "observation", "text": "图谱与审计阶段形成覆盖边界", "mentioned_at": "2026-08-15T00:00:00Z", "document_id": "graph"},
        ]

        receipt = controller.evaluate_coverage(results, plan)

        self.assertNotIn("stage_coverage", receipt["missing"])

    def test_timeline_policy_question_does_not_require_unasked_origin_stage(self):
        """A maintenance policy is not a request to reconstruct history."""
        query = (
            "请梳理同一主题经历多个版本或状态变更时的完整时间线："
            "哪些历史结论必须保留，哪些可以标记为 superseded，"
            "如何按证据时间、生效时间、来源和范围去重，避免吞掉有价值的历史过程？"
        )
        plan = controller.build_plan(
            query,
            "codex",
            POLICY,
            {},
            runtime_context={"full_prompt": query, "full_prompt_source": "agent_contract"},
        )

        self.assertIn("timeline", plan["matched_shapes"])
        self.assertTrue(plan["coverage_required"])
        self.assertNotIn("stage_coverage", plan["coverage_dimensions"])
        timeline_route = next(
            item for item in plan["route_decisions"] if item["shape"] == "timeline"
        )
        self.assertNotIn("stage_coverage", timeline_route["coverage_dimensions"])

    def test_version_conflict_method_question_does_not_activate_verbatim_provenance_gate(self):
        """Metadata wording (来源/证据/时间) is not a request for user quotes.

        The adjacent real-window variant asked how to arbitrate conflicting
        states.  Its broad Bank candidates were incorrectly sent through the
        strict "来源核对" proposition gate merely because the question
        mentioned 来源.  That discarded the version/supersession evidence
        returned by escalation and left only two records in the Packet.
        """
        query = (
            "如果同一对象在不同时间、来源和范围里出现互相矛盾的状态，"
            "Hindsight 如何决定哪条旧结论只标记为 superseded、哪条历史经历必须保留？"
            "请给出判断顺序，并说明证据不足时如何保留 unresolved。"
        )
        record = {
            "id": "supersession-policy",
            "type": "world",
            "text": "同一主题应按对象、属性、范围和生效时间建立版本链；冲突未解决时保留双边证据并标记 unresolved。",
            "metadata": {"semantic_relevance_score": 0.88},
        }
        decision = admission_decision(query, record, deep=True)
        self.assertFalse(controller.explicit_user_source_request(query))
        self.assertNotEqual(decision["decision"], "rejected_provenance_proposition_mismatch")
        self.assertEqual(decision["decision"], "qualified")

    def test_version_conflict_method_admits_bank_evidence_without_reranker_score(self):
        """The foreground deadline may skip the reranker, but must not erase
        concrete supersession/timeline evidence returned by the Bank.

        Production controller rows expose a fused retrieval ``score`` while
        the optional semantic score is absent when the foreground reranker is
        disabled.  The admission lane therefore needs a reusable contextual
        conflict proposition, rather than a per-question score override.
        """
        query = (
            "如果同一对象在不同时间、来源和范围里出现互相矛盾的状态，"
            "Hindsight 如何决定哪条旧结论只标记为 superseded、哪条历史经历必须保留？"
            "请给出判断顺序，并说明证据不足时如何保留 unresolved。"
        )
        rows = [
            {
                "id": "supersession-conditions",
                "type": "experience",
                "score": 0.90,
                "text": (
                    "旧状态断言标记为 superseded 需同时满足：同一主体与属性、"
                    "作用范围相同或重叠、有效时间区间冲突、新证据权威性更高或直接可核验，"
                    "并建立显式 old.superseded_by = new_id 关系及记录原因。"
                ),
            },
            {
                "id": "four-layer-governance",
                "type": "world",
                "score": 0.97,
                "text": (
                    "Hindsight V3定义了四类记忆：世界事实记录当前或特定时期成立的状态，"
                    "较新明确证据可更新或取代旧结论；经历以追加历史为主，后一次成功不删除前一次失败；"
                    "观察和心智模型均保留证据范围与边界。"
                ),
            },
            {
                "id": "bare-field-noise",
                "type": "world",
                "score": 0.95,
                "text": "数据库的 superseded 字段只是删除标记，不涉及时间线、证据或冲突裁决。",
            },
        ]
        admitted, rejected = controller.admit_controller_results(
            query,
            [{"item": row} for row in rows],
            {"explicit_deep_recall": True, "primary_shape": "timeline"},
        )
        self.assertEqual(
            {row["id"] for row in admitted},
            {"supersession-conditions", "four-layer-governance", "bare-field-noise"},
        )
        self.assertEqual(rejected, [])
        direct = {row["id"] for row in admitted if row["metadata"]["_ccy_admission"]["relevance_strength"] == "direct"}
        self.assertEqual(direct, {"supersession-conditions", "four-layer-governance"})
        background = next(row for row in admitted if row["id"] == "bare-field-noise")["metadata"]["_ccy_admission"]
        self.assertEqual(background["relevance_strength"], "weak")
        self.assertFalse(background["proposition_alignment"]["relation_support"])
        self.assertFalse(background["authority_verified"])
        for row in admitted:
            self.assertEqual(
                (row.get("metadata") or {}).get("_ccy_admission", {}).get("policy"),
                controller.RELEVANCE_POLICY,
            )

    def test_explicit_verbatim_provenance_still_uses_provenance_gate(self):
        query = "我之前说过 Hindsight 的 superseded 规则吗？请找出我的原话和出处。"
        record = {
            "id": "generic-policy",
            "type": "world",
            "text": "Hindsight 负责长期记忆，按版本治理冲突。",
            "metadata": {"semantic_relevance_score": 0.88},
        }
        decision = admission_decision(query, record, deep=True)
        self.assertTrue(controller.explicit_user_source_request(query))
        self.assertEqual(decision["decision"], "rejected_provenance_proposition_mismatch")

    def test_incidental_stage_and_numeric_ranges_do_not_activate_timeline_filter(self):
        """A feasibility prompt's “阶段” and 400-800/3-5 ranges are not a history ask."""
        query = (
            "确认当前选题梳理阶段已结束，核心逻辑是以论文证据支撑方向可行性。"
            "请检索400-800条论文，形成3-5个候选方向包。"
        )
        self.assertFalse(controller.timeline_query_signal(query))
        self.assertNotIn("timeline", controller.detect_shapes(query))
        kept, rejected = filter_timeline_evidence(
            query,
            [{"id": "paper", "text": "论文实现已完成，支持当前路线并形成候选方向。"}],
        )
        self.assertEqual([item["id"] for item in kept], ["paper"])
        self.assertFalse(rejected)

    def test_operational_audit_keeps_structured_chain_rows_out_of_verbatim_gate(self):
        """A logs/Bank/Packet comparison needs chain evidence, not only raw quotes."""
        query = (
            "今天所有我说过的话，从日志脚本链路和 Bank 找到全面候选，"
            "与实际注入回执做对比，找出问题并解决。"
        )
        self.assertTrue(operational_audit_query(query))
        record = {
            "id": "chain-evidence",
            "type": "world",
            "text": (
                "真实链路审计应区分并沿 Full Prompt、Hook、Controller、Bank、Packet 和回执对齐；"
                "候选、准入、实际注入和页面投影必须用同一 execution_id 核对。"
            ),
            "metadata": {"semantic_relevance_score": 0.86},
        }
        decision = admission_decision(query, record, deep=True)
        self.assertNotEqual(decision["decision"], "rejected_provenance_proposition_mismatch")
        self.assertEqual(decision["decision"], "qualified")
        self.assertTrue(decision["operational_audit_query"])

    def test_literature_workflow_is_not_misclassified_as_runtime_audit(self):
        """Workflow words such as chain/candidate/search are not runtime proof."""
        query = (
            "立即启动研究方向与论文检索执行规划：覆盖监护仪、呼吸机和CRRT，"
            "批量获取400-800条论文，追踪引用链路，筛选候选方向并做外部验证。"
        )
        self.assertFalse(operational_audit_query(query))
        self.assertNotIn("audit", controller.detect_shapes(query))

    def test_media_file_operation_is_not_misclassified_as_system_map(self):
        """A role/scene noun in a file task is not a component taxonomy ask."""
        query = (
            "针对项目从片段V20到V25精简参考图，每个文件夹最多9张，"
            "保留首尾帧、角色设定和关键动作节点，并同步修改提示词文件名。"
        )
        self.assertNotIn("system_map", controller.detect_shapes(query))

    def test_media_artifact_change_uses_reference_and_prompt_scope(self):
        """The exact V20--V25 history row crosses the scoped-change gate."""
        query = (
            "从片段V20到V25精简参考图，每个文件夹最多9张，删除多余图片，"
            "同步修改提示词文件名。"
        )
        record = {
            "id": "media-change",
            "type": "experience",
            "text": "已完成V20至V25片段的参考图精简（每片段不超过9张）及提示词、README和清单的同步更新。",
        }
        decision = admission_decision(query, record, deep=True)
        self.assertEqual(decision["decision"], "qualified")

    def test_explicit_research_preference_admits_durable_boundary(self):
        """A concrete non-CNN/less-common preference must survive admission."""
        query = "不要CNN为主的AI，我不擅长；请继续找半冷门论文和可落地实现路径。"
        record = {
            "id": "research-preference",
            "type": "world",
            "text": "示例用户明确要求AI研究方向不以CNN为主，偏好半冷门方向及论文，并要求提供具体的实现路径。",
        }
        decision = admission_decision(query, record, deep=True)
        self.assertEqual(decision["decision"], "qualified")

    def test_preference_lane_does_not_admit_generic_cnn_record(self):
        """A CNN record without the user's preference relation stays out."""
        query = "不要CNN为主的AI，我不擅长；请继续找半冷门论文和可落地实现路径。"
        record = {"id": "generic-cnn", "type": "world", "text": "CNN是一种常用的深度学习模型。"}
        decision = admission_decision(query, record, deep=True)
        self.assertNotEqual(decision["decision"], "qualified")

    def test_research_preference_requires_two_independent_anchors(self):
        """A research preference must not widen to every generic agent rule."""
        query = (
            "你不用特意写非CNN；请看看最后一公里能否搭建智能体实现落地应用，"
            "多找论文并分析。"
        )
        generic_agent_rule = {
            "id": "generic-agent-rule",
            "type": "world",
            "text": "正文扩写时建议由同一个智能体完成，以保持上下文连贯和职责边界清晰。",
        }
        decision = admission_decision(query, generic_agent_rule, deep=True)
        self.assertTrue(decision["explicit_preference_alignment"]["research_scoped"])
        self.assertEqual(decision["explicit_preference_alignment"]["minimum_shared_anchors"], 2)
        self.assertNotEqual(decision["decision"], "qualified")

    def test_research_preference_keeps_two_anchor_boundary(self):
        """The durable preference survives when topic and evidence anchors match."""
        query = (
            "你不用特意写非CNN；请看看最后一公里能否搭建智能体实现落地应用，"
            "多找论文并分析。"
        )
        record = {
            "id": "research-boundary",
            "type": "world",
            "text": "用户偏好围绕CNN之外的论文与智能体落地方向，要求给出可执行的实现路径。",
        }
        decision = admission_decision(query, record, deep=True)
        self.assertEqual(decision["decision"], "qualified")

    def test_concrete_artifact_scope_keeps_named_app_evidence(self):
        """A named package query admits the same app/source artifact."""
        query = (
            "基于上一轮提到的 `iina-develop.zip`（GitHub 源码包），"
            "请解释该文件的用途以及为什么不能直接作为应用程序安装。"
        )
        record = {
            "id": "iina-source",
            "type": "experience",
            "text": "iina-develop.zip 是 IINA 的 GitHub 源码包，需用 Xcode 编译，不能直接安装 IINA.app。",
        }
        decision = admission_decision(query, record, deep=True)
        self.assertEqual(decision["decision"], "qualified")
        self.assertTrue(decision["concrete_artifact_scope_alignment"]["qualified"])

    def test_concrete_artifact_scope_rejects_other_zip(self):
        """A different zip must not enter merely because both are archives."""
        query = (
            "基于上一轮提到的 `iina-develop.zip`（GitHub 源码包），"
            "请解释该文件的具体用途以及为什么不能直接作为应用程序安装。"
        )
        record = {
            "id": "openssh-zip",
            "type": "experience",
            "text": "OpenSSH-Win64.zip 是 Windows 的安装包，适合绕过 Windows Update 下载。",
            "metadata": {"semantic_relevance_score": 0.99},
        }
        decision = admission_decision(query, record, deep=True)
        self.assertEqual(decision["decision"], "rejected_artifact_scope_mismatch")

    def test_generic_zip_word_does_not_activate_artifact_scope(self):
        """A generic extension without a concrete filename stays unrestricted."""
        query = "这个 zip 有什么用？"
        record = {"id": "generic-archive", "type": "experience", "text": "zip 用于压缩和打包文件。"}
        decision = admission_decision(query, record, deep=True)
        self.assertFalse(decision["concrete_artifact_scope_alignment"]["requested"])

    def test_path_scope_keeps_named_media_workflow_and_rejects_parent_directory_noise(self):
        """An absolute media-workflow path is a scope anchor, not a project allow-list.

        The production row that exposed this regression asked to assemble 25
        videos under one named folder. A generic Codex config or school
        storage memory must not enter merely because the parent directory and
        the word ``视频`` overlap. A same-folder row remains admissible even
        when it names one numbered clip.
        """
        query = (
            "基于已生成的25段视频文件（位于 /Users/apple/Projects/Codex/优优汽车队），"
            "执行最终剪辑合成任务：按V01–V25顺序拼接，添加片头片尾、背景音乐、字幕并输出MP4。"
        )
        relevant = {
            "type": "experience",
            "text": "文件 /Users/apple/Projects/Codex/优优汽车队/15.mp4 已生成，可作为本次视频合成的第15段素材。",
        }
        unrelated_config = {
            "type": "experience",
            "text": "Codex 配置文件位于 /Users/apple/.codex/config.toml，记录 MCP 服务器设置。",
        }
        unrelated_storage = {
            "type": "world",
            "text": "学校现有约200TB存储用于课堂音视频文件保存，不占用服务器本地磁盘。",
        }
        self.assertEqual(admission_decision(query, relevant, deep=True)["decision"], "qualified")
        self.assertEqual(
            admission_decision(query, unrelated_config, deep=True)["decision"],
            "rejected_path_scope_mismatch",
        )
        self.assertEqual(
            admission_decision(query, unrelated_storage, deep=True)["decision"],
            "rejected_path_scope_mismatch",
        )

    def test_path_scope_is_inactive_for_non_path_queries(self):
        """The generic guard must not narrow ordinary semantic recall."""
        query = "如何解释视频剪辑中的背景音乐和字幕处理？"
        item = {
            "type": "observation",
            "text": "视频后期处理中可统一处理字幕、音轨和背景音乐，需保证对白清晰。",
        }
        decision = admission_decision(query, item, deep=True)
        self.assertTrue(decision["path_artifact_scope_alignment"]["qualified"])
        self.assertFalse(decision["path_artifact_scope_alignment"]["requested"])

    def test_generic_url_and_folder_fragments_do_not_admit_unrelated_project_records(self):
        """URLs and folder nouns are transport hints, not subject identity."""
        query = (
            "参考 https://www.dilabs.cn/，完善天津财经大学商学院50万元人工智能实训平台"
            "申报材料，结合GB10算力集群、ComfyUI和本地视觉大模型，给出实训课程方案。"
        )
        proxy = {
            "type": "world",
            "text": "系统环境变量配置了代理，http_proxy、https_proxy和all_proxy均指向http://127.0.0.1:7890。",
        }
        generic_folder = {
            "type": "world",
            "text": "当前项目文件夹总体积约2.9GB，网站依赖含近3万个文件，绝大部分可重新安装。",
        }
        matching = {
            "type": "world",
            "text": "天津财经大学商学院人工智能实训平台采用GB10算力集群和本地视觉大模型，提供十个实训课程。",
        }
        self.assertNotEqual(admission_decision(query, proxy, deep=True)["decision"], "qualified")
        self.assertNotEqual(admission_decision(query, generic_folder, deep=True)["decision"], "qualified")
        self.assertEqual(admission_decision(query, matching, deep=True)["decision"], "qualified")

    def test_ui_window_operation_rejects_incidental_network_memory(self):
        """Chrome/Codex in a window task must not pull a traffic diagnosis."""
        query = (
            "纠正上一轮操作：请关闭浏览器右下角的三个 Chrome 窗口，"
            "保持其他文件和环境状态不变。"
        )
        network = {
            "id": "network-noise",
            "type": "world",
            "text": "高消耗来源为远程桌面和上传下载，海外访问走代理与机场，网络流量需要监控。",
            "metadata": {"semantic_relevance_score": 0.99},
        }
        alignment = current_operation_scope_alignment(query, network["text"])
        self.assertTrue(alignment["requested"])
        self.assertTrue(alignment["mismatch"])
        self.assertEqual(
            admission_decision(query, network, deep=True)["decision"],
            "rejected_current_operation_scope_mismatch",
        )

    def test_ui_window_operation_keeps_matching_window_action(self):
        query = "请关闭浏览器右下角的三个 Chrome 窗口，保持其他窗口不变。"
        matching = {
            "id": "window-action",
            "type": "experience",
            "text": "已按要求关闭 Chrome 右下角三个窗口，其他浏览器窗口保持不变。",
        }
        alignment = current_operation_scope_alignment(query, matching["text"])
        self.assertTrue(alignment["requested"])
        self.assertFalse(alignment["mismatch"])
        self.assertEqual(admission_decision(query, matching, deep=True)["decision"], "qualified")

    def test_ui_scope_does_not_block_explicit_network_question(self):
        query = "关闭浏览器后，请检查 VPN 连接和代理流量是否仍然异常。"
        network = {
            "id": "network-question",
            "type": "world",
            "text": "VPN 连接异常时应检查代理分流和网络流量消耗。",
            "metadata": {"semantic_relevance_score": 0.90},
        }
        alignment = current_operation_scope_alignment(query, network["text"])
        self.assertFalse(alignment["requested"])
        self.assertEqual(admission_decision(query, network, deep=True)["decision"], "qualified")

    def test_procurement_rule_lane_admits_complete_selection_policy(self):
        query = (
            "你下载的招标文件有问题，不是完整的招标公告，应该在中标公告里才能找到招标文件；"
            "记住，找服务标，找开发或建设的，别找运维租赁的。找近三年天津的，找10个，300万以下的。"
        )
        record = {
            "id": "procurement-policy",
            "type": "world",
            "text": (
                "用户规定查找招标文件的规则：必须从中标或成交公告附件中回溯完整采购文件，"
                "而非直接下载招标公告；仅限服务类标（开发、建设或升级），排除运维、租赁及货物项目；"
                "范围限定为近三年天津地区，金额300万元以下，目标数量为10份。"
            ),
        }
        alignment = procurement_scope_alignment(query, record["text"])
        self.assertTrue(alignment["requested"])
        self.assertIn("source_document", alignment["shared_families"])
        self.assertIn("selection_rule", alignment["shared_families"])
        self.assertEqual(admission_decision(query, record, deep=True)["decision"], "qualified")

    def test_procurement_rule_lane_rejects_generic_template(self):
        query = (
            "找近三年天津服务类中标项目，排除运维租赁，下载中标公告附件中的完整招标文件，"
            "金额300万以下，找10个。"
        )
        generic = {
            "id": "generic-procurement",
            "type": "world",
            "text": "招标文件模板包含项目名称、投标须知和设备采购技术参数，可作为通用参考。",
            "metadata": {"semantic_relevance_score": 0.99},
        }
        alignment = procurement_scope_alignment(query, generic["text"])
        self.assertTrue(alignment["requested"])
        self.assertFalse(alignment["qualified"])
        self.assertEqual(
            admission_decision(query, generic, deep=True)["decision"],
            "rejected_procurement_scope_mismatch",
        )

    def test_runtime_audit_requires_a_runtime_surface(self):
        """A true reconciliation mentions a Hook/Bank/receipt or status surface."""
        query = (
            "从日志、脚本和链路中找全候选，与 Bank 的实际注入回执、"
            "Controller 结果和状态页逐项对比，找出问题。"
        )
        self.assertTrue(operational_audit_query(query))

    def test_multi_facet_coverage_route_uses_deep_deadline_without_literal_deep(self):
        """Several required facets must not be squeezed into the complex cap."""
        query = (
            "请排查昨天 VPN 流量异常消耗近 100GB 的原因，结合当前节点状态、"
            "历史分流规则和应用后台行为，给出支持与反例证据及监控建议。"
        )
        plan = controller.build_plan(query, "codex", POLICY, {"complexDeadlineMs": 18000, "deepDeadlineMs": 180000})
        self.assertTrue(plan["coverage_required"])
        self.assertTrue(plan["coverage_budget_route"])
        self.assertEqual(plan["deadline_ms"], 180000)

    def test_operational_audit_rejects_stale_generic_project_memory(self):
        """A shared product word cannot bypass a multi-surface audit gate."""
        query = (
            "今天所有我说过的话，从日志脚本链路和 Bank 找到全面候选，"
            "与实际注入回执做对比，找出问题并解决。"
        )
        unrelated = {
            "id": "stale-plugin-memory",
            "type": "world",
            "text": "MemoryPlugin 已完成同步，旧任务配置可以继续使用 Hindsight。",
            "metadata": {
                "semantic_relevance_score": 0.94,
                "_ccy_admission": {"policy": "v9_association_closure_scope", "decision": "qualified"},
            },
        }
        alignment = operational_audit_alignment(query, unrelated["text"])
        self.assertFalse(alignment["qualified"])
        decision = admission_decision(
            query, unrelated, deep=True, preserve_controller_decision=True
        )
        self.assertEqual(decision["decision"], "rejected")
        self.assertIn("证据面", decision["reason"])

    def test_operational_audit_rejects_embedded_archived_conversation_wrapper(self):
        """A candidate containing another chat must not borrow its assistant claims."""
        query = (
            "今天所有我说过的话，从日志脚本链路和 Bank 找到全面候选，"
            "与实际注入回执做对比，找出问题并解决。"
        )
        archived = {
            "id": "candidate:archived-chat",
            "type": "world",
            "text": (
                "[role: user]\n秘书y的待办事件怎么以前的都没了呢\n[user:end]\n"
                "[role: assistant]\n已查明运行时源文件缺失；请核对日志、脚本和回执。\n[assistant:end]"
            ),
            "metadata": {"semantic_relevance_score": 0.96},
        }
        alignment = operational_audit_alignment(query, archived["text"])
        self.assertTrue(alignment["legacy_pending_wrapper"])
        self.assertFalse(alignment["qualified"])
        decision = admission_decision(query, archived, deep=True)
        self.assertEqual(decision["decision"], "rejected")

    def test_legacy_candidate_wrapper_is_not_admitted_on_a_normal_document_query(self):
        """A timeout fallback transcript cannot smuggle an unrelated task.

        This guards the non-audit path too: a long old assistant transcript
        can contain the same ``doc/docx/Word`` and ``替换`` words as a current
        document prompt, but it is not a canonical Bank claim.
        """
        query = "针对文件《招标文件技术参数初稿.docx》，恢复表格格式并替换为实际参数。"
        archived = {
            "id": "candidate:archived-document-chat",
            "type": "world",
            "text": (
                "[pending item 1/1; session=old]\n[role: user]\n请修改 Word 文档\n[user:end]\n"
                "[role: assistant]\n已完成网络拓扑和预算替换，保持系统边界不变。\n[assistant:end]"
            ),
            "metadata": {"semantic_relevance_score": 0.99},
        }
        decision = admission_decision(query, archived, deep=True, preserve_controller_decision=True)
        self.assertNotEqual(decision["decision"], "qualified")
        self.assertTrue(decision["legacy_candidate_wrapper"])
        self.assertIn("旧对话包装", decision["reason"])

    def test_pending_assistant_only_candidate_wrapper_is_not_admitted(self):
        """The fast fallback may contain only a pending assistant half."""
        query = (
            "根据申报指南和 ICU 设备数据，帮我找有论文支撑、工程量可控的 AI+医疗申报方向，"
            "并结合模板和智慧城市研究院的分工给出建议。"
        )
        archived = {
            "id": "candidate:pending-assistant-only",
            "type": "world",
            "text": (
                "[pending item 1/1; session=old] [role: assistant] "
                "## Current Task Editing the Tianjin Agricultural University formal Word proposal. "
                "Actual modified text must be red; expert opinion blue."
            ),
            "metadata": {"semantic_relevance_score": 0.99},
        }
        decision = admission_decision(query, archived, deep=True, preserve_controller_decision=True)
        self.assertEqual(decision["decision"], "rejected")
        self.assertTrue(decision["legacy_candidate_wrapper"])
        self.assertIn("旧对话包装", decision["reason"])

    def test_operational_audit_keeps_full_prompt_context_method_record(self):
        """The audit gate retains the method that separates Prompt and context."""
        query = (
            "今天所有我说过的话，从日志脚本链路和 Bank 找到全面候选，"
            "与实际注入回执做对比，找出问题并解决。"
        )
        method = {
            "id": "full-prompt-method",
            "type": "world",
            "text": (
                "系统应把用户原始 Prompt 与用于检索的 Full Prompt 分开保存，"
                "Full Prompt 补足上下文和工作集，再把完整问题交给 Hindsight 检索；"
                "审计时按来源、时间和证据核对实际结果。"
            ),
            "metadata": {"semantic_relevance_score": 0.78},
        }
        alignment = operational_audit_alignment(query, method["text"])
        self.assertTrue(alignment["qualified"])
        decision = admission_decision(query, method, deep=True)
        self.assertEqual(decision["decision"], "qualified")

    def test_origin_evolution_policy_question_keeps_strict_stage_requirement(self):
        """Adding a maintenance policy must not weaken an explicit evolution ask."""
        query = (
            "请梳理 Hindsight 从官方下载安装到现在经历的变迁，"
            "并说明每个版本如何按证据时间、生效时间、来源和范围去重，"
            "哪些历史结论可以标记为 superseded。"
        )
        plan = controller.build_plan(
            query,
            "codex",
            POLICY,
            {},
            runtime_context={"full_prompt": query, "full_prompt_source": "agent_contract"},
        )

        self.assertTrue(controller.evolution_question(query))
        self.assertIn("stage_coverage", plan["coverage_dimensions"])

    def test_evaluation_cutoff_excludes_later_self_generated_memory(self):
        results = [
            {"id": "historical", "mentioned_at": "2026-08-30T10:00:00Z", "text": "真实历史证据"},
            {"id": "self-answer", "mentioned_at": "2026-08-30T12:00:00Z", "text": "本次测试之后才写入的答案"},
        ]

        kept, excluded = controller.filter_results_as_of(
            results, "2026-08-30T11:00:00Z"
        )

        self.assertEqual([item["id"] for item in kept], ["historical"])
        self.assertEqual([item["id"] for item in excluded], ["self-answer"])

    def test_evaluation_cutoff_never_reuses_an_unbounded_cache_entry(self):
        app = object.__new__(controller.MemoryQueryController)
        path = "/v1/default/banks/test-bank/memories/recall"
        body = {"query": "从最开始到现在经历了多少次明显变迁？", "types": [], "budget": "high", "max_tokens": 2400}
        ordinary = {"X-Memory-Recall-Profile": "deep", "X-Memory-Session-Id": "cleanroom"}
        bounded = {
            **ordinary,
            "X-Memory-Execution-Mode": "shadow_replay",
            "X-Memory-Evaluation-As-Of": "2026-08-30T11:23:00Z",
        }
        self.assertNotEqual(
            app._recall_key(path, body, ordinary, "test-bank"),
            app._recall_key(path, body, bounded, "test-bank"),
        )
        self.assertNotEqual(
            app._foreground_reuse_key(path, body, ordinary, "test-bank"),
            app._foreground_reuse_key(path, body, bounded, "test-bank"),
        )

    def test_admission_contract_revision_invalidates_both_reuse_caches(self):
        app = object.__new__(controller.MemoryQueryController)
        path = "/v1/default/banks/test-bank/memories/recall"
        body = {"query": "同题修复后必须重新走真实 Hook", "types": [], "budget": "high", "max_tokens": 2400}
        old = {"X-Memory-Recall-Profile": "deep", "X-Memory-Session-Id": "cleanroom", "X-Memory-Admission-Revision": "v1:old"}
        new = {**old, "X-Memory-Admission-Revision": "v1:new"}
        self.assertNotEqual(app._recall_key(path, body, old, "test-bank"), app._recall_key(path, body, new, "test-bank"))
        self.assertNotEqual(app._foreground_reuse_key(path, body, old, "test-bank"), app._foreground_reuse_key(path, body, new, "test-bank"))

    def test_named_mechanism_stage_promotes_operational_evidence_without_a_top_k_override(self):
        query = "刚才提到的“Example Router”引入阶段，具体解决了什么召回问题？请只列关键机制与验证边界。"
        self.assertTrue(controller.named_mechanism_stage_question(query))
        record = {
            "id": "router-stage",
            "type": "world",
            "text": "Example Router 作为读取编排层，统一召回路由与来源校验，避免入口检索不一致，并以 Hook 注入回执验证实际交付。",
        }
        admitted, rejected = controller.admit_controller_results(
            query, [record], {"primary_shape": "point"}
        )
        self.assertEqual([item["id"] for item in admitted], ["router-stage"])
        self.assertEqual(rejected, [])
        decision = (admitted[0].get("metadata") or {}).get("_ccy_admission") or {}
        self.assertEqual(decision.get("decision"), "qualified_mechanism_stage")

    def test_named_mechanism_stage_does_not_promote_bare_component_mention(self):
        query = "“Example Router”引入阶段解决了什么问题，验证边界是什么？"
        bare = {"id": "bare", "type": "world", "text": "Example Router 是一个组件名称。"}
        admitted, _ = controller.admit_controller_results(query, [bare], {"primary_shape": "point"})
        self.assertEqual([row["id"] for row in admitted], ["bare"])
        admission = admitted[0]["metadata"]["_ccy_admission"]
        self.assertEqual(admission["relevance_strength"], "weak")
        self.assertNotEqual(admission["decision"], "qualified_mechanism_stage")
        self.assertFalse(controller.mechanism_stage_evidence_alignment(query, bare["text"])["qualified"])
        self.assertFalse(admission["authority_verified"])

    def test_discourse_openers_starting_with_wo_de_do_not_trigger_attribute_calibration(self):
        """``我的意思/理解`` is not a missing personal attribute.

        A broad possessive prefix previously converted this common follow-up
        into an attribute point query and erased all qualified Hindsight
        results in the Hook's final pass.
        """
        for prompt in (
            "我的意思你在另一个对话框里输入内容，这个你能做到，好多对话记录都是你发的，弄这种方式测试",
            "我的理解是先核对Bank再看实际注入",
            "我的要求是逐条测试，不要同时测试",
        ):
            self.assertEqual(specific_personal_attribute_anchor(prompt), "")

    def test_explicit_personal_attribute_still_uses_narrow_calibration(self):
        self.assertEqual(specific_personal_attribute_anchor("我的名字是什么"), "名字")
        self.assertEqual(specific_personal_attribute_anchor("我的鞋码是多少"), "鞋码")
        rows = [{"id": "name", "text": "用户正确姓名是示例用户"}]
        kept, anchor, negative = filter_specific_personal_attribute(
            "我的名字是什么", rows, {"primary_shape": "point"}
        )
        self.assertEqual(anchor, "名字")
        self.assertEqual([row["id"] for row in kept], ["name"])
        self.assertFalse(negative)

    def test_client_disconnect_is_not_a_functional_controller_failure(self):
        """A timed-out caller must not poison health after recall completes."""
        self.assertTrue(controller.is_client_disconnect(BrokenPipeError(32, "broken pipe")))
        self.assertTrue(controller.is_client_disconnect(ConnectionResetError(54, "reset")))
        self.assertTrue(controller.is_client_disconnect(ConnectionAbortedError(53, "aborted")))
        self.assertFalse(controller.is_client_disconnect(ValueError("bad request")))


if __name__ == "__main__":
    unittest.main()

class AgentMemoryScopeRegressionTests(unittest.TestCase):
    def test_named_agentmemory_question_rejects_codex_only_role_memory(self):
        query = "AgentMemory 现在在系统中承担什么角色？为什么不能因为历史残留就把它当作当前 Codex 路由？"
        codex_only = {
            "type": "world",
            "text": "Codex是系统的入口Agent，负责接收问题、执行任务和生成回答，并通过Hook/Adapter接入长期记忆，其本身不是记忆库。",
        }
        agentmemory = {
            "type": "world",
            "text": "AgentMemory 当前状态：MCP关闭、插件关闭、3111端口未监听；原数据库和备份保留，已退出当前 Codex 正式路由。",
        }
        self.assertNotEqual(admission_decision(query, codex_only, deep=True)["decision"], "qualified")
        self.assertEqual(admission_decision(query, agentmemory, deep=True)["decision"], "qualified")
