"""Read bounded native Session metadata for an already verified Prompt binding."""
import hashlib
import json
import uuid
from pathlib import Path


def resolve_source_metadata(binding, session_root, *, tail_bytes=262144):
    if binding.get('state') != 'prompt_bound' or not binding.get('turn_id'): return {}
    try: sid = str(uuid.UUID(str(binding.get('session_id'))))
    except (ValueError, TypeError): return {}
    root = Path(session_root).expanduser()
    paths = list(root.rglob(f'rollout-*-{sid}.jsonl'))[:8] if root.is_dir() else []
    matched = []
    for path in paths:
        try:
            with path.open('rb') as stream:
                stream.seek(0, 2); size = stream.tell()
                start = max(0, size-tail_bytes); stream.seek(start)
                if start: stream.readline()
                while True:
                    offset = stream.tell(); raw = stream.readline()
                    if not raw or not raw.endswith(b'\n'): break
                    try: row = json.loads(raw)
                    except (ValueError, UnicodeError): continue
                    payload = row.get('payload') or {}
                    if row.get('type') == 'turn_context' and str(payload.get('turn_id') or '') == str(binding['turn_id']):
                        matched.append({**{k: payload[k] for k in ('cwd','model','model_provider') if payload.get(k)},
                            'transcript_path': str(path), 'raw_context_locator': {'source_path': str(path),
                            'byte_offset': offset, 'raw_line_sha256': hashlib.sha256(raw).hexdigest()}})
        except (OSError, TypeError): continue
    if len(matched) != 1: return {}
    return matched[0]
