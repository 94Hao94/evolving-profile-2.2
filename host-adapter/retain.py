#!/usr/bin/env python3
"""Token-aware auto-retain hook for the Codex Stop event.

Every completed turn is first persisted to a local queue without calling an
LLM. Hindsight retain is submitted only when a project reaches the configured
token threshold. A separate tail worker flushes stale sub-threshold projects.
"""

import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.runtime_paths import ham_source_root

HAM_SOURCE_ROOT = ham_source_root(__file__)
if HAM_SOURCE_ROOT not in sys.path:
    sys.path.insert(0, HAM_SOURCE_ROOT)
try:
    from ham.adapter import emit as ham_emit
except Exception:
    def ham_emit(*_args, **_kwargs):
        return {"attempted": False, "reason": "adapter_unavailable"}

from lib.bank import derive_bank_id, ensure_bank_mission
from lib.client import HindsightClient
from lib.config import debug_log, load_config
from lib.content import prepare_retention_transcript, read_transcript
from lib.daemon import get_api_url
from lib.governance import record_provisional_timeline_events, record_semantic_lifecycle_events
from lib.handoff import save_handoff
from lib.midtask import acknowledge_journal, clear_checkpoint, snapshot_journal
from lib.journal_audit import archive_tool_journal
from lib.retention_queue import RetentionQueue, estimate_tokens
from lib.retention_policy import retention_exclusion, allowed_retention_batches, retention_turns, submission_metadata
from lib.state import read_state, write_state


LAST_RECALL_STATE = "last_recall.json"


def session_recall_state_name(session_id: str) -> str:
    value = hashlib.sha256(str(session_id or "unknown").encode("utf-8")).hexdigest()[:20]
    return f"last_recall-{value}.json"


def _normalized_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value or "").casefold())


def classify_memory_use(answer: str, items: list[dict]) -> dict:
    """Conservatively classify visible evidence of memory use in an answer.

    ``likely_used`` is a deterministic wording-overlap heuristic, not a claim
    about the model's internal attention.  Everything else remains ``unknown``;
    this function never infers ``ignored`` merely because no overlap is visible.
    """
    answer_raw = str(answer or "")
    answer_folded = answer_raw.casefold()
    answer_norm = _normalized_text(answer_raw)
    cited_ids: list[str] = []
    likely_used_ids: list[str] = []
    unknown_ids: list[str] = []
    for item in items or []:
        memory_id = str(item.get("id") or item.get("chunk_id") or "")
        if not memory_id:
            continue
        if len(memory_id) >= 4 and memory_id.casefold() in answer_folded:
            cited_ids.append(memory_id)
            continue
        preview = _normalized_text(item.get("text_preview") or item.get("text") or item.get("content") or "")
        likely = False
        if preview and answer_norm:
            match = SequenceMatcher(None, preview, answer_norm, autojunk=False).find_longest_match(
                0, len(preview), 0, len(answer_norm)
            )
            # At least twelve normalized characters and roughly one third of a
            # bounded memory preview must reappear.  This is strict enough to
            # avoid common words while still recognizing a genuinely reused
            # Chinese sentence that was lightly paraphrased.
            likely = match.size >= 12 and match.size / max(1, min(len(preview), 80)) >= 0.28
        if likely:
            likely_used_ids.append(memory_id)
        else:
            unknown_ids.append(memory_id)
    return {
        "cited_ids": cited_ids,
        "likely_used_ids": likely_used_ids,
        "unknown_ids": unknown_ids,
        "ignored_ids": [],
        "method": "exact_id_or_distinctive_phrase_overlap",
        "semantics": "可见回答文字的可验证复用线索；不声称读取模型内部注意力。",
    }


def _message_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content") or item.get("output_text")
                if value:
                    parts.append(str(value))
        return "\n".join(parts)
    return str(content or "")


def _latest_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if str(message.get("role") or "").casefold() == "assistant":
            value = _message_text(message).strip()
            if value:
                return value
    return ""


def post_memory_feedback(config: dict, payload: dict) -> bool:
    url = str(config.get("memoryEffectivenessFeedbackUrl") or "http://127.0.0.1:12079/v1/feedback")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Memory-Client": "codex-stop-hook"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def submit_answer_feedback(session_id: str, messages: list[dict], config: dict) -> None:
    state_name = session_recall_state_name(session_id)
    state = read_state(state_name, {}) or {}
    if not state:
        candidate = read_state(LAST_RECALL_STATE, {}) or {}
        if str(candidate.get("session_id") or "") == session_id:
            state = candidate
    if (
        not config.get("memoryEffectivenessEnabled", True)
        or not state.get("query_id")
        or not state.get("injected_ids")
        or state.get("answer_feedback_submitted")
    ):
        return
    answer = _latest_assistant_text(messages)
    if not answer:
        return
    classified = classify_memory_use(answer, state.get("injected_items") or [])
    payload = {
        "query_id": state["query_id"],
        "stage": "answer",
        "session_id": session_id,
        **classified,
        "answer_chars": len(answer),
        "answer_preview": answer[:320],
        "answer_observation": {
            "answer_available": True,
            "scope": "latest_assistant_text_after_this_recall",
            "meaning": "只评估回答文字可见的复用线索；未命中不等于没有注入或没有被模型读取。",
        },
        "feedback_id": hashlib.sha256(
            f"answer:{state['query_id']}:{answer}".encode("utf-8")
        ).hexdigest()[:24],
    }
    if post_memory_feedback(config, payload):
        state["answer_feedback_submitted"] = True
        state["answer_feedback"] = classified
        write_state(state_name, state)
        global_state = read_state(LAST_RECALL_STATE, {}) or {}
        if global_state.get("query_id") == state.get("query_id"):
            write_state(LAST_RECALL_STATE, state)


def _priority_short_task(transcript: str, config: dict) -> bool:
    """Mark only meaningful closed-task tails for earlier asynchronous flush."""
    minimum = int(config.get("shortTaskTailMinTokens", 800) or 800)
    if estimate_tokens(transcript) < minimum:
        return False
    markers = config.get("shortTaskTailMarkers") or []
    normalized = "".join(str(transcript or "").lower().split())
    return any("".join(str(marker).lower().split()) in normalized for marker in markers)


def _resolve_template(value: str, template_vars: dict) -> str:
    for key, replacement in template_vars.items():
        value = value.replace(f"{{{key}}}", replacement)
    return value


def main():
    if sys.platform == "win32":
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    config = load_config()
    if not config.get("autoRetain"):
        debug_log(config, "Auto-retain disabled, exiting")
        return

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("[Evolving Profile] Failed to read hook input", file=sys.stderr)
        return

    native_delegation = False
    if hook_input.get('session_id') in config.get('nativeDelegationSessions',[]):
        try:
            from native_delegation import observe
            observed=observe(hook_input)
            native_delegation = bool(observed.get('check_ids'))
            if observed.get('errors'):print('[Evolving Profile] native delegation observation incomplete: '+str(observed['errors']),file=sys.stderr)
        except Exception as error:
            print('[Evolving Profile] native delegation observation unavailable: '+type(error).__name__,file=sys.stderr)
    if config.get('scenarioCompletionGateEnabled', True):
        try:
            from memory_turn_check import scenario_completion_gate
            decision = scenario_completion_gate(hook_input, enabled=True)
            if decision:
                json.dump(decision, sys.stdout, ensure_ascii=False); sys.stdout.flush()
                return  # One bounded continuation; do not retain a premature answer.
        except Exception as error:
            print('[Evolving Profile] scenario completion check unavailable: '+type(error).__name__, file=sys.stderr)
    if hook_input.get('session_id') in config.get('memoryCompletionGateSessions',[]):
        try:
            from memory_turn_check import completion_gate
            decision=completion_gate(hook_input,enabled=True)
            if decision:
                json.dump(decision,sys.stdout,ensure_ascii=False);sys.stdout.flush()
                return  # Do not retain a provisional answer as a completed turn.
        except Exception as error:
            print('[Evolving Profile] completion check unavailable: '+type(error).__name__,file=sys.stderr)
    ham_emit("Stop", hook_input, "assistant", "turn", "")
    try:
        from memory_turn_check import observe_stop
        observe_stop(hook_input)
    except Exception as error:
        print('[Evolving Profile] stop execution audit unavailable: '+type(error).__name__,file=sys.stderr)
    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")
    project = hook_input.get("cwd") or "unknown"
    bank_id = derive_bank_id(hook_input, config)

    include_tool_calls = config.get("retainToolCalls", False)
    all_messages = read_transcript(transcript_path, include_tool_calls=include_tool_calls)
    source_turns = retention_turns(all_messages)
    allowed_source_messages = [message for turn in source_turns if turn['write_policy']['knowledge_allowed']
                               for message in turn['messages']]
    if config.get("assistantSelectiveRetentionShadow", True):
        try:
            from lib.assistant_retention_shadow import record_final_answer_shadow

            shadow = record_final_answer_shadow(
                all_messages,
                Path(os.environ.get("EVOLVING_PROFILE_STATE_ROOT", "~/.evolving-profile")).expanduser(),
                session_id,
                project,
            )
            debug_log(config, f"Assistant retention shadow recorded {shadow['recorded']} final answers")
        except Exception as error:
            debug_log(config, f"Assistant retention shadow unavailable: {type(error).__name__}")
    # Close the retrieval -> injection -> answer loop before any retention
    # threshold early return.  This writes one small local audit event and makes
    # no model or memory-retrieval request.
    exclusion = retention_exclusion(project, [session_id], config)
    if exclusion:
        debug_log(config, f"Retention held by {exclusion}; local evidence preserved")
        return
    # Close the 10-30 minute token-batch freshness gap with a deterministic,
    # local-only task receipt. This performs no model or Hindsight request.
    if config.get("taskHandoffEnabled", True):
        try:
            if source_turns and source_turns[-1]['write_policy']['knowledge_allowed']:
                save_handoff(session_id, project, allowed_source_messages, transcript_path, bank_id)
        except Exception as error:
            debug_log(config, f"Deterministic handoff save skipped: {error}")
    journal_snapshot = snapshot_journal(session_id)
    pending_journal = journal_snapshot["text"]
    # Tool output can contain recalled memories, copied user text and our own
    # audit explanations. Preserve it as audit, not fresh personal evidence.
    journal_audit = archive_tool_journal(session_id, project, journal_snapshot)
    if pending_journal and journal_audit.get('durable'):
        acknowledge_journal(session_id, journal_snapshot['through_sequence'])
    elif pending_journal:
        debug_log(config, 'Tool journal audit unavailable; retained for retry, not sent to knowledge extraction')
    if not all_messages and not pending_journal:
        debug_log(config, "No messages in transcript, skipping retain")
        return

    threshold = max(1, int(config.get("retainTokenThreshold", 12_000)))
    queue_path = config.get("retainQueuePath")
    queue = (
        RetentionQueue(queue_path, threshold_tokens=threshold)
        if queue_path
        else RetentionQueue(threshold_tokens=threshold)
    )

    cursor = queue.session_cursor(session_id, bank_id, project)
    if cursor > len(all_messages):
        cursor = 0
    new_messages = all_messages[cursor:]
    transcript, message_count = prepare_retention_transcript(
        new_messages,
        config.get("retainRoles", ["user", "assistant"]),
        True,
        include_tool_calls=include_tool_calls,
    )
    # Only real transcript messages enter the knowledge queue. A journal is
    # already captured above, without borrowing the authority of nearby users.
    captured = False
    allowed_new_messages = []
    # Derive from the full source window so a continuation inherits the prior
    # human prohibition even after the previous Stop advanced the cursor.
    for turn in source_turns:
        if turn['end'] <= cursor:
            continue
        if turn['write_policy']['knowledge_allowed']:
            allowed_new_messages.extend(all_messages[max(cursor,turn['start']):turn['end']])
        turn_transcript, _ = prepare_retention_transcript(
            all_messages[max(cursor, turn['start']):turn['end']],
            config.get('retainRoles', ['user', 'assistant']), True,
            include_tool_calls=include_tool_calls)
        structured_transcript, _ = prepare_retention_transcript(
            all_messages[max(cursor,turn['start']):turn['end']],
            config.get('retainRoles',['user','assistant']),True,include_tool_calls=True)
        captured = queue.capture(
            session_id=session_id, message_count=turn['end'], bank_id=bank_id,
            project=project, content=turn_transcript or '',
            write_policy=turn['write_policy'],
            messages=json.loads(structured_transcript) if structured_transcript else [],
            metadata={'source':'codex-hook-token-batch', 'session_id':session_id,
                      'project':project, 'priority_tail':str(_priority_short_task(turn_transcript, config)).lower()},
        ) or captured
    if captured:
        debug_log(config, f"Persisted {message_count} new messages to the local token queue")
        if journal_audit.get('durable'):
            clear_checkpoint(session_id)

    # Durable capture precedes optional network/model-dependent follow-ups.
    # A feedback/provider outage must not lose this completed turn's raw input.
    if not native_delegation:
        submit_answer_feedback(session_id, all_messages, config)
    record_provisional_timeline_events(allowed_new_messages, config)
    record_semantic_lifecycle_events(allowed_new_messages, config)
    if config.get('backgroundRetainWorkerEnabled', False):
        debug_log(config, 'Raw turn captured; background worker owns submission/reconciliation')
        return

    ready_batches = [batch for batch in queue.ready_batches(force_tail=False, config=config) if batch["bank_id"] == bank_id]
    pending_operations = queue.pending_operations()
    if not ready_batches and not pending_operations:
        debug_log(config, f"Token queue below threshold ({threshold}); no model request")
        return

    def _dbg(*args):
        debug_log(config, *args)

    try:
        api_url = get_api_url(config, debug_fn=_dbg, allow_daemon_start=True)
        client = HindsightClient(api_url, config.get("evolvingProfileApiToken"))
    except (RuntimeError, ValueError) as error:
        print(f"[Evolving Profile] {error}", file=sys.stderr)
        return

    statuses = {}
    for operation_id in pending_operations:
        try:
            statuses[operation_id] = client.operation_status(bank_id, operation_id, timeout=10).get("status")
        except Exception as error:
            debug_log(config, f"Operation status unavailable for {operation_id}: {error}")
    if statuses:
        queue.reconcile(statuses)
        ready_batches = [batch for batch in queue.ready_batches(force_tail=False, config=config) if batch["bank_id"] == bank_id]

    # A different project/session may already own the single async worker. Keep
    # newly captured work durable in the local queue, but never enqueue another
    # Hindsight operation until every existing operation is terminal.
    if queue.pending_operations():
        debug_log(config, "Another retain operation is still in flight; keeping this batch local")
        return

    if not ready_batches:
        return

    ensure_bank_mission(client, bank_id, config, debug_fn=_dbg)
    # Submit at most one async operation per Stop event. The next Stop (or the
    # nightly tail worker) reconciles it before any later batch is submitted,
    # preventing multiple long-lived Coding Plan requests from queueing behind
    # the serial proxy and exhausting their own HTTP timeout.
    for batch in allowed_retention_batches(ready_batches, config)[:1]:
        project_key = hashlib.sha256(batch["project"].encode("utf-8")).hexdigest()[:12]
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        template_vars = {
            "session_id": session_id,
            "bank_id": bank_id,
            "project_key": project_key,
            "timestamp": timestamp,
        }
        raw_tags = config.get("retainTags", [])
        tags = [_resolve_template(tag, template_vars) for tag in raw_tags] if raw_tags else None
        metadata = {
            "retained_at": timestamp,
            "source": "codex-hook-token-batch",
            "project": batch["project"],
            "session_ids": ",".join(batch["session_ids"]),
            "estimated_tokens": str(batch["estimated_tokens"]),
        }
        for key, value in config.get("retainMetadata", {}).items():
            metadata[key] = _resolve_template(str(value), template_vars)
        metadata.update(submission_metadata(batch))

        document_id = f"codex-batch-{batch['batch_id']}"
        debug_log(config, f"Submitting {document_id} ({batch['estimated_tokens']} estimated tokens)")
        try:
            response = client.retain(
                bank_id=bank_id,
                content=batch["content"],
                document_id=document_id,
                context=config.get("retainContext", "codex"),
                metadata=metadata,
                tags=tags,
                observation_scopes=config.get("retainObservationScopes", "shared"),
                strategy=config.get("retainStrategy"),
                timeout=config.get("retainSubmitTimeout", 30),
            )
            operation_id = response.get("operation_id")
            if not response.get("success") or not operation_id:
                raise RuntimeError(f"retain was not accepted asynchronously: {response}")
            queue.mark_submitted(batch["batch_id"], operation_id)
            debug_log(config, f"Retain accepted as operation {operation_id}")
        except Exception as error:
            print(f"[Evolving Profile] Retain failed: {error}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"[Evolving Profile] Unexpected error in retain: {error}", file=sys.stderr)
        try:
            sys.exit(2 if load_config().get("debug") else 0)
        except Exception:
            sys.exit(0)
