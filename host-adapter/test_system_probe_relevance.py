import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from system_probe import run_probe, count_tokens


PLAN = {"recommended_route": "recall", "minimum_action": "recall_probe", "query": "presentation overflow rendering",
        "focus_terms": ["presentation"], "candidate_policy": "positive_anchor_overlap", "fallback_route": "research"}
SETTINGS = {"auto_probe": True, "probe_max_tokens": 1200}


class SystemProbeRelevanceTests(unittest.TestCase):
    def test_legacy_caller_reads_current_runtime_policy_from_isolated_state_root_each_time(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'config/runtime-settings.json'
            target.parent.mkdir()
            transport = lambda *args, **kwargs: {"results": [{"id":"weak", "text":"presentation guidance"}]}
            with patch.dict('os.environ', {'EVOLVING_PROFILE_STATE_ROOT':root, 'EVOLVING_PROFILE_RUNTIME_SETTINGS':str(target)}):
                target.write_text(json.dumps({"recall_policy":{"default_min_relevance":"weak"}}))
                _, weak = run_probe(PLAN, SETTINGS, transport, "fixture")
                target.write_text(json.dumps({"recall_policy":{"default_min_relevance":"strong"}}))
                _, strong = run_probe(PLAN, SETTINGS, transport, "fixture")
        self.assertEqual(weak['returned_count'], 1)
        self.assertEqual(strong['returned_count'], 0)
        self.assertEqual(strong['state'], 'filtered_empty')

    def test_runtime_disabled_probe_does_not_call_bank(self):
        for runtime in ({'routing':{'ep_enabled':False}}, {'modules':{'facts':{'retrieve':False}}}):
            with self.subTest(runtime=runtime):
                output, receipt = run_probe(PLAN, SETTINGS, lambda *args, **kwargs:self.fail('disabled runtime must not query'), 'fixture', runtime_settings=runtime)
                self.assertEqual(output, '')
                self.assertEqual(receipt['calls'], 0)
                self.assertEqual(receipt['reason'], 'disabled_by_runtime_settings')
                self.assertEqual(receipt['relevance_audit']['status'], 'not_run')

    def test_global_strong_policy_filters_before_three_preview_budget(self):
        rows = [{"id": f"weak-{index}", "text": "presentation guidance"} for index in range(3)]
        rows.append({"id": "strong", "text": "presentation overflow rendering"})
        transport = lambda *args, **kwargs: {"results": rows}
        weak_output, weak = run_probe(PLAN, SETTINGS, transport, "fixture", runtime_settings={})
        strong_output, strong = run_probe(PLAN, SETTINGS, transport, "fixture", runtime_settings={"recall_policy": {"default_min_relevance": "strong"}})
        self.assertEqual(strong["returned_count"], 1)
        self.assertEqual(strong["items"][0]["id"], "strong")
        self.assertNotIn("weak-", strong_output)
        self.assertEqual(weak["relevance_audit"]["kept_count"], 4)
        self.assertEqual(strong["relevance_audit"]["excluded_count"], 3)
        self.assertEqual(strong["relevance_audit"]["configuration_source"], "global_default")
        self.assertLessEqual(count_tokens(strong_output)[0], 1200)

    def test_user_plane_override_and_full_content_are_used_without_relaxing_anchor_gate(self):
        rows = [{"id": "full", "text": "presentation " + "padding " * 100 + "overflow rendering"},
                {"id": "outside_scope", "text": "overflow rendering"}]
        output, receipt = run_probe(PLAN, SETTINGS, lambda *args, **kwargs: {"results": rows}, "fixture",
            runtime_settings={"recall_policy": {"default_min_relevance": "weak", "user_memory": "strong"}})
        self.assertEqual(receipt["returned_count"], 1)
        self.assertEqual(receipt["items"][0]["id"], "full")
        self.assertEqual(receipt["items"][0]["relevance_level"], "strong")
        self.assertEqual(receipt["admission"]["rejected_count"], 1)
        self.assertEqual(receipt["relevance_audit"]["configuration_source"], "plane_override")
        self.assertNotIn("outside_scope", output)

    def test_generic_content_cannot_gain_relevance_from_metadata(self):
        plan = {**PLAN, "query": "pagination filtering count", "focus_terms": ["tool"]}
        output, receipt = run_probe(plan, SETTINGS, lambda *args, **kwargs: {"results": [
            {"id": "metadata_only", "text": "tool test repair", "metadata": {"path": "pagination filtering count", "query": "pagination filtering count"}}]}, "fixture", runtime_settings={})
        self.assertEqual(receipt["state"], "filtered_empty")
        self.assertEqual(receipt["returned_count"], 0)
        self.assertEqual(receipt["relevance_audit"]["excluded_count"], 1)
        self.assertNotIn("metadata_only", output)

    def test_empty_failure_and_disabled_probe_have_distinct_audits(self):
        _, empty = run_probe(PLAN, SETTINGS, lambda *args, **kwargs: {"results": []}, "fixture", runtime_settings={})
        def failed(*args, **kwargs): raise TimeoutError("fixture")
        _, failure = run_probe(PLAN, SETTINGS, failed, "fixture", runtime_settings={})
        _, disabled = run_probe(PLAN, {**SETTINGS, "auto_probe": False}, failed, "fixture", runtime_settings={})
        self.assertEqual(empty["state"], "empty")
        self.assertEqual(empty["relevance_audit"]["kept_count"], 0)
        self.assertEqual(failure["state"], "unavailable")
        self.assertIsNone(failure["relevance_audit"]["kept_count"])
        self.assertEqual(disabled["calls"], 0)
        self.assertEqual(disabled["relevance_audit"]["status"], "not_run")

    def test_invalid_strict_configuration_does_not_call_or_default_to_weak(self):
        for runtime in ({"recall_policy": {"default_min_relevance": "strict"}}, {"recall_policy": None}, {"recall_policy": []}, []):
            with self.subTest(runtime=runtime):
                calls = []
                _, receipt = run_probe(PLAN, SETTINGS, lambda *args, **kwargs: calls.append(args) or {"results": []}, "fixture", runtime_settings=runtime)
                self.assertEqual(calls, [])
                self.assertEqual(receipt["state"], "unavailable")
                self.assertIsNone(receipt["relevance_audit"]["kept_count"])


if __name__ == "__main__": unittest.main()
