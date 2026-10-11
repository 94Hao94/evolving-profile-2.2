import io
import json
import os
import tempfile
import unittest
import time
from contextlib import redirect_stdout
from unittest.mock import patch

import pre_tool_use
import recall
from memory_turn_check import local_memory_access_guard, observe_tool, register_route, process_memory_route_hint, declare, retrieval_preflight, scenario_completion_gate


class PreToolUseMemoryGuardTest(unittest.TestCase):
    def test_process_route_hint_prefers_agent_recall_for_execution_failures(self):
        hint = process_memory_route_hint("我过去反复遇到页面修复失败和走弯路，现在请参考Agent过去的执行经验")
        self.assertTrue(hint["required"])
        self.assertEqual(hint["primary_tool"], "agent_recall")
        self.assertEqual(hint["escalation_tool"], "agent_research")

    def test_process_route_hint_escalates_cross_project_patterns_to_agent_research(self):
        hint = process_memory_route_hint("请比较跨项目重复失败的Agent修复模式，并评估模型迁移泛化")
        self.assertTrue(hint["required"])
        self.assertEqual(hint["primary_tool"], "agent_research")

    def test_process_route_hint_does_not_hijack_ordinary_user_history(self):
        hint = process_memory_route_hint("我以前对备份配置有哪些要求？")
        self.assertFalse(hint["required"])

    def test_process_prompt_requires_agent_lane_before_user_research(self):
        with tempfile.TemporaryDirectory() as root:
            register_route({"invocation_id": "route-process", "session_id": "s-process", "turn_id": "t-process", "raw_prompt": "过去反复遇到页面修复过程，请参考Agent执行经验", "required_ep_tool": "mcp__evolving_profile_controller__user_research"}, root=root)
            declaration = declare("route-process", "过去反复遇到页面修复过程，请参考Agent执行经验", "required", "需要历史过程经验", root=root)
            blocked = retrieval_preflight({"tool_name": "mcp__evolving_profile_controller__user_research", "tool_use_id": "research-1", "session_id": "s-process", "turn_id": "t-process", "tool_input": {"check_id": "route-process", "query": "过去页面修复"}}, root=root)
            self.assertEqual(blocked["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("agent_recall", blocked["hookSpecificOutput"]["permissionDecisionReason"])
            observe_tool({"tool_name": "mcp__evolving_profile_controller__agent_recall", "tool_use_id": "agent-1", "session_id": "s-process", "turn_id": "t-process", "tool_input": {"check_id": "route-process", "query": "过去页面修复"}, "tool_response": {"content": [{"type": "text", "text": "{}"}]}}, root=root)
            allowed = retrieval_preflight({"tool_name": "mcp__evolving_profile_controller__user_research", "tool_use_id": "research-2", "session_id": "s-process", "turn_id": "t-process", "tool_input": {"check_id": "route-process", "query": "用户历史约束"}}, root=root)
            self.assertEqual(allowed["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_scenario_followup_gates_source_read_until_scope_is_checked(self):
        with tempfile.TemporaryDirectory() as root:
            register_route({"invocation_id": "route-scenario", "session_id": "s-scenario", "turn_id": "t-scenario", "raw_prompt": "比较不同项目过去的Agent修复过程", "required_ep_tool": "mcp__evolving_profile_controller__agent_recall"}, root=root)
            declare("route-scenario", "比较不同项目过去的Agent修复过程", "required", "需要过程经验", root=root)
            observe_tool({"tool_name": "mcp__evolving_profile_controller__agent_recall", "tool_use_id": "agent-1", "session_id": "s-scenario", "turn_id": "t-scenario", "tool_input": {"check_id": "route-scenario", "query": "修复过程"}, "tool_response": {"content": [{"type": "text", "text": json.dumps({"scenario_followup": {"required": True, "next_tool": "search_scenario_summary", "scenarios": []}})}]}}, root=root)
            blocked = retrieval_preflight({"tool_name": "mcp__evolving_profile_controller__read_source", "tool_use_id": "source-1", "session_id": "s-scenario", "turn_id": "t-scenario", "tool_input": {"check_id": "route-scenario", "memory_id": "a"}}, root=root)
            self.assertEqual(blocked["hookSpecificOutput"]["permissionDecision"], "deny")
            scenario = retrieval_preflight({"tool_name": "mcp__evolving_profile_controller__search_scenario_summary", "tool_use_id": "scenario-1", "session_id": "s-scenario", "turn_id": "t-scenario", "tool_input": {"check_id": "route-scenario", "query": "项目范围"}}, root=root)
            self.assertEqual(scenario["hookSpecificOutput"]["permissionDecision"], "allow")
            observe_tool({"tool_name": "mcp__evolving_profile_controller__search_scenario_summary", "tool_use_id": "scenario-1", "session_id": "s-scenario", "turn_id": "t-scenario", "tool_input": {"check_id": "route-scenario", "query": "项目范围"}, "tool_response": {"content": [{"type": "text", "text": json.dumps({"source": "scenario_context_index", "items": [{"scenario_id": "s1"}]})}]}}, root=root)
            allowed = retrieval_preflight({"tool_name": "mcp__evolving_profile_controller__read_source", "tool_use_id": "source-2", "session_id": "s-scenario", "turn_id": "t-scenario", "tool_input": {"check_id": "route-scenario", "memory_id": "a"}}, root=root)
            self.assertEqual(allowed["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_scenario_completion_gate_blocks_once_then_releases_after_tool(self):
        with tempfile.TemporaryDirectory() as root:
            register_route({"invocation_id": "route-stop-scenario", "session_id": "s-stop", "turn_id": "t-stop", "raw_prompt": "比较不同项目的Agent修复过程", "required_ep_tool": "mcp__evolving_profile_controller__agent_recall"}, root=root)
            declare("route-stop-scenario", "比较不同项目的Agent修复过程", "required", "需要过程经验", root=root)
            observe_tool({"tool_name": "mcp__evolving_profile_controller__agent_recall", "tool_use_id": "agent-stop", "session_id": "s-stop", "turn_id": "t-stop", "tool_input": {"check_id": "route-stop-scenario", "query": "修复过程"}, "tool_response": {"content": [{"type": "text", "text": json.dumps({"scenario_followup": {"required": True, "next_tool": "search_scenario_summary"}})}]}}, root=root)
            first = scenario_completion_gate({"session_id": "s-stop", "turn_id": "t-stop"}, enabled=True, root=root)
            self.assertEqual(first["decision"], "block")
            second = scenario_completion_gate({"session_id": "s-stop", "turn_id": "t-stop"}, enabled=True, root=root)
            self.assertEqual(second, {})
            observe_tool({"tool_name": "mcp__evolving_profile_controller__search_scenario_summary", "tool_use_id": "scenario-stop", "session_id": "s-stop", "turn_id": "t-stop", "tool_input": {"check_id": "route-stop-scenario", "query": "项目范围"}, "tool_response": {"content": [{"type": "text", "text": json.dumps({"source": "scenario_context_index", "items": [{"scenario_id": "s1"}]})}]}}, root=root)
            self.assertEqual(scenario_completion_gate({"session_id": "s-stop", "turn_id": "t-stop"}, enabled=True, root=root), {})

    def test_agent_process_id_cannot_be_sent_to_user_source_readback(self):
        with tempfile.TemporaryDirectory() as root:
            register_route({"invocation_id": "route-readback", "session_id": "s-readback", "turn_id": "t-readback", "raw_prompt": "查看过去的Agent修复经验", "required_ep_tool": "mcp__evolving_profile_controller__agent_recall"}, root=root)
            declare("route-readback", "查看过去的Agent修复经验", "required", "需要过程经验", root=root)
            blocked = retrieval_preflight({"tool_name": "mcp__evolving_profile_controller__read_source", "tool_use_id": "source-process", "session_id": "s-readback", "turn_id": "t-readback", "tool_input": {"check_id": "route-readback", "memory_id": "pm_trace_123"}}, root=root)
            self.assertEqual(blocked["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("read_agent_process_memory", blocked["hookSpecificOutput"]["permissionDecisionReason"])

    def test_guard_retries_a_transient_missing_receipt(self):
        calls = []
        def guard(_hook):
            calls.append(True)
            return "EP 回执暂缺" if len(calls) == 1 else None
        with patch.object(pre_tool_use, "local_memory_access_guard", guard, create=True), \
             patch.object(pre_tool_use.time, "sleep"):
            result = pre_tool_use.guard_with_retry({"tool_name": "exec"}, attempts=2)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)

    def test_missing_receipt_reason_is_distinguished_from_policy_block(self):
        with tempfile.TemporaryDirectory() as root:
            hook = {"tool_name": "exec", "session_id": "s", "turn_id": "t",
                    "tool_input": {"command": "rg x /Users/apple/.codex/memories/MEMORY.md"}}
            reason = local_memory_access_guard(hook, root=root)
        self.assertIn("回执暂缺", reason)

    def test_shell_search_of_codex_memory_is_blocked_without_ep_route_receipt(self):
        hook = {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec",
            "tool_use_id": "call-1",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_input": {"command": "rg -n 优优 /Users/apple/.codex/memories/MEMORY.md"},
        }
        output = io.StringIO()
        with patch.object(pre_tool_use, "load_config", return_value={}), \
             patch.object(pre_tool_use.sys, "stdin", io.StringIO(json.dumps(hook))), \
             redirect_stdout(output):
            pre_tool_use.main()

        self.assertIn('"permissionDecision":"deny"', output.getvalue().replace(" ", ""))
        self.assertIn("Evolving Profile", output.getvalue())

    def test_shell_command_that_only_mentions_memory_path_text_is_not_blocked(self):
        hook = {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec",
            "tool_use_id": "call-mention-only",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_input": {"command": "python3 -c 'print(\"/.codex/memories\")'"},
        }
        output = io.StringIO()
        with patch.object(pre_tool_use, "load_config", return_value={}), \
             patch.object(pre_tool_use.sys, "stdin", io.StringIO(json.dumps(hook))), \
             redirect_stdout(output):
            pre_tool_use.main()

        self.assertEqual(output.getvalue(), "")

    def test_route_requires_the_exact_ep_tool_before_native_memory_can_be_used(self):
        with tempfile.TemporaryDirectory() as root:
            register_route({
                "invocation_id": "route-1", "session_id": "session-1", "turn_id": "turn-1",
                "raw_prompt": "我和天津农学院是什么关系？", "required_ep_tool": "mcp__evolving_profile_controller__user_recall",
                "allow_native_memory": False, "recommended_route": "recall",
            }, root=root)
            hook = {
                "tool_name": "exec", "session_id": "session-1", "turn_id": "turn-1",
                "tool_input": {"command": "rg 学校 /Users/apple/.codex/memories/MEMORY.md"},
            }
            reason = local_memory_access_guard(hook, root=root)
            self.assertIn("mcp__evolving_profile_controller__user_recall", reason)

            observe_tool({
                "tool_name": "mcp__evolving_profile_controller__user_recall", "session_id": "session-1",
                "turn_id": "turn-1", "tool_use_id": "ep-call-1", "tool_input": {"query": "天津农学院关系"},
                "tool_response": {"content": [{"type": "text", "text": "{}"}]},
            }, root=root)
            self.assertIsNone(local_memory_access_guard(hook, root=root))

    def test_preference_route_requires_get_preference_not_recall(self):
        with tempfile.TemporaryDirectory() as root:
            register_route({
                "invocation_id": "route-2", "session_id": "session-1", "turn_id": "turn-2",
                "raw_prompt": "按我习惯记录的公文格式有哪些？", "required_ep_tool": "mcp__evolving_profile_controller__user_preference",
                "allow_native_memory": False, "recommended_route": "get_preference",
            }, root=root)
            hook = {"tool_name": "exec", "session_id": "session-1", "turn_id": "turn-2",
                    "tool_input": {"command": "rg 格式 /Users/apple/.codex/memories/MEMORY.md"}}
            observe_tool({"tool_name": "mcp__evolving_profile_controller__user_recall", "session_id": "session-1",
                          "turn_id": "turn-2", "tool_use_id": "wrong-tool", "tool_response": {"content": []}}, root=root)
            self.assertIn("user_preference", local_memory_access_guard(hook, root=root))
            observe_tool({"tool_name": "mcp__evolving_profile_controller__user_preference", "session_id": "session-1",
                          "turn_id": "turn-2", "tool_use_id": "right-tool", "tool_response": {"content": [{"type": "text", "text": "{}"}]}}, root=root)
            self.assertIsNone(local_memory_access_guard(hook, root=root))

    def test_explicit_request_for_native_codex_memory_allows_local_read(self):
        with tempfile.TemporaryDirectory() as root:
            register_route({
                "invocation_id": "route-3", "session_id": "session-1", "turn_id": "turn-3",
                "raw_prompt": "请搜索 Codex 原生 Memory 里的内容", "required_ep_tool": "mcp__evolving_profile_controller__user_recall",
                "allow_native_memory": True, "recommended_route": "recall",
            }, root=root)
            hook = {"tool_name": "exec", "session_id": "session-1", "turn_id": "turn-3",
                    "tool_input": {"command": "rg 记忆 /Users/apple/.codex/memories/MEMORY.md"}}
            self.assertIsNone(local_memory_access_guard(hook, root=root))

    def test_user_prompt_hook_registers_required_ep_tool_before_shell_fallback(self):
        prompt='我说公文和方案里面的格式，你这根据我习惯和要求记录的都有哪几种？'
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HINDSIGHT_TURN_CHECK_ROOT':root,'EVOLVING_PROFILE_STATE_ROOT':root}), \
             patch.object(recall, 'record_prompt_ingress'), patch.object(recall, 'emit_hook_output'), \
             patch.object(recall, 'is_shadow_replay', return_value=False), \
             patch('system_probe.run_probe', return_value=('', {'state':'skipped','calls':0,'candidate_count':None,'returned_count':0,'items':[]})):
            recall.emit_bounded_system_probe(
                {'session_id':'session-2','turn_id':'turn-2','memory_prompt_origin':'user_direct'},
                prompt, {}, 'hook-check-2',
            )
            reason=local_memory_access_guard({
                'tool_name':'exec','session_id':'session-2','turn_id':'turn-2',
                'tool_input':{'command':'rg 格式 /Users/apple/.codex/memories/MEMORY.md'},
            },root=root)
        self.assertIn('user_preference',reason)


if __name__ == "__main__":
    unittest.main()
