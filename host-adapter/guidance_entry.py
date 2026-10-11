#!/usr/bin/env python3
"""Forced UserPromptSubmit task-guidance entry check.

This is the host adapter boundary for the guidance MCP implementation.  It
invokes the same read-only GuidanceRepository selector used by
``get_preference`` and records that the check happened.  It does not run
historical recall, research, source reads, or long-term publication.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import sys
import uuid
from lib.instruction_entry import VERSION, instruction_block, instruction_receipt
from task_state import TaskStateStore
from lib.memory_policy import classify_memory_policy
from entry_navigation import build_navigation_map

GUIDANCE_SRC = Path("/Users/apple/.evolving-profile/runtime/guidance")
GUIDANCE_CONFIG = Path("/Users/apple/.evolving-profile/guidance-v1/guidance-v1.json")
RECEIPT_ROOT = Path.home() / ".evolving-profile/audit/guidance-entry-receipts"
MAX_TOKENS = 5000
MAX_CANDIDATES = 6
MAX_CONTEXT_CHARS = 6500
AGENT_ENTRY_MAX_CONTEXT_CHARS = 12000
PROMPT_INGRESS = Path.home() / ".evolving-profile" / "audit" / "prompt-ingress.jsonl"
TASK_STATE_ROOT = Path.home() / ".evolving-profile" / "task-state"


def _continuation_only(text: str) -> bool:
    value=re.sub(r'[\s，。！？!?、,]+','',str(text or ''))
    simple=bool(re.fullmatch(r'(?:那|你|就|赶紧|啊|吧|好|好的|可以|继续|接着|执行|开工|开始|处理|不要停|别停|都完事儿了吗)+',value))
    referenced=bool(re.fullmatch(r'(?:那)?(?:你)?(?:就)?按(?:你|上面|刚才)(?:的)?(?:建议|方案|思路)(?:的)?(?:执行|处理|继续|做)(?:吧|啊)?',value))
    repair=bool(re.fullmatch(r'(?:那|这个|这件事|刚才|上面|上述)?(?:你)?(?:建议)?(?:怎么|如何)?(?:修|修改|改|优化|处理|做|解决|推进)(?:好|完|下|一下)?(?:呢|啊|吧)?',value))
    return simple or referenced or repair


def _active_context(hook_input: dict, current_prompt: str) -> str:
    supplied = str(hook_input.get("memory_full_prompt") or "").strip()
    if supplied:
        return supplied
    session_id = str(hook_input.get("session_id") or "").strip()
    if not session_id or not PROMPT_INGRESS.is_file():
        return ""
    try:
        # Read a bounded tail: it contains the most recent ingress rows while
        # avoiding a full history scan at UserPromptSubmit.
        with PROMPT_INGRESS.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 512_000))
            rows = stream.read().decode("utf-8", errors="ignore").splitlines()
        recent=[]
        for raw in reversed(rows):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if str(row.get("session_id") or "") != session_id:
                continue
            prior = str(row.get("prompt_preview") or row.get("prompt") or "").strip()
            if prior and prior != current_prompt and not _continuation_only(prior) and prior not in recent:
                recent.append(prior[:1200])
                if len(recent)>=3:break
        if recent:return "最近用户前文（仅消解本轮指代，不继承旧授权）：\n" + "\n".join(reversed(recent))
    except OSError:
        pass
    return ""


def _phase(prompt: str) -> str:
    value = str(prompt or "")
    if any(term in value for term in ("验收", "测试", "验证", "回归", "审计", "核对")):
        return "verify"
    if any(term in value for term in ("交付", "发布", "发送", "写成", "整理成")):
        return "deliver"
    if any(term in value for term in ("分析", "判断", "比较", "为什么", "原因", "权衡")):
        return "analyze"
    if any(term in value for term in ("解释", "讲讲", "怎么理解", "机制", "原理")):
        return "understand"
    return "execute" if any(term in value for term in ("继续", "修复", "修改", "推进", "开始", "开工")) else "understand"


def simple_self_contained(prompt: str) -> bool:
    value = "".join(str(prompt or "").split()).casefold()
    return value in {"你好", "您好", "嗨", "hi", "hello", "hey", "谢谢", "谢谢你", "好的", "收到"}


def preference_memory_policy(prompt: str, requested: str = "allowed") -> str:
    classified = classify_memory_policy(prompt)["guidance_memory_policy"]
    return "forbidden" if requested == "forbidden" or classified == "forbidden" else "allowed"

def configured_candidate_limit() -> int:
    try:
        value = json.loads((Path.home() / ".evolving-profile/config/guidance-settings.json").read_text(encoding="utf-8"))
        return max(1, min(20, int(value.get("max_candidates", MAX_CANDIDATES))))
    except (OSError, ValueError, TypeError):
        return MAX_CANDIDATES


def build_request(hook_input: dict, prompt: str, context_ref: str, *, memory_policy: str = "allowed") -> dict:
    value = str(prompt or "").strip()
    normalized = re.sub(r"\s+", "", value).casefold()
    active_context = _active_context(hook_input, value) if classify_memory_policy(value)['history_allowed'] else ''
    if re.search(r'换个话题|另一个问题|新任务',value) or simple_self_contained(value):active_context=''
    explicit_continuation = _continuation_only(value) or value.startswith(("继续", "接着")) or bool(
        active_context and re.match(r"^(?:开工|开始执行|开始修|按(?:你|上面|刚才).{0,8}(?:建议|方案|思路))", value)
    )
    # A compact “再检查下” is a continuation only when the host supplied a
    # bounded active-task reconstruction. Without that evidence it remains a
    # self-contained request rather than inheriting arbitrary prior work.
    recheck_continuation = bool(
        active_context
        and len(normalized) <= 96
        and any(marker in normalized for marker in ("再检查", "再检察", "检查下", "检察下"))
    )
    referential_followup = bool(active_context and (
        re.match(r'^(?:那|那么|所以|还有|另外|再|还是|刚才|上面|上述|这个|这次)', value)
        or re.search(r'为什么(?:你)?(?:没|不)(?:用|查|读)|你(?:漏了|没查|没用|没读)',value)))
    continuation = explicit_continuation or recheck_continuation or referential_followup
    if not continuation:
        active_context = ""
    phase = _phase(value)
    return {
        "context_ref": context_ref,
        "task": {
            "objective": value or "本轮用户消息入口检查",
            "current_user_message": value,
            "context_summary": active_context[:6000],
            "previous_user_messages": [],
            "continuation": continuation,
            "phase": phase,
            "current_constraints": ["当前用户 Prompt 优先", "多维度偏好仅作 advisory reference"],
            "domains": [],
            "media": [],
            "resolved_entities": [],
            "unresolved_references": [],
        },
        "loaded": [],
        "memory_policy": preference_memory_policy(value, memory_policy),
        "max_tokens": MAX_TOKENS,
        "max_candidates": configured_candidate_limit(),
        "entry_adapter": True,
        # The Hook is the production entry path.  Keep command-line evaluation
        # deterministic unless it explicitly opts in, while every submitted
        # user prompt gets semantic candidate discovery with safe fallback.
        "semantic_recall": "local",
    }


def _call_preference(request: dict) -> dict:
    if str(GUIDANCE_SRC) not in sys.path:
        sys.path.insert(0, str(GUIDANCE_SRC))
    script_root = str(Path(__file__).resolve().parent)
    if script_root not in sys.path:
        sys.path.insert(0, script_root)
    # Call the same implementation behind the registered
    # mcp__evolving_profile_controller__get_preference tool. The stdio server is a
    # line-oriented process and must not be imported from a Hook (it would
    # consume the Hook stdin); the entry adapter uses its underlying runtime
    # function directly and records the boundary separately.
    from mcp_runtime import load_repository, get_preference_response
    return get_preference_response(load_repository(GUIDANCE_CONFIG), request, record=False)


# Test/replay compatibility only; current entry path calls _call_preference.
_call_task_guidance = _call_preference


def _entry_relevant(prompt: str, item: dict) -> bool:
    """Drop narrow topic guidance when the entry query has no matching anchor."""
    query = "".join(str(prompt or "").split()).casefold()
    text = "".join(" ".join(str(item.get(key) or "") for key in ("text", "applies_when", "exceptions")).split()).casefold()
    # The current Evolving Profile product supersedes a narrow earlier rule
    # that required preserving the original Hindsight product wholesale. Keep
    # it in the registry for historical audit, but do not inject it as current
    # guidance merely because the prompt mentions the memory system.
    obsolete_markers = ("原版hindsight", "完整保留", "严禁删减", "轻量原型", "替代memory.md", "绕过hook")
    if "evolvingprofile" in query and "hindsight" in text and sum(marker in text for marker in obsolete_markers) >= 2:
        return False
    # Avoid returning a PPT/Word/Excel-specific preference for a generic UI
    # or test task. The task can still page the original result if needed.
    narrow = re.findall(r"[a-z][a-z0-9+.#-]{2,}", text)
    for token in narrow:
        if token.upper() in {"PPT", "PPTX", "WORD", "EXCEL", "PDF", "DOCX"} and token not in query:
            return False
    return True


def render_entry(result: dict, prompt: str = "", *, max_chars=None, include_instruction=True) -> tuple[str, dict]:
    """Pack whole records; the receipt describes exactly the emitted bodies."""
    budget = MAX_CONTEXT_CHARS if max_chars is None else max(0, int(max_chars))
    manual = instruction_block('user-prompt-submit') + '\n' if include_instruction else ''
    stable, included, models, bodies = [], [], [], []
    selected_ids = {str(row.get('id')) for row in result.get('included') or []}
    deferred = [{k:v for k,v in row.items() if k in ('id','revision','section_id','reason')}
                for row in result.get('deferred') or []]
    start = f'<evolving_profile_guidance_entry version="{VERSION}" mode="forced_task_guidance_check">\n'
    footer = '\n候选需核对条件和例外；未读内容可用 Get Preference 分页或 read_preference_unit 补读。当前要求优先，查询不扩大授权。\n</evolving_profile_guidance_entry>'
    used = len(manual) + len(start) + len(footer) + 260
    groups = (('候选偏好', result.get('included') or [], included),
              ('稳定协作骨架', [x for x in result.get('stable_profile') or [] if str(x.get('id')) not in selected_ids], stable),
              ('融合心智模型', result.get('model_sections') or [], models))
    for kind, rows, emitted in groups:
        for row in rows:
            identity = str(row.get('id') or row.get('section_id') or '')
            text = str(row.get('text') or row.get('content') or '')
            body = f'- {kind} {identity}：{text}'
            for field, label in (('scope','作用范围'),('applies_when','适用条件'),('exceptions','例外'),('effect_on_action','行动影响')):
                if row.get(field):
                    body += '\n  ' + label + '：' + json.dumps(row[field], ensure_ascii=False)
            # Escape structural markup from stored content; never cut a record.
            body = body.replace('<', '&lt;')
            if not text or used + len(body) + 1 > budget:
                deferred.append({'id':identity,'revision':row.get('revision'),'reason':'entry_body_budget'})
                continue
            bodies.append(body); emitted.append(row); used += len(body) + 1
    coverage = 'partial_entry_body_with_deferred' if deferred else str(result.get('coverage') or 'unknown')
    summary = f'coverage={coverage}；{len(stable)} 条稳定协作骨架；{len(included)} 条候选偏好已写入本轮上下文；{len(models)} 个模型章节；{len(deferred)} 条未读。\n'
    value = manual + start + summary + '\n'.join(bodies) + footer
    if len(value) > budget:
        value = ''
        stable, included, models = [], [], []
        coverage = 'entry_budget_unavailable'
    return value, {'stable_profile':stable,'included':included,'preference_candidates':included,
        'model_sections':models,'deferred':deferred,'coverage':coverage,
        'budget':dict(result.get('budget') or {}),'next_cursor':result.get('next_cursor')}


def format_context(result: dict, prompt: str = "") -> str:
    return render_entry(result, prompt)[0]


def _task_state_context(task_state: dict, session_id: str, budget: int) -> str:
    """Bound a duplicate task preview without cutting the manual/map or JSON."""
    start, end = '\n<evolving_profile_task_state>\n', '\n</evolving_profile_task_state>'
    fields = ('current_objective','current_message','continuation','continuation_context','source','authority','version',
              'update_reason','expires_at','constraints','completed','unresolved','objects','source_versions')
    block = {key:task_state[key] for key in fields if task_state.get(key) is not None}

    def render(value):
        return start + json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c') + end

    full = render(block)
    if len(full) <= budget:
        return full
    # Full state remains in the local task store and the entry receipt. The
    # current user message is already visible to the Agent, so do not repeat it.
    block.pop('current_message', None)
    block.update(projection_truncated=True, full_state_file=str(TASK_STATE_ROOT / (hashlib.sha256(session_id.encode()).hexdigest()+'.json')))
    for limit in (160, 80, 40):
        preview = {key:(value if key in ('version','continuation','projection_truncated','full_state_file')
                        else (str(value) if isinstance(value,str) else json.dumps(value,ensure_ascii=False))[:limit])
                   for key,value in block.items()}
        rendered = render(preview)
        if len(rendered) <= budget:
            return rendered
    return render({key:block[key] for key in ('version','projection_truncated','full_state_file')})

def _fit_entry_base(context: str, budget: int) -> str:
    """Shorten only L0 navigation lines while preserving complete XML blocks."""
    if len(context) <= budget:
        return context
    match = re.search(r'(<evolving_profile_navigation_map[^>]*>)([\s\S]*?)(</evolving_profile_navigation_map>)', context)
    if not match:
        return context[:budget]
    before, after = context[:match.start()], context[match.end():]
    allowance = max(200, budget - len(before) - len(after) - len(match.group(1)) - len(match.group(3)) - 60)
    body = match.group(2)
    compact = body[:allowance].rsplit('\n', 1)[0] + '\nL0 其余目录已按预算截断；仍可使用 catalog/recall/research 下钻。\n'
    return before + match.group(1) + compact + match.group(3) + after


def prepare_agent_owned_entry(hook_input: dict, prompt: str, *, memory_policy: str = "allowed") -> dict:
    """Supply L0 navigation plus a bounded Get Preference candidate packet."""
    invocation=str(hook_input.get('hook_invocation_id') or uuid.uuid4().hex)
    request=build_request(hook_input,prompt,'entry:'+invocation,memory_policy=memory_policy)
    policy=classify_memory_policy(prompt)
    policy['guidance_memory_policy']=request['memory_policy']
    navigation_context,navigation=build_navigation_map(GUIDANCE_CONFIG,policy)
    task_state=None;session_id=str(hook_input.get('session_id') or '').strip()
    if session_id:
        try:
            task_state=TaskStateStore(TASK_STATE_ROOT).record(
                session_id,prompt,hook_input.get('turn_id'),invocation,
                continuation=bool(request['task'].get('continuation')) and policy['history_allowed'],
            )
        except OSError:task_state=None
    context=instruction_block('user-prompt-submit')+'\n'+navigation_context+'\n'+(
        '<evolving_profile_guidance_entry version="'+VERSION+'" mode="navigation_plus_get_preference_candidate_packet">'
        '入口已提供轻量偏好与Bank地图，并附带有界的Get Preference候选包；候选仍由当前Agent结合完整Prompt判断是否展开或采用。'
        'Get Preference不是完整偏好注入；需要时继续按ID补读。'
        '据证据缺口调用catalog/recall/research/read_source；不得仅因当前上下文看似足够而跳过地图检查。'
        '</evolving_profile_guidance_entry>'
    )
    context = _fit_entry_base(context, AGENT_ENTRY_MAX_CONTEXT_CHARS - MAX_CONTEXT_CHARS - 400)
    guidance_result=None;guidance_rendered={'stable_profile':[],'included':[],'preference_candidates':[],'model_sections':[],'deferred':[],'coverage':'not_requested'}
    if request.get('memory_policy') != 'forbidden' and not simple_self_contained(prompt):
        try:
            candidate_request=dict(request,max_tokens=5000)
            guidance_result=_call_preference(candidate_request)
            remaining=max(0,AGENT_ENTRY_MAX_CONTEXT_CHARS-len(context)-400)
            guidance_context,guidance_rendered=render_entry(guidance_result,prompt,max_chars=remaining,include_instruction=False)
            if remaining:
                context += '\n' + guidance_context
        except Exception:
            guidance_result={'coverage':'unavailable','included':[],'deferred':[]}
            guidance_rendered['coverage']='unavailable'
    if task_state:
        context+=_task_state_context(task_state,session_id,AGENT_ENTRY_MAX_CONTEXT_CHARS-len(context))
    receipt={
        'schema':'hindsight.guidance-entry-check.v1','kind':'preference_entry_check','tool_name':'user_preference',
        'invocation_mode':'navigation_plus_get_preference_candidate_packet','delivery_stage':'navigation_and_guidance_candidates_prepared',
        'host_visibility':'hook_context_pending','at':datetime.now(timezone.utc).isoformat(),
        'session_id':hook_input.get('session_id'),'turn_id':hook_input.get('turn_id'),'hook_invocation_id':invocation,
        'request':request,'task_state':task_state,'instruction':instruction_receipt(),'navigation_map':navigation,
        'rendered_guidance':guidance_rendered,
        'stable_profile_count':len(guidance_rendered.get('stable_profile') or []),'preference_candidate_count':len(guidance_rendered.get('preference_candidates') or []),'selection_revision':(guidance_result or {}).get('selection_revision'),'coverage':guidance_rendered.get('coverage') or 'not_requested',
        'included_count':len((guidance_result or {}).get('included') or []),'entry_context_included_count':len(guidance_rendered.get('included') or []),'model_section_count':len(guidance_rendered.get('model_sections') or []),'deferred_count':len(guidance_rendered.get('deferred') or []),
        'result':guidance_result,'context_chars':len(context),'context_sha256':hashlib.sha256(context.encode()).hexdigest(),'error':None,
    }
    try:
        RECEIPT_ROOT.mkdir(parents=True,exist_ok=True);target=RECEIPT_ROOT/(hashlib.sha256(invocation.encode()).hexdigest()+'.json');temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(target)
    except OSError:pass
    return {'result':None,'context':context,'receipt':receipt}


def run_entry_check(hook_input: dict, prompt: str, *, memory_policy: str = "allowed") -> dict:
    invocation = str(hook_input.get("hook_invocation_id") or uuid.uuid4().hex)
    request = build_request(hook_input, prompt, "entry:" + invocation, memory_policy=memory_policy)
    task_state = None
    session_id = str(hook_input.get("session_id") or "").strip()
    if session_id:
        try:
            task_state = TaskStateStore(TASK_STATE_ROOT).record(
                session_id, prompt, hook_input.get("turn_id"), invocation,
                continuation=bool(request["task"].get("continuation")),
            )
            request["task"]["task_state"] = {
                key: task_state.get(key) for key in (
                    "current_objective", "current_message", "continuation",
                    "continuation_context", "source", "authority", "version",
                    "update_reason", "expires_at", "constraints", "completed",
                    "unresolved", "objects", "source_versions",
                )
            }
        except OSError:
            task_state = None
    started = datetime.now(timezone.utc).isoformat()
    try:
        result = _call_preference(request)
        error = None
    except Exception as exc:  # Entry check must never block the user turn.
        result = {"coverage": "unavailable", "included": [], "model_sections": [], "deferred": [], "errors": [type(exc).__name__]}
        error = type(exc).__name__
    context, rendered = render_entry(result, prompt)
    if task_state:
        task_block = {
            key: task_state.get(key) for key in (
                "current_objective", "current_message", "continuation",
                "continuation_context", "source", "authority", "version",
                "update_reason", "expires_at", "constraints", "completed",
                "unresolved", "objects", "source_versions",
            )
        }
        context += "\n<evolving_profile_task_state>\n" + json.dumps(task_block, ensure_ascii=False) + "\n</evolving_profile_task_state>"
    receipt = {
        "schema": "hindsight.guidance-entry-check.v1",
        "kind": "guidance_entry_check",
        "tool_name": "user_preference",
        "invocation_mode": "codex_UserPromptSubmit_entry_adapter",
        "delivery_stage": "entry_selected_then_hook_context_prepared",
        "host_visibility": "hook_context_pending",
        "at": started,
        "session_id": hook_input.get("session_id"),
        "turn_id": hook_input.get("turn_id"),
        "hook_invocation_id": invocation,
        "request": request,
        "task_state": task_state,
        "instruction": instruction_receipt(),
        "rendered_guidance": rendered,
        "stable_profile_count": len(rendered.get('stable_profile') or []),
        "preference_candidate_count": len(rendered.get('preference_candidates') or []),
        "selection_revision": result.get("selection_revision"),
        "coverage": rendered['coverage'],
        "included_count": len(result.get("included") or []),
        "entry_context_included_count": len(rendered['included']),
        "model_section_count": len(rendered['model_sections']),
        "deferred_count": len(rendered['deferred']),
        "result": result,
        "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "error": error,
    }
    try:
        RECEIPT_ROOT.mkdir(parents=True, exist_ok=True)
        target = RECEIPT_ROOT / (hashlib.sha256(invocation.encode()).hexdigest() + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        pass
    return {"result": result, "context": context, "receipt": receipt}
