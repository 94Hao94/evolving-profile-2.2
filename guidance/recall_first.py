"""Local intent expansion and corpus-weighted retrieval; no foreground LLM calls.

Scores are ranking signals, not probabilities. Lifecycle and explicit scope
are hard boundaries; missing topic keywords are not a universal veto.
"""
import base64
from collections import Counter
from hashlib import sha256
import json
import math
import os
import re

VERSION='recall-first.v2-20260922'
DEFAULT_CANDIDATE_LIMIT = 6
MAX_CANDIDATE_LIMIT = 20
INTENTS={
 'current_draft':('我改','我修改','改好的','当前底稿','用户修改','用户改','甲方修改','不要改回','原稿','最新版本','权威底稿','最小必要','修订'),
 'consistency':('附表','附件','预算','费用','金额','正文','一致','同步','联动','多文件','多文档','整体都调','所有文件'),
 'verification':('检查','检察','验收','测试','复测','复查','回归','验证','确认','修复','返工','没改好','看不清','溢出','computeruse','computer use','点击','弹出'),
 'explanation':('解释','讲清','讲明','没明白','不懂','看不懂','举例','例子','比喻','类比','怎么回事','是什么','什么意思','原理','术语','音标','全称'),
 'memory_selection':('多维度','偏好','注入','召回','记忆','selector','mcp','recall','research','相关性','漏选','误选'),
 'policy':('政策','必要性','申报','引用','国家级','文件名称'),
 'recommendation':('推荐','建议','比较','权衡','决策','方案','选项','选不选','该不该','要不要'),
 'visual':('界面','页面','视觉','可读','字体','颜色','格式','图表','图谱','流程图','架构图','星座'),
 'execution':('执行','授权','连续推进','继续推进','进度','未完成','不要停','开工'),
 'tool_recovery':('computer_use','computer use','工具失败','工具不可用','超时','timeout','重试','恢复验收','验证工具'),
}
WEIGHTS={'current_draft':12,'consistency':9,'verification':7,'policy':8,'memory_selection':7,'explanation':7,'recommendation':4,'visual':4,'execution':3,'tool_recovery':10}
SWITCH=re.compile(r'换个话题|另一个问题|新任务|不做.{0,30}了|停止.{0,30}(?:改做|开始)')
_CONDITION_PLATFORM_TERMS=('windows','macos','android','ios','u盘','便携')
_DOMAIN_TERMS={
 'visual_document':('ppt','pptx','幻灯片','word','docx','文档','表格','附表','附件','公文','投屏','排版'),
 'web_ui':('网站','网页','按钮','界面','浏览器','桌面应用','交互页面'),
 'memory_system':('记忆系统','记忆链路','长期记忆','召回','注入','memory map','memory_check','recall','research','read_source','bank'),
 'software':('软件','系统','代码','接口','链路','网站','网页','按钮','浏览器','app','应用故障'),
 'procurement':('采购','招标','投标','标书','预算','费用','金额','报销','评审'),
 'video':('视频','字幕','剪辑','画面'),
 'formal_writing':('对外说明','正式文本','报告','方案文档','客户文案','申报材料'),
 'tooling':('skill','computer use','computer_use','工具选择','工具失败'),
 'education':('高校','高教','课程','教学','学校','实训'),
 'observability':('观察页','状态页','链路页','回执页面','审计页面','时间线模块'),
 'context_management':('上下文压缩','存储加密','filevault','安全配置','资源管理'),
 'diagram':('流程图','链路图','布局图','架构图','图谱','星座图'),
 'customer_visit':('客户拜访','拜访统计','外部签到','签到计数','去重客户日'),
}


def topic_domains(text):
    value=str(text or '').casefold()
    return {domain for domain,aliases in _DOMAIN_TERMS.items() if any(alias in value for alias in aliases)}


def task_topic_domains(text):
    value=str(text or '').casefold();domains=topic_domains(value)
    if re.search(r'(?:不要|不需要|没让你|不用|无需|别给我).{0,8}(?:ppt|幻灯片)',value,re.I):
        positive=re.sub(r'(?:不要|不需要|没让你|不用|无需|别给我).{0,8}(?:ppt|幻灯片)','',value,flags=re.I)
        if not any(term in positive for term in ('ppt','pptx','幻灯片','word','docx','文档','表格','附表','附件','公文','排版')):
            domains.discard('visual_document');domains.discard('formal_writing')
    return domains


def named_product_anchors(text):
    return {value.casefold() for value in re.findall(r'\b[A-Z][a-z]+[A-Z][A-Za-z0-9_-]*\b',str(text or ''))}


def trivial_self_contained_task(text):
    value=re.sub(r'\s+','',str(text or '')).casefold()
    if re.fullmatch(r'(?:请?计算)?\d+(?:\.\d+)?(?:乘以|加|减|除以|[×x*+\-/])\d+(?:\.\d+)?(?:等于多少)?[？?]?',value):
        return True
    if ('翻译' in value and len(value)<=80 and not any(term in value for term in ('历史','偏好','术语风格','以前'))):
        return True
    if any(term in value for term in ('从小到大','从大到小','升序','降序')):
        residue=re.sub(r'[0-9.,，、；;：:\-从小到大升序降序排序把个数字换个话题。]+','',value)
        if not residue or (re.search(r'\d',value) and any(term in value for term in ('只给结果','排好','排序'))):return True
    return False

def semantic_condition_conflicts(unit, task):
    """Return true only for a direct conflict with the current Prompt."""
    raw_condition=' '.join(unit.get('applies_when') or [])
    condition=raw_condition.casefold()
    if not condition:
        return False
    task_text=' '.join([str(task.get('current_user_message') or ''),str(task.get('objective') or ''),str(task.get('context_summary') or '')]).casefold()
    unit_text=corpus(unit)
    if re.search(r'只给结论|一句话|不要展开',task_text) and not re.search(r'建议|推荐度|决策|结论',unit_text):
        return True
    if re.search(r'只诊断|先别动代码',task_text) and not re.search(r'诊断|故障|原因|根因|日志|进程|时序|排查|异常',unit_text):
        return True
    if re.search(r'(?:不要|不需要|没让你|不用|无需|别给我).{0,8}(?:ppt|幻灯片)',task_text,re.I) and re.search(r'ppt|幻灯片',condition,re.I):
        return True
    return False


def semantic_applicability_risks(unit, task):
    """Describe missing applicability evidence without hiding the candidate."""
    raw_condition=' '.join(unit.get('applies_when') or [])
    condition=raw_condition.casefold()
    task_text=' '.join([str(task.get('current_user_message') or ''),str(task.get('objective') or ''),str(task.get('context_summary') or '')]).casefold()
    unit_text=corpus(unit);risks=[]
    named=[name.casefold() for name in re.findall(r'\b[A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*)+\b',raw_condition)]
    named.extend(named_product_anchors(unit_text))
    if any(name not in task_text for name in named):risks.append('named_condition_unmentioned')
    if any(term in condition and term not in task_text for term in _CONDITION_PLATFORM_TERMS):risks.append('platform_condition_unmentioned')
    unit_domains=topic_domains(unit_text);task_domains=task_topic_domains(task_text)
    if unit_domains and not unit_domains.intersection(task_domains):risks.append('domain_condition_unmentioned')
    condition_match=any(value and value.casefold() in task_text for value in unit.get('applies_when') or [])
    if not condition_match and not (intents(task_text)&intents(unit_text)):risks.append('behavioral_condition_unconfirmed')
    return list(dict.fromkeys(risks))

def terms(text):
    text=str(text or '').casefold()
    result=set(re.findall(r'[a-z][a-z0-9_+-]{1,}',text))
    for chunk in re.findall(r'[\u4e00-\u9fff]+',text):
        for size in (2,3):
            result.update(chunk[i:i+size] for i in range(len(chunk)-size+1))
    return result

def intents(text):
    text=str(text or '').casefold()
    found={key for key,aliases in INTENTS.items() if any(alias in text for alias in aliases)}
    if re.search(r'我.{0,12}(?:修改|改动|改了)|(?:基于|保留).{0,12}(?:现在|原来|格式)',text):found.add('current_draft')
    return found

def corpus(unit):
    return ' '.join([str(unit.get('text') or ''),*unit.get('applies_when',[]),str(unit.get('effect_on_action') or '')])

def runtime_event_text(task):
    """Bounded execution facts used for a mid-task guidance refresh only."""
    events=task.get('runtime_events') or []
    values=[]
    for event in events[:4]:
        if not isinstance(event,dict):continue
        for key in ('capability','tool','failure','occurrence','required_for'):
            value=event.get(key)
            if value is not None:values.append(str(value)[:160])
    return ' '.join(values)

def scoped_out(unit,task):
    scope=unit.get('scope') or {}
    for scope_key,task_key in (('project_ids','project_ids'),('task_ids','task_ids'),('agent_roles','agent_roles')):
        required=set(scope.get(scope_key) or [])
        if required and not required.intersection(task.get(task_key) or []):return True
    return False

def rank(units,task):
    current=str(task.get('current_user_message') or task.get('objective') or '')
    runtime=runtime_event_text(task)
    context='' if SWITCH.search(current) or not task.get('continuation') else str(task.get('context_summary') or '')
    # Context is useful for short continuations; long standalone questions
    # should not be dominated by an earlier unrelated topic.
    context_weight=.8 if task.get('continuation') else 0
    qterms=terms(current+' '+runtime);cterms=terms(context)
    qi=intents(current+' '+runtime);ci=intents(context)
    docs=[terms(corpus(u)) for u in units]
    df=Counter(t for doc in docs for t in doc)
    idf={t:math.log(1+(len(units)+1)/(count+1)) for t,count in df.items()}
    ranked=[]
    for unit,doc in zip(units,docs):
        state=(unit.get('preference_audit') or {}).get('state','not_reviewed')
        if state not in {'approved','restricted'} or scoped_out(unit,task):continue
        body=corpus(unit);ui=intents(body);conditions=' '.join(unit.get('applies_when') or [])
        if re.search(r'只给结论|一句话|不要展开',current) and not re.search(r'建议|推荐度|决策|结论',body):continue
        if re.search(r'只诊断|先别动代码',current) and not re.search(r'诊断|故障|原因|根因|日志|进程|时序|排查|异常',body):continue
        unit_domains=topic_domains(body);primary_domains=topic_domains(unit.get('text'));task_domains=task_topic_domains(current+' '+context+' '+runtime)
        task_text=(current+' '+context+' '+runtime).casefold();condition_text=conditions.casefold()
        if any(term in condition_text and term not in task_text for term in _CONDITION_PLATFORM_TERMS):continue
        named_conditions={name.casefold() for name in re.findall(r'\b[A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*)+\b',conditions)}
        named_conditions.update(named_product_anchors(conditions))
        if any(name not in task_text for name in named_conditions):continue
        hard_domains={'memory_system','procurement','video','tooling','education','observability','context_management','diagram','customer_visit'}
        if any(domain in primary_domains and domain not in task_domains for domain in hard_domains):continue
        if any(anchor not in (current+' '+context).casefold() for anchor in named_product_anchors(body)):continue
        if unit_domains & {'visual_document','web_ui','formal_writing','software'} and not unit_domains.intersection(task_domains):continue
        # Explicitly declined output types must not be reintroduced by a
        # remembered formatting requirement.
        if re.search(r'(?:不要|不需要|没让你|不用|无需|别给我).{0,8}(?:ppt|幻灯片)',current,re.I) and re.search(r'ppt|幻灯片',conditions,re.I):continue
        if re.search(r'(?:不用|不要|不需要)公文',current) and '公文' in conditions:continue
        if re.search(r'政策|必要性',current) and re.search(r'(?:删除|削减).{0,8}(?:政策|背景)',str(unit.get('text') or '')):continue
        shared=qi & ui
        contextual=(ci & ui)-shared
        topic_score=max([WEIGHTS[k] for k in shared] or [0])+.2*sum(WEIGHTS[k] for k in shared)+context_weight*sum(WEIGHTS[k] for k in contextual)
        # Match the concrete behavioral obligation, not the number of generic
        # actions mentioned in a long preference.
        core_text=str(unit.get('text') or '')
        focus={
          'current_draft':bool(re.search(r'权威底稿|最小必要|以甲方|以用户|用户.{0,8}(?:底稿|改稿)',core_text)),
          'consistency':bool(re.search(r'一致|同步',core_text) and re.search(r'附表|预算|多文档|所有相关',core_text)),
          'policy':bool(re.search(r'政策文件|国家级|引用.{0,8}政策',core_text)),
          'verification':bool(re.search(r'回归|修复后|真实.{0,5}(?:交互|页面)|用户可见',core_text)),
          'memory_selection':bool(re.search(r'相关记忆|相关信息|只注入|上下文负担|漏注|误注',core_text)),
          'tool_recovery':bool(re.search(r'工具.{0,12}(?:失败|不可用|超时)|computer\s*use|先.{0,8}(?:修复|解决).{0,12}(?:工具|skill)|替代.{0,12}(?:不得|不能)',core_text,re.I)),
        }
        focused=[key for key,yes in focus.items() if yes and key in qi]
        topic_score+=30*bool(focused)
        # Require an actual task noun/condition as well as generic intent for
        # specialized subjects (names are not generalized across projects).
        specialised=re.findall(r'光泰|竞业达|智慧教室|录播|吊顶麦克风|谈判|五步法|因果认知|物理|科学|PPT|Word|文档|标书|招投标|视频|客户|高校|政府|事迹|公文',conditions,re.I)
        task_text=current+' '+context
        if re.search(r'申报|附表|附件|docx|word|文档',task_text,re.I):task_text+=' 文档 Word'
        if 'policy' in qi:task_text+=' 政府'
        if re.search(r'因果|原理|概念',current):task_text+=' 科学'
        if specialised and not any(w.casefold() in task_text.casefold() for w in specialised):continue
        def similarity(q):
            overlap=sum(idf.get(t,1)**2 for t in doc&q)
            return overlap/math.sqrt(max(1,sum(idf.get(t,1)**2 for t in doc))*max(1,sum(idf.get(t,1)**2 for t in q)))
        lexical=similarity(qterms)+context_weight*similarity(cterms)
        score=topic_score+8*lexical
        if not shared and not contextual and lexical<.045:continue
        condition_match=any(c and c in current for c in unit.get('applies_when',[]))
        domain_match=bool(unit_domains & task_domains)
        cross_cutting=bool((unit.get('scope') or {}).get('cross_cutting'))
        learning_match=unit.get('primary_category')=='learning' and 'explanation' in shared
        recommendation_match='recommendation' in shared
        if shared and not focused and not condition_match and not domain_match and not cross_cutting and not learning_match and not recommendation_match and lexical<.06:continue
        if condition_match:score+=10
        ranked.append((score,unit,{'intents':sorted(shared),'focused':focused,'context_intents':sorted(contextual),'lexical':round(lexical,4),'score':round(score,3),'tier':'core' if topic_score>=7 or condition_match else 'related'}))
    ranked.sort(key=lambda r:(r[0],r[1]['id']),reverse=True)
    # Weak evidence may add two useful references, not suppress core items.
    weak=0;out=[];seen=set()
    for score,u,why in ranked:
        signature=json.dumps([u.get(k) for k in ('text','scope','applies_when','exceptions')],ensure_ascii=False,sort_keys=True)
        if signature in seen or any(_near_duplicate(u,prior) for prior,_ in out):continue
        seen.add(signature)
        if why['tier']=='related':
            weak+=1
            if weak>2:continue
        out.append((u,why))
    return out


def _near_duplicate(unit, previous):
    def norm(value):return re.sub(r'[^a-z0-9\u4e00-\u9fff]+','',str(value or '').casefold())
    quotes={norm(ref.get('quote')) for ref in unit.get('evidence_refs') or [] if len(norm(ref.get('quote')))>=20}
    previous_quotes={norm(ref.get('quote')) for ref in previous.get('evidence_refs') or [] if len(norm(ref.get('quote')))>=20}
    return bool(quotes & previous_quotes)


def _deduplicate_ranked(rows):
    result=[]
    for row in rows:
        if any(_near_duplicate(row[0],previous[0]) for previous in result):continue
        result.append(row)
    return result


def semantic_rank(units, task, embed_many):
    """Discover paraphrased candidates before the normal packet budget applies."""
    from semantic_recall import semantic_candidates_with_batch
    query=str(task.get('current_user_message') or task.get('objective') or '')
    if not query:
        return []
    return semantic_candidates_with_batch(units, query, embed_many=embed_many, task=task)

def compact_unit(unit,why):
    result={k:unit.get(k) for k in ('id','revision','primary_category','nature','text','scope','applies_when','exceptions','effect_on_action')}
    result['content_sha256']=sha256(json.dumps({key:unit.get(key) for key in ('text','applies_when','exceptions','effect_on_action')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    result['source_locator']={'tool':'read_preference_unit','id':unit['id'],'revision':unit['revision'],'evidence_count':len(unit.get('evidence_refs') or [])}
    result['evidence_manifest']=[{key:ref.get(key) for key in ('memory_id','document_id','chunk_id','stored_role','origin','source_revision')}
                                 for ref in (unit.get('evidence_refs') or [])[:3]]
    result['preference_audit']={k:(unit.get('preference_audit') or {}).get(k) for k in ('state','validity_kind')}
    result.update(selection_reason=why,guidance_role='advisory_reference',may_override_current_prompt=False,may_authorize_action=False)
    return result

def select(repo,request,char_budget=None):
    task=request.get('task') or {};max_tokens=max(500,min(8000,int(request.get('max_tokens') or 3000)))
    requested_limit=request.get('max_candidates',DEFAULT_CANDIDATE_LIMIT)
    candidate_limit=max(1,min(MAX_CANDIDATE_LIMIT,int(requested_limit)))
    budget=char_budget if char_budget is not None else max_tokens*2
    result={'selection_revision':repo.active_revision(),'selector_version':VERSION,'stable_profile':[],'preference_candidates':[],'included':[],'model_sections':[],'already_loaded_valid':[],'deferred':[],'held':[],'errors':[],'next_cursor':None,'coverage':'complete_active_set','foreground_model_calls':0,'long_term_model_created':False,'decision_policy':{'guidance_is_advisory':True,'current_prompt_wins':True,'preference_cannot_authorize_or_force_execution':True}}
    if request.get('memory_policy')=='forbidden':result['coverage']='not_requested';return result
    if not str(task.get('objective') or '').strip():result.update(coverage='invalid_request',errors=['task_objective_required']);return result
    if trivial_self_contained_task(task.get('current_user_message') or task.get('objective')):
        result['budget']={'requested_max_tokens':max_tokens,'soft':True,'candidate_scope':'trivial_self_contained','body_serialized_chars':0,'candidate_count':0,'semantic_recall':'skipped','estimated_response_tokens':0}
        return result
    query_hash=sha256(json.dumps([VERSION,task],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    result['validity_token']='sha256:'+query_hash
    start=0
    if request.get('cursor'):
        try:
            cursor=json.loads(base64.urlsafe_b64decode(request['cursor']+'='*(-len(request['cursor'])%4)))
            if cursor['query_revision']!=query_hash or cursor['selection_revision']!=repo.active_revision():raise ValueError()
            start=int(cursor['offset'])
            if start<0:raise ValueError()
        except Exception:result.update(coverage='cursor_invalidated',errors=['cursor_scope_or_revision_changed']);return result
    loaded={(r.get('id'),r.get('revision'),r.get('content_sha256')) for r in request.get('loaded',[]) if isinstance(r,dict)}
    active_units=repo.active_units()
    from profile_projection import stable_profile
    result['stable_profile']=stable_profile(active_units)
    ranked=rank(active_units,task)
    semantic_embed_many=request.get('_semantic_embed_many')
    semantic_status='not_configured'
    semantic_service=None
    if not callable(semantic_embed_many) and request.get('semantic_recall')=='local':
        from semantic_recall import LocalGuidanceEmbeddingClient, SemanticRecallService
        base=os.getenv('EVOLVING_PROFILE_GUIDANCE_SEMANTIC_API_BASE','http://127.0.0.1:12088')
        bank=os.getenv('EVOLVING_PROFILE_GUIDANCE_BANK_ID','personal-memory')
        cache=os.getenv('EVOLVING_PROFILE_GUIDANCE_SEMANTIC_CACHE','/Users/apple/.evolving-profile/guidance-v1/semantic-vectors.json')
        semantic_service=SemanticRecallService(
            LocalGuidanceEmbeddingClient(f'{base.rstrip("/")}/v1/default/banks/{bank}/internal/guidance-embeddings'),cache
        )
    if callable(semantic_embed_many) or semantic_service is not None:
        try:
            semantic_results=(semantic_rank(active_units,task,semantic_embed_many) if callable(semantic_embed_many)
                              else semantic_service.candidates(active_units,task))
            existing={unit['id'] for unit,_ in ranked}
            semantic_backfill=[]
            for semantic in semantic_results:
                unit=semantic['unit']
                if unit['id'] in existing:
                    for ranked_unit, reason in ranked:
                        if ranked_unit['id'] == unit['id']:
                            reason['semantic'] = semantic['reason']
                            break
                    continue
                if semantic_condition_conflicts(unit,task):
                    continue
                existing.add(unit['id'])
                semantic_backfill.append((unit,{'intents':[],'focused':[],'context_intents':[],'lexical':0.0,
                                                 'score':semantic['reason']['score'],'tier':'semantic_backfill','semantic':semantic['reason'],
                                                 'applicability_risks':semantic_applicability_risks(unit,task)}))
                if len(semantic_backfill)==2:
                    break
            # Recall-first policy: two high-confidence paraphrase matches get
            # packet space before lower-ranked lexical overflow can consume it.
            ranked=_deduplicate_ranked(semantic_backfill+ranked)
            semantic_status='available'
        except Exception as exc:
            semantic_status='degraded:'+type(exc).__name__
    next_offset=None;used=0
    page=ranked[start:start+candidate_limit]
    for index,(u,why) in enumerate(page,start):
        item=compact_unit(u,why)
        cost=len(json.dumps(item,ensure_ascii=False,separators=(',',':')))
        if (u['id'],u['revision'],item['content_sha256']) in loaded:result['already_loaded_valid'].append({'id':u['id'],'revision':u['revision'],'content_sha256':item['content_sha256']});continue
        if next_offset is None and used+cost<=budget:
            result['included'].append(item);used+=cost
        else:
            if next_offset is None:next_offset=index
            result['deferred'].append({'id':u['id'],'revision':u['revision'],'text':u.get('text',''),'applies_when':u.get('applies_when',[]),'exceptions':u.get('exceptions',[]),'effect_on_action':u.get('effect_on_action',''),'reason':'budget_requires_next_page','requires_read_before_dependent_action':True})
    refs={(u['id'],u['revision']) for u in result['included']+result['already_loaded_valid']}
    result['preference_candidates']=[{**item,'status':'candidate_requires_agent_judgment'} for item in result['included']]
    allowed={(u['id'],u['revision']) for u in repo.active_units() if (u.get('preference_audit') or {}).get('state') in {'approved','restricted'}}
    for m in repo.active_models():
        for section in m.get('sections',[]):
            sr={(r.get('id'),r.get('revision')) for r in section.get('guidance_refs',[])}
            if not sr or not sr<=allowed or not sr&refs:continue
            item={**section,'model_id':m['id'],'model_revision':m['revision'],'model_title':m['title'],'model_kind':m.get('model_kind'),'model_dimensions':m.get('dimensions',[])}
            cost=len(json.dumps(item,ensure_ascii=False,separators=(',',':')))
            if used+cost<=budget:result['model_sections'].append(item);used+=cost
            else:result['deferred'].append({'id':m['id'],'revision':m['revision'],'section_id':section.get('section_id'),'reason':'model_body_budget'})
    if start + candidate_limit < len(ranked):
        overflow_offset = start + candidate_limit
        if next_offset is None:next_offset = overflow_offset
        result['deferred'].extend({'id':u['id'],'revision':u['revision'],'reason':'candidate_limit_requires_next_page','requires_read_before_dependent_action':True} for u,_ in ranked[overflow_offset:])
    # Unread entries carry locators only; their bodies must not bypass the
    # candidate or token limits by appearing a second time as deferred text.
    result['deferred']=[{k:v for k,v in row.items() if k in {'id','revision','section_id','reason','requires_read_before_dependent_action'}} for row in result['deferred']]
    if result['deferred']:result['coverage']='partial_page_with_deferred'
    if next_offset is not None:result['next_cursor']=base64.urlsafe_b64encode(json.dumps({'offset':next_offset,'query_revision':query_hash,'selection_revision':repo.active_revision()}).encode()).decode().rstrip('=')
    result['budget']={'requested_max_tokens':max_tokens,'soft':True,'candidate_scope':'all_reviewed_units','body_serialized_chars':used,'candidate_count':len(ranked),'candidate_limit':candidate_limit,'semantic_recall':semantic_status}
    result['budget']['estimated_response_tokens']=len(json.dumps(result,ensure_ascii=False,separators=(',',':')))//2
    return result
