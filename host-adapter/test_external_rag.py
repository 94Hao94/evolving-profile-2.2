import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from lib.external_rag import search_external_rag, _jev_review
import urllib.error


class ExternalRagTest(unittest.TestCase):
    def test_raw_vector_scores_do_not_bypass_semantic_policy_and_levels_are_monotonic(self):
        with tempfile.TemporaryDirectory() as root:
            for name, text in {"strong": "presentation overflow rendering", "medium": "presentation overflow", "weak": "presentation guidance", "none": "Potato soup"}.items():
                Path(root, f"{name}.md").write_text(text, encoding="utf-8")
            settings = {"rag": {"enabled": True, "root_path": root, "minimum_relevance": "weak", "rerank_enabled": False},
                        "routing": {"external_rag_enabled": True}, "retrieval_models": {"embedding": {"model": "fixture"}}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings), patch("lib.external_rag._vector_scores", return_value=([0.99] * 4, "fixture")):
                weak = search_external_rag("presentation overflow rendering")
                medium = search_external_rag("presentation overflow rendering", min_relevance="medium")
                strong = search_external_rag("presentation overflow rendering", min_relevance="strong")
                settings["rag"]["minimum_relevance"] = "strong"
                refused_loosening = search_external_rag("presentation overflow rendering", min_relevance="weak")
        self.assertEqual({Path(item["path"]).stem for item in weak["items"]}, {"strong", "medium", "weak"})
        self.assertEqual({Path(item["path"]).stem for item in medium["items"]}, {"strong", "medium"})
        self.assertEqual({Path(item["path"]).stem for item in strong["items"]}, {"strong"})
        self.assertEqual({Path(item["path"]).stem for item in refused_loosening["items"]}, {"strong"})
        self.assertEqual(weak["relevance_policy"]["level_counts"], {"strong": 1, "medium": 1, "weak": 1, "none": 1, "unknown": 0})
        self.assertFalse(refused_loosening["relevance_policy"]["requested_applied"])

    def test_enabled_rag_without_root_does_not_scan_current_directory(self):
        settings = {"rag": {"enabled": True, "root_path": ""}, "routing": {"external_rag_enabled": True}}
        with patch("lib.external_rag.load_runtime_settings", return_value=settings), patch("lib.external_rag._read_file", side_effect=AssertionError("no root must not scan")):
            value = search_external_rag("query")
        self.assertEqual(value["status"], "root_unavailable")
        self.assertIsNone(value["relevance_policy"]["kept_count"])

    def test_relevance_admission_has_no_hidden_fifty_result_cap(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(64):
                Path(root, f"policy-{index:03}.md").write_text("预算条款明确列出采购支出", encoding="utf-8")
            settings = {"rag": {"enabled": True, "root_path": root, "minimum_relevance": "weak", "vector_enabled": False, "rerank_enabled": False},
                        "routing": {"external_rag_enabled": True}, "recall_policy": {"default_min_relevance": "strong"}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings):
                value = search_external_rag("预算条款", limit=64)
        self.assertEqual(len(value["items"]), 64)
        self.assertEqual(value["relevance_policy"]["effective_level"], "weak")
        self.assertEqual(value["relevance_policy"]["kept_count"], 64)
        self.assertEqual(sum(value["relevance_policy"]["level_counts"].values()), 64)
        self.assertTrue(all(item["relevance_level"] in {"strong", "medium", "weak"} for item in value["items"]))

    def test_relevance_classifies_all_discovered_chunks_before_result_pagination(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(9):
                Path(root, f"policy-{index}.md").write_text("预算条款明确列出采购支出", encoding="utf-8")
            settings = {"rag": {"enabled": True, "root_path": root, "minimum_relevance": "weak", "vector_enabled": False, "rerank_enabled": False},
                        "routing": {"external_rag_enabled": True}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings):
                value = search_external_rag("预算条款", limit=2)
        self.assertEqual(len(value["items"]), 2)
        self.assertEqual(value["relevance_policy"]["kept_count"], 9)
        self.assertEqual(value["relevance_policy"]["returned_count"], 2)
        self.assertEqual(value["relevance_policy"]["pagination_omitted_count"], 7)

    def test_exact_document_identifier_cannot_be_broadened_by_weak_rag_policy(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "other.md").write_text("TJBD-2026-A-169 招标预算条款", encoding="utf-8")
            Path(root, "target.md").write_text("TJBD-2026-A-168 招标预算条款", encoding="utf-8")
            settings = {"rag": {"enabled": True, "root_path": root, "minimum_relevance": "weak", "vector_enabled": False, "rerank_enabled": False},
                        "routing": {"external_rag_enabled": True}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings):
                value = search_external_rag("精确查找 TJBD-2026-A-168 招标预算条款")
        self.assertEqual([Path(item["path"]).name for item in value["items"]], ["target.md"])
        self.assertEqual(value["relevance_policy"]["excluded_count"], 1)

    def test_rrf_score_is_separate_from_lexical_vector_and_semantic_relevance(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "policy.md").write_text("预算条款", encoding="utf-8")
            settings = {"rag": {"enabled": True, "root_path": root, "fusion": "rrf", "minimum_relevance": "weak", "rerank_enabled": False},
                        "routing": {"external_rag_enabled": True}, "retrieval_models": {"embedding": {"model": "fixture"}}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings), patch("lib.external_rag._vector_scores", return_value=([0.9], "fixture")):
                value = search_external_rag("预算条款")
        item = value["items"][0]
        self.assertEqual(item["score"], 1.0)
        self.assertEqual(item["vector_score"], 0.9)
        self.assertAlmostEqual(item["fusion_score"], 2 / 61)
        self.assertEqual(item["relevance_level"], "strong")
        self.assertFalse(value["retrieval"]["legacy_score_threshold"]["applied"])

    def test_jev_authentication_uses_official_endpoint_not_stale_config(self):
        settings = {"retrieval_models": {"judge": {"enabled": True, "mode_policy": "assist", "api_key": "fixture-key", "base_url": "https://jevmodel.org/v1/systemone"}}}
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"model":"jev-1.13.0","answers":{"keep_0":{"type":"noul","noul":0.97},"risk":{"type":"choice","choice":"low"}},"usage":{"input_tokens":20,"output_tokens":2}}'
        def provider(request, **kwargs):
            if request.full_url != "https://api.typesafe.ai/v1/systemone":
                raise urllib.error.HTTPError(request.full_url, 401, "wrong credential issuer", {}, None)
            return response
        with patch("lib.external_rag.urllib.request.urlopen", side_effect=provider):
            result = _jev_review("预算", [{"id": "rag:1", "path": "budget.md", "score": 1}], settings)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["keep_ids"], ["rag:1"])

    def test_disabled_rag_never_reads_ep_or_files(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "note.md").write_text("外部资料", encoding="utf-8")
            settings = {"rag": {"enabled": False, "root_path": root}, "routing": {"external_rag_enabled": False}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings):
                value = search_external_rag("外部资料")
        self.assertEqual(value["status"], "disabled_by_runtime_settings")
        self.assertFalse(value["ep_accessed"])

    def test_enabled_rag_returns_only_external_file_sources(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "policy.md").write_text("外部政策文件的预算条款", encoding="utf-8")
            settings = {"rag": {"enabled": True, "root_path": root, "rerank_enabled": True}, "routing": {"external_rag_enabled": True}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings):
                value = search_external_rag("预算条款")
        self.assertEqual(value["status"], "ok")
        self.assertTrue(value["items"])
        self.assertTrue(all(item["source"] == "external_rag" for item in value["items"]))
        self.assertFalse(value["ep_accessed"])

    def test_rag_uses_bound_retrieval_profiles(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "policy.md").write_text("外部政策文件的预算条款", encoding="utf-8")
            settings = {
                "rag": {"enabled": True, "root_path": root, "vector_enabled": True,
                        "rerank_enabled": True, "embedding_profile_id": "embedding-default",
                        "reranker_profile_id": "reranker-default"},
                "routing": {"external_rag_enabled": True},
                "retrieval_models": {
                    "embedding": {"enabled": True, "profile_id": "embedding-default", "model": "local-embedding"},
                    "reranker": {"enabled": True, "profile_id": "reranker-default", "model": "local-reranker"},
                },
            }
            with patch("lib.external_rag.load_runtime_settings", return_value=settings), \
                 patch("lib.external_rag._vector_scores", return_value=([0.9], "test-vector")) as vectors, \
                 patch("lib.external_rag._rerank", side_effect=lambda query, items, model: (items, model)) as rerank:
                value = search_external_rag("预算条款")
        self.assertEqual(vectors.call_args.args[2], "local-embedding")
        self.assertEqual(rerank.call_args.args[2], "local-reranker")
        self.assertEqual(value["retrieval"]["embedding_profile_id"], "embedding-default")
        self.assertEqual(value["retrieval"]["reranker_profile_id"], "reranker-default")

    def test_rag_resolves_non_active_profile_by_id(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "policy.md").write_text("外部政策文件的预算条款", encoding="utf-8")
            settings = {
                "rag": {"enabled": True, "root_path": root, "vector_enabled": True,
                        "rerank_enabled": True, "embedding_profile_id": "embedding-bge",
                        "reranker_profile_id": "rerank-mini"},
                "routing": {"external_rag_enabled": True},
                "retrieval_models": {
                    "embedding": {"enabled": True, "profile_id": "embedding-default", "model": "active"},
                    "reranker": {"enabled": True, "profile_id": "reranker-default", "model": "active-rerank"},
                    "embedding_profiles": [{"enabled": True, "profile_id": "embedding-bge", "model": "bge-m3"}],
                    "reranker_profiles": [{"enabled": True, "profile_id": "rerank-mini", "model": "mini-reranker"}],
                },
            }
            with patch("lib.external_rag.load_runtime_settings", return_value=settings), \
                 patch("lib.external_rag._vector_scores", return_value=([0.9], "test-vector")) as vectors, \
                 patch("lib.external_rag._rerank", side_effect=lambda query, items, model: (items, model)) as rerank:
                search_external_rag("预算条款")
        self.assertEqual(vectors.call_args.args[2], "bge-m3")
        self.assertEqual(rerank.call_args.args[2], "mini-reranker")

    def test_jev_shadow_reviews_candidates_without_filtering_them(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "policy.md").write_text("预算条款和采购周期", encoding="utf-8")
            settings = {
                "rag": {"enabled": True, "root_path": root, "vector_enabled": False, "rerank_enabled": False},
                "routing": {"external_rag_enabled": True},
                "retrieval_models": {"embedding": {"enabled": False}, "reranker": {"enabled": False},
                    "judge": {"enabled": True, "mode_policy": "shadow", "base_url": "https://jev.example", "model": "jev-1", "api_key": "secret"}},
            }
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"model":"jev-latest","answers":{"keep_0":{"type":"noul","noul":0.9},"risk":{"type":"choice","choice":"low"}},"usage":{"input_tokens":42}}'
            with patch("lib.external_rag.load_runtime_settings", return_value=settings), patch("lib.external_rag.urllib.request.urlopen", return_value=response) as call:
                value = search_external_rag("预算条款")
        self.assertEqual(value["retrieval"]["judge"]["status"], "ok")
        self.assertEqual(value["retrieval"]["judge"]["calls"], 1)
        self.assertTrue(value["items"])
        self.assertEqual(call.call_count, 1)

    def test_jev_enforce_rejects_only_explicitly_rejected_ids(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "policy.md").write_text("预算条款", encoding="utf-8")
            settings = {
                "rag": {"enabled": True, "root_path": root, "vector_enabled": False, "rerank_enabled": False},
                "routing": {"external_rag_enabled": True},
                "retrieval_models": {"embedding": {"enabled": False}, "reranker": {"enabled": False},
                    "judge": {"enabled": True, "mode_policy": "enforce", "base_url": "https://jev.example", "model": "jev-1", "api_key": "secret"}},
            }
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"model":"jev-latest","answers":{"keep_0":{"type":"noul","noul":0.9},"risk":{"type":"choice","choice":"medium"}}}'
            with patch("lib.external_rag.load_runtime_settings", return_value=settings), patch("lib.external_rag.urllib.request.urlopen", return_value=response):
                value = search_external_rag("预算条款")
        self.assertEqual(value["retrieval"]["judge"]["status"], "ok")
        # The test response targets a non-existent id; deterministic fallback keeps the candidate.
        self.assertTrue(value["items"])

    def test_jev_missing_credentials_falls_back_without_a_network_call(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "policy.md").write_text("预算条款", encoding="utf-8")
            settings = {"rag": {"enabled": True, "root_path": root, "vector_enabled": False, "rerank_enabled": False},
                        "routing": {"external_rag_enabled": True},
                        "retrieval_models": {"embedding": {"enabled": False}, "reranker": {"enabled": False},
                            "judge": {"enabled": True, "mode_policy": "assist", "base_url": "", "model": "", "api_key": ""}}}
            with patch("lib.external_rag.load_runtime_settings", return_value=settings), patch("lib.external_rag.urllib.request.urlopen") as call:
                value = search_external_rag("预算条款")
        self.assertEqual(value["retrieval"]["judge"]["status"], "not_configured")
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
