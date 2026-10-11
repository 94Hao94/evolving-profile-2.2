"""One semantic source for API, list, detail and flow status rendering."""
from __future__ import annotations

import time

from prompt_origin import ORIGIN_KINDS


def prompt_population_projection(rows, prompt_source='natural', *, detail_id=None):
    """Filter and denominators derive from one classified audit population."""
    requested = str(prompt_source or 'natural').casefold()
    valid = requested in {'natural', 'all', *ORIGIN_KINDS}
    source_filter = requested if valid else 'natural'
    counts = {kind: 0 for kind in ORIGIN_KINDS}
    for row in rows:
        kind = row.get('origin_kind') if row.get('origin_kind') in ORIGIN_KINDS else 'unknown'
        counts[kind] += 1
    selected = list(rows) if detail_id or source_filter == 'all' else [row for row in rows if row.get('origin_kind', 'unknown') == ('human' if source_filter == 'natural' else source_filter)]
    return selected, {'source_filter': source_filter, 'source_filter_status': 'valid' if valid else 'invalid_defaulted',
                      'source_counts': counts, 'natural_total': counts['human'], 'audit_total': len(rows),
                      'statistics_denominator': len(selected), 'classification_scope': 'bounded_prompt_ingress_window',
                      'source_verification_status': 'partial' if counts['unknown'] else 'complete',
                      'source_verification_pending_total': sum(row.get('origin_kind')=='unknown' and (row.get('origin_evidence') or {}).get('reason')=='native_prompt_source_scan_incomplete' for row in rows),
                      'statistics_denominator_scope': 'verified_natural_user_occurrences_only' if source_filter in {'natural','human'} else 'selected_origin_filter_in_audit_window'}


def select_delivery_view(stdout_receipt: dict | None, host_receipt: dict | None) -> dict:
    stdout_ids = (stdout_receipt or {}).get("record_ids") or []
    host_ids = (host_receipt or {}).get("record_ids") if host_receipt else None
    if host_ids is None:
        state = "unknown"
    elif stdout_ids == host_ids and (stdout_receipt or {}).get("content_sha256") == (host_receipt or {}).get("content_sha256"):
        state = "host_tool_response_observed"
    else:
        state = "partial"
    return {"state": state, "stdout_count": len(stdout_ids), "host_count": len(host_ids) if host_ids is not None else None,
            "complete_receipt": state == "host_tool_response_observed", "stdout": stdout_receipt, "host": host_receipt}


def processing_view(queue: dict, now: float | None = None) -> dict:
    pending = list(queue.get("pending") or [])
    now = time.time() if now is None else now
    ages = [max(0, now - row.get("first_seen", now)) for row in pending if isinstance(row, dict)]
    oldest = max(ages, default=0)
    return {"pending": len(pending), "completed": len(queue.get("completed") or []), "oldest_pending_age_seconds": oldest,
            "refresh_due": bool(pending and oldest >= 300), "state": "idle" if not pending else "pending"}


def snapshot(repo, deliveries: list[dict] | None = None, queue: dict | None = None) -> dict:
    units = repo.active_units()
    models = repo.active_models()
    deliveries = deliveries or []
    adoption = "host_tool_response_observed" if any(row.get("state") == "host_tool_response_observed" for row in deliveries) else "unknown"
    incremental = processing_view(queue or {})
    latest_incremental = next((job for job in repo.jobs() if job.get("kind") == "incremental"), None)
    if latest_incremental:
        incremental.update(state=latest_incremental["state"], **latest_incremental["payload"])
    migration_job = next((job for job in repo.jobs() if job.get("kind") == "migration"), None)
    migration = ({"state": migration_job["state"], **migration_job["payload"]} if migration_job else {"state": "not_started"})
    publication_job=next((job for job in repo.jobs() if job.get('kind')=='observation_publication'),None)
    taxonomy_job=next((job for job in repo.jobs() if job.get('kind')=='taxonomy'),None)
    held=int((publication_job or {}).get('payload',{}).get('held',0))
    audit_states={}
    for unit in units:
        state=(unit.get('preference_audit') or {}).get('state','not_reviewed')
        audit_states[state]=audit_states.get(state,0)+1
    return {"schema": "guidance.status.v1", "snapshot_at": time.time(), "source_revision": repo.active_revision(),
            "service_health": "ready", "guidance_health": "ready" if units else "attention", "processing_health": incremental["state"],
            "adoption_health": adoption, "model_use_health": "unknown", "guidance": {"active": len(units), "held": held, "invalid_dependencies": 0, "active_models": len(models)},
            "guidance_policy": {"role": "advisory_reference", "current_prompt_wins": True,
                                "current_authoritative_source_wins": True, "tool_results_and_permissions_win": True,
                                "preference_cannot_authorize_or_force_execution": True,
                                "conflict_action": "report_conflict_and_use_current_evidence; preserve historical preference"},
            "processing": {"incremental": incremental, "migration": migration,
             "taxonomy":({"state":taxonomy_job['state'],**taxonomy_job['payload']} if taxonomy_job else {'state':'not_started'}),
             "observation_publication":({"state":publication_job['state'],**publication_job['payload']} if publication_job else {'state':'not_started'})},
            "preference_audit":{"state_counts":audit_states,"reviewed":sum(v for k,v in audit_states.items() if k!='not_reviewed'),"pending":audit_states.get('needs_review',0)+audit_states.get('not_reviewed',0)}, "deliveries": deliveries}
