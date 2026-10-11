"""Deterministic A/B and transfer-gate helpers for EP5.1 evaluation."""
from __future__ import annotations

from statistics import mean
import math
from typing import Any


def _avg(rows: list[dict[str, Any]], key: str) -> float:
    values = [float(row.get(key) or 0) for row in rows]
    return mean(values) if values else 0.0


def evaluate_ab(baseline: list[dict[str, Any]], memory: list[dict[str, Any]]) -> dict[str, Any]:
    if len(baseline) != len(memory):
        raise ValueError("evaluation_pair_count_mismatch")
    if not baseline:
        return {"sample_count": 0, "failure_rate_delta": 0.0, "recovery_seconds_delta": 0.0, "tool_calls_delta": 0.0, "context_cost_delta": 0.0}
    verified_pairs = 0
    for before, after in zip(baseline, memory):
        if not isinstance(before.get('failed'), bool) or not isinstance(after.get('failed'), bool):
            raise ValueError('evaluation_outcome_missing')
        if before.get('task_id') != after.get('task_id'):
            raise ValueError('evaluation_task_mismatch')
        for row in (before, after):
            for field in ('recovery_seconds', 'tool_calls', 'tokens'):
                if field in row and (not isinstance(row[field], (int, float)) or not math.isfinite(row[field]) or row[field] < 0):
                    raise ValueError('evaluation_metric_invalid')
        if all(row.get('task_id') and row.get('source_id') and row.get('verifier_kind') in {'automated_test', 'deterministic_tool', 'source_check', 'render_or_visual_check', 'user_acceptance'} and row.get('verifier_status') in {'passed', 'failed', 'accepted'} and row.get('data_kind') == 'real_task' for row in (before, after)):
            verified_pairs += 1
    failure_baseline = mean(1.0 if row.get("failed") else 0.0 for row in baseline)
    failure_memory = mean(1.0 if row.get("failed") else 0.0 for row in memory)
    return {
        "sample_count": len(baseline),
        "verified_receipt_pairs": verified_pairs,
        "negative_transfer_rate": mean(float(not before['failed'] and after['failed']) for before, after in zip(baseline, memory)),
        "improvement_rate": mean(float(before['failed'] and not after['failed']) for before, after in zip(baseline, memory)),
        "failure_rate_delta": round(failure_memory - failure_baseline, 6),
        "recovery_seconds_delta": round(_avg(memory, "recovery_seconds") - _avg(baseline, "recovery_seconds"), 6),
        "tool_calls_delta": round(_avg(memory, "tool_calls") - _avg(baseline, "tool_calls"), 6),
        "context_cost_delta": round(_avg(memory, "tokens") - _avg(baseline, "tokens"), 6),
    }


def transfer_gate(metrics: dict[str, Any], *, min_samples: int = 5, max_negative_transfer: float = 0.1) -> dict[str, Any]:
    samples = int(metrics.get("sample_count") or 0)
    negative = float(metrics.get("negative_transfer_rate") or 0.0)
    improvement = float(metrics.get("improvement_rate") or 0.0)
    if negative > max_negative_transfer:
        status = "blocked"
    elif samples < min_samples or int(metrics.get('verified_receipt_pairs') or 0) < min_samples:
        status = "candidate"
    elif improvement > 0 and negative <= max_negative_transfer:
        status = "verified"
    else:
        status = "candidate"
    return {"status": status, "sample_count": samples, "negative_transfer_rate": negative, "improvement_rate": improvement}


def build_revalidation_queue(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record.get("drift_status") in {"watch", "revalidation_required", "deprecated"}]
