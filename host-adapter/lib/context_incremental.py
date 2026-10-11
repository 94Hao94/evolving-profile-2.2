"""Bounded, recoverable background Scenario discovery and summary processing.

No model call runs in an MCP/Prompt hook. SQLite leases coordinate workers;
source watermarks and the index lock protect concurrent session updates.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path

from .context_associations import project_key
from .context_pipeline import write_progress
from .context_summary import read_context_index, update_context_index, context_index_lock
from .scenario_source import read_session_source, normalize_scenario_user
from .content import is_synthetic_codex_user_message
from .context_retry import summary_failure_class, SOURCE_WAIT_ERRORS

MAX_HEADER_BYTES = 1024 * 1024
MAX_NAVIGATION_BYTES = 1024 * 1024


def _read_session_header(path, *, max_bytes=MAX_HEADER_BYTES):
    """Read a whole native metadata line within a fixed byte budget.

    Only navigation metadata leaves this reader; embedded instructions are
    never persisted in the index or diagnostic receipt.
    """
    try:
        with Path(path).open('rb') as stream:
            raw = stream.readline(max_bytes + 1)
    except OSError:
        return {'status':'unreadable_headers'}
    if len(raw) > max_bytes:
        return {'status':'oversized_headers','bytes_read':len(raw)}
    if not raw.endswith(b'\n'):
        return {'status':'incomplete_headers','bytes_read':len(raw)}
    try: header = json.loads(raw)
    except (ValueError,UnicodeError):
        return {'status':'unparseable_headers','bytes_read':len(raw)}
    if not isinstance(header,dict) or not isinstance(header.get('payload'),dict):
        return {'status':'unparseable_headers','bytes_read':len(raw)}
    payload = header['payload']
    return {'status':'ok','bytes_read':len(raw),'header':{'type':header.get('type'),
        'timestamp':header.get('timestamp'),'payload':{key:payload.get(key) for key in ('id','cwd','timestamp')}}}


def _revision(paths):
    rows = []
    for path in sorted(paths):
        stat = path.stat()
        rows.append([str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns])
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with context_index_lock(self.db_path):
            return method(self, *args, **kwargs)
    return call


def _utc(timestamp):
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


def _source_policy(source):
    from .retention_policy import retention_turns
    messages=[{'role':m['role'],'content':m['text'],'source_record':{
        'turn_id':m.get('turn_id'),'session_id':source['thread_id'],'byte_offset':m.get('byte_offset'),
        'source_path':m.get('source_path'),'raw_line_sha256':m.get('raw_line_sha256')}} for m in source['messages']]
    return [turn['write_policy'] for turn in retention_turns(messages)]


def _input_revision(source,project,bank_id):
    payload={'revision':source.get('source_revision'),'status':source.get('status'),
        'files':source.get('source_files'),'exclusions':source.get('context_metadata_exclusions'),
        'authorization':_source_policy(source),'project':project,'bank_id':bank_id}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def qualify_summary_input(source):
    """Eligibility is source-bound; an assistant answer is never a user request."""
    status = source.get('status')
    messages = source.get('messages') or []
    if any(not policy['knowledge_allowed'] for policy in _source_policy(source)):
        target, reason = 'protected', 'scenario_knowledge_write_prohibited'
    elif status != 'complete':
        target, reason = 'waiting_source', 'scenario_' + str(status or 'source_incomplete')
    elif not any(m.get('role') == 'user' for m in messages):
        target, reason = 'source_not_applicable', 'scenario_source_no_user_messages'
    elif messages[0].get('role') != 'user':
        target, reason = 'waiting_source', 'scenario_episode_first_message_not_user'
    else:
        target, reason = 'eligible', None
    return {'status': target, 'reason': reason, 'source_status': status,
            'source_revision': source.get('source_revision'),
            'user_count': sum(m.get('role') == 'user' for m in messages),
            'assistant_count': sum(m.get('role') == 'assistant' for m in messages),
            'source_chars': source.get('total_chars'), 'provider_calls': 0}


def _ensure_qualification_schema(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
    for name, kind in (('input_revision', 'TEXT'), ('prior_status', 'TEXT'),
                       ('source_check_pending', 'INTEGER DEFAULT 0'), ('input_qualification', 'TEXT')):
        if name not in columns:
            db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + kind)
    db.execute('''CREATE TABLE IF NOT EXISTS job_evidence(
        id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, evidence TEXT NOT NULL)''')


def _record_job_evidence(db, current, kind, qualification=None):
    # Retain the prior per-source budget and diagnostic codes before transitions.
    prior = {key: current[key] for key in ('session_id', 'status', 'error_code', 'attempts',
             'revision', 'input_revision', 'completed_revision', 'prior_status', 'source_check_pending')
             if key in current.keys()}
    value = {'kind': kind, 'prior': prior, 'qualification': qualification,
             'recorded_at': _utc(time.time())}
    db.execute('INSERT INTO job_evidence(session_id,evidence) VALUES(?,?)',
               (current['session_id'], json.dumps(value, sort_keys=True)))


def summary_runtime_permission(state_root):
    """Fresh strict permission proof; no absent/malformed setting grants writes.

    The entire file hash is the observed generation for legacy v1 settings
    without an explicit generation. Provider secrets never leave this helper.
    """
    path = Path(state_root).expanduser() / 'config/runtime-settings.json'
    try:
        raw = path.read_bytes()
        settings = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        return {'allowed':False, 'status':'unknown', 'reason':'runtime_settings_unavailable',
                'generation':None, 'configured_generation':None}
    generation = hashlib.sha256(raw).hexdigest()
    invalid = {'allowed':False, 'status':'unknown', 'reason':'runtime_settings_invalid',
               'generation':generation, 'configured_generation':None}
    if not isinstance(settings, dict):
        return invalid
    routing, modules = settings.get('routing'), settings.get('modules')
    if not isinstance(routing, dict) or not isinstance(modules, dict):
        return invalid
    scenario = modules.get('scenario_summary')
    if (not isinstance(scenario, dict) or type(routing.get('ep_enabled')) is not bool
            or type(scenario.get('record')) is not bool):
        return invalid
    configured = settings.get('generation')
    if configured is not None and type(configured) not in (str, int):
        return invalid
    allowed = routing['ep_enabled'] and scenario['record']
    return {'allowed':allowed, 'status':'allowed' if allowed else 'disabled',
            'reason':None if allowed else 'scenario_recording_disabled',
            'generation':generation, 'configured_generation':configured}


def _require_summary_runtime_permission(state_root, expected=None):
    current = summary_runtime_permission(state_root)
    if not current['allowed']:
        raise ValueError('reassessment_recording_disabled' if current['status']=='disabled'
                         else 'reassessment_runtime_permission_unavailable')
    if expected is not None and current != expected:
        raise ValueError('reassessment_runtime_permission_changed')
    return current


def _reassessment_plan(db, index, session_root, session_ids, runtime_permission):
    observations, changes = [], []
    indexed = {r.get('session_id'): r for r in index['sessions']}
    scope = ' WHERE session_id IN (' + ','.join('?' for _ in session_ids) + ')' if session_ids else ''
    for raw in db.execute('SELECT * FROM jobs' + scope + ' ORDER BY session_id', session_ids):
        job = dict(raw)
        row = indexed.get(job['session_id']) or {}
        from .memory_recovery_scenario import MAX_SOURCE_CHARS
        source = read_session_source(job['session_id'], session_root, max_chars=MAX_SOURCE_CHARS)
        qualification = qualify_summary_input(source)
        try:
            current_stat = _revision([Path(p) for p in source['source_files']])
        except OSError:
            current_stat = None
        signature = _input_revision(source, job['project'], row.get('bank_id'))
        headers = [_read_session_header(p) for p in source['source_files']]
        identity_ok = all(h.get('status') == 'ok' and h['header']['type'] == 'session_meta'
            and str(h['header']['payload'].get('id')) == job['session_id']
            and str(h['header']['payload'].get('cwd') or '') == job['project'] for h in headers)
        observation = {'session_id': job['session_id'], 'status': job['status'],
            'error_code': job['error_code'], 'attempts': job['attempts'],
            'remaining_attempts': max(0, 3 - job['attempts']),
            'failure_class': summary_failure_class(job['status'], job['error_code']),
            'qualification': qualification, 'input_revision': signature,
            'current_source_stat_revision': current_stat, 'source_identity_verified': identity_ok,
            'source_stat_changed': current_stat != job['revision']}
        observations.append(observation)
        target = qualification['status']
        # Complete, protected and active work are never touched by a classifier.
        # No paid work is requeued, including legacy unknown input baselines.
        if (job['status'] in {'failed', 'waiting_source', 'source_not_applicable'}
                and target in {'waiting_source', 'source_not_applicable', 'protected'}
                and identity_ok and job['status'] != target and row):
            changes.append({'session_id': job['session_id'], 'to_status': target,
                'prior_error_code': job['error_code'], 'qualification': qualification,
                'current_source_stat_revision': current_stat, 'input_revision': signature,
                'expected_job': job, 'expected_index_row': row})
        elif (job['status'] == 'failed' and job['error_code'] == 'scenario_accepted_summary_unavailable'
                and qualification['status'] == 'eligible' and identity_ok):
            from .memory_recovery_scenario import complete_same_source_metadata
            cached = row.get('previous_reviewed_summary') or row
            completed = complete_same_source_metadata(cached, source)
            if (completed and completed.get('project_key') == row.get('project_key') == project_key(job['project'])
                    and completed.get('bank_id') == row.get('bank_id')
                    and job.get('input_revision') == signature):
                changes.append({'session_id': job['session_id'], 'to_status': 'complete',
                    'action': 'reuse_reviewed_summary', 'prior_error_code': job['error_code'],
                    'qualification': qualification, 'input_revision': signature,
                    'current_source_stat_revision': current_stat,
                    'metadata_completion': completed['source_metadata_completion'],
                    'restored_summary': completed, 'expected_job': job, 'expected_index_row': row})
    payload = {'session_ids': session_ids, 'runtime_permission':runtime_permission,
               'observations': observations, 'changes': changes}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'schema': 'evolving-profile.source-reassessment.v1', 'dry_run': True,
        'model_calls': 0, 'plan_sha256': digest, **payload}


def reassess_source_jobs(state_root, session_root, *, session_ids=None, apply=False, expected_plan_sha256=None):
    """Default is read-only. Apply needs exact scope and a fresh reviewed digest."""
    ids = sorted({str(uuid.UUID(sid)) for sid in session_ids or []})
    if apply and (not ids or len(ids) > 32 or not expected_plan_sha256):
        raise ValueError('reassessment_explicit_scope_and_digest_required')
    root = Path(state_root).expanduser() / 'context'
    path, index_path = root / 'context-incremental.sqlite', root / 'context-index.json'
    if not path.is_file():
        return {'schema': 'evolving-profile.source-reassessment.v1', 'dry_run': not apply,
                'status': 'state_unavailable', 'model_calls': 0, 'changes': [], 'observations': []}
    def connect(read_only):
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=' + ('ro' if read_only else 'rw'), uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        return db
    if not apply:
        # Opening even mode=ro on a WAL DB can create/update its shared-memory
        # sidecar. Copy a stable bounded snapshot before opening SQLite so a
        # dry run cannot mutate any production file (including WAL/SHM).
        def fingerprint():
            return [(str(p), p.stat().st_size, p.stat().st_mtime_ns)
                    for p in (path, Path(str(path) + '-wal')) if p.exists()]
        for _ in range(3):
            before = fingerprint()
            with tempfile.TemporaryDirectory(prefix='ep-source-reassessment-') as directory:
                snapshot = Path(directory) / path.name
                for name, _, _ in before:
                    shutil.copyfile(name, Path(directory) / Path(name).name)
                if before != fingerprint():
                    continue
                db = sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)
                db.row_factory = sqlite3.Row
                try:
                    return _reassessment_plan(db, read_context_index(index_path), session_root, ids,
                                              summary_runtime_permission(state_root))
                finally:
                    db.close()
        raise ValueError('reassessment_busy_snapshot')
    _require_summary_runtime_permission(state_root)
    with context_index_lock(path):
        permission = _require_summary_runtime_permission(state_root)
        db = connect(False)
        try:
            db.execute('BEGIN IMMEDIATE')
            plan = _reassessment_plan(db, read_context_index(index_path), session_root, ids, permission)
            if plan['plan_sha256'] != expected_plan_sha256:
                raise ValueError('reassessment_stale_plan')
            _require_summary_runtime_permission(state_root, plan['runtime_permission'])
            _ensure_qualification_schema(db)
            for change in plan['changes']:
                current = db.execute('SELECT * FROM jobs WHERE session_id=?', (change['session_id'],)).fetchone()
                _record_job_evidence(db, current, 'source_qualification_reassessment', change['qualification'])
                db.execute('''UPDATE jobs SET status=?,input_revision=?,input_qualification=?,
                    source_check_pending=0,prior_status=NULL,token=NULL,lease_until=0 WHERE session_id=?''',
                    (change['to_status'], change['input_revision'], json.dumps(change['qualification']), change['session_id']))
                if change.get('action') == 'reuse_reviewed_summary':
                    db.execute('UPDATE jobs SET revision=?,completed_revision=?,error_code=NULL WHERE session_id=?',
                        (change['current_source_stat_revision'], change['current_source_stat_revision'], change['session_id']))
            def project(index):
                _require_summary_runtime_permission(state_root, plan['runtime_permission'])
                by_id = {c['session_id']: c for c in plan['changes']}
                for row in index['sessions']:
                    c = by_id.get(row.get('session_id'))
                    if c:
                        if row != c['expected_index_row']:
                            raise ValueError('reassessment_index_changed')
                        from .memory_recovery_scenario import MAX_SOURCE_CHARS
                        live = read_session_source(c['session_id'], session_root, max_chars=MAX_SOURCE_CHARS)
                        headers = [_read_session_header(p) for p in live['source_files']]
                        identity_ok = all(h.get('status') == 'ok' and h['header']['type'] == 'session_meta'
                            and str(h['header']['payload'].get('id')) == c['session_id']
                            and str(h['header']['payload'].get('cwd') or '') == c['expected_job']['project']
                            for h in headers)
                        if (not identity_ok or _revision([Path(p) for p in live['source_files']]) != c['current_source_stat_revision']
                                or _input_revision(live, c['expected_job']['project'], row.get('bank_id')) != c['input_revision']):
                            raise ValueError('reassessment_source_changed')
                        if c.get('action') == 'reuse_reviewed_summary':
                            from .memory_recovery_scenario import complete_same_source_metadata
                            if complete_same_source_metadata(c['restored_summary'], live) is None:
                                raise ValueError('reassessment_source_changed')
                            old_evidence = {'status': c['expected_job']['status'], 'error_code': c['prior_error_code'],
                                            'attempts': c['expected_job']['attempts']}
                            restored = {key: value for key, value in c['restored_summary'].items()
                                        if key not in {'previous_reviewed_summary', 'error_code', 'summary_failure_detail'}}
                            restored.update(source_stat_revision=c['current_source_stat_revision'],
                                input_qualification=c['qualification'],
                                summary_kind='canonical_model_summary', summary_reuse={
                                    'basis': 'same_verified_source_scope_authorization', 'provider_calls': 0,
                                    'metadata_completion': c['metadata_completion'], 'prior_job': old_evidence})
                            row.clear()
                            row.update(restored)
                        else:
                            row.update(status='raw_available_summary_' + c['to_status'],
                                       input_qualification=c['qualification'])
                _require_summary_runtime_permission(state_root, plan['runtime_permission'])
                return index
            if plan['changes']:
                update_context_index(index_path, project)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    return {**plan, 'dry_run': False, 'applied': len(plan['changes'])}


def _navigation_title(paths):
    for path in paths:
        try:
            with Path(path).open('rb') as stream:
                for raw in stream.read(MAX_NAVIGATION_BYTES).splitlines():
                    try: row = json.loads(raw)
                    except (ValueError, UnicodeError): continue
                    if not isinstance(row,dict) or not isinstance(row.get('payload'),dict): continue
                    payload = row['payload']
                    if row.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                        text = str(payload.get('message') or '')
                    elif row.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'user':
                        text = '\n'.join(str(c.get('text') or '') for c in payload.get('content') or [] if isinstance(c,dict))
                    else: continue
                    if is_synthetic_codex_user_message(text): continue
                    request,_ = normalize_scenario_user(text)
                    if request: return ' '.join(request.split())[:120]
        except OSError: continue
    return ''


def process_summary(source, row, session_root, config):
    from .scenario_model import request_episode_bundle, request_episode_bundle_review
    from .memory_recovery_scenario import request_source_coverage_review, publish_automated_session
    params = {'base_url': config['EVOLVING_PROFILE_API_LLM_BASE_URL'],
              'api_key': config['EVOLVING_PROFILE_API_LLM_API_KEY'],
              'model': config['EVOLVING_PROFILE_API_LLM_MODEL']}
    bundle = request_episode_bundle(source, **params)
    review = request_episode_bundle_review(source, bundle, **params)
    audit = request_source_coverage_review(source, bundle, **params)
    return publish_automated_session(row, source, bundle, review, audit, session_root)


class ContextIncremental:
    def __init__(self, state_root, session_root, *, debounce=30, capacity=1024, lease_seconds=900, bank_id=None, session_ids=None, new_after=None):
        self.root = Path(state_root).expanduser() / 'context'
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_root = Path(session_root).expanduser()
        self.index = self.root / 'context-index.json'
        self.db_path = self.root / 'context-incremental.sqlite'
        self.progress_path = self.root / 'context-pipeline-progress.json'
        self.debounce, self.capacity, self.lease_seconds = debounce, capacity, lease_seconds
        self.bank_id = bank_id
        self.session_ids = sorted({str(uuid.UUID(sid)) for sid in session_ids or []})
        self.new_after = datetime.fromisoformat(new_after.replace('Z','+00:00')).timestamp() if new_after else None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs(session_id TEXT PRIMARY KEY, revision TEXT,
                    source_paths TEXT, project TEXT, status TEXT, due REAL, attempts INTEGER,
                    lease_until REAL, token TEXT, error_code TEXT, completed_revision TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns={r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            for name,kind in (('input_revision','TEXT'),('prior_status','TEXT'),('source_check_pending','INTEGER DEFAULT 0')):
                if name not in columns: db.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+kind)
            _ensure_qualification_schema(db)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db: yield db
        finally:
            db.close()

    @_serialized
    def discover(self, *, now=None, limit=128):
        now = time.time() if now is None else now
        if not 1 <= limit <= 4096: raise ValueError('invalid_discovery_limit')
        sources = {}
        for path in self.session_root.rglob('rollout-*.jsonl'):
            try: sid = str(uuid.UUID(path.stem[-36:]))
            except ValueError: continue
            sources.setdefault(sid, []).append(path)
        changed, overflow = [], 0
        errors = {name:0 for name in ('oversized_headers','unparseable_headers','incomplete_headers',
                                     'unreadable_headers','identity_mismatch_headers','invalid_header_type')}
        valid_headers = 0
        indexed = {r.get('session_id'): r for r in read_context_index(self.index)['sessions']}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()
            after = cursor[0] if cursor else ''
            ids = sorted(sid for sid in sources if not self.session_ids or sid in self.session_ids)
            selected = ([sid for sid in ids if sid > after] + [sid for sid in ids if sid <= after])[:limit]
            pending = db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','retrying','running')").fetchone()[0]
            for sid in selected:
                paths = sources[sid]
                try:
                    revision = _revision(paths)
                except (OSError, ValueError, TypeError):
                    errors['unreadable_headers'] += 1
                    continue
                read = _read_session_header(paths[0])
                if read['status'] != 'ok':
                    errors[read['status']] += 1
                    continue
                header = read['header']; meta = header['payload']
                if header['type'] != 'session_meta':
                    errors['invalid_header_type'] += 1
                    continue
                if str(meta.get('id')) != sid:
                    errors['identity_mismatch_headers'] += 1
                    continue
                valid_headers += 1
                old = db.execute('SELECT * FROM jobs WHERE session_id=?', (sid,)).fetchone()
                if self.new_after is not None and not old and sid not in self.session_ids:
                    try:created = datetime.fromisoformat(str(meta.get('timestamp') or header.get('timestamp')).replace('Z','+00:00')).timestamp()
                    except (ValueError,TypeError):continue
                    if created < self.new_after: continue
                if old and old['revision'] == revision:
                    if (indexed.get(sid) or {}).get('source_stat_revision') != revision:
                        changed.append({'session_id':sid, 'revision':revision, 'project':str(meta.get('cwd') or ''), 'paths':[str(p) for p in paths]})
                    continue
                if (not old or old['status'] not in {'pending','retrying','running'}) and pending >= self.capacity:
                    overflow += 1; continue
                project = str(meta.get('cwd') or '')
                if old:
                    _record_job_evidence(db, old, 'transport_source_revision_changed')
                db.execute('''INSERT INTO jobs(session_id,revision,source_paths,project,status,due,attempts,lease_until,token,error_code,completed_revision)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,
                    source_paths=excluded.source_paths,project=excluded.project,status='pending',
                    due=max(jobs.due,excluded.due),lease_until=0,token=NULL,
                    prior_status=CASE WHEN jobs.source_check_pending=1 THEN jobs.prior_status ELSE jobs.status END,
                    source_check_pending=1''',
                    (sid, revision, json.dumps([str(p) for p in paths]), project, 'pending',
                     now + self.debounce, 0, 0, None, None, None))
                if not old or old['status'] not in {'pending','retrying','running'}: pending += 1
                changed.append({'session_id': sid, 'revision': revision, 'project': project, 'paths': [str(p) for p in paths]})
            if selected: db.execute("INSERT OR REPLACE INTO metadata VALUES('cursor',?)", (selected[-1],))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('discovered_at',?)", (str(now),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('source_sessions',?)", (str(len(sources)),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('capacity_deferred',?)", (str(overflow),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('discovery_errors',?)", (json.dumps(errors),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('valid_headers',?)", (str(valid_headers),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('last_scan_count',?)", (str(len(selected)),))
        if changed:
            def upsert(index):
                rows = {row['session_id']: row for row in index['sessions'] if row.get('session_id')}
                projects = {row['project_key']: row for row in index['projects'] if row.get('project_key')}
                for item in changed:
                    sid, pkey = item['session_id'], project_key(item['project'])
                    old = rows.get(sid, {})
                    title = (old.get('title') if old.get('title_authority') != 'session_id_locator_only' else None) or _navigation_title(item['paths'])
                    rows[sid] = {**old, 'context_id': 'session:' + sid, 'context_type': 'session', 'session_id': sid,
                        'title': title or sid,
                        'title_authority': 'original_user_request_excerpt_navigation_only' if title else 'session_id_locator_only',
                        'project_key': pkey, 'raw_source_files': item['paths'], 'source_ids': item['paths'],
                        'source_stat_revision': item['revision'], 'summary': {}, 'episodes': [],
                        'status': 'raw_available_summary_pending', 'summary_kind': 'not_yet_generated',
                        'evidence_role': 'context_navigation_only', 'discovered_at': _utc(now),
                        'updated_at': _utc(max(Path(p).stat().st_mtime for p in item['paths'])),
                        **({'previous_reviewed_summary': old} if old.get('status') in {'model_reviewed','episode_directory_ready'} else {})}
                    if self.bank_id and not old.get('bank_id'): rows[sid]['bank_id'] = self.bank_id
                    if pkey:
                        project = projects.get(pkey, {'context_id': 'project:' + pkey, 'context_type': 'project',
                            'project_key': pkey, 'identity_status': 'unverified_workspace_bucket', 'summary': {},
                            'status': 'navigation_only_summary_pending', 'evidence_role': 'context_navigation_only'})
                        field = 'pending_session_ids' if project.get('identity_status') == 'verified_project' else 'session_ids'
                        project[field] = sorted(set(project.get(field) or []) | {sid})
                        projects[pkey] = project
                return {**index, 'status': 'ready', 'sessions': list(rows.values()), 'projects': list(projects.values()),
                        'incremental_coverage': {'status': 'partial_pending', 'source_sessions': len(sources), 'last_discovery_at': now}}
            update_context_index(self.index, upsert)
        self.progress()
        return {'discovered': len(changed), 'scanned': len(selected), 'source_sessions': len(sources),
                'capacity_deferred': overflow,'valid_headers':valid_headers,'header_max_bytes':MAX_HEADER_BYTES,**errors}

    @_serialized
    def claim(self, *, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE jobs SET status='failed',error_code='worker_lease_expired' WHERE status='running' AND lease_until<=? AND attempts>=3", (now,))
            scope = ' AND session_id IN ('+','.join('?' for _ in self.session_ids)+')' if self.session_ids else ''
            row = db.execute("SELECT * FROM jobs WHERE (attempts<3 OR source_check_pending=1) AND ((status IN ('pending','retrying') AND due<=?) OR (status='running' AND lease_until<=?))"+scope+" ORDER BY due,session_id LIMIT 1", (now, now, *self.session_ids)).fetchone()
            if not row: return None
            token = uuid.uuid4().hex
            attempts=row['attempts']+(0 if row['source_check_pending'] else 1)
            db.execute("UPDATE jobs SET status='running', attempts=?,lease_until=?,token=? WHERE session_id=?", (attempts,now+self.lease_seconds, token, row['session_id']))
            return {**dict(row), 'token': token, 'attempts': attempts}

    @_serialized
    def prepare_input(self,job,source,row,reuse=False):
        """A transport append buys a source check, never a fresh model budget."""
        signature=_input_revision(source,job['project'],self.bank_id or row.get('bank_id'))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT * FROM jobs WHERE session_id=?',(job['session_id'],)).fetchone()
            if not current or current['token']!=job['token'] or current['revision']!=job['revision']:
                return {'status':'superseded'}
            old=current['input_revision']; prior=current['prior_status']
            if not reuse and current['source_check_pending'] and prior=='complete' and (old is None or old==signature):
                code='scenario_accepted_summary_unavailable'
                db.execute("UPDATE jobs SET status='failed',error_code=?,source_check_pending=0,prior_status=NULL,token=NULL,lease_until=0 WHERE session_id=?",(code,job['session_id']))
                return {'status':'failed','error_code':code,'unchanged_source':old==signature,'provider_calls':0,'restore_terminal':True}
            if not reuse and current['source_check_pending'] and prior in {'failed','protected'} and (old is None or old==signature):
                status=prior; code=current['error_code']
                if old is None:
                    db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('legacy-observed:'+job['session_id'],json.dumps({
                        'previous_input_revision':'unknown','current_observed_input_revision':signature,
                        'observed_at':_utc(time.time()),'prior_status':prior,'prior_error_code':code,'prior_attempts':current['attempts']})))
                    db.execute('UPDATE jobs SET input_revision=? WHERE session_id=?',(signature,job['session_id']))
                db.execute('UPDATE jobs SET status=?,source_check_pending=0,prior_status=NULL,token=NULL,lease_until=0 WHERE session_id=?',(status,job['session_id']))
                return {'status':status,'error_code':code,'unchanged_source':old==signature,
                    'legacy_source_revision_unknown':old is None,'provider_calls':0,'restore_terminal':True,
                    'next_action':'source-backed recovery or independent accepted summary required; current observation is only a future comparison baseline' if old is None else None}
            if reuse:
                attempts=current['attempts'] if current['source_check_pending'] else max(0,current['attempts']-1)
            elif old is not None and old!=signature:
                _record_job_evidence(db, current, 'semantic_source_budget_transition', qualify_summary_input(source))
                attempts=1
            elif current['source_check_pending']: attempts=current['attempts']+1
            else: attempts=current['attempts']
            if not reuse and attempts>3:
                db.execute("UPDATE jobs SET status='failed',source_check_pending=0,prior_status=NULL,token=NULL,lease_until=0 WHERE session_id=?",(job['session_id'],))
                return {'status':'failed','error_code':current['error_code'],'unchanged_source':True,'provider_calls':0,'restore_terminal':True}
            qualification = {**qualify_summary_input(source), 'input_revision': signature}
            db.execute('UPDATE jobs SET input_revision=?,input_qualification=?,attempts=?,source_check_pending=0,prior_status=NULL WHERE session_id=?',
                (signature,json.dumps(qualification),attempts,job['session_id']))
            job.update(input_revision=signature,input_qualification=qualification,attempts=attempts)
        return None

    @_serialized
    def finish(self, job, published, *, now=None, revalidate_reuse=False):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM jobs WHERE session_id=?', (job['session_id'],)).fetchone()
            if not current or current['revision'] != job['revision'] or current['token'] != job['token']: return False
            if _revision([Path(p) for p in json.loads(job['source_paths'])]) != job['revision']:
                db.execute("UPDATE jobs SET revision='',status='pending',token=NULL,due=? WHERE session_id=?", (now+self.debounce,job['session_id']))
                return False
            def upsert(index):
                if revalidate_reuse:
                    from .memory_recovery_scenario import accepted_session_source,MAX_SOURCE_CHARS
                    live=read_session_source(job['session_id'],self.session_root,max_chars=MAX_SOURCE_CHARS)
                    header=_read_session_header(json.loads(job['source_paths'])[0])
                    current_row=next((r for r in index['sessions'] if r.get('session_id')==job['session_id']),{})
                    if (header.get('status')!='ok' or str(header['header']['payload'].get('cwd') or '')!=job['project']
                            or not accepted_session_source(published,live)
                            or any(not p['knowledge_allowed'] for p in _source_policy(live))
                            or published.get('project_key')!=current_row.get('project_key')
                            or published.get('bank_id')!=current_row.get('bank_id')
                            or _input_revision(live,job['project'],self.bank_id or published.get('bank_id'))!=job.get('input_revision')
                            or _revision([Path(p) for p in json.loads(job['source_paths'])])!=job['revision']):
                        raise ValueError('scenario_source_changed')
                rows = []
                for row in index['sessions']:
                    if row.get('session_id') == job['session_id']:
                        if row.get('source_stat_revision') != job['revision']: raise ValueError('scenario_source_changed')
                        row = {**published, 'source_stat_revision': job['revision'], 'summary_kind': 'canonical_model_summary'}
                        if job.get('input_qualification') is not None:
                            row['input_qualification'] = job['input_qualification']
                    rows.append(row)
                return {**index, 'sessions': rows}
            update_context_index(self.index, upsert)
            _record_job_evidence(db, current, 'accepted_summary_published_or_reused')
            db.execute("UPDATE jobs SET status='complete',completed_revision=revision,lease_until=0,token=NULL,error_code=NULL WHERE session_id=?", (job['session_id'],))
        self.progress()
        return True

    @_serialized
    def fail(self, job, code, *, now=None, terminal=False, protected=False, failure_detail=None, qualification=None):
        now = time.time() if now is None else now
        status = 'protected' if protected else 'failed' if terminal or job['attempts'] >= 3 else 'retrying'
        if not protected and code == 'scenario_source_no_user_messages': status = 'source_not_applicable'
        elif not protected and code in SOURCE_WAIT_ERRORS: status = 'waiting_source'
        with self.db() as db:
            current = db.execute('SELECT * FROM jobs WHERE session_id=? AND revision=? AND token=?',
                (job['session_id'], job['revision'], job['token'])).fetchone()
            if current:
                _record_job_evidence(db, current, 'summary_job_' + status, qualification)
            changed = db.execute('UPDATE jobs SET status=?,due=?,lease_until=0,token=NULL,error_code=? WHERE session_id=? AND revision=? AND token=?',
                (status, now + min(300, 30 * 2 ** job['attempts']), code, job['session_id'], job['revision'], job['token'])).rowcount
            if changed and qualification is not None:
                db.execute('UPDATE jobs SET input_revision=?,input_qualification=?,source_check_pending=0,prior_status=NULL WHERE session_id=?',
                    (qualification['input_revision'], json.dumps(qualification), job['session_id']))
        if changed and status in {'failed','protected','waiting_source','source_not_applicable'}:
            def mark(index):
                for row in index['sessions']:
                    if row.get('session_id') == job['session_id'] and row.get('source_stat_revision') == job['revision']:
                        row.update(status='raw_available_summary_'+status, error_code=code)
                        if qualification is not None: row['input_qualification'] = qualification
                        if failure_detail is not None: row['summary_failure_detail']=failure_detail
                return index
            update_context_index(self.index, mark)
        self.progress()
        return status

    def run_once(self, config, *, now=None, processor=process_summary):
        job = self.claim(now=now)
        if not job: return {'status': 'idle'}
        self.progress()
        try:
            from .memory_recovery_scenario import MAX_SOURCE_CHARS
            source = read_session_source(job['session_id'], self.session_root, max_chars=MAX_SOURCE_CHARS)
            row = next(row for row in read_context_index(self.index)['sessions'] if row.get('session_id') == job['session_id'])
            from .memory_recovery_scenario import accepted_session_source, complete_same_source_metadata
            cached=row.get('previous_reviewed_summary') or row
            qualification = qualify_summary_input(source)
            qualification['input_revision'] = _input_revision(source, job['project'], self.bank_id or row.get('bank_id'))
            if qualification['status'] != 'eligible':
                code = qualification['reason']
                status = self.fail(job, code, now=now, terminal=True,
                    protected=qualification['status']=='protected', qualification=qualification)
                return {'status': status, 'error_code': code, 'session_id': job['session_id'],
                    'source_status': source.get('status'), 'source_chars': source.get('total_chars'),
                    'source_char_limit': MAX_SOURCE_CHARS, 'source_message_count': len(source.get('messages') or []),
                    'raw_fallback_available': bool(source.get('source_files')), 'provider_calls': 0,
                    'unchanged_source': job.get('input_revision') == qualification['input_revision'],
                    'input_qualification': qualification}
            allowed=not any(not p['knowledge_allowed'] for p in _source_policy(source))
            completed = complete_same_source_metadata(cached, source) if allowed else None
            reuse=(source['status']=='complete' and allowed and completed is not None
                and cached.get('project_key')==row.get('project_key')==project_key(job['project'])
                and cached.get('bank_id')==row.get('bank_id') and (self.bank_id is None or row.get('bank_id')==self.bank_id)
                )
            held=self.prepare_input(job,source,row,reuse=reuse)
            if held:
                if held.pop('restore_terminal',False):
                    def mark(index):
                        for current in index['sessions']:
                            if current.get('session_id')==job['session_id'] and current.get('source_stat_revision')==job['revision']:
                                current.update(status='raw_available_summary_'+held['status'],error_code=held.get('error_code'))
                        return index
                    update_context_index(self.index,mark)
                self.progress();return {**held,'session_id':job['session_id']}
            if reuse:
                restored={key:value for key,value in completed.items() if key!='previous_reviewed_summary'}
                restored['summary_reuse']={'basis':'same_verified_source_scope_authorization','provider_calls':0,'checked_at':_utc(now if now is not None else time.time()),
                    'prior_job_status':job.get('prior_status'),'prior_error_code':job.get('error_code'),'prior_attempts':job.get('attempts'),
                    'metadata_completion':completed['source_metadata_completion']}
                complete=self.finish(job,restored,now=now,revalidate_reuse=True)
                return {'status':'complete' if complete else 'superseded','session_id':job['session_id'],'summary_reused':complete,'provider_calls':0}
            if source['status'] != 'complete':
                code='scenario_'+source['status']
                return {'status':self.fail(job,code,now=now,terminal=True),'error_code':code,
                    'session_id':job['session_id'],'source_status':source['status'],
                    'source_chars':source.get('total_chars'),'source_char_limit':MAX_SOURCE_CHARS,
                    'raw_fallback_available':bool(source.get('source_files'))}
            if not any(message.get('role')=='user' for message in source['messages']):
                code='scenario_source_no_user_messages'
                return {'status':self.fail(job,code,now=now,terminal=True),'error_code':code,
                    'session_id':job['session_id'],'raw_fallback_available':bool(source['messages']),
                    'source_status':source['status'],'source_message_count':len(source['messages'])}
            if row.get('title') and normalize_scenario_user(row['title'])[1]:
                title=next((' '.join(m['text'].split())[:120] for m in source['messages'] if m['role']=='user'),job['session_id'])
                def relabel(index):
                    for current in index['sessions']:
                        if current.get('session_id')==job['session_id'] and current.get('source_stat_revision')==job['revision']:
                            current.update(title=title,title_authority='original_user_request_excerpt_navigation_only',
                                context_metadata_exclusions=source.get('context_metadata_exclusions') or [],
                                source_record_coverage=source.get('source_record_coverage') or {})
                    return index
                update_context_index(self.index,relabel)
                row={**row,'title':title,'title_authority':'original_user_request_excerpt_navigation_only'}
            from .retention_policy import retention_turns
            messages = [{ 'role': item['role'], 'content': item['text'], 'source_record': {
                'turn_id':item.get('turn_id'), 'session_id':source['thread_id'], 'byte_offset':item.get('byte_offset'),
                'source_path':item.get('source_path'), 'raw_line_sha256':item.get('raw_line_sha256')}} for item in source['messages']]
            if any(not turn['write_policy']['knowledge_allowed'] for turn in retention_turns(messages)):
                return {'status': self.fail(job, 'scenario_knowledge_write_prohibited', now=now, terminal=True, protected=True)}
            published = processor(source, row, self.session_root, config)
            if published.get('status') not in {'model_reviewed', 'episode_directory_ready'}:
                raise ValueError('scenario_model_review_not_accepted')
            published={**published,'context_metadata_exclusions':source.get('context_metadata_exclusions') or [],
                'source_record_coverage':source.get('source_record_coverage') or {}}
            if source.get('context_metadata_exclusions'):
                published['review_scope']='semantic_source_coverage_with_explicit_host_context_exclusions; '+str(published.get('review_scope') or 'not_external_fact_verification')
            return {'status': 'complete' if self.finish(job, published, now=now) else 'superseded', 'session_id': job['session_id']}
        except (ValueError, KeyError, StopIteration) as error:
            from .scenario_model import safe_validation_error_code
            code=safe_validation_error_code(error)
            if code == 'unclassified_model_error':
                code = str(error) if isinstance(error,ValueError) and str(error) in {
                    'scenario_model_review_not_accepted','automated_source_coverage_incomplete',
                    'automated_source_revision_changed','automated_source_coverage_budget_exceeded','scenario_source_changed'} else 'scenario_review_or_source_invalid'
            retryable = code in {'scenario_model_response_incomplete','scenario_state_invalid','scenario_state_quote_invalid',
                'scenario_state_phase_invalid','scenario_summary_budget_or_empty','scenario_episode_model_response_invalid',
                'scenario_review_invalid','scenario_episode_review_invalid','scenario_model_review_not_accepted',
                'automated_source_coverage_incomplete'}
            from .memory_recovery_scenario import CoverageError
            from .scenario_state_v3 import StateValidationError
            detail=error.safe_detail() if isinstance(error,(CoverageError,StateValidationError)) else None
            return {'status':self.fail(job,code,now=now,terminal=not retryable,failure_detail=detail),
                    'error_code':code,'session_id':job['session_id'],**({'failure_detail':detail} if detail is not None else {})}
        except Exception:
            return {'status': self.fail(job, 'scenario_provider_unavailable', now=now)}

    @_serialized
    def progress(self):
        with self.db() as db:
            counts = dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status').fetchall())
            meta = dict(db.execute('SELECT key,value FROM metadata').fetchall())
            failure_classes = {}
            for state, code, count in db.execute('SELECT status,error_code,count(*) FROM jobs GROUP BY status,error_code'):
                label = summary_failure_class(state, code)
                failure_classes[label] = failure_classes.get(label, 0) + count
        partial = any(counts.get(s,0) for s in ('pending','running','retrying')) or int(meta.get('source_sessions',0)) > sum(counts.values())
        errors = json.loads(meta.get('discovery_errors','{}'))
        status = 'not_discovered' if not meta.get('discovered_at') else 'partial_pending' if partial else 'partial_failed' if counts.get('failed') else 'partial_waiting_source' if counts.get('waiting_source') else 'partial_protected' if counts.get('protected') else 'partial_not_applicable' if counts.get('source_not_applicable') else 'complete'
        if any(errors.values()): status = 'partial_source_unavailable'
        snapshot = {'schema': 'evolving-profile.context-progress.v1', 'status': status,
            'phase': 'incremental_background_summary', 'model_policy': 'ep_configured_provider',
            'summary_coverage': 'source_bounded_automated_review; raw_fallback_unreviewed',
            'total': sum(counts.values()), 'pending': counts.get('pending', 0), 'queued': counts.get('pending',0),
            'running': counts.get('running',0), 'retrying': counts.get('retrying',0), 'succeeded': counts.get('complete',0),
            'failed': counts.get('failed',0), 'source_sessions': int(meta.get('source_sessions',0)),
            'protected': counts.get('protected',0),
            'not_applicable': counts.get('source_not_applicable',0),
            'waiting_source': counts.get('waiting_source',0),
            'failure_classes': failure_classes,
            'discovery_errors':errors,'valid_headers':int(meta.get('valid_headers',0)),
            'last_scan_count':int(meta.get('last_scan_count',0)),'header_max_bytes':MAX_HEADER_BYTES,
            'last_discovery_at': _utc(meta['discovered_at']) if meta.get('discovered_at') else None, 'capacity_deferred': int(meta.get('capacity_deferred',0)),
            'updated_at': _utc(time.time())}
        write_progress(self.progress_path, snapshot)
        return snapshot
