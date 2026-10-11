"""Local audit snapshots of exactly returned public tool records, never new retrieval."""
import hashlib
import json
import os
import uuid
from pathlib import Path

PREVIEW_CHARS = 2000
PREVIEW_TOTAL_CHARS = 32000
MAX_ARCHIVE_CHARS = 2000000
MAX_ARCHIVE_ITEMS = 200

def _body_allowed(value):
    if not isinstance(value,dict):return False
    scope=value.get('scope_verification')
    return not (value.get('permission_status') in ('denied','blocked') or value.get('hard_scope_match') is False
        or value.get('scope_status') in ('denied','mismatch')
        or scope is not None and (not isinstance(scope,dict) or scope.get('status') in ('denied','mismatch')))


def returned_items(value):
    if not isinstance(value, dict):
        return []
    if not _body_allowed(value):
        return []
    rows = []
    for key in ('memories', 'records', 'items', 'results', 'sources'):
        if isinstance(value.get(key), list):
            rows.extend((item, 'process_candidate' if value.get('source') == 'agent_process_memory' else 'returned_candidate') for item in value[key])
            break
    for key in ('record', 'unit'):
        if isinstance(value.get(key), dict):
            rows.append((value[key], 'returned_unit'))
    guidance = value.get('guidance_view') or value
    if isinstance(guidance, dict):
        for key in ('included', 'stable_profile', 'guidance_items', 'model_sections', 'entries'):
            if isinstance(guidance.get(key), list):
                rows.extend((item, key) for item in guidance[key])
    source = value.get('source')
    if isinstance(source, dict) and isinstance(source.get('text'), str):
        # Original source, not memory.text's extracted paraphrase.
        rows = [({**source,'id': (value.get('memory') or {}).get('id') or source.get('id'), 'text': source['text']}, 'original_source')]
    result = []
    seen = set()
    for row, role in rows:
        if not _body_allowed(row):
            continue
        body = next((row.get(k) for k in ('text', 'text_preview', 'content', 'summary', 'title') if isinstance(row.get(k), str)), '')
        identity = row.get('id') or row.get('process_memory_id') or row.get('unit_id') or row.get('scenario_id')
        if not identity and row.get('section_id'):
            identity = f"{row.get('model_id') or 'section'}:{row['section_id']}"
        if not identity and not body:
            continue
        key = (str(identity or ''), role, body)
        if key in seen:
            continue
        seen.add(key)
        item = {'id': str(identity or f'item-{len(result)}'), 'text': body, 'source_role': role,
                'item_index': len(result), 'total_chars': len(body), 'text_sha256': hashlib.sha256(body.encode()).hexdigest()}
        for field in ('applies_when', 'exceptions', 'effect_on_action', 'scope', 'status', 'source_locator', 'evidence_manifest'):
            if field in row:
                item[field] = row[field]
        if value.get('source') == 'agent_process_memory':
            item['readback_tool'] = 'read_agent_process_memory'
        result.append(item)
    return result


def preview_items(items):
    output = []
    remaining = PREVIEW_TOTAL_CHARS
    for item in items[:MAX_ARCHIVE_ITEMS]:
        count = min(PREVIEW_CHARS, remaining)
        output.append({**item, 'text': item['text'][:count], 'text_truncated': len(item['text']) > count})
        remaining -= min(len(item['text']), count)
    return output


def archive_returned_items(home, tool, items, binding, call):
    """Bind immutable snapshots to a real invocation; unbound calls remain previews only."""
    identities=[binding.get('hook_invocation_id'),binding.get('session_id'),binding.get('turn_id'),call.get('tool_call_id')]
    if not items or binding.get('state') != 'prompt_bound' or not all(isinstance(v,str) and v.strip() and len(v)<=256 for v in identities):
        return None
    archived = []
    remaining = MAX_ARCHIVE_CHARS
    for item in items[:MAX_ARCHIVE_ITEMS]:
        body = item['text'][:remaining]
        archived.append({**item, 'text': body, 'archive_truncated': len(body) != len(item['text'])})
        remaining -= len(body)
    payload = {'schema': 'evolving-profile.returned-content.v1', 'tool': tool,
               'check_id': binding.get('hook_invocation_id'), 'session_id': binding.get('session_id'),
               'turn_id': binding.get('turn_id'), 'tool_call_id': call['tool_call_id'],
               'items': archived, 'item_count': len(items), 'archived_count': len(archived),
               'coverage': 'complete' if len(items) == len(archived) and not any(i['archive_truncated'] for i in archived) else 'partial',
               'authority': 'historical_return_snapshot_not_live_fact_or_answer_adoption'}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    reference = hashlib.sha256(encoded).hexdigest()
    root = Path(home) / '.evolving-profile/audit/returned-content'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f'{reference}.json'
    if not target.exists():
        temp = root / f'.{reference}.{uuid.uuid4().hex}.tmp'
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
        os.replace(temp, target)
    return {'ref': reference, 'schema': payload['schema'], 'check_id': payload['check_id'],
            'session_id': payload['session_id'], 'turn_id': payload['turn_id'], 'tool_call_id': payload['tool_call_id'],
            'item_count': len(items), 'archived_count': len(archived), 'coverage': payload['coverage'],
            'boundary': payload['authority']}
