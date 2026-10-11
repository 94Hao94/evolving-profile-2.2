"""Bounded source relations for version histories with omitted fact subjects.

Version labels come from returned content and must name a subject in the main
query. Source hits locate chunks; only their live, same-document facts become
candidates. They acquire no relevance, speaker authority or execution rights.
"""
import datetime as dt
import re
import time
import urllib.parse
import uuid

from .recall_relevance import _literal_targets, _semantic_text


def hard_scope_denied(row):
    witness=row.get('scope_verification') or {}
    scope=row.get('scope_status') or (witness.get('status') if isinstance(witness,dict) else None)
    return row.get('permission_status') in {'denied','blocked'} or scope in {'denied','mismatch'} or row.get('hard_scope_match') is False


def observed_version_terms(query, records):
    query = _semantic_text(str(query))
    if _literal_targets(query) or not re.search(r'版本|演进|先前发布|历史发布|\b(?:versions?|evolution|release\s+history|(?:prior|previous|earlier|past)\s+releases?)\b|\bcompare\b.*\d+\.\d+', query, re.I):
        return []
    # A negated version mention is never an affirmative retrieval seed. Keep
    # this automatic route conservative; host-authored facets retain the query.
    if re.search(r'\b(?:excluding|exclude|except|without)\b|\bbut\s+not\b|不要包含|不包含|不包括|排除',query,re.I):
        return []
    pattern = r'(?<![A-Za-z0-9_])([A-Za-z][A-Za-z_-]{1,40}|[\u3400-\u9fff]{2,16})\s*(v?)\s*(\d+(?:\.\d+){1,3})(?![A-Za-z0-9_.])'
    explicit = {(m.group(1).casefold(), m.group(3)) for m in re.finditer(pattern, query, re.I)}
    versions = {}
    for row in records:
        if row.get('state') == 'invalidated' or hard_scope_denied(row):
            continue
        for match in re.finditer(pattern, _semantic_text(str(row.get('text') or '')), re.I):
            subject, marker, version = match.groups()
            if not re.search(r'(?<![A-Za-z0-9_])' + re.escape(subject) + r'(?![A-Za-z_])', query, re.I):
                continue
            if explicit and (subject.casefold(), version) not in explicit:
                continue
            versions.setdefault((subject.casefold(), version), (subject, marker, version))
    ordered = sorted(versions.values(), key=lambda v: tuple(int(n) for n in v[2].split('.')), reverse=True)[:2]
    return [[subject + marker + version, subject + ' ' + marker + version] for subject, marker, version in ordered]


def _timestamp(value):
    try:
        value = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value
    except (TypeError,ValueError):
        return None


def temporal_relation(row, window):
    if not window: return 'not_requested'
    start,end=_timestamp(window['start']),_timestamp(window['end'])
    own_start=_timestamp(row.get('occurred_start')) or _timestamp(row.get('mentioned_at'))
    own_end=_timestamp(row.get('occurred_end')) or own_start
    if None in (start,end,own_start,own_end): return 'unknown'
    return 'outside' if own_start>end or own_end<start else 'inside'


def discover_history_sources(bank, query, records, api, types=None, temporal_window=None):
    terms = observed_version_terms(query, records)
    audit = {'status': 'not_applicable' if not terms else 'bounded_source_discovery',
             'main_query': query, 'version_terms': terms, 'api_call_count': 0,
             'coverage': 'not_bank_exhaustive', 'source_receipts': [], 'document_receipts': [],
             'continuation_hints': [], 'deferred_source_locators': [],
             'temporal_window': temporal_window, 'outside_temporal_window_count': 0, 'unknown_temporal_count': 0,
             'deadline_seconds': 12.0, 'deadline_exhausted': False,
             'max_source_pages_per_version': 3, 'max_documents': 24,
             'max_facts_per_document': 100, 'max_new_candidates': 600}
    if not terms:
        return [], audit
    started = time.monotonic()
    deadline = started + audit['deadline_seconds']
    base = '/v1/default/banks/' + urllib.parse.quote(bank, safe='')
    docs = {}
    def continuation(variant,cursor):
        args={'terms':variant,'match':'any','role':'any','limit':20}
        if cursor is not None: args['cursor']=cursor
        if not any(hint['arguments']==args for hint in audit['continuation_hints']):
            audit['continuation_hints'].append({'tool':'find_sources','arguments':args,
                'optional':True,'when':'required_evidence_gap_remains','main_query':query})
    for variant in terms:
        cursor = None
        for _ in range(3):
            remaining=deadline-time.monotonic()
            if remaining<=0:
                audit.update(coverage='partial',deadline_exhausted=True)
                continuation(variant,cursor)
                break
            receipt = {'terms': variant, 'cursor': cursor}
            audit['api_call_count'] += 1
            try:
                receipt['timeout_seconds']=min(15.0,remaining)
                response = api(base + '/sources/search', {'terms': variant, 'match': 'any', 'role': 'any', 'limit': 20, 'cursor': cursor}, timeout=receipt['timeout_seconds'])
                if not isinstance(response.get('items'), list):
                    raise ValueError('malformed_source_search_response')
                receipt.update(status='returned', next_cursor=response.get('next_cursor'), scanned_chunks=response.get('scanned_chunks'), returned_spans=len(response['items']))
                for item in response['items']:
                    did, cid = item.get('document_id'), item.get('chunk_id')
                    text = str(item.get('text') or '')
                    # SQL substring search is navigation only; avoid borrowing
                    # an unrelated word containing a short subject/version.
                    if not did or not cid or not any(re.search(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_.])', text, re.I) for term in variant):
                        continue
                    if did not in docs and len(docs) >= 24:
                        audit['coverage'] = 'partial'
                        audit['deferred_source_locators'].append({'document_id':did,'chunk_id':cid,
                            'source_path':'/v1/default/chunks/'+urllib.parse.quote(str(cid),safe='')})
                        continue
                    docs.setdefault(did, set()).add(cid)
                cursor = response.get('next_cursor')
            except Exception as error:
                receipt.update(status='failed', error_type=type(error).__name__)
                audit['coverage'] = 'partial'
                if time.monotonic()>=deadline:
                    audit['deadline_exhausted']=True
                    continuation(variant,cursor)
                audit['source_receipts'].append(receipt)
                break
            audit['source_receipts'].append(receipt)
            if cursor is None:
                break
        if cursor is not None:
            audit['coverage'] = 'partial'
            continuation(variant,cursor)
    pools, seen = [], {str(row.get('id')) for row in records}
    for did, chunks in docs.items():
        pool = []
        receipt = {'document_id': did, 'matched_chunk_ids': sorted(chunks), 'offset': 0, 'limit': 100}
        read_path=base+'/memories/list?'+urllib.parse.urlencode({'document_id':did,'state':'valid','limit':100,'offset':0})
        remaining=deadline-time.monotonic()
        if remaining<=0:
            audit.update(coverage='partial',deadline_exhausted=True)
            receipt.update(status='deferred_deadline',next_read={'method':'GET','path':read_path})
            audit['document_receipts'].append(receipt)
            continue
        audit['api_call_count'] += 1
        try:
            receipt['timeout_seconds']=min(15.0,remaining)
            response = api(read_path, timeout=receipt['timeout_seconds'])
            if not isinstance(response.get('items'), list):
                raise ValueError('malformed_source_facts_response')
            receipt.update(status='returned', total=response.get('total'), returned_facts=len(response['items']))
            if response.get('total', 0) > 100:
                receipt['next_offset'] = 100
                receipt['next_read'] = {'method':'GET','path':base+'/memories/list?'+urllib.parse.urlencode({'document_id':did,'state':'valid','limit':100,'offset':100})}
                audit['coverage'] = 'partial'
            for row in response['items']:
                if row.get('document_id') != did or row.get('chunk_id') not in chunks or row.get('state') != 'valid':
                    continue
                if types and (row.get('type') or row.get('fact_type')) not in types:
                    continue
                if temporal_window is not None:
                    row={**row,'retrieval_temporal_relation':temporal_relation(row,temporal_window)}
                    if row['retrieval_temporal_relation']=='unknown':
                        audit['unknown_temporal_count'] += 1
                    elif row['retrieval_temporal_relation']=='outside':
                        audit['outside_temporal_window_count'] += 1
                try:
                    mid = str(uuid.UUID(str(row.get('id'))))
                except (ValueError, AttributeError):
                    continue
                if mid not in seen:
                    seen.add(mid); pool.append({**row, 'id': mid})
        except Exception as error:
            receipt.update(status='failed', error_type=type(error).__name__)
            audit['coverage'] = 'partial'
            if time.monotonic()>=deadline:
                audit['deadline_exhausted']=True
                receipt['next_read']={'method':'GET','path':read_path}
        audit['document_receipts'].append(receipt)
        pools.append(pool)
    # Scheduling does not assert semantic relevance. Each document keeps its
    # backend order while long transcripts share the bounded candidate budget.
    result = [pool[rank] for rank in range(max(map(len,pools),default=0)) for pool in pools if rank<len(pool)]
    if temporal_window:
        result.sort(key=lambda row:{'inside':0,'outside':1,'unknown':2}.get(row.get('retrieval_temporal_relation'),2))
    if len(result)>600:
        audit['coverage']='partial'
        audit['candidate_budget_deferred_count']=len(result)-600
        result=result[:600]
    audit.update(new_candidate_count=len(result), seconds=time.monotonic() - started,
                 cost_boundary='Discovery HTTP requests only; source-context and live-page reads are counted separately by the caller.')
    return result, audit
