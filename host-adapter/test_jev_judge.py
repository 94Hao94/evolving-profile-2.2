import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.jev_judge import review, caller_view


class JevJudgeTests(unittest.TestCase):
    def settings(self, mode="assist"):
        return {"rag": {"enabled": False}, "retrieval_models": {"judge": {
            "enabled": True, "mode_policy": mode, "api_key": "fixture-secret",
            "scopes": {"internal_memory": True, "quality_diagnosis": True, "external_rag": True},
            "risk_gate_enabled": False,
        }}}

    def response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"model": "jev-1.13.0", "answers": {
            "evidence": {"type": "choice", "choice": "needs_source", "confidence": 0.8},
            "scope": {"type": "choice", "choice": "unknown", "confidence": 0.7},
            "diagnosis": {"type": "choice", "choice": "delivery_unknown", "confidence": 0.9},
        }, "usage": {"input_tokens": 25, "output_tokens": 5}}).encode()
        return response

    def test_internal_memory_works_with_rag_off_and_does_not_repeat_cached_request(self):
        with tempfile.TemporaryDirectory() as root, patch("lib.jev_judge.urllib.request.urlopen", return_value=self.response()) as call:
            result = review(["internal_memory", "quality_diagnosis"], {"tool": "user_recall", "returned_count": 2}, self.settings(), root=Path(root))
            again = review(["internal_memory", "quality_diagnosis"], {"tool": "user_recall", "returned_count": 2}, self.settings(), root=Path(root))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answers"]["evidence"]["choice"], "needs_source")
        self.assertTrue(again["cache_hit"])
        self.assertEqual(again["calls"], 0)
        self.assertEqual(call.call_count, 1)

    def test_scope_off_never_calls_provider(self):
        settings = self.settings()
        settings["retrieval_models"]["judge"]["scopes"]["internal_memory"] = False
        with patch("lib.jev_judge.urllib.request.urlopen") as call:
            result = review(["internal_memory"], {}, settings)
        self.assertEqual(result["status"], "disabled")
        call.assert_not_called()

    def test_risk_is_off_by_default_even_when_judge_is_on(self):
        with patch("lib.jev_judge.urllib.request.urlopen") as call:
            result = review(["operation_risk"], {"irreversible": True}, self.settings())
        self.assertEqual(result["status"], "disabled")
        call.assert_not_called()

    def test_shadow_answers_are_audit_only_not_agent_advice(self):
        with tempfile.TemporaryDirectory() as root, patch("lib.jev_judge.urllib.request.urlopen", return_value=self.response()):
            result = review(["internal_memory"], {}, self.settings("shadow"), root=Path(root))
        public = caller_view(result)
        self.assertNotIn("answers", public)
        self.assertEqual(public["effect"], "audit_only")

    def test_network_failure_returns_rules_without_changing_memory(self):
        with tempfile.TemporaryDirectory() as root, patch("lib.jev_judge.urllib.request.urlopen", side_effect=urllib.error.URLError("fixture")):
            result = review(["internal_memory", "quality_diagnosis"], {"binding_state": "unbound_missing_check_id", "returned_count": 2}, self.settings(), root=Path(root))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["fallback"], "rules")
        self.assertEqual(result["answers"]["diagnosis"]["choice"], "binding_unknown")
        self.assertFalse(result["memory_mutated"])

    def test_credentials_are_removed_from_review_state(self):
        with tempfile.TemporaryDirectory() as root, patch("lib.jev_judge.urllib.request.urlopen", return_value=self.response()) as call:
            review(["internal_memory"], {"api_key": "private-secret", "text": "key apikey_12345678901234567890"}, self.settings(), root=Path(root))
        body = json.loads(call.call_args.args[0].data)
        self.assertNotIn("private-secret", json.dumps(body))
        self.assertNotIn("apikey_12345678901234567890", json.dumps(body))

    def test_risk_review_runs_only_with_its_separate_switch(self):
        settings = self.settings()
        settings["retrieval_models"]["judge"]["risk_gate_enabled"] = True
        response = self.response()
        response.read.return_value = b'{"model":"jev-1.13.0","answers":{"operation":{"type":"choice","choice":"confirm","confidence":0.9}},"usage":{"input_tokens":20}}'
        with tempfile.TemporaryDirectory() as root, patch("lib.jev_judge.urllib.request.urlopen", return_value=response):
            result = review(["operation_risk"], {"irreversible": True}, settings, root=Path(root))
        self.assertTrue(result["requires_confirmation"])
        self.assertFalse(result["memory_mutated"])

    def test_invalid_model_answers_use_rules_not_a_success_claim(self):
        response = self.response()
        response.read.return_value = b'{"answers":{}}'
        with tempfile.TemporaryDirectory() as root, patch("lib.jev_judge.urllib.request.urlopen", return_value=response):
            result = review(["internal_memory"], {}, self.settings(), root=Path(root))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["fallback"], "rules")


if __name__ == "__main__":
    unittest.main()
