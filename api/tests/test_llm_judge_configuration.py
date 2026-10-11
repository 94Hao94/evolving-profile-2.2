"""Judge authentication must never drift from its configured provider/endpoint."""
from unittest.mock import Mock

import pytest

from tests import llm_judge

PRIMARY = {
    "EVOLVING_PROFILE_API_LLM_PROVIDER": "openai",
    "EVOLVING_PROFILE_API_LLM_MODEL": "qwen-test-model",
    "EVOLVING_PROFILE_API_LLM_BASE_URL": "https://coding.dashscope.aliyuncs.com/v1",
    "EVOLVING_PROFILE_API_LLM_API_KEY": "primary-test-only",
}


def test_unspecified_judge_reuses_the_complete_primary_config_and_reports_non_independence():
    resolved = llm_judge._resolve_judge_configuration(PRIMARY)
    assert resolved["configured"]
    assert resolved["provider"] == "openai"
    assert resolved["model"] == "qwen-test-model"
    assert resolved["base_url"] == PRIMARY["EVOLVING_PROFILE_API_LLM_BASE_URL"]
    assert resolved["api_key"] == "primary-test-only"
    assert resolved["independence"] == "same_model_not_independent"


@pytest.mark.parametrize("provider", ["gemini", "anthropic", "groq"])
def test_other_provider_without_dedicated_credentials_never_borrows_primary_key(provider):
    resolved = llm_judge._resolve_judge_configuration({
        **PRIMARY, "HINDSIGHT_TEST_JUDGE_PROVIDER": provider, "HINDSIGHT_TEST_JUDGE_MODEL": f"{provider}-test-model",
    })
    assert not resolved["configured"]
    assert resolved["api_key"] == ""
    assert "dedicated" in resolved["reason"]


def test_other_provider_with_its_own_key_does_not_inherit_primary_endpoint():
    resolved = llm_judge._resolve_judge_configuration({
        **PRIMARY, "HINDSIGHT_TEST_JUDGE_PROVIDER": "gemini", "HINDSIGHT_TEST_JUDGE_MODEL": "gemini-test-model",
        "GEMINI_API_KEY": "gemini-test-only",
    })
    assert resolved["configured"]
    assert resolved["api_key"] == "gemini-test-only"
    assert resolved["base_url"] == ""
    assert resolved["independence"] == "different_provider"


def test_other_provider_cannot_relabel_primary_key_as_dedicated():
    resolved = llm_judge._resolve_judge_configuration({
        **PRIMARY, "HINDSIGHT_TEST_JUDGE_PROVIDER": "gemini", "HINDSIGHT_TEST_JUDGE_MODEL": "gemini-test-model",
        "HINDSIGHT_TEST_JUDGE_API_KEY": "primary-test-only",
    })
    assert not resolved["configured"]
    assert resolved["api_key"] == ""


@pytest.mark.parametrize("override", [
    {"HINDSIGHT_TEST_JUDGE_BASE_URL": "https://api.openai.com/v1"},
    {"HINDSIGHT_TEST_JUDGE_API_KEY": "different-key-test-only"},
])
def test_same_provider_partial_endpoint_credential_override_fails_closed(override):
    resolved = llm_judge._resolve_judge_configuration({**PRIMARY, **override})
    assert not resolved["configured"]
    assert resolved["api_key"] == ""
    assert "together" in resolved["reason"]


def test_same_provider_complete_override_keeps_endpoint_and_key_together():
    resolved = llm_judge._resolve_judge_configuration({
        **PRIMARY, "HINDSIGHT_TEST_JUDGE_PROVIDER": "openai", "HINDSIGHT_TEST_JUDGE_MODEL": "independent-test-model",
        "HINDSIGHT_TEST_JUDGE_BASE_URL": "https://api.openai.com/v1", "HINDSIGHT_TEST_JUDGE_API_KEY": "openai-test-only",
    })
    assert resolved["configured"]
    assert resolved["base_url"] == "https://api.openai.com/v1"
    assert resolved["api_key"] == "openai-test-only"
    assert resolved["independence"] == "different_model_or_endpoint"


def test_primary_credential_cannot_be_rebound_to_another_same_provider_endpoint():
    resolved = llm_judge._resolve_judge_configuration({
        **PRIMARY, "HINDSIGHT_TEST_JUDGE_BASE_URL": "https://api.openai.com/v1",
        "HINDSIGHT_TEST_JUDGE_API_KEY": "primary-test-only",
    })
    assert not resolved["configured"]
    assert resolved["api_key"] == ""


def test_unconfigured_judge_fails_before_constructing_an_external_client(monkeypatch):
    config = llm_judge._resolve_judge_configuration({
        **PRIMARY, "HINDSIGHT_TEST_JUDGE_PROVIDER": "gemini", "HINDSIGHT_TEST_JUDGE_MODEL": "gemini-test-model",
    })
    factory = Mock()
    monkeypatch.setattr(llm_judge, "_JUDGE_CONFIG", config)
    monkeypatch.setattr(llm_judge, "_judge_instance", None)
    monkeypatch.setattr(llm_judge, "create_llm_provider", factory)
    with pytest.raises(RuntimeError, match="Judge not configured"):
        llm_judge._get_judge()
    factory.assert_not_called()
