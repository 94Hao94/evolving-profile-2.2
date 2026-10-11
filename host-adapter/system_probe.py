"""Bounded, read-only Hook discovery. No Controller expansion or foreground LLM."""
from functools import lru_cache
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from lib.memory_policy import classify_memory_policy, _instruction_text
from lib.recall_relevance import apply_relevance_policy, resolve_min_relevance
from source_safety import mask_text


_GENERIC_FOCUS_TERMS = {
    '老师', '学校', '项目', '方案', '客户', '工作', '事情', '活动', '事项', '记录',
    '历史', '最近', '近期', '之前', '过去', '本轮', '刚才', '对话', '调用', '工具',
    '回执', '链路', '研究', '检索', '查询', '看看', '有没有', '是否', '什么', '怎么',
    '哪个', '哪些', '一个', '相关', '通过', '里面', '这个', '那个', '所有', '实际',
    '情况', '效果', '内容', '时间', '范围', '状态', '是否', '用户', '助手', '银行',
    'codex', 'threads', 'thread', '对话', '线程', '问题', '检查', '审计', '修复',
    '先别', '找明白', '建议', '思路', '开工', '有什么', '看看', '找找',
    '分钟', 'minute', 'minutes', 'min', 'mins', 'word', 'docx', 'pdf',
    '没有关系', '无关', '不相关', '现在只是', '连不上服务器', '这么麻烦', '让你重新写个',
}

_NEGATION_RE = re.compile(r'没有关系|没关系|无关|不相关|不涉及|not\s+related|unrelated|irrelevant', re.I)


def _is_generic_focus_term(term: str) -> bool:
    value=str(term or '').casefold().strip()
    return value in _GENERIC_FOCUS_TERMS or bool(re.search(r'(?:\d+\s*)?分钟|\b(?:minutes?|mins?)\b',value))


def _focus_terms(value: str) -> list[str]:
    """Extract narrow entity phrases used only for the probe admission gate.

    This is deliberately lexical and conservative.  It is not a semantic
    relevance score and it never decides whether the Agent may call a tool.
    """
    text = str(value or '').casefold()
    terms = []
    for token in re.findall(r'[a-z][a-z0-9_.+-]{2,}', text):
        if not _is_generic_focus_term(token) and not re.fullmatch(r'[0-9a-f-]{12,}', token):
            terms.append(token)
    for span in re.findall(r'[\u4e00-\u9fff]{2,}', text):
        if _is_generic_focus_term(span):
            continue
        # Keep short exact phrases such as “鹏飞学校” or “尹老师”.  Long
        # clauses are not useful as an admission key and create false misses.
        span = re.sub(r'[呢啊吗吧呀哦呗了]+$', '', span)
        if len(span) <= 12:
            terms.append(span)
        else:
            for piece in re.split(r'的|在|与|和|及|或|关于|相关', span):
                piece = piece.strip()
                if len(piece) >= 2 and not _is_generic_focus_term(piece):
                    terms.append(piece)
    return list(dict.fromkeys(term for term in terms if not _is_generic_focus_term(term)))[:24]


def _negated_focus_terms(value: str, terms: list[str]) -> list[str]:
    """Return entity anchors explicitly negated in the current prompt."""
    negative=set()
    for clause in re.split(r'[，,。！？!?；;]',str(value or '').casefold()):
        for match in _NEGATION_RE.finditer(clause):
            before=clause[:match.start()]
            after=clause[match.end():match.end()+48]
            for term in terms:
                if term.casefold() in before or term.casefold() in after:
                    negative.add(term)
    return [term for term in terms if term in negative]


def plan_history(prompt, task=None):
    task = task or {}
    policy = classify_memory_policy(prompt)
    current = _instruction_text(prompt)
    context = str(task.get('context_summary') or '') if task.get('continuation') else ''
    query = str(prompt).strip() + ('\n当前任务背景：' + context[:4000] if context else '')
    text = current + _instruction_text(context)
    # Audit intent belongs to the current user message.  A prior task's
    # wording may mention tools, but must not turn a normal continuation into
    # a live-audit route.
    thread_audit = bool(re.search(r'codex://threads/[0-9a-z-]+', current, re.I)) and bool(
        re.search(r'对话|线程|找.{0,8}问题|检查|审计|调用|回执|先别修复|找明白', current)
    )
    # Audit language is often phrased as a defect report rather than a
    # question: "应该调用但没有调用，继续测试". Treat that as a live
    # observability request so we inspect the current hook/tool receipts
    # instead of launching an ordinary historical recall against those words.
    audit_text = re.sub(r'\s+', '', current)
    quoted_transform = bool(re.match(r'^\s*(?:把|将|请?)', current)) and bool(
        re.search(r'[“\"].+[”\"]', current)
    ) and bool(re.search(r'翻译|润色|改写|重写', current))
    live_audit = (thread_audit or bool(re.search(
        r'最新(?:的)?prompt|本轮|这一轮|刚才(?:这)?一轮|刚才.{0,8}对话|几条对话|调用效果|调用记录|工具回执|链路页|有没有调用|是否调用|'
        r'检验.*(?:recall|research)|检查.*(?:recall|research)|(?:上一轮|上轮|前一轮).*(?:链路|调用|工具|回执|审计)|'
        r'(?:应该|本应|需要|必须).{0,18}(?:调用|使用).{0,18}(?:recall|research|get[_ ]?preference|EP工具|历史工具).{0,18}(?:没有|未|没)(?:调用|使用)|'
        r'(?:没有|未|没)(?:调用|使用).{0,18}(?:recall|research|get[_ ]?preference|EP工具|历史工具).{0,24}(?:测试|检查|修复|问题|回执|链路)',
        audit_text,
    ))) and not (quoted_transform and '不是翻译任务' not in current)
    historical = bool(re.search(r'以前|之前|过去|上次|上回|回顾|后来|当初|最近|近期|上周|上月|做过|干过|拍板|咱们定过', text))
    # These are positive, transparent routing hints. Missing hints never veto
    # the Agent or turn uncertain relevance into "no memory exists".
    temporal = bool(re.search(r'最近|近期|过去|上周|上月|近.{0,5}[天周月年]|[0-9一二三四五六七八九十两]+[天周月年]', text))
    activity_inventory = bool(re.search(r'干什么|做什么|做了|做过|干了|工作|忙|项目|活动|事项|事情|客户|分.{0,3}类', text))
    asset_inventory = bool(re.search(r'有哪些|哪些|什么|列出|盘点|都做了', text)) and bool(
        re.search(r'生图|效果图|生成图|图片|视频|素材|附件|文件', text)
    )
    inventory = temporal and (activity_inventory or asset_inventory)
    # Comparisons across explicitly listed objects need a multi-object
    # research pass even when the prompt does not use the word "多主体".
    # A single separator plus a relation/contrast marker is enough; this
    # catches forms such as “A、B、C之间的关系” without promoting every
    # ordinary sentence containing “和” to Research.
    multi_object_compare = (
        bool(re.search(r'比较|对比|区别|差异', text)) and bool(
            re.search(r'(?:、|和|与|及).*(?:之间|关系|各自|区别|差异)', text)
        )
    ) or bool(re.search(r'(?:、|和|与|及).{0,20}分别对应(?:哪些|什么)', text))
    complex_history = inventory or multi_object_compare or bool(re.search(
        r'时间线|跨项目|跨会话|全部历史|完整回顾|多主体|多个实体|多个学校|多个客户|多个项目|多家|对应起来|对应关系|关联起来|批量|'
        r'从最初.{0,20}(?:到现在|至今)|版本到现在|变化.{0,12}(?:原因|未解决)|反复出现|共同问题|哪些已经修复',
        text,
    ))
    arithmetic = bool(re.fullmatch(r'(?:请?计算)?\d+(?:\.\d+)?(?:乘以|加|减|除以|[×x*+\-/])\d+(?:\.\d+)?(?:等于多少)?[？?。]?', current))
    language = bool(re.search(r'翻译|译成|病句|润色|改写|改成|重写成|转换成', current)) and not historical and not bool(re.search(r'回顾|按.*(?:偏好|风格|习惯)|检索|找原话', current))
    personal_reference = bool(re.search(r'我的|我们|咱们|我跟|我与', current)) and not bool(re.search(r'不要结合|不结合|不涉及|不要联系', current))
    generic_explanation = bool(re.search(r'解释|科普|什么意思|通识|英文单词', current)) and not historical and not context and not personal_reference
    live = bool(re.search(r'本机|电脑上|这台电脑', current) and re.search(r'终端|命令.*结果|系统命令', current)) and not historical
    supplied = bool(re.search(r'只对以下|下面两条|把这句话|按当前粘贴|只看上述|所有素材都在这次附件|不涉及已发生的项目|代码就在工作区|刚写的方案|依据刚才这次实测',current)) and not historical
    greeting = current in {'你好','您好','hi','hello','谢谢','谢谢你','收到','好的'}
    preference_history = policy.get('guidance_memory_policy') != 'forbidden' and bool(re.search(
        r'我的?偏好|个人偏好|习惯和要求|根据我习惯|按我.{0,8}(?:习惯|要求|风格)|'
        r'(?:要求|格式|风格).{0,10}(?:记录|记过|保存)|记录的.{0,8}(?:偏好|格式|要求)', current
    ))
    explicit_native_memory = bool(re.search(
        r'(?:查|搜索|检索|读取|打开|查看).{0,16}(?:codex.{0,8}memory|本地记忆|原生记忆)|'
        r'(?:codex.{0,8}memory|本地记忆|原生记忆).{0,16}(?:查|搜索|检索|读取|打开|查看)', current,
        re.I,
    ))
    skip = not policy['history_allowed'] or (not live_audit and (arithmetic or language or generic_explanation or live or supplied or greeting))
    missing_context = not context and bool(re.fullmatch(r'(?:那|你|就|可以|继续|开工|执行|好|啊|吧|！|!|，|,|。)+',current))
    if not context and re.search(r'第[一二三四五六七八九十0-9]+(?:份|个|项)(?:材料|文档|文件|方案)',current):
        missing_context = not bool(task.get('resolved_entities'))
    route = 'skip' if skip else 'live_audit' if live_audit else 'get_preference' if preference_history else 'research' if complex_history else 'recall'
    reason = ('当前用户禁止历史读取。' if not policy['history_allowed'] else '当前请求有明确的自足信息来源。') if skip else ('问题针对本轮工具调用、回执或链路状态；优先读取实时审计，不查询历史 Bank。' if live_audit else ('问题明确询问用户已记录的偏好，应调用 Get Preference 读取条件化条目。' if preference_history else ('跨时间或开放盘点，需要按范围检索并检查覆盖；可直接 Research。' if complex_history else '历史可能补充事实、旧决定或相关经验；候选由 Agent 结合完整上下文判断。')))
    if not skip and 'research' in policy['denied_tools'] and route == 'research':route='recall'
    if not skip and 'recall' in policy['denied_tools'] and route == 'recall':route='research' if 'research' not in policy['denied_tools'] else 'agent_decides'
    # Broad inventories and live audits go straight to the Agent's auditable
    # route.  A low-budget probe is useful only for a narrow recall question;
    # otherwise generic historical previews become anchoring noise.
    minimum = ('skip' if skip else 'live_audit' if live_audit else
               'needs_context' if missing_context else
               'agent_query' if route in {'research','get_preference'} or 'recall' in policy['denied_tools']
               else 'recall_probe')
    negative_focus_terms = _negated_focus_terms(current,_focus_terms(current))
    focus_terms = [term for term in _focus_terms(current) if term not in negative_focus_terms]
    if minimum=='recall_probe' and not focus_terms:
        # The Agent already sees the whole task context. A generic time/tool
        # phrase is not a safe anchor for an automatic Bank preview.
        minimum='agent_query'
    return {'schema':'evolving-profile.history-plan.v3','query':query,'decision':route,'recommended_route':route,
        'history_dependency':'none' if skip else 'complex' if complex_history else 'likely' if historical else 'possible',
        'minimum_action':minimum,'reason':reason,'memory_policy':policy,'agent_may_override':True,
        'context_source':'bounded_task_context' if context else 'current_prompt_only',
        'required_slots':['time_range','activities','completion_state','sources'] if inventory else [],
        'suggested_tools':[] if skip or live_audit else [route] if route != 'agent_decides' else [],
           'required_ep_tool': None if skip else 'mcp__evolving_profile_controller__user_preference' if route == 'get_preference' else 'mcp__evolving_profile_controller__user_research' if route == 'research' else 'mcp__evolving_profile_controller__user_recall' if route == 'recall' else 'mcp__evolving_profile_controller__audit_thread_history' if route == 'live_audit' else None,
        'allow_native_memory': bool(explicit_native_memory),
        'boundary':'routing_hint_not_fact_or_coverage_verdict',
        'focus_terms':focus_terms,
        'negative_focus_terms':negative_focus_terms,
        'candidate_policy': 'positive_anchor_overlap' if route == 'recall' else 'agent_query_only',
        'fallback_route': 'research' if route == 'recall' else None,
        'fallback_trigger': 'recall_empty_or_scope_insufficient' if route == 'recall' else None}


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken
        return tiktoken.get_encoding('o200k_base')
    except Exception:
        return None


def count_tokens(text):
    encoding = _encoding()
    if encoding:
        return len(encoding.encode(text,disallowed_special=())), 'o200k_base'
    return len(text.encode('utf-8')), 'utf8_bytes_conservative_upper_bound'


def _load_probe_runtime_settings():
    """Read the same local settings as the tools, without a service call."""
    guidance = os.environ.get('EVOLVING_PROFILE_GUIDANCE_SRC', str(Path(__file__).resolve().parents[1] / 'guidance'))
    if guidance not in sys.path:
        sys.path.insert(0, guidance)
    from runtime_settings import load_runtime_settings
    state_root = Path(os.environ.get('EVOLVING_PROFILE_STATE_ROOT', str(Path.home() / '.evolving-profile')))
    path = Path(os.environ.get('EVOLVING_PROFILE_RUNTIME_SETTINGS', str(state_root / 'config/runtime-settings.json')))
    return load_runtime_settings(path)


def run_probe(plan, settings, api, bank, *, runtime_settings=None):
    """A single direct Bank call; only previews enter the final token budget."""
    limit = max(300,min(1200,int(settings.get('probe_max_tokens',500))))
    from lib.candidate_audit import snapshot, mark_delivery
    receipt = {'actor':'system_probe','state':'skipped','calls':0,'candidate_count':None,
               'returned_count':0,'items':[],'max_tokens':limit,'context_tokens':0,
               'text_returned_count':0,'locator_returned_count':0,'candidate_audit':[],
               'token_counter':'not_injected','answer_use':'not_measured',
               'relevance_audit': {'status':'not_run','reason':'retrieval_not_run','level_counts':None,'kept_count':None,'excluded_count':None}}
    if not settings.get('auto_probe',True) or plan['minimum_action'] != 'recall_probe':
        receipt['reason'] = 'disabled' if not settings.get('auto_probe',True) else plan['minimum_action']
        receipt['admission'] = {'mode': plan.get('candidate_policy') or 'not_run', 'admitted_count': 0,
                                'rejected_count': 0, 'reason': receipt['reason']}
        return '',receipt
    started=time.monotonic()
    try:
        runtime = runtime_settings if runtime_settings is not None else settings.get('runtime_settings')
        if runtime is None:
            runtime = settings if 'recall_policy' in settings else _load_probe_runtime_settings()
        if not isinstance(runtime, dict):
            raise ValueError('invalid_probe_runtime_settings')
        if 'recall_policy' in runtime and not isinstance(runtime['recall_policy'], dict):
            raise ValueError('invalid_probe_recall_policy')
        policy = resolve_min_relevance(runtime, 'user_memory')
        receipt['relevance_audit'].update(policy)
        if not (runtime.get('routing') or {}).get('ep_enabled', True) or not (((runtime.get('modules') or {}).get('facts') or {}).get('retrieve', True)):
            receipt['reason'] = 'disabled_by_runtime_settings'
            return '', receipt
        receipt['calls']=1
        data=api('/v1/default/banks/'+urllib.parse.quote(bank,safe='')+'/memories/recall',
                 {'query':plan['query'],'budget':'low','max_tokens':limit},timeout=5)
        rows=data.get('results')
        if not isinstance(rows,list):raise ValueError('malformed_recall_response')
        receipt.update(state='returned' if rows else 'empty',candidate_count=len(rows))
        items=[]; rejected=0; scoped=[]
        focus_terms=[str(term).casefold() for term in plan.get('focus_terms') or [] if str(term).strip()]
        negative_focus_terms=[str(term).casefold() for term in plan.get('negative_focus_terms') or [] if str(term).strip()]
        required_matches=1 if len(focus_terms)<=1 else 2
        for row in rows:
            if not isinstance(row,dict) or not row.get('id'):continue
            if row.get('state')=='invalidated':
                receipt['candidate_audit'].append(snapshot(row,outcome='blocked',reason='withdrawn'))
                continue
            text=mask_text(str(row.get('text') or ''))[:600]
            matched_terms=[term for term in focus_terms if term in text.casefold()]
            if len(matched_terms)<required_matches:
                rejected += 1
                receipt['candidate_audit'].append(snapshot(row,outcome='scope_uncertain',reason='insufficient_literal_overlap'))
                continue
            scoped.append({**row, 'id':str(row['id']), '_preview_text':text, '_focus_matches':matched_terms[:8]})
        admitted, relevance_audit = apply_relevance_policy(plan['query'], scoped, policy, main_query=plan['query'])
        receipt['relevance_audit'] = {**relevance_audit, 'status':'ok', 'audit_scope':'scope_admitted_probe_candidates', 'source_scope_rejected_count':rejected}
        admitted_by_id = {row['id']:row for row in admitted}
        for row in scoped:
            if row['id'] not in admitted_by_id:
                receipt['candidate_audit'].append(snapshot(row,outcome='relevance_excluded',reason='relevance_below_policy'))
        for row in admitted:
            if len(items)>=3:
                receipt['candidate_audit'].append(snapshot(row,outcome='budget_deferred',reason='probe_preview_budget'))
                continue
            receipt['candidate_audit'].append(snapshot(row,outcome='prepared',reason='scope_and_relevance_admitted'))
            items.append({'id':row['id'],'text':row['_preview_text'],'preview':True,'admission':'positive_anchor_overlap',
                          'matched_focus_terms':row['_focus_matches'], 'relevance_level':row['relevance_level']})
        if rows and not items:
            receipt['state'] = 'filtered_empty'
        discovery_order = {str(row['id']): index for index, row in enumerate(rows) if isinstance(row, dict) and row.get('id')}
        receipt['candidate_audit'].sort(key=lambda row: discovery_order.get(row['id'], len(rows)))
        receipt['admission'] = {
            'mode': plan.get('candidate_policy') or 'positive_anchor_overlap',
            'focus_terms': focus_terms[:12],
            'negative_focus_terms':negative_focus_terms[:12],
            'required_match_count':required_matches,
            'admitted_count': len(items),
            'rejected_count': rejected,
            'relevance_rejected_count':relevance_audit['excluded_count'],
            'reason': '候选必须匹配当前正向实体/主题锚点；多锚点问题至少匹配两个，明确否定的实体不参与准入。',
        }
    except Exception as error:
        items=[];receipt.update(state='unavailable',error_type=type(error).__name__)
        receipt['relevance_audit'].update(status='unavailable',reason='probe_unavailable',level_counts=None,kept_count=None,excluded_count=None)
    def render(compact=False, rows=None):
        rows = items if rows is None else rows
        admission=receipt.get('admission') or {}
        if compact:
            admission={key: admission.get(key) for key in ('mode','admitted_count','rejected_count','required_match_count') if admission.get(key) is not None}
            visible_items=[{'id':item.get('id'),'preview':True} for item in rows]
            body={'state':receipt['state'],'recommended_route':plan['recommended_route'],'fallback':plan.get('fallback_route'),'fallback_trigger':plan.get('fallback_trigger'),'items':visible_items,'admission':admission}
            return '<evolving_profile_system_probe>\n'+json.dumps(body,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')+'\n</evolving_profile_system_probe>'
        else:
            visible_items=rows
            next_action=({'tool':'research','when':'recall_empty_or_scope_insufficient','reason':'窄 Recall 无结果或范围不足时升级 Research。'}
                         if plan.get('fallback_route') == 'research' else
                         {'tool':'agent_query','when':'direct_agent_route','reason':'综合盘点由 Agent 直接调用 Research。'})
        body={'state':receipt['state'],'recommended_route':plan['recommended_route'],'items':visible_items,
              'coverage':'unverified_candidates_only','admission':admission,'next':next_action}
        return '<evolving_profile_system_probe>\n'+json.dumps(body,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')+'\n</evolving_profile_system_probe>'
    original_items=list(items)
    visible_items=list(items)
    output=render(rows=visible_items)
    while count_tokens(output)[0]>limit and visible_items:
        longest=max(visible_items,key=lambda item:len(item['text']))
        if len(longest['text'])>40:longest['text']=longest['text'][:len(longest['text'])//2]
        else:visible_items.pop()
        output=render(rows=visible_items)
    if count_tokens(output)[0] > limit:
        output=render(compact=True, rows=original_items)
        delivered_items=[{'id':item['id']} for item in original_items]
    else:
        delivered_items=visible_items
    count,method=count_tokens(output)
    receipt['candidate_audit']=mark_delivery(receipt['candidate_audit'],delivered_items)
    text_count=sum(bool(item.get('text')) for item in delivered_items)
    receipt.update(items=delivered_items,returned_count=len(delivered_items),context_tokens=count,token_counter=method,
                   text_returned_count=text_count,locator_returned_count=len(delivered_items)-text_count,
                   elapsed_ms=round((time.monotonic()-started)*1000,1),delivery_stage='context_prepared')
    receipt['relevance_audit']['returned_count'] = len(delivered_items)
    return output,receipt
