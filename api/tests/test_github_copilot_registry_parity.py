from pathlib import Path
from runpy import run_path

import pytest


def test_all_copied_llm_registries_allow_github_copilot_without_an_api_key(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    registry_files = [root / "host-adapter" / "lib" / "llm.py"]

    monkeypatch.setenv("EVOLVING_PROFILE_API_LLM_PROVIDER", "github-copilot")
    monkeypatch.setenv("EVOLVING_PROFILE_API_LLM_MODEL", "gpt-5.6-terra")
    monkeypatch.delenv("EVOLVING_PROFILE_API_LLM_API_KEY", raising=False)

    assert registry_files
    for path in registry_files:
        namespace = run_path(str(path))
        no_key_required = namespace.get("NO_KEY_REQUIRED")
        assert isinstance(no_key_required, set), f"{path} has no no-key provider registry"
        assert "github-copilot" in no_key_required, f"{path} does not register github-copilot"
        detected = namespace["detect_llm_config"]({})
        assert detected["provider"] == "github-copilot"
        assert detected["api_key"] == ""
        assert detected["model"] == "gpt-5.6-terra"


def test_host_no_key_registry_matches_canonical_api_rules():
    from evolving_profile_api.engine.llm_wrapper import requires_api_key

    root = Path(__file__).resolve().parents[2]
    namespace = run_path(str(root / "host-adapter" / "lib" / "llm.py"))
    registered = {entry["name"] for entry in namespace["PROVIDER_DETECTION"]}
    no_key_required = namespace["NO_KEY_REQUIRED"]
    assert "github-copilot" in registered
    assert no_key_required <= registered
    for provider in registered:
        assert (provider in no_key_required) is (not requires_api_key(provider)), provider


@pytest.mark.parametrize("external_api", [False, True])
def test_oauth_providers_are_not_selected_by_auto_detection(monkeypatch, external_api):
    root = Path(__file__).resolve().parents[2]
    namespace = run_path(str(root / "host-adapter" / "lib" / "llm.py"))
    for name in (
        "EVOLVING_PROFILE_API_LLM_PROVIDER",
        "EVOLVING_PROFILE_API_LLM_MODEL",
        "EVOLVING_PROFILE_API_LLM_API_KEY",
        "EVOLVING_PROFILE_API_LLM_BASE_URL",
        *(entry["key_env"] for entry in namespace["PROVIDER_DETECTION"] if entry["key_env"]),
    ):
        monkeypatch.delenv(name, raising=False)

    if external_api:
        detected = namespace["detect_llm_config"]({"evolvingProfileApiUrl": "http://test-api.invalid"})
        assert detected["provider"] is None
        assert detected["source"] == "external-api-mode-no-llm"
    else:
        with pytest.raises(RuntimeError, match="No LLM configuration found"):
            namespace["detect_llm_config"]({})
