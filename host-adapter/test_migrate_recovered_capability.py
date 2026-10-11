import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def migration():
    assert importlib.util.find_spec("migrate_recovered_capability") is not None, "proof-backed migration missing"
    import migrate_recovered_capability
    return migrate_recovered_capability


def fixture(tmp_path, *, requested="recovered", rejected=False):
    profile = {"sample_count": 1, "success_count": 0, "failure_count": 1, "confidence_interval": [0.0, 0.7934567085261071], "confidence": "unknown", "model_family": "Model A", "model_version": None, "task_archetype": "bugfix", "phase": "verify", "time_window": {"start": "original-start", "end": "original-end"}}
    record = {"process_memory_id": "pm_capability_proven", "kind": "capability_observation", "outcome": "ambiguous", "phase": "verify", "task_archetype": ["bugfix"], "model_profile": {"family": "Model A", "version": None}, "verification_evidence": [{"verifier_kind": "automated_test", "status": "passed", "scope": "task_result"}], "source_trace_ids": [], "profile": profile, "created_at": "original-created", "updated_at": "original-updated"}
    other = {**record, "process_memory_id": "pm_capability_unproven", "model_profile": {"family": "Other Model", "version": "2"}}
    store = tmp_path / "store.json"
    store.write_text(json.dumps({"schema": "agent-process-memory.v1", "records": [record, other], "profiles": {"Model A|bugfix|verify": profile}, "updated_at": "original-store-time", "unrelated": {"keep": True}}))
    event = {"type": "event_msg", "payload": {"thread_id": "thread-proof", "turn_id": "turn-proof", "item": {"type": "McpToolCall", "tool": "record_agent_capability_observation", "status": "completed", "arguments": {"outcome": requested, "model_family": "Model A", "task_archetype": "bugfix", "phase": "verify", "verifier_kind": "automated_test", "status": "passed", "check_id": "check-proof"}, "result": {"isError": rejected, "content": [{"type": "text", "text": json.dumps({"status": "recorded", "record": record})}]}}}}
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "other"}}) + "\n" + json.dumps(event) + "\n")
    return store, rollout, record, other


def test_dry_run_proves_exact_receipt_but_does_not_touch_store(tmp_path):
    api = migration()
    store, rollout, _, _ = fixture(tmp_path)
    before = store.read_bytes()
    plan = api.prepare_migration(store, [rollout])
    assert plan["status"] == "dry_run"
    assert len(plan["changes"]) == 1
    assert plan["changes"][0]["record_id"] == "pm_capability_proven"
    assert plan["changes"][0]["proof"]["event_line"] == 2
    assert plan["changes"][0]["proof"]["check_id"] == "check-proof"
    assert store.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["rollout.jsonl", "store.json"]


def test_apply_changes_only_proven_record_and_one_sample_profile_and_backup_rolls_back(tmp_path):
    api = migration()
    store, rollout, original, other = fixture(tmp_path)
    before = store.read_bytes()
    plan = api.prepare_migration(store, [rollout])
    receipt = api.apply_migration(store, [rollout], expected_store_sha256=plan["store_sha256"], expected_plan_sha256=plan["plan_sha256"], backup_root=tmp_path / "backups")
    data = json.loads(store.read_text())
    record = data["records"][0]
    profile = data["profiles"]["Model A|bugfix|verify"]
    assert record["outcome"] == "recovered"
    assert record["requested_outcome"] == "recovered"
    assert record["model_profile"]["version"] is None
    assert record["created_at"] == "original-created"
    assert record["updated_at"] == "original-updated"
    assert record["verification_evidence"] == original["verification_evidence"]
    assert record["source_trace_ids"] == []
    assert data["records"][1] == other
    assert data["updated_at"] == "original-store-time"
    assert data["unrelated"] == {"keep": True}
    assert [profile[key] for key in ("sample_count", "success_count", "failure_count", "first_pass_success_count", "recovery_success_count", "unknown_count", "assessed_count")] == [1, 1, 0, 0, 1, 0, 1]
    assert profile["confidence_interval"] == pytest.approx([0.20654329147389294, 1.0])
    assert profile["time_window"] == original["profile"]["time_window"]
    provenance = record["migration_history"][0]
    assert provenance["prior_record"] == original
    assert provenance["proof"]["rollout_sha256"]
    backup = Path(receipt["backup_path"])
    assert backup.read_bytes() == before
    store.write_bytes(backup.read_bytes())
    assert store.read_bytes() == before


@pytest.mark.parametrize("requested,rejected", [("ambiguous", False), ("recovered", True)])
def test_ambiguous_request_and_rejected_attempt_never_supply_migration_proof(tmp_path, requested, rejected):
    api = migration()
    store, rollout, _, _ = fixture(tmp_path, requested=requested, rejected=rejected)
    assert api.prepare_migration(store, [rollout])["changes"] == []


def test_store_and_evidence_hash_mismatch_block_before_any_mutation(tmp_path):
    api = migration()
    store, rollout, _, _ = fixture(tmp_path)
    plan = api.prepare_migration(store, [rollout])
    data = json.loads(store.read_text()); data["unrelated"]["keep"] = False
    store.write_text(json.dumps(data)); current = store.read_bytes()
    with pytest.raises(ValueError, match="store_sha256_changed"):
        api.apply_migration(store, [rollout], expected_store_sha256=plan["store_sha256"], expected_plan_sha256=plan["plan_sha256"], backup_root=tmp_path / "backups")
    assert store.read_bytes() == current
    assert not (tmp_path / "backups").exists()
    refreshed = api.prepare_migration(store, [rollout])
    rollout.write_text(rollout.read_text() + "\n")
    with pytest.raises(ValueError, match="plan_sha256_changed"):
        api.apply_migration(store, [rollout], expected_store_sha256=refreshed["store_sha256"], expected_plan_sha256=refreshed["plan_sha256"], backup_root=tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_multiple_samples_or_current_state_mismatch_are_not_guessed(tmp_path):
    api = migration()
    store, rollout, _, _ = fixture(tmp_path)
    data = json.loads(store.read_text())
    data["profiles"]["Model A|bugfix|verify"]["sample_count"] = 2
    store.write_text(json.dumps(data))
    assert api.prepare_migration(store, [rollout])["changes"] == []
    data["profiles"]["Model A|bugfix|verify"]["sample_count"] = 1
    data["records"][0]["model_profile"]["version"] = "different"
    store.write_text(json.dumps(data))
    assert api.prepare_migration(store, [rollout])["changes"] == []


def test_cli_defaults_to_dry_run_and_requires_explicit_backup_and_hashes_for_apply(tmp_path):
    api = migration()
    store, rollout, _, _ = fixture(tmp_path)
    before = store.read_bytes()
    result = subprocess.run([sys.executable, str(Path(api.__file__)), "--store", str(store), "--rollout", str(rollout)], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "dry_run"
    denied = subprocess.run([sys.executable, str(Path(api.__file__)), "--store", str(store), "--rollout", str(rollout), "--apply"], capture_output=True, text=True)
    assert denied.returncode != 0
    assert store.read_bytes() == before
