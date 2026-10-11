#!/usr/bin/env python3
"""Repair only receipt-proven recovery misclassification; preview by default.

Apply requires the reviewed store and plan SHA256 plus an explicit backup
directory. Run with store writers quiescent: other writers do not participate
in this script's precondition checks. No phase/model/version inference, no
source-trace links synthesized from proximity, and no model or network calls.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.process_memory import _verifier_is_independent

SCHEMA = "evolving-profile.recovered-capability-migration.v1"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _profile_key(family: Any, archetype: Any, phase: Any, version: Any) -> str:
    return "|".join(str(value or "unknown") for value in (family, archetype, phase)) + ("|" + str(version) if version else "")


def _record_key(record: dict) -> str:
    model = record.get("model_profile") or {}
    archetypes = record.get("task_archetype") or []
    return _profile_key(model.get("family"), archetypes[0] if len(archetypes) == 1 else None, record.get("phase"), model.get("version"))


def _proofs(path: Path, raw: bytes):
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            event = json.loads(line)
            payload = event.get("payload") or {}
            item = payload.get("item") or {}
            args, result = item.get("arguments") or {}, item.get("result") or {}
            if event.get("type") != "event_msg" or item.get("type") != "McpToolCall" or item.get("tool") != "record_agent_capability_observation" or item.get("status") != "completed" or result.get("isError"):
                continue
            if args.get("outcome") != "recovered" or args.get("status") != "passed" or not _verifier_is_independent(args):
                continue
            for block in result.get("content") or []:
                if block.get("type") != "text":
                    continue
                receipt = json.loads(block["text"])
                record = receipt.get("record") or {}
                if receipt.get("status") != "recorded" or record.get("kind") != "capability_observation" or not record.get("process_memory_id"):
                    continue
                yield args, record, {"rollout_path": str(path), "rollout_sha256": sha256(raw), "event_line": line_number, "check_id": args.get("check_id"), "thread_id": payload.get("thread_id"), "turn_id": payload.get("turn_id"), "receipt_kind": "accepted_capability_observation_mcp_result", "requested_outcome": "recovered", "source_trace_linkage": "not_inferred_from_turn_proximity"}
        except (ValueError, TypeError, AttributeError, KeyError):
            continue


def prepare_migration(store_path: str | Path, rollout_paths: list[str | Path]) -> dict:
    store_path = Path(store_path).resolve()
    store_raw = store_path.read_bytes()
    data = json.loads(store_raw)
    if not isinstance(data, dict) or not isinstance(data.get("records"), list) or not isinstance(data.get("profiles"), dict):
        raise ValueError("store_schema_invalid")
    records = [row for row in data["records"] if isinstance(row, dict)]
    by_id: dict[str, list[dict]] = {}
    for record in records:
        by_id.setdefault(record.get("process_memory_id"), []).append(record)
    changes, skipped, sources, seen = [], [], [], set()
    for value in rollout_paths:
        path = Path(value).resolve(); raw = path.read_bytes()
        sources.append({"path": str(path), "sha256": sha256(raw)})
        for args, receipt_record, proof in _proofs(path, raw):
            rid = receipt_record["process_memory_id"]
            if rid in seen:
                continue
            seen.add(rid)
            matches = by_id.get(rid) or []
            reason = None
            record = matches[0] if len(matches) == 1 else {}
            model = record.get("model_profile") or {}
            key = _profile_key(args.get("model_family"), args.get("task_archetype"), args.get("phase"), args.get("model_version"))
            profile = data["profiles"].get(key) or {}
            if len(matches) != 1:
                reason = "current_record_missing_or_duplicate"
            elif record.get("outcome") != "ambiguous" or receipt_record.get("outcome") != "ambiguous" or record.get("requested_outcome") not in (None, "recovered"):
                reason = "current_or_receipt_outcome_not_legacy_ambiguous"
            elif record.get("kind") != "capability_observation" or model.get("family") != args.get("model_family") or model.get("version") != args.get("model_version") or record.get("phase") != args.get("phase") or record.get("task_archetype") != [args.get("task_archetype")]:
                reason = "record_scope_does_not_match_accepted_arguments"
            elif any(record.get(field) != receipt_record.get(field) for field in ("model_profile", "task_archetype", "phase", "verification_evidence", "created_at")):
                reason = "current_record_differs_from_accepted_receipt"
            elif not any(isinstance(item, dict) and _verifier_is_independent(item) for item in record.get("verification_evidence") or []):
                reason = "independent_verifier_missing"
            elif any(profile.get(field) != expected for field, expected in (("sample_count", 1), ("success_count", 0), ("failure_count", 1))) or any(profile.get(field, 0) != 0 for field in ("first_pass_success_count", "recovery_success_count", "unknown_count")):
                reason = "profile_not_exact_legacy_one_failure_sample"
            elif sum(row.get("kind") == "capability_observation" and _record_key(row) == key for row in records) != 1:
                reason = "profile_has_additional_or_ambiguous_record_scope"
            if reason:
                skipped.append({"record_id": rid, "reason": reason})
                continue
            repaired = copy.deepcopy(profile)
            z = 1.96
            center = (1 + z * z / 2) / (1 + z * z)
            margin = z * math.sqrt(z * z / 4) / (1 + z * z)
            repaired.update({"sample_count": 1, "success_count": 1, "failure_count": 0, "first_pass_success_count": 0, "recovery_success_count": 1, "unknown_count": 0, "assessed_count": 1, "confidence_interval": [max(0.0, center - margin), min(1.0, center + margin)], "confidence": "unknown"})
            changes.append({"record_id": rid, "profile_key": key, "proof": proof, "prior_record_sha256": sha256(_canonical(record)), "prior_profile_sha256": sha256(_canonical(profile)), "replacement_profile": repaired})
    plan = {"schema": SCHEMA, "status": "dry_run", "store_path": str(store_path), "store_sha256": sha256(store_raw), "evidence_sources": sources, "changes": changes, "skipped": skipped, "source_linkage": "no_new_source_trace_ids", "requires_quiescent_writers": True}
    plan["plan_sha256"] = sha256(_canonical(plan))
    return plan


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600, exclusive: bool = False) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".recovery-migration-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)  # Never overwrite an existing backup.
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_migration(store_path: str | Path, rollout_paths: list[str | Path], *, expected_store_sha256: str, expected_plan_sha256: str, backup_root: str | Path) -> dict:
    if not expected_store_sha256 or not expected_plan_sha256 or not backup_root:
        raise ValueError("apply_requires_reviewed_hashes_and_backup_root")
    plan = prepare_migration(store_path, rollout_paths)
    if plan["store_sha256"] != expected_store_sha256:
        raise ValueError("store_sha256_changed")
    if plan["plan_sha256"] != expected_plan_sha256:
        raise ValueError("plan_sha256_changed")
    if not plan["changes"]:
        return {"schema": SCHEMA, "status": "no_provable_changes", "changed_count": 0}
    store_path = Path(store_path).resolve()
    original = store_path.read_bytes()
    if sha256(original) != expected_store_sha256:
        raise ValueError("store_sha256_changed")
    data = json.loads(original)
    now = datetime.now(timezone.utc).isoformat()
    for change in plan["changes"]:
        record = next(row for row in data["records"] if row.get("process_memory_id") == change["record_id"])
        prior_record = copy.deepcopy(record)
        prior_profile = copy.deepcopy(data["profiles"][change["profile_key"]])
        if sha256(_canonical(record)) != change["prior_record_sha256"] or sha256(_canonical(prior_profile)) != change["prior_profile_sha256"]:
            raise ValueError("record_or_profile_precondition_changed")
        record.update(outcome="recovered", requested_outcome="recovered", profile=copy.deepcopy(change["replacement_profile"]))
        record.setdefault("migration_history", []).append({"schema": SCHEMA, "migrated_at": now, "proof": change["proof"], "prior_record": prior_record, "prior_profile": prior_profile, "reviewed_plan_sha256": expected_plan_sha256})
        data["profiles"][change["profile_key"]] = copy.deepcopy(change["replacement_profile"])
    backup_root = Path(backup_root).resolve(); backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / (store_path.name + "." + uuid.uuid4().hex + ".backup.json")
    _atomic_write(backup, original, exclusive=True)
    # Guard again after preparing the recoverable backup, just before replace.
    if sha256(store_path.read_bytes()) != expected_store_sha256 or any(sha256(Path(source["path"]).read_bytes()) != source["sha256"] for source in plan["evidence_sources"]):
        raise ValueError("store_or_evidence_changed_before_replace")
    updated = json.dumps(data, ensure_ascii=False, indent=2).encode() + b"\n"
    _atomic_write(store_path, updated, mode=store_path.stat().st_mode & 0o777)
    return {"schema": SCHEMA, "status": "applied", "changed_count": len(plan["changes"]), "changed_record_ids": [change["record_id"] for change in plan["changes"]], "backup_path": str(backup), "backup_sha256": sha256(original), "store_sha256": sha256(updated), "reviewed_plan_sha256": expected_plan_sha256}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--rollout", action="append", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root")
    parser.add_argument("--expected-store-sha256")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args()
    try:
        result = apply_migration(args.store, args.rollout, expected_store_sha256=args.expected_store_sha256, expected_plan_sha256=args.expected_plan_sha256, backup_root=args.backup_root) if args.apply else prepare_migration(args.store, args.rollout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({"schema": SCHEMA, "status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
