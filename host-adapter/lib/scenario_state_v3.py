"""Source-linked Session state and deterministic three-tier navigation summaries."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .context_summary import BUDGETS, bounded_summary


FORM_TAG = "send_user_message_question_reply"
FORM_PATTERN = re.compile(rf"^\s*<{FORM_TAG}>\s*(.*?)\s*</{FORM_TAG}>\s*$", re.S)
STATE_FIELDS = ("subject", "goal", "phase", "constraints", "corrections", "assistant_reports", "unresolved")
STATE_LIMITS={'text_max_chars_per_claim':180,'message_ids_min_per_claim':1,
              'message_ids_max_per_claim':4}
# Cardinality is a transport bound, not a reason to drop explicit conditions.
# The rendered full tier still independently enforces its total hard budget.
STATE_LIMITS['verbatim_text_max_chars_per_claim']=BUDGETS['session']['standard']['max_chars']
STATE_LIMITS['claims_max_per_array']=max(8,min(64,BUDGETS['session']['full']['max_chars']//80))
STATE_FIELD_ROLES={'subject':'user','goal':'user','constraints':'user','corrections':'user',
                   'assistant_reports':'assistant','unresolved':'user'}
USER_CLAIM_PROTOCOL='user_constraints_corrections_whole_source_clause.v1'
INTENT_FIELD_RULES={'covered':{'request':['subject','goal','constraints','unresolved'],
                             'constraint':['constraints'],'correction':['corrections'],
                             'acknowledgment':['subject','goal','constraints']},
                    'answered':{'request':['assistant_reports']}}


class StateValidationError(ValueError):
    """Stable public code plus bounded contract diagnostics, never claim text."""
    def __init__(self, violations):
        super().__init__('scenario_state_invalid')
        self.violations = violations

    def safe_detail(self):
        allowed_reasons={'state_shape_invalid','claim_shape_invalid','text_invalid','text_over_limit',
                         'array_invalid','array_over_limit','message_ids_invalid','message_ids_count_invalid'}
        rows=[]
        for item in self.violations if isinstance(self.violations,list) else []:
            if not isinstance(item,dict):continue
            field=item.get('field');reason=item.get('reason')
            if (not isinstance(field,str) or not re.fullmatch(
                r'state|(?:subject|goal)/(?:text|message_ids)|(?:constraints|corrections|assistant_reports|unresolved)(?:/(?:0|[1-9][0-9]{0,2})/(?:text|message_ids))?',field)
                or not isinstance(reason,str) or reason not in allowed_reasons):continue
            row={'field':field,'reason':reason}
            for key in ('actual','minimum','maximum'):
                value=item.get(key)
                if type(value) is int and 0<=value<=10**9:row[key]=value
            rows.append(row)
        maximum=max(128,4+8*STATE_LIMITS['claims_max_per_array'])
        return {'reason':'state_contract_violations','fields':list(dict.fromkeys(r['field'] for r in rows[:maximum])),
                'violations':rows[:maximum],'total_violations':len(rows),'truncated':len(rows)>maximum}


def _check_state_contract(state):
    """Collect all structural repairs in one retry; do not truncate or accept."""
    if not isinstance(state,dict) or set(state)!=set(STATE_FIELDS):
        raise StateValidationError([{'field':'state','reason':'state_shape_invalid'}])
    violations=[]
    def check_claim(value,path):
        maximum=STATE_LIMITS.get('verbatim_text_max_chars_per_claim',STATE_LIMITS['text_max_chars_per_claim']) if path.split('/')[0] in {'constraints','corrections'} else STATE_LIMITS['text_max_chars_per_claim']
        if not isinstance(value,dict) or set(value)!={'text','message_ids'}:
            violations.append({'field':path+'/text','reason':'claim_shape_invalid'});return
        text=value['text'];ids=value['message_ids']
        if not isinstance(text,str) or not text.strip():
            violations.append({'field':path+'/text','reason':'text_invalid'})
        elif len(text)>maximum:
            violations.append({'field':path+'/text','reason':'text_over_limit','actual':len(text),
                               'maximum':maximum})
        if not isinstance(ids,list):
            violations.append({'field':path+'/message_ids','reason':'message_ids_invalid'})
        elif not STATE_LIMITS['message_ids_min_per_claim']<=len(ids)<=STATE_LIMITS['message_ids_max_per_claim']:
            violations.append({'field':path+'/message_ids','reason':'message_ids_count_invalid','actual':len(ids),
                               'minimum':STATE_LIMITS['message_ids_min_per_claim'],'maximum':STATE_LIMITS['message_ids_max_per_claim']})
    for field in ('subject','goal'):check_claim(state[field],field)
    for field in ('constraints','corrections','assistant_reports','unresolved'):
        values=state[field]
        if not isinstance(values,list):violations.append({'field':field,'reason':'array_invalid'});continue
        if len(values)>STATE_LIMITS['claims_max_per_array']:
            violations.append({'field':field,'reason':'array_over_limit','actual':len(values),
                               'maximum':STATE_LIMITS['claims_max_per_array']})
        for index,value in enumerate(values[:64]):check_claim(value,f'{field}/{index}')
    if violations:raise StateValidationError(violations)
PHASE_LABELS = {"requested": "待处理", "in_progress": "讨论或执行中",
                "assistant_reported": "已答复（助手自述未独立核验）", "unknown": "状态未核实"}
CORRECTION_HINT = re.compile(r"不要|别再|别写|不是|改成|改为|更正|纠正|名字叫|正确写法|实际上")


def whole_user_clauses(text):
    """Whole sentences or explicit list items; commas never split conditions."""
    answers=parse_form_reply(text)
    if answers:return [f"问题：{r['question']} 回答：{r['answer']}" for r in answers]
    normalized=' '.join(str(text).split())
    clauses=[normalized] if normalized else []
    segments=[];start=0
    for delimiter in re.finditer(r'[。；;？！]|[!?](?=\s|$)',normalized):
        segments.append(normalized[start:delimiter.end()]);start=delimiter.end()
    if start<len(normalized):segments.append(normalized[start:])
    for segment in segments:
        clause=segment.strip(' "\'“”‘’');clauses.append(clause)
        # A full stop followed by a new capitalized sentence is distinct from
        # dots inside URLs, paths, numbers and version identifiers. Preserve
        # conventional short title abbreviations rather than cutting names.
        starts=[0]
        for boundary in re.finditer(r'\.\s+(?=[A-Z])',clause):
            prefix=clause[:boundary.start()];word=re.search(r'([A-Za-z]+)$',prefix)
            if re.search(r'(?:\b[A-Za-z]\.)+[A-Za-z]$',prefix):continue
            if word and (len(word.group(1))==1 or word.group(1).lower() in {'mr','mrs','ms','dr','prof','sr','jr','vs','etc','dept','inc','ltd','corp','no','fig','approx'}):continue
            starts.append(boundary.end())
        if len(starts)>1:
            for i,start in enumerate(starts):clauses.append(clause[start:starts[i+1] if i+1<len(starts) else len(clause)].strip())
    for line in str(text).splitlines():
        match=re.match(r'^\s*(?:[-*•]|[0-9]+[.)、])\s+(.+)$',line)
        if match:clauses.append(' '.join(match.group(1).split()).strip(' "\'“”‘’'))
    return list(dict.fromkeys(c for c in clauses if c))


def user_clause_catalog(source):
    from source_safety import mask_text
    return [{'message_id':m['evidence_id'],'clauses':[
        {'text':mask_text(c),'eligible':len(c)<=STATE_LIMITS.get('verbatim_text_max_chars_per_claim',STATE_LIMITS['text_max_chars_per_claim']) and mask_text(c)==c}
        for c in whole_user_clauses(m['text'])]} for m in source['messages'] if m['role']=='user']


def state_field_catalog(source,draft,refs=None):
    refs=refs or {m['evidence_id']:m['evidence_id'] for m in source['messages']}
    result=[]
    for field,role in STATE_FIELD_ROLES.items():
        claims=[draft['state'][field]] if field in {'subject','goal'} else draft['state'][field]
        for i,claim in enumerate(claims):
            path=field+'/text' if field in {'subject','goal'} else f'{field}/{i}/text'
            result.append({'state_path':path,'role':role,'supported_message_refs':[refs[mid] for mid in claim['message_ids']],
                           'field_text':claim['text'],'summary_paths':[tier for tier in ('compact','standard','full')
                                                                     if claim['text'] in draft['summaries'][tier]]})
    return result


def parse_form_reply(text: str) -> list[dict] | None:
    match = FORM_PATTERN.fullmatch(str(text or ""))
    if not match:
        return None
    try:
        rows = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    if not isinstance(rows, list) or not rows:
        return None
    result = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        question, answer = row.get("question"), row.get("answer")
        if not isinstance(question, str) or not question.strip() or not isinstance(answer, str) or not answer.strip():
            return None
        result.append({"question": question.strip(), "answer": answer.strip()})
    return result


def project_source_messages(source: dict) -> list[dict]:
    from source_safety import mask_text

    projected = []
    for row in source.get("messages") or []:
        raw = str(row.get("text") or "")
        if raw.lstrip().startswith(f"<{FORM_TAG}>"):
            answers = parse_form_reply(raw)
            if answers:
                body = "；".join(f"问题：{item['question']} 回答：{item['answer']}" for item in answers)
                kind = "structured_user_answer"
            else:
                body, kind = "[结构化表单答复无法解析，需回读原文]", "opaque_structured_reply"
        else:
            body = raw
            kind = "user_direct" if row.get("role") == "user" else "assistant_report_only"
        if row.get("merge_projection"):
            kind = "source_linked_state_candidate"
        projected.append({"message_id": row.get("evidence_id"), "role": row.get("role"),
                          "at": row.get("at"), "turn_id": row.get("turn_id"),
                          "text": mask_text(body), "provenance_kind": kind})
    return projected


def correction_review_hints(source: dict, limit: int = 12) -> list[dict]:
    """Focus model attention without treating keyword matches as corrections."""
    from source_safety import mask_text

    hints = []
    for row in source.get("messages") or []:
        text = str(row.get("text") or "")
        if row.get("role") != "user" or text.lstrip().startswith(f"<{FORM_TAG}>"):
            continue
        matches = list(CORRECTION_HINT.finditer(text))
        if not matches:
            continue
        last = matches[-1]
        excerpt = " ".join(text[max(0, last.start() - 80):last.end() + 120].split())
        hints.append({"message_id": row.get("evidence_id"), "excerpt": mask_text(excerpt),
                      "evidence_role": "review_hint_not_verified_claim"})
    return hints[-max(1, min(200, int(limit))):]


def _claim(value: object, source_by_id: dict, allowed_role: str, maximum=None) -> dict:
    if not isinstance(value, dict) or set(value) != {"text", "message_ids"}:
        raise ValueError("scenario_state_invalid")
    text = value.get("text")
    ids = value.get("message_ids")
    if (not isinstance(text,str) or not text.strip() or len(text)>(maximum or STATE_LIMITS['text_max_chars_per_claim'])
            or not isinstance(ids,list) or not STATE_LIMITS['message_ids_min_per_claim']<=len(ids)<=STATE_LIMITS['message_ids_max_per_claim']):
        raise ValueError("scenario_state_invalid")
    if any(not isinstance(mid, str) or mid not in source_by_id for mid in ids) or len(set(ids)) != len(ids):
        raise ValueError("scenario_evidence_id_invalid")
    if any(source_by_id[mid]["role"] != allowed_role for mid in ids):
        raise ValueError("scenario_state_role_invalid")
    return {"text": " ".join(text.split()), "message_ids": ids}


def _list_claims(value: object, source_by_id: dict, role: str, maximum=None) -> list[dict]:
    if not isinstance(value, list) or len(value) > STATE_LIMITS['claims_max_per_array']:
        raise ValueError("scenario_state_invalid")
    return [_claim(item, source_by_id, role,maximum) for item in value]


def _normalize_correction_categories(state: dict, source_by_id: dict) -> None:
    """Move explicit user corrections out of constraints without rewriting text.

    Provider state passes occasionally classify a verbatim correction such as
    "不要把……写死" as a constraint.  The coverage contract treats the two
    fields differently, so keeping that misclassification would reject an
    otherwise source-complete draft.  Only an exact user clause containing one
    of the existing correction markers is eligible; generic constraints and
    assistant text are untouched.  This is categorisation, not semantic
    expansion or truncation.
    """
    constraints = state.get("constraints") or []
    corrections = state.get("corrections") or []
    existing = {(item.get("text"), tuple(item.get("message_ids") or []))
                for item in corrections if isinstance(item, dict)}
    retained = []
    for claim in constraints:
        if not isinstance(claim, dict):
            retained.append(claim)
            continue
        text = str(claim.get("text") or "")
        ids = claim.get("message_ids") or []
        is_explicit = bool(CORRECTION_HINT.search(text)) and bool(ids)
        is_user_clause = any(
            source_by_id.get(mid, {}).get("role") == "user"
            and text in whole_user_clauses(source_by_id[mid].get("text", ""))
            for mid in ids
        )
        key = (text, tuple(ids))
        if is_explicit and is_user_clause and key not in existing:
            if len(corrections) >= STATE_LIMITS['claims_max_per_array']:
                retained.append(claim)
                continue
            corrections.append(claim)
            existing.add(key)
        else:
            retained.append(claim)
    state["constraints"] = retained
    state["corrections"] = corrections


def _ensure_explicit_corrections(state: dict, source: dict) -> None:
    """Ensure every eligible, source-visible correction has a state field.

    A model may omit a later correction while still producing a structurally
    valid state.  The omission is unsafe for the coverage contract, so add the
    exact whole user clause as a source-linked correction.  This never invents
    wording and remains bounded by the existing claim/cardinality limits.
    """
    corrections = state.get("corrections") or []
    existing = {(item.get("text"), tuple(item.get("message_ids") or []))
                for item in corrections if isinstance(item, dict)}
    maximum = STATE_LIMITS.get("verbatim_text_max_chars_per_claim", STATE_LIMITS["text_max_chars_per_claim"])
    for message in source.get("messages") or []:
        if message.get("role") != "user":
            continue
        mid = message.get("evidence_id")
        for clause in whole_user_clauses(message.get("text", "")):
            if len(clause) > maximum or not CORRECTION_HINT.search(clause):
                continue
            key = (clause, (mid,))
            if key in existing:
                continue
            if len(corrections) >= STATE_LIMITS["claims_max_per_array"]:
                return
            corrections.append({"text": clause, "message_ids": [mid]})
            existing.add(key)
    state["corrections"] = corrections


def _render_tiers(state: dict) -> dict:
    compact_lines = [f"对象：{state['subject']['text']}", f"任务：{state['goal']['text']}",
                     f"当前阶段：{PHASE_LABELS[state['phase']]}"]
    standard_lines = list(compact_lines)
    if state["phase"] == "assistant_reported" and state["assistant_reports"]:
        report=state["assistant_reports"][-1]["text"]
        excerpt=report if len(report)<=80 else report[:80]+"…"
        compact_lines.append("最近进展（助手报告，未独立核验）："+excerpt)
    if state["unresolved"]:
        compact_lines.append("待核：" + state["unresolved"][0]["text"])
        standard_lines.append("待核：" + state["unresolved"][0]["text"])
    compact = "\n".join(compact_lines)
    for title, field in (("关键约束", "constraints"), ("后续纠正", "corrections")):
        standard_lines.extend(f"{title}：{item['text']}" for item in state[field][:3])
    if state["assistant_reports"]:
        standard_lines.append("助手报告（未独立核验）：" + state["assistant_reports"][-1]["text"])
    if len(state["unresolved"]) > 1:
        standard_lines.extend("其他待核：" + item["text"] for item in state["unresolved"][1:3])
    standard = "\n".join(standard_lines)
    full_lines = [f"对象：{state['subject']['text']} [来源:{','.join(state['subject']['message_ids'])}]",
                  f"任务：{state['goal']['text']} [来源:{','.join(state['goal']['message_ids'])}]",
                  f"当前阶段：{PHASE_LABELS[state['phase']]}"]
    for title, field in (("约束", "constraints"), ("纠正", "corrections"),
                         ("助手报告（未独立核验）", "assistant_reports"), ("未决", "unresolved")):
        for item in state[field]:
            full_lines.append(f"{title}：{item['text']} [来源:{','.join(item['message_ids'])}]")
    return {"compact": compact, "standard": standard, "full": "\n".join(full_lines)}


def validate_state_draft(source: dict, state: dict, *, model: str, user_claim_protocol=None) -> dict:
    if source.get("status") != "complete" or not source.get("messages") or not source.get("source_revision"):
        raise ValueError("scenario_source_incomplete")
    source_by_id = {row["evidence_id"]: row for row in source["messages"]}
    # Normalize a narrow provider categorisation error before the structural
    # contract check; no text is generated, widened, or dropped.
    _normalize_correction_categories(state, source_by_id)
    # A merge projection contains compressed candidate text, not the original
    # user wording; do not manufacture a new clause from that projection.
    if not source.get("merge_projection"):
        _ensure_explicit_corrections(state, source)
    _check_state_contract(state)
    normalized = {"subject": _claim(state["subject"], source_by_id, STATE_FIELD_ROLES['subject']),
                  "goal": _claim(state["goal"], source_by_id, STATE_FIELD_ROLES['goal'])}
    phase = state.get("phase")
    if phase not in PHASE_LABELS:
        raise ValueError("scenario_state_phase_invalid")
    normalized["phase"] = phase
    for field in ('constraints','corrections','assistant_reports','unresolved'):
        maximum=STATE_LIMITS.get('verbatim_text_max_chars_per_claim',STATE_LIMITS['text_max_chars_per_claim']) if field in {'constraints','corrections'} else None
        normalized[field] = _list_claims(state[field], source_by_id, STATE_FIELD_ROLES[field],maximum)
    if user_claim_protocol not in {None,USER_CLAIM_PROTOCOL}:raise ValueError('scenario_state_invalid')
    if user_claim_protocol==USER_CLAIM_PROTOCOL:
        for field in ('constraints','corrections'):
            for claim in normalized[field]:
                if not any(claim['text'] in whole_user_clauses(source_by_id[mid]['text']) for mid in claim['message_ids']):
                    raise ValueError('scenario_user_claim_not_verbatim')
    positions={row['evidence_id']:i for i,row in enumerate(source['messages'])}
    normalized['assistant_reports'].sort(key=lambda claim:max(positions[mid] for mid in claim['message_ids']))
    last = source["messages"][-1]
    if last["role"] == "user":
        if phase not in {"requested", "unknown"}:
            raise ValueError("scenario_state_phase_invalid")
    if last["role"] == "user" and parse_form_reply(last["text"]) is None:
        if not any(last["evidence_id"] in item["message_ids"] for item in normalized["unresolved"]):
            raise ValueError("scenario_state_unresolved_final_request")
    if phase == "assistant_reported" and (not normalized["assistant_reports"] or
            not any(last["evidence_id"] in item["message_ids"] for item in normalized["assistant_reports"])):
        raise ValueError("scenario_state_phase_invalid")
    tiers = _render_tiers(normalized)
    summaries = {}
    for tier, text in tiers.items():
        bounded, receipt = bounded_summary(text, "session", tier)
        if not bounded or receipt["truncated"] or len(text) > BUDGETS["session"][tier]["max_chars"]:
            raise ValueError("scenario_summary_budget_or_empty")
        summaries[tier] = bounded
    evidence = [{"statement": item["text"], "message_ids": item["message_ids"], "field": field}
                for field in ("subject", "goal") for item in [normalized[field]]]
    evidence.extend({"statement": item["text"], "message_ids": item["message_ids"], "field": field}
                    for field in ("constraints", "corrections", "assistant_reports", "unresolved")
                    for item in normalized[field])
    unknowns = [item["text"] for item in normalized["unresolved"]]
    if last["role"] == "user" and parse_form_reply(last["text"]) is None:
        unknowns.append("最后一条用户请求后未见助手最终答复。")
    return {**({'state_claim_protocol':user_claim_protocol} if user_claim_protocol is not None else {}),
            "schema": "evolving-profile.scenario-draft.v3", "context_id": "session:" + source["thread_id"],
            "context_type": "session", "status": "source_linked_draft", "review_status": "pending_independent_review",
            "source_check": "message_id_and_role_only_not_semantic_entailment",
            "source": source.get("source"), "source_files": source.get("source_files") or [],
            "source_revision": source["source_revision"], "source_message_count": len(source["messages"]),
            "summary_model": model, "generated_at": datetime.now(timezone.utc).isoformat(),
            "state": normalized, "summaries": summaries, "evidence": evidence, "unknowns": unknowns,
            "evidence_role": "context_navigation_only"}
