"""Retry and checkpoint primitives for Codex-side Context model jobs."""

from __future__ import annotations

import time
from datetime import datetime, timezone


RETRYABLE_ERRORS = (TimeoutError, ConnectionError)

SOURCE_WAIT_ERRORS = frozenset({
    'scenario_source_missing', 'scenario_source_empty', 'scenario_source_incomplete',
    'scenario_over_budget', 'scenario_too_many_source_files',
    'scenario_source_changed', 'automated_source_revision_changed',
    'automated_source_coverage_budget_exceeded', 'scenario_episode_first_message_not_user',
})


def summary_failure_class(status, code=None):
    """Separate input eligibility from extraction and permission failures."""
    current = {'protected':'protected', 'complete':'complete',
               'source_not_applicable':'not_applicable', 'waiting_source':'waiting_source',
               'pending':'pending', 'running':'pending'}
    if status in current:
        return current[status]
    if status == 'protected' or code == 'scenario_knowledge_write_prohibited':
        return 'protected'
    if status == 'complete':
        return 'complete'
    if status == 'source_not_applicable' or code == 'scenario_source_no_user_messages':
        return 'not_applicable'
    if status == 'waiting_source' or code in SOURCE_WAIT_ERRORS:
        return 'waiting_source'
    if status in {'failed', 'retrying'}:
        return 'extraction_failed'
    return 'pending' if status in {'pending', 'running'} else 'unknown'


def run_with_retry(worker, *, max_attempts=5, base_delay=1.0, sleep=time.sleep):
    """Run one idempotent job, retrying transient failures with backoff.

    The worker receives the 1-based attempt number. The returned receipt never
    labels a failed job as generated; callers can checkpoint it and resume.
    """
    attempts = []
    max_attempts = max(1, int(max_attempts))
    for attempt in range(1, max_attempts + 1):
        started = datetime.now(timezone.utc).isoformat()
        try:
            result = worker(attempt)
            return {"status": "succeeded", "attempt": attempt, "attempts": attempts + [{"attempt": attempt, "status": "succeeded", "started_at": started}], "result": result}
        except Exception as error:  # model adapters may expose provider-specific transient errors
            retryable = isinstance(error, RETRYABLE_ERRORS) or bool(getattr(error, "retryable", False))
            record = {"attempt": attempt, "status": "failed", "retryable": retryable, "error": type(error).__name__, "started_at": started}
            attempts.append(record)
            if not retryable or attempt >= max_attempts:
                return {"status": "failed", "attempt": attempt, "attempts": attempts, "error": type(error).__name__}
            sleep(max(0.0, float(base_delay)) * (2 ** (attempt - 1)))
    return {"status": "failed", "attempt": max_attempts, "attempts": attempts, "error": "retry_loop_exhausted"}
