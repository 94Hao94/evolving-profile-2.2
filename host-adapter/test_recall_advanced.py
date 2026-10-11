"""Advanced eligibility is independent of ordinal relevance and truth."""
import pytest

from lib import recall_relevance as api


ADVANCED = {"allow_transferable_methods": True, "allow_background": True,
            "historical_mode": "reference_only", "scope_unknown_mode": "keep_navigation", "adaptive_enabled": True}


def policy(**advanced):
    return api.resolve_min_relevance({"recall_policy": {"advanced": advanced}})


def test_legacy_settings_supply_independent_advanced_defaults():
    assert api.normalize_recall_policy({})["advanced"] == ADVANCED
    assert policy()["effective_level"] == "weak"


@pytest.mark.parametrize("value", [None, [], {"allow_background": "false"}, {"adaptive_enabled": 1}, {"historical_mode": "all"}, {"scope_unknown_mode": "discard"}])
def test_invalid_advanced_values_fail_closed(value):
    with pytest.raises(ValueError):
        api.normalize_recall_policy({"advanced": value})


def test_method_switch_excludes_transfer_but_keeps_same_subject_procedure():
    query = "Orion pagination filtering offset"
    rows = [{"id": "method", "text": "For reporting pagination, filter rows before calculating offset."},
            {"id": "context", "text": "Orion pagination filtering offset: filter rows before calculating offset."}]
    kept, audit = api.apply_relevance_policy(query, rows, policy(allow_transferable_methods=False))
    assert [row["id"] for row in kept] == ["context"]
    assert audit["decisions"][0]["relationship"] == "transferable_method"
    assert audit["decisions"][0]["exclusion_reason"] == "transferable_methods_disabled"


def test_background_switch_does_not_disable_contextual_answers():
    rows = [{"id": "background", "text": "Presentation guidance"}, {"id": "answer", "text": "presentation overflow rendering"}]
    kept, _ = api.apply_relevance_policy("presentation overflow rendering", rows, policy(allow_background=False))
    assert [row["id"] for row in kept] == ["answer"]


def test_historical_truth_is_reference_only_and_current_mode_excludes_it():
    row = {"id": "old", "text": "Orion pagination filtering offset", "time_validity": "historical", "truth_status": "verified"}
    kept, _ = api.apply_relevance_policy("Orion pagination filtering offset", [row], policy())
    assert kept[0]["truth_status"] == "verified"
    assert kept[0]["temporal_role"] == "historical_reference"
    assert kept[0]["execution_eligible"] is False
    assert api.apply_relevance_policy("Orion pagination filtering offset", [row], policy(historical_mode="current_only"))[0] == []


def test_require_scope_verification_keeps_generic_method_and_unknown_is_navigation():
    rows = [{"id": "method", "text": "For reporting pagination, filter rows before calculating offset."},
            {"id": "project", "text": "Orion pagination filtering offset", "scope_status": "unknown"}]
    kept, audit = api.apply_relevance_policy("Orion pagination filtering offset", rows, policy(scope_unknown_mode="require_verified"))
    assert [row["id"] for row in kept] == ["method"]
    assert audit["decisions"][1]["level"] == "strong"
    assert audit["decisions"][1]["exclusion_reason"] == "scope_verification_required"
    navigation, _ = api.apply_relevance_policy("Orion pagination filtering offset", rows[1:], policy())
    assert navigation[0]["evidence_role"] == "navigation_only"


def test_explicit_collection_browse_is_navigation_not_fact_verification():
    kept, _ = api.apply_relevance_policy("Show all my memories", [{"id": "row", "text": "Potato soup"}], policy())
    assert kept[0]["relationship"] == "collection_navigation"
    assert kept[0]["evidence_role"] == "navigation_only"
    assert kept[0]["execution_eligible"] is False


def test_adaptive_paths_obey_floor_preserve_constraints_and_do_not_claim_calls():
    resolved = api.resolve_min_relevance({"recall_policy": {"default_min_relevance": "medium"}}, requested="strong")
    args = {"query": "exact QKM_552", "minimum_relevance": "strong", "primary_context": {"project": "Orion"}, "temporal_window": "2026", "check_id": "c"}
    hint = api.adaptive_recall_hint(resolved, args)
    assert hint["allowed_levels"] == ["strong", "medium"]
    assert hint["allowed_tool_arguments"] == [{**args, "minimum_relevance": "medium"}]
    assert hint["decision_owner"] == "answering_agent"
    assert hint["retrieval_performed"] is False
    assert hint["requires_evidence_gap_decision"] is True
    assert api.adaptive_recall_hint(resolved, args, provider_status="failed")["allowed_tool_arguments"] == []
    assert api.adaptive_recall_hint(policy(adaptive_enabled=False), args)["allowed_tool_arguments"] == []


def test_hint_lists_all_permitted_levels_but_no_expansion_at_broad_floor():
    hint = api.adaptive_recall_hint(policy(), {"query": "photosynthesis"})
    assert hint["allowed_levels"] == ["strong", "medium", "weak"]
    assert hint["expansion_levels"] == []
    assert hint["allowed_tool_arguments"] == []


def test_advanced_cannot_relax_literal_floor_or_hard_permission():
    rows = [{"id": "wrong", "text": "QKM_553"}, {"id": "denied", "text": "QKM_552", "permission_status": "denied"}]
    kept, _ = api.apply_relevance_policy("exact QKM_552", rows, policy())
    assert kept == []


def test_explicit_nested_scope_mismatch_is_hard_gate_in_navigation_mode():
    kept, audit = api.apply_relevance_policy("photosynthesis", [{"id": "wrong", "text": "Photosynthesis converts sunlight into energy.", "scope_verification": {"status": "mismatch"}}], policy())
    assert kept == []
    assert audit["decisions"][0]["level"] == "strong"
    assert audit["decisions"][0]["exclusion_reason"] == "hard_scope_or_permission_denied"


def test_resolved_policy_cannot_forge_a_looser_effective_floor():
    rows = [{"id": "weak", "text": "Presentation guidance"}]
    resolved = api.resolve_min_relevance({"recall_policy": {"default_min_relevance": "strong"}})
    resolved["effective_level"] = "weak"
    assert api.apply_relevance_policy("presentation overflow rendering", rows, resolved)[0] == []


def test_scoped_unknown_gate_does_not_require_identity_for_plain_subjects():
    kept, _ = api.apply_relevance_policy("photosynthesis", [{"id": "plain", "text": "Photosynthesis converts sunlight into energy."}], policy(scope_unknown_mode="require_verified"))
    assert len(kept) == 1


def test_external_rag_keeps_independent_defaults_when_ep_controls_disabled():
    settings = {"recall_policy": {"default_min_relevance": "strong", "advanced": {"allow_background": False, "allow_transferable_methods": False, "scope_unknown_mode": "require_verified", "historical_mode": "current_only", "adaptive_enabled": False}}, "rag": {"enabled": False, "minimum_relevance": "weak"}}
    resolved = api.resolve_min_relevance(settings, "external_rag")
    assert resolved["advanced"] == ADVANCED
    assert resolved["effective_level"] == "weak"
    kept, _ = api.apply_relevance_policy("presentation overflow rendering", [{"id": "background", "text": "Presentation guidance"}], resolved)
    assert len(kept) == 1
    assert settings["rag"]["enabled"] is False
