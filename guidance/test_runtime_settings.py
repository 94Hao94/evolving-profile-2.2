import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_settings import DEFAULT_SETTINGS, load_runtime_settings, module_enabled, route_policy


class RuntimeSettingsTest(unittest.TestCase):
    def test_missing_relevance_settings_normalize_without_enabling_rag(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "settings.json"
            path.write_text(json.dumps({"rag": {"enabled": False}, "custom": {"keep": 1}}), encoding="utf-8")
            value = load_runtime_settings(path)
        self.assertEqual(value["recall_policy"], {"default_min_relevance": "weak", "user_memory": "inherit", "agent_memory": "inherit", "advanced": {"allow_transferable_methods": True, "allow_background": True, "historical_mode": "reference_only", "scope_unknown_mode": "keep_navigation", "adaptive_enabled": True}})
        self.assertEqual(value["rag"]["minimum_relevance"], "weak")
        self.assertFalse(value["rag"]["enabled"])
        self.assertEqual(value["custom"], {"keep": 1})

    def test_invalid_explicit_relevance_setting_does_not_fall_back_to_weak(self):
        for override in ({"recall_policy": {"default_min_relevance": "strict"}}, {"recall_policy": []}, {"recall_policy": None}, {"rag": {"minimum_relevance": "inherit"}}, [], {"providers": {"fallbacks": {}}}):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "settings.json"
                path.write_text(json.dumps(override), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_runtime_settings(path)

    def test_malformed_settings_file_does_not_silently_broaden_policy(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "settings.json"
            path.write_text('{"recall_policy":', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_runtime_settings(path)

    def test_defaults_keep_ep_modules_on_and_external_rag_off(self):
        with tempfile.TemporaryDirectory() as root, patch("runtime_settings.SETTINGS_PATH", Path(root) / "settings.json"):
            value = load_runtime_settings()
        self.assertTrue(value["modules"]["facts"]["retrieve"])
        self.assertTrue(value["modules"]["scenario_summary"]["retrieve"])
        self.assertTrue(value["modules"]["agent_process_memory"]["record"])
        self.assertTrue(value["modules"]["agent_process_memory"]["retrieve"])
        self.assertTrue(value["modules"]["agent_process_memory"]["inject"])
        for name in ("agent_process_trajectory", "agent_process_observation", "agent_process_failure_episode", "agent_process_repair_pattern", "agent_process_capability", "agent_process_strategy", "agent_process_revalidation"):
            self.assertTrue(value["modules"][name]["record"])
        self.assertFalse(value["rag"]["enabled"])
        self.assertEqual(value["routing"]["mode"], "auto")

    def test_disabled_module_is_reported_without_mutating_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "settings.json"
            path.write_text(json.dumps({"modules": {"preferences": {"retrieve": False}}}), encoding="utf-8")
            with patch("runtime_settings.SETTINGS_PATH", path):
                value = load_runtime_settings()
        self.assertFalse(module_enabled(value, "preferences", "retrieve"))
        self.assertTrue(module_enabled(value, "facts", "retrieve"))

    def test_route_policy_never_enables_rag_when_disabled(self):
        value = dict(DEFAULT_SETTINGS)
        value["rag"] = {**DEFAULT_SETTINGS["rag"], "enabled": False}
        value["routing"] = {**DEFAULT_SETTINGS["routing"], "mode": "both_isolated"}
        policy = route_policy(value, request_source="auto")
        self.assertEqual(policy["sources"], ["ep"])
        self.assertFalse(policy["rag_enabled"])

    def test_provider_profiles_keep_fallbacks_out_of_ep_module_switches(self):
        with tempfile.TemporaryDirectory() as root:
            value = load_runtime_settings(Path(root) / "settings.json")
        value["providers"]["fallbacks"] = [{"name": "backup", "api_key": "secret"}]
        self.assertTrue(module_enabled(value, "preferences", "retrieve"))
        self.assertEqual(value["providers"]["fallbacks"][0]["name"], "backup")


if __name__ == "__main__":
    unittest.main()
