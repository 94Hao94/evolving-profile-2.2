"""Bounded original Session transcript input for Scenario Summary drafts."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from .content import extract_user_request, read_transcript


NORMALIZATION_VERSION = "scenario-source-v2"


def normalize_scenario_user(content):
    """Separate structurally identified host UI metadata from human text.

    Only a leading, well-formed native page-context block is recognized;
    quoted examples and ordinary user prose are preserved.
    """
    text = extract_user_request(str(content or ''))
    exclusions = []
    pattern = r'^\s*<external_codex_apps_open_page>(.*?)</external_codex_apps_open_page>'
    while (match := re.match(pattern,text,re.S)):
        try: data=json.loads(match.group(1))
        except (ValueError,TypeError): break
        if not isinstance(data,dict) or 'page_id' not in data: break
        exclusions.append({'metadata_kind':'native_app_page_context','original_role':'user',
            'fact_authority':'none','excluded_from_semantic_input':True,'excluded_chars':match.end(),
            'normalization_scope':'after_existing_prompt_envelope_normalization'})
        text=text[match.end():].strip()
    return text,exclusions


def revision_for_messages(messages: list[dict], context_metadata_exclusions=None) -> str:
    canonical = [{"raw_line_sha256": item.get("raw_line_sha256"), "role": item.get("role"),
                  "text": item.get("text"), "turn_id": item.get("turn_id")}
                 for item in messages]
    payload = {"normalization_version": NORMALIZATION_VERSION, "messages": canonical}
    if context_metadata_exclusions:
        payload['context_metadata_exclusions'] = context_metadata_exclusions
        payload['normalization_version'] = NORMALIZATION_VERSION+'-explicit-host-context-exclusions'
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def read_session_source(thread_id: str, session_root: str | Path, *, max_chars: int = 30000) -> dict:
    identity = str(uuid.UUID(str(thread_id)))
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars_must_be_positive")
    root = Path(session_root).expanduser()
    paths = sorted(root.rglob(f"rollout-*-{identity}.jsonl")) if root.is_dir() else []
    base = {"thread_id": identity, "source": "codex_thread_history", "source_files": [str(path) for path in paths],
            "coverage": "visible_user_and_final_assistant_messages_only_not_tool_outputs", "messages": []}
    if not paths:
        return {**base, "status": "source_missing", "total_chars": 0, "user_count": 0, "assistant_count": 0, "source_revision": None}
    if len(paths) > 8:
        return {**base, "status": "too_many_source_files", "total_chars": 0, "user_count": 0, "assistant_count": 0, "source_revision": None}

    messages = []
    context_metadata = []
    for path in paths:
        for item in read_transcript(str(path)):
            role = item.get("role")
            text = str(item.get("content") or "").strip()
            source_record = item.get("source_record") or {}
            if role not in {"user", "assistant"}:
                continue
            raw_hash = str(source_record.get("raw_line_sha256") or "")
            if not raw_hash:
                continue
            if role == "user":
                text,exclusions = normalize_scenario_user(text)
                for excluded in exclusions:
                    context_metadata.append({**excluded,'evidence_id':raw_hash[:16],
                        'turn_id':source_record.get('turn_id'),'source_path':str(path),
                        'byte_offset':source_record.get('byte_offset'),'raw_line_sha256':raw_hash})
            if not text: continue
            messages.append({"evidence_id": raw_hash[:16], "role": role, "text": text,
                             "turn_id": source_record.get("turn_id"), "at": source_record.get("recorded_at"),
                             "source_path": str(path), "byte_offset": source_record.get("byte_offset"),
                             "raw_line_sha256": raw_hash})
    for position, item in enumerate(messages, start=1):
        item["model_ref"] = f"m{position}"
    total_chars = sum(len(item["text"]) for item in messages)
    digest = revision_for_messages(messages,context_metadata) if messages else None
    result = {**base, "total_chars": total_chars, "user_count": sum(item["role"] == "user" for item in messages),
              "assistant_count": sum(item["role"] == "assistant" for item in messages), "source_revision": digest,
              'context_metadata_exclusions':context_metadata,'source_record_coverage':{
                  'semantic_message_count':len(messages),'excluded_context_metadata_count':len(context_metadata),
                  'review_scope':'semantic_messages_with_explicit_host_context_exclusions_not_fact_authority'}}
    if not messages:
        return {**result, "status": "source_empty"}
    if total_chars > max_chars:
        return {**result, "status": "over_budget"}
    return {**result, "status": "complete", "messages": messages}
