"""Resolve omitted subjects only through identity-checked original chunks.

The witness is local source navigation, never fact verification or a new
support vote. Retrieval query metadata cannot construct this field.
"""
import hashlib
import re
import urllib.parse

from .recall_relevance import classify_candidate, _scope_terms, _literal_targets, _identifier_roots, _semantic_text, _terms, _content, subject_contract, subject_witness


def _structural_text(text):
    """Preserve source offsets while excluding fenced examples from parsing."""
    lines=[]; fence=None
    for line in text.splitlines(keepends=True):
        match=re.match(r'^[ \t]{0,3}(`{3,}|~{3,})',line)
        if fence:
            lines.append(re.sub(r'[^\n]',' ',line))
            if match and match.group(1)[0]==fence[0] and len(match.group(1))>=len(fence): fence=None
        elif match:
            fence=match.group(1); lines.append(re.sub(r'[^\n]',' ',line))
        else: lines.append(line)
    return ''.join(lines)


def _subject_segments(query, text, include_unrelated=False):
    """Locate original discourse boundaries without granting speaker authority.

    A formatting role span, peer heading or an explicit topic switch prevents
    the neighbouring topic from acting as the extracted fact's subject. For
    compound subjects, a less-specific peer topic cannot borrow their name.
    """
    structural=_structural_text(text)
    markers = list(re.finditer(r'^\[(?:role: (?:user|assistant|tool|system|developer)|(?:user|assistant|tool|system|developer):end)\][ \t]*$',structural,re.M))
    roles = []
    for i,marker in enumerate(markers):
        if marker.group().startswith('[role: '):
            roles.append((marker.end(),markers[i+1].start() if i+1<len(markers) else len(text)))
    if not roles: roles=[(0,len(text))]
    segments=[]
    switches=r'(?im)(?:^\s*[^\n:：]{0,100}(?:\btopic|主题|话题)\s*[:：]|\b(?:separately|meanwhile)\b|(?:turning\s+to|moving\s+to|in|for)\s+(?:an?\s+)?(?:another|different|separate|unrelated)\s+(?:project|topic|task|school|company|institution)\b|换(?:个|一个)?话题|另一个(?:项目|任务|学校|公司)|转到(?:另一个|其他)(?:项目|话题)|(?:另外|另说)(?:[，,:：]|聊|说|问))'
    for left,right in roles:
        body=structural[left:right]
        headings=list(re.finditer(r'(?m)^\s*(#{1,6})\s+[^\n]+',body))
        boundaries=[left,right]
        stack=[]
        for heading in headings:
            level=len(heading.group(1))
            while stack and stack[-1][0]>=level: stack.pop()
            # Each child/peer owns its section body. Ancestor headings are
            # locators, never a blanket subject grant across sibling topics.
            stack.append((level,left+heading.start()))
            boundaries.append(stack[-1][1])
        boundaries.extend(left+m.start() for m in re.finditer(switches,body))
        if not markers and not headings:
            boundaries.extend(left+m.end() for m in re.finditer(r'\n\s*\n',body))
        points=sorted(set(boundaries))
        segments.extend((a,b) for a,b in zip(points,points[1:]) if a<b)
    if include_unrelated:return segments
    # Version/history wording is the requested relation, not another subject.
    # A bound label like Product5.0 must retain its actual identifier family.
    subject_query=re.sub(r'版本|演进|历史|\b(?:versions?|evolution|history|releases?|prior|previous|earlier|past)\b',' ',query,flags=re.I)
    contract=subject_contract(query)
    scope=contract['terms']
    identifiers=contract['identifiers']
    scores=[len(scope&_scope_terms(_semantic_text(text[a:b]))) + sum(bool(re.search(r'(?<![a-z0-9_])'+re.escape(root)+r'(?![a-z_])',_semantic_text(text[a:b]).casefold())) for root in identifiers) for a,b in segments]
    best=max(scores,default=0)
    return [(a,b) for (a,b),score in zip(segments,scores) if score==best and score>0]


def _clause_ranges(text,left,right):
    """Punctuation/line clauses; decimal dots and fenced examples stay intact."""
    structural=_structural_text(text)
    start=left;result=[]
    for delimiter in re.finditer(r'[。！？!?;；，,\n]|(?<!\d)\.|\.(?!\d)',structural[left:right]):
        end=left+delimiter.start()
        if structural[start:end].strip(): result.append((start,end))
        start=left+delimiter.end()
    if structural[start:right].strip():result.append((start,right))
    return result


def _clause_bridges(query,record,text,segments):
    """A body must match the actual subject-bearing clause, not its neighbour.

    An explicit pronoun can link only the immediately preceding clause, with
    a concrete body term independently present in that antecedent. A role or
    heading is a structural boundary, never a relationship assertion.
    """
    contract=subject_contract(query)
    identifiers=contract['identifiers'];subjects=contract['terms']
    body_terms=_terms(_semantic_text(_content(record)))
    all_clauses=[clause for a,b in _subject_segments(query,text,include_unrelated=True) for clause in _clause_ranges(text,a,b)]
    best_body_match=max((len(body_terms&_terms(_semantic_text(text[a:b]))) for a,b in all_clauses),default=0)
    def subject_present(fragment):
        return subject_witness(query,fragment,record)['matched']
    bridges=[]
    for left,right in segments:
        clauses=_clause_ranges(text,left,right)
        matches=[body_terms&_terms(_semantic_text(text[a:b])) for a,b in clauses]
        if not best_body_match:continue
        for index,(a,b) in enumerate(clauses):
            if len(matches[index])!=best_body_match:continue
            if subject_present(text[a:b]):
                bridges.append((a,b,'same_actual_clause'))
                continue
            if index==0:continue
            pronoun=re.match(r'^\s*(?:(?:and|but|then)\s+)?(?:it|its|this\s+(?:version|project|release)|that\s+(?:version|project|release))\b|^\s*(?:它|该(?:版本|项目|产品|系统)|这个(?:版本|项目|产品|系统))',text[a:b],re.I)
            if not pronoun:continue
            prior_a,prior_b=clauses[index-1]
            antecedent=_semantic_text(text[prior_a:prior_b])
            independent=(body_terms&_terms(antecedent))-subjects-set(identifiers)
            if subject_present(antecedent) and independent:
                bridges.append((prior_a,b,'explicit_pronoun_with_independent_antecedent_relation'))
    return bridges


def enrich_source_context(query, records, api, bank):
    cache, result = {}, []
    anchors = sorted(_scope_terms(str(query)) | set(_identifier_roots(str(query))), key=lambda x: -len(x))
    for original in records:
        record = {key: value for key, value in original.items() if key != 'source_context'}
        relation = classify_candidate(query, record)
        cid, did = record.get('chunk_id'), record.get('document_id')
        if relation['level'] not in {'none', 'unknown'} or _literal_targets(str(query)) or not cid or not did or not anchors:
            result.append(record); continue
        key = (str(cid), str(did))
        if key not in cache:
            try:
                chunk = api('/v1/default/chunks/' + urllib.parse.quote(str(cid), safe=''), timeout=8)
                cache[key] = chunk if isinstance(chunk, dict) and chunk.get('bank_id') == bank and chunk.get('chunk_id') == cid and chunk.get('document_id') == did and isinstance(chunk.get('chunk_text'), str) else None
            except Exception: cache[key] = None
        chunk = cache[key]
        if chunk:
            text = chunk['chunk_text']
            # A mixed transcript cannot borrow a distant topic as the subject.
            # Keep a local window around a query subject; direct body relation
            # to that window remains required by the relevance classifier.
            segments=_subject_segments(query,text)
            for start,end,bridge_method in _clause_bridges(query,record,text,segments):
                    witness = {'status':'source_read', 'document_id':did,
                        'source_revision':hashlib.sha256(text.encode()).hexdigest(), 'text':text[start:end],
                        'scope_source_range':{'start':start,'end':end,
                            'semantics':'actual_clause_relation_not_verified_project_identity'},
                        'subject_bridge_method':bridge_method,
                        'locators':[{'source_path':'bank:'+str(bank)+'/chunk:'+str(cid), 'byte_offset':start,
                                     'offset_semantics':'character_offset_in_original_chunk',
                                     'raw_line_sha256':hashlib.sha256(text.encode()).hexdigest(), 'chunk_id':cid, 'document_id':did}],
                        'claims_independently_verified':False, 'evidence_role':'source_context_navigation_only'}
                    test = {**record, 'source_context':witness}
                    if classify_candidate(query, test)['level'] == 'weak':
                        record = test; break
            if not record.get('source_context'):
                record['_unverified_source_locator']={'memory_id':record.get('id'),'document_id':did,'chunk_id':cid,
                    'source_revision':hashlib.sha256(text.encode()).hexdigest(),'subject_relation':'unverified',
                    'claim_verification':'not_performed','authority':'unverified_source_claim',
                    'next_action':{'tool':'read_source','arguments':{'memory_id':record.get('id'),'scope':'chunk'}}}
        result.append(record)
    return result
