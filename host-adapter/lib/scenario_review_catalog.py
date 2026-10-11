"""Exact review choices, not model judgments or synthesized evidence."""
from __future__ import annotations
import copy,hashlib,json
from .scenario_state_v3 import whole_user_clauses,state_field_catalog


def review_catalog(source,bundle):
    aliases={m['evidence_id']:f'm{i}' for i,m in enumerate(source['messages'],1)}
    quotes=[];fields=[]
    for message in source['messages']:
        if message['role']!='user':continue
        candidates=list(dict.fromkeys([message['text'],*whole_user_clauses(message['text'])]))
        for index,text in enumerate(candidates,1):
            quotes.append({'ref':f"{aliases[message['evidence_id']]}q{index}",'message_ref':aliases[message['evidence_id']],
                           'source_quote':text,'whole_clause':text in whole_user_clauses(message['text'])})
    for index,episode in enumerate(bundle['episodes'],1):
        for n,field in enumerate(state_field_catalog(source,episode['draft'],aliases),1):
            fields.append({'ref':f'e{index}f{n}','episode_ref':f'e{index}',**field})
    from .scenario_model import fingerprint_episode_bundle
    value={'schema':'evolving-profile.review-selection-catalog.v1',
           'thread_id':source['thread_id'],'source_revision':source['source_revision'],
           'bundle_sha256':fingerprint_episode_bundle(bundle),'source_quotes':quotes,'state_fields':fields}
    return value


def catalog_sha(catalog):
    return hashlib.sha256(json.dumps(catalog,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def expand_review_choices(checked,catalog):
    # Lazy import avoids a module cycle. All expanded fields are validated by
    # the unchanged exact-source/role/episode/primary-link gate afterwards.
    from .memory_recovery_scenario import CoverageError
    result=copy.deepcopy(checked)
    result.pop('selection_annotations',None)  # Internal authority is never provider-controlled.
    quotes={r['ref']:r for r in catalog['source_quotes']};fields={r['ref']:r for r in catalog['state_fields']}
    entries=result.get('user_intent_coverage')
    if not isinstance(entries,list):return result,False
    has_choices=any(isinstance(entry,dict) and isinstance(entry.get('intents'),list) and any(
        isinstance(intent,dict) and ('source_quote_ref' in intent or 'superseding_quote_ref' in intent or
        isinstance(intent.get('field_links'),list) and any(isinstance(link,dict) and 'field_ref' in link for link in intent['field_links']))
        for intent in entry['intents']) for entry in entries)
    if has_choices and result.get('selection_catalog_sha256')!=catalog_sha(catalog):
        raise CoverageError('review_catalog_binding_mismatch')
    used=False;annotations=[]
    for entry in entries:
        if not isinstance(entry,dict) or not isinstance(entry.get('intents'),list):continue
        for intent in entry['intents']:
            if not isinstance(intent,dict):continue
            selected_reply_fields=[]
            for ref_key,quote_key,message_key in [('source_quote_ref','source_quote','message_ref'),('superseding_quote_ref','superseding_quote','superseded_by_ref')]:
                if ref_key not in intent:continue
                ref=intent[ref_key];item=quotes.get(ref) if isinstance(ref,str) else None
                expected=entry.get(message_key) if message_key=='message_ref' else intent.get(message_key)
                if item is None or item['message_ref']!=expected or quote_key in intent:
                    raise CoverageError('review_catalog_choice_invalid')
                intent[quote_key]=item['source_quote'];del intent[ref_key];used=True
            links=intent.get('field_links')
            if not isinstance(links,list):continue
            for index,link in enumerate(links):
                if not isinstance(link,dict) or 'field_ref' not in link:continue
                ref=link['field_ref'];item=fields.get(ref) if isinstance(ref,str) else None
                if item is None or set(link)-{'field_ref','context_note'}:raise CoverageError('review_catalog_choice_invalid')
                if 'context_note' in link:
                    from source_safety import mask_text
                    note=link['context_note']
                    if not isinstance(note,str) or len(note)>500:raise CoverageError('review_catalog_choice_invalid')
                    annotations.append({'message_ref':entry.get('message_ref'),'field_ref':ref,
                        'text':mask_text(note),'authority':'model_comment_not_source_or_verdict'})
                links[index]={'episode_ref':item['episode_ref'],'state_path':item['state_path'],
                              'field_quote':item['field_text'],'summary_paths':item['summary_paths']}
                if item['role']=='assistant' and item['state_path'].startswith('assistant_reports/'):
                    selected_reply_fields.append(item)
                used=True
            if intent.get('disposition')=='answered' and 'answer_message_ref' not in intent and selected_reply_fields:
                # The model already selected the exact reply field. Resolve
                # its single source, never infer an answer from unselected text.
                message_ref=entry.get('message_ref')
                all_refs={ref for field in selected_reply_fields for ref in field['supported_message_refs']}
                if (len(all_refs)==1 and isinstance(message_ref,str) and message_ref.startswith('m')
                    and message_ref[1:].isdigit()):
                    answer_ref=next(iter(all_refs))
                    if isinstance(answer_ref,str) and answer_ref.startswith('m') and answer_ref[1:].isdigit() and int(answer_ref[1:])>int(message_ref[1:]):
                        intent['answer_message_ref']=answer_ref
    if annotations:result['selection_annotations']=annotations
    return result,used
