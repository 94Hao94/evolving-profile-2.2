import json
import os
import subprocess
import unittest
from pathlib import Path


class McpToolCatalogTest(unittest.TestCase):
    def test_runtime_recovery_tool_is_listed_and_callable(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": ":".join(str(root / part) for part in ("host-adapter", "guidance", "controller", "ham-os")),
            "EVOLVING_PROFILE_GUIDANCE_SRC": str(root / "guidance"),
            "EVOLVING_PROFILE_GUIDANCE_CONFIG": str(Path.home() / ".evolving-profile/guidance-v1/guidance-v1.json"),
        })
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "refresh_runtime_guidance", "arguments": {
                "task": {"objective": "恢复视觉验收", "current_user_message": "恢复视觉验收", "phase": "verify"},
                "runtime_event": {"capability": "computer_use", "failure": "transport_closed", "occurrence": 1, "required_for": "visual_acceptance"},
            }}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "user_preference", "arguments": {
                "memory_policy": "allowed", "loaded": [], "task": {"objective": "偏好", "phase": "understand", "current_constraints": [], "domains": [], "media": [], "resolved_entities": [], "unresolved_references": []},
                "check_id": "catalog-check-1",
            }}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "search_scenario_summary", "arguments": {
                "query": "天津财经大学 商务智能与数据分析", "context_type": "session", "limit": 8,
            }}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "catalog_search", "arguments": {
                "query": "商务智能与数据分析", "scope": "scenarios", "limit": 8,
            }}},
        ]
        completed = subprocess.run(
            [str(Path.home() / ".evolving-profile/runtime/python-3.11/bin/python"), str(root / "host-adapter/evolving_profile_controller_mcp.py")],
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
            check=True,
        )
        rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        tools = {item["name"] for item in next(row for row in rows if row.get("id") == 2)["result"]["tools"]}
        self.assertIn("refresh_runtime_guidance", tools)
        self.assertTrue({'catalog_list','catalog_search','catalog_read','record_evidence_decision','update_task_state','user_recall','user_research','user_preference','agent_recall','agent_research'} <= tools)
        self.assertNotIn('read_context_summary', tools)
        self.assertIn('search_scenario_summary', tools)
        self.assertNotIn('get_task_guidance', tools)
        self.assertNotIn('read_guidance', tools)
        self.assertNotIn('read_guidance_unit', tools)
        self.assertNotIn('search_scenario_contexts', tools)
        self.assertIn('rag_search', tools)
        listed = {item['name']: item for item in next(row for row in rows if row.get('id') == 2)['result']['tools']}
        for tool in ('read_scenario_summary', 'scenario_gate'):
            self.assertIn(tool, listed)
            self.assertIn('check_id', listed[tool]['inputSchema']['properties'])
        self.assertIn('episode_id', listed['read_scenario_summary']['inputSchema']['properties'])
        preference = next(item for item in next(row for row in rows if row.get("id") == 2)["result"]["tools"] if item["name"] == "user_preference")
        self.assertIn("check_id", preference["inputSchema"]["required"])
        self.assertEqual(preference["inputSchema"]["properties"]["check_id"]["type"], "string")
        payload = json.loads(next(row for row in rows if row.get("id") == 3)["result"]["content"][0]["text"])
        self.assertEqual(payload["mode"], "runtime_guidance_refresh")
        self.assertEqual(payload["persistence"], "none_current_turn_only")
        preference_reply = next(row for row in rows if row.get("id") == 4)
        self.assertIn("content", preference_reply["result"])
        scenario = json.loads(next(row for row in rows if row.get("id") == 5)["result"]["content"][0]["text"])
        self.assertEqual(scenario["schema"], "evolving-profile.search-scenario-summary.v1")
        self.assertTrue(scenario["coverage"]["navigation_only"])
        self.assertTrue(all("summary" not in item for item in scenario["items"]))
        scenario_catalog = json.loads(next(row for row in rows if row.get("id") == 6)["result"]["content"][0]["text"])
        self.assertEqual(scenario_catalog["schema"], "evolving-profile.scenario-context-search.v1")
        self.assertTrue(scenario_catalog["coverage"]["navigation_only"])
        self.assertTrue(all("summary" not in item for item in scenario_catalog["items"]))


if __name__ == "__main__":
    unittest.main()
