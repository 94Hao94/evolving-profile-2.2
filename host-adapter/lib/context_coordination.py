"""Coordinate native task context with long-term Hindsight recall.

The Codex hook can add context but cannot delete or replace the host's active
conversation.  This module therefore does three narrow things:

* suppress a long-term memory when the user has already stated the same fact
  in the recent live task context;
* keep and label a relevant memory when it reactivates an older part of a long
  task (useful after compaction or under long-context position bias);
* report honest token/coverage metrics for the recall observability UI.

The policy is deliberately deterministic and local.  It adds no model call and
never imposes a fixed number-of-items cap.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
from typing import Iterable

from lib.content import extract_task_user_request as extract_user_request
from lib.retention_queue import estimate_tokens
from lib.relevance import is_association_closure_query


_PROVENANCE_MARKERS = (
    "原话", "来源", "出处", "哪说", "什么时候说", "时间", "证据", "逐字", "引用",
)


def _text(item: dict) -> str:
    return " ".join(str(item.get("text") or item.get("content") or "").split())


def _core(text: str) -> str:
    """Remove common Hindsight provenance suffixes before overlap checks."""
    value = str(text or "")
    value = re.split(r"\s*\|\s*(?:When|Involving|涉及|来源|Source)\s*:", value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"^#+\s*", "", value).strip()
    return value


def _normalized(text: str) -> str:
    return re.sub(r"[^\u3400-\u9fffA-Za-z0-9]+", "", _core(text)).casefold()


def _significant_chunks(text: str) -> set[str]:
    value = _core(text)
    chunks = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.:/-]{1,}|[\u3400-\u9fff]{2,}", value):
        token = token.casefold()
        if len(token) <= 8:
            chunks.add(token)
        else:
            for size in (3, 4):
                chunks.update(token[index:index + size] for index in range(0, len(token) - size + 1))
    return chunks


def overlap_score(left: str, right: str) -> dict:
    a, b = _normalized(left), _normalized(right)
    if not a or not b:
        return {"duplicate": False, "similarity": 0.0, "containment": 0.0}
    shortest, longest = (a, b) if len(a) <= len(b) else (b, a)
    literal_containment = len(shortest) / max(1, len(longest)) if shortest in longest else 0.0
    sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
    aa, bb = _significant_chunks(left), _significant_chunks(right)
    containment = len(aa & bb) / max(1, min(len(aa), len(bb)))
    return {
        "duplicate": False,
        "similarity": round(sequence, 6),
        "containment": round(max(literal_containment, containment), 6),
    }


def _partition_user_messages(messages: list[dict], recent_count: int) -> tuple[list[str], list[str]]:
    user_messages = []
    for message in messages or []:
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            content = str(content or "")
        content = extract_user_request(content)
        if content:
            user_messages.append(content)
    count = max(1, int(recent_count or 4))
    if len(user_messages) <= count:
        return user_messages, []
    return user_messages[-count:], user_messages[:-count]


def build_profile(prompt: str, messages: list[dict], config: dict) -> dict:
    prompt = extract_user_request(str(prompt or ""))
    policy = config.get("contextMemoryCoordination") or {}
    recent_count = int(policy.get("recentUserMessages") or 4)
    recent, older = _partition_user_messages(messages, recent_count)
    all_text_rows = []
    for message in messages or []:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "")
        if role == "user":
            content = extract_user_request(content)
        if content:
            all_text_rows.append(content)
    all_text = "\n".join(all_text_rows)
    return {
        "policy": "context_memory_coordinator_v2",
        "enabled": bool(policy.get("enabled", True)),
        "native_context_messages": len(messages or []),
        "native_context_estimated_tokens": estimate_tokens(all_text) if all_text else 0,
        "recent_user_messages": len(recent),
        "older_user_messages": len(older),
        "recent": recent,
        "older": older,
        "provenance_request": any(marker in str(prompt or "") for marker in _PROVENANCE_MARKERS),
        "prompt": extract_user_request(str(prompt or "")),
    }


def _clip_utf8(text: str, maximum_bytes: int) -> str:
    """Clip a message without splitting UTF-8, retaining its ending as well."""
    value = str(text or "").strip()
    if len(value.encode("utf-8")) <= maximum_bytes:
        return value
    marker = " …[会话片段已压缩]… "
    budget = max(80, maximum_bytes - len(marker.encode("utf-8")))
    head_budget = int(budget * 0.72)
    encoded = value.encode("utf-8")
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail_bytes = max(0, budget - len(head.encode("utf-8")))
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return head + marker + tail


def build_full_prompt_context_slice(prompt: str, messages: list[dict], *, max_bytes: int = 44000) -> tuple[list[dict], dict]:
    """Build a bounded, auditable slice for Full-Prompt resolution.

    Live Codex transcripts can be gigabytes long. Keep the latest dialogue
    window, then add older lexical-topic matches. This is resolver input only,
    never an injection of old dialogue into the answer context.
    """
    prompt = extract_user_request(str(prompt or ""))
    history = []
    for row in messages or []:
        role = str(row.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "").strip()
        if role == "user":
            content = extract_user_request(content)
        if content:
            history.append({"role": role, "content": content})
    if not history:
        return [], {"strategy": "none", "source_message_count": 0, "selected_message_count": 0}
    raw_size = len(json.dumps(history, ensure_ascii=False).encode("utf-8"))
    if raw_size <= max_bytes:
        return history, {"strategy": "complete_transcript", "source_message_count": len(history), "selected_message_count": len(history), "source_bytes": raw_size, "selected_bytes": raw_size}

    recent_start = max(0, len(history) - 16)
    anchor_text = "\n".join([str(prompt or "")] + [row["content"] for row in history[recent_start:]])
    anchor_chunks = _significant_chunks(anchor_text)
    scored_older = []
    for index, row in enumerate(history[:recent_start]):
        chunks = _significant_chunks(row["content"])
        overlap = len(anchor_chunks & chunks) / max(1, len(chunks))
        score = overlap + (0.04 if row["role"] == "user" else 0.0)
        if score > 0:
            scored_older.append((score, index))
    older_indices = [index for _, index in sorted(scored_older, reverse=True)[:12]]
    wanted = sorted(set(range(recent_start, len(history))) | set(older_indices))
    chosen_rev, used = [], 0
    for index in sorted(wanted, reverse=True):
        row = history[index]
        cap = 3600 if row["role"] == "user" else 2600
        if index < recent_start:
            cap = min(cap, 1800)
        remaining = max_bytes - used - 48
        if remaining < 180:
            continue
        content = _clip_utf8(row["content"], min(cap, remaining))
        row_size = len(json.dumps({"role": row["role"], "content": content}, ensure_ascii=False).encode("utf-8"))
        if row_size > remaining:
            continue
        chosen_rev.append((index, {"role": row["role"], "content": content}))
        used += row_size
    chosen = [row for _, row in sorted(chosen_rev)]
    selected_size = len(json.dumps(chosen, ensure_ascii=False).encode("utf-8"))
    return chosen, {"strategy": "recent_window_plus_ranked_older_context", "source_message_count": len(history), "selected_message_count": len(chosen), "source_bytes": raw_size, "selected_bytes": selected_size, "recent_window_messages": len(history) - recent_start, "older_ranked_messages": sum(1 for index, _ in chosen_rev if index < recent_start)}


def explicit_memory_opt_out(prompt: str) -> bool:
    from lib.memory_policy import explicit_memory_opt_out as classify_opt_out
    return classify_opt_out(prompt)


def build_contextual_intent_envelope(prompt: str, messages: list[dict], config: dict) -> dict:
    """Build a bounded, auditable context layer for intent interpretation.

    The current user sentence remains the authority.  Earlier user turns and
    assistant replies can only resolve references such as ``刚才``/``这条`` and
    identify the active object; they cannot introduce a new instruction.  This
    deliberately avoids placing an unbounded transcript into the semantic
    router while correcting the former opposite error: treating a dependent
    follow-up as a fully self-contained query.
    """
    current = extract_user_request(str(prompt or ""))
    policy = config.get("contextMemoryCoordination") or {}
    max_items = max(2, min(8, int(policy.get("intentContextTurns") or 6)))
    max_chars = max(80, min(320, int(policy.get("intentContextItemChars") or 180)))
    rows: list[dict] = []
    for message in messages or []:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()
        if role == "user":
            content = extract_user_request(content)
        if not content or content == current:
            continue
        rows.append({"role": role, "content": " ".join(content.split())[:max_chars]})
    rows = rows[-max_items:]
    normalized = re.sub(r"\s+", "", current).casefold()
    anaphoric = any(marker in normalized for marker in (
        "刚才", "这句话", "这条", "这个", "这种", "这种情况", "上述", "前述", "上面", "前面", "刚刚", "还是这样", "它", "这里",
    ))
    # Keep a compact normalized view of the available task evidence.  It is
    # used below only as a gate for continuation detection, never as answer
    # context by itself.
    context_text = "\n".join(row["content"] for row in rows)
    context_normalized = re.sub(r"\s+", "", context_text).casefold()
    # Continuations are often expressed without a pronoun: “能更细吗？” and
    # “继续展开” refer to the immediately preceding user request just as much
    # as “这条再展开”.  A real Codex turn can, however, arrive with only
    # assistant status rows (for example after a heartbeat or a long-running
    # task hand-off).  Keep the short-turn rule, and add a second generic lane
    # for an explicit continuation/action cue backed by strong active-task
    # evidence.  The latter is deliberately evidence-gated so “继续整理旅行
    # 笔记” is still an independent request when no active Hindsight task is in
    # the available context.
    continuation_markers = (
        "更细", "详细", "展开", "继续", "逐项", "分别说", "再说", "补充",
        "接着", "往下", "持续", "保持", "别停", "不要停", "追加", "补上",
        "完善", "写回", "写入", "做到最后", "一口气", "不中断",
        "全部修改", "全部执行", "赶紧修改", "赶紧修复", "赶紧处理",
        # Short imperatives commonly used to continue an active debugging
        # task. They only activate the continuation lane when the preceding
        # context already proves the same active task.
        "想办法", "优化好", "修复好", "全面检查", "彻底检查",
    )
    strong_active_context_markers = (
        "case-", "真实窗口", "执行契约", "台账", "run_state", "userpromptsubmit",
        "blocked_by_real_ui_submission",
    )
    ordinary_active_context_markers = (
        "hindsight", "hook", "controller", "状态页", "packet", "bank", "回执",
        "实际注入", "fullprompt", "回归", "根因", "修复", "测试",
    )
    prior_user_rows = [row for row in rows if row.get("role") == "user"]
    prior_user = str(prior_user_rows[-1].get("content") or "") if prior_user_rows else ""
    explicit_continuation = any(marker in normalized for marker in continuation_markers)
    strong_active_hits = list(dict.fromkeys(
        marker for marker in strong_active_context_markers if marker in context_normalized
    ))
    ordinary_active_hits = list(dict.fromkeys(
        marker for marker in ordinary_active_context_markers if marker in context_normalized
    ))
    active_task_context = bool(
        strong_active_hits
        or len(ordinary_active_hits) >= 3
    )
    short_continuation = bool(
        prior_user
        and len(normalized) <= 96
        and explicit_continuation
    )
    active_task_continuation = bool(
        rows
        and active_task_context
        and explicit_continuation
        # Avoid classifying a very large new task as a follow-up merely
        # because it mentions “继续”; the complete transcript/Qwen resolver
        # remains available for genuinely long prompts.
        and len(normalized) <= 4000
    )
    continuation = short_continuation or active_task_continuation
    mechanism_terms = ("语义", "上下文", "状态页", "链路", "路由", "召回", "检索", "注入", "回执", "记忆")
    audit_terms = ("检查", "合格", "有没有做到", "是否做到", "对不对", "问题", "为什么")
    mechanism_hits = [term for term in mechanism_terms if term in normalized]
    # “这个注入是 0 条，正常吗？” is semantically incomplete on its own:
    # “这个” refers to a visible status/packet record.  It must therefore
    # retain the active status object and enter Full-Prompt resolution instead
    # of being reduced to a generic point lookup.  Do not broaden this to all
    # numeric questions; require both an anaphoric marker and a memory-delivery
    # phrase so ordinary “这个价格是 0 吗” remains an independent turn.
    status_injection_followup = bool(
        anaphoric and rows and any(marker in normalized for marker in (
            "注入是0", "注入为0", "实际注入0", "实际注入为0", "注入零", "0条注入",
            "memorypacket", "packet注入", "注入正常吗",
        ))
    )
    # Context may name the active artefact (for example a status page) when a
    # brief follow-up only says "这条".  It can add a subject hint, never alter
    # the user's requested action.
    # A short verification prompt can omit the object and a pronoun.  After an
    # active memory/status audit, “现在怎么样，能测试出来吗” means whether that
    # audit is now verifiable.  Require a compact verification phrase AND
    # multiple delivery/audit anchors in preceding context so an unrelated
    # “现在怎么样” never inherits this scope.
    verification_followup = bool(
        rows
        and len(normalized) <= 48
        and any(marker in normalized for marker in ("能测试", "测试出来", "怎么样", "还有问题吗", "测出来吗"))
        and sum(term in context_normalized for term in ("状态页", "实际注入", "注入", "fullprompt", "hook", "packet", "回执", "链路")) >= 2
    )
    # “还有其他问题吗，先找出来” and the natural short form “检查下还有什么
    # 问题” are acceptance/audit continuations when
    # the immediately bounded context proves an active delivery audit.  It is
    # not a generic inventory request: require both the search instruction and
    # at least two concrete delivery anchors in prior task context.
    audit_followup = bool(
        rows and len(normalized) <= 72
        and any(marker in normalized for marker in ("其他问题", "还有什么问题", "检查下", "检察下"))
        and any(marker in normalized for marker in ("找出来", "先找", "检查", "检察"))
        and sum(term in context_normalized for term in ("状态页", "实际注入", "注入", "hook", "packet", "回执", "链路", "验收", "修复")) >= 2
    )
    # “你测试的真实吗” (also “你的测试跟真实的测试一样吗”) compares the preceding test protocol
    # with the user's live interaction.  The noun “测试” is not enough by
    # itself (it could start a new request), so require the possessive/comparison
    # phrasing plus concrete test-path anchors in prior context.
    test_protocol_comparison = bool(
        rows
        and len(normalized) <= 72
        and "测试" in normalized
        and any(marker in normalized for marker in ("真实", "实际输入", "偏差", "差异", "一样吗", "相同吗", "区别", "不一样"))
        and (
            any(marker in normalized for marker in ("你的测试", "刚才测试", "这个测试"))
            or bool(re.search(r"你.{0,8}测试.{0,12}真实", normalized))
        )
        and sum(term in context_normalized for term in ("hook", "controller", "bank", "packet", "9998", "回执", "生产等价", "真实输入")) >= 2
    )
    # An acceptance/responsibility question can be elliptical without “刚才”.
    # Require the prior work context and a deictic/completion question together;
    # never carry an old project into an explicitly new topic.
    acceptance_followup = bool(audit_followup or (
        rows and len(normalized)<=120
        and not any(x in normalized for x in ('换个话题','另一个问题','新问题','另外问'))
        and sum(x in context_normalized for x in ('测试','验收','交付','修复','验证','回执','完成'))>=2
        and any(x in normalized for x in ('你的意思','所以','该我','让我','还得我','轮到我','你这','剩下'))
        and any(x in normalized for x in ('测试','验证','验收','交付','完成','再来一遍','还得我','剩下'))
    ))
    resolved_subjects = list(dict.fromkeys(
        [term for term in mechanism_terms if term in normalized or ((anaphoric or verification_followup or test_protocol_comparison) and term in context_normalized)]
    ))[:8]
    is_mechanism_audit = status_injection_followup or verification_followup or test_protocol_comparison or (
        len(mechanism_hits) >= 2
        and any(term in normalized for term in audit_terms)
    )
    # A conceptual graph/constellation question often contains the words
    # “检索/注入/为什么”, but it is not asking why one concrete Packet was
    # empty.  If it is allowed to fall through the generic mechanism-audit
    # rule, the envelope forces ``audit`` and suppresses the route that can
    # explain entity aliases, graph/constellation edges and anti-generic
    # admission.  Keep concrete delivery audits in the narrow lane; only the
    # conceptual form is reclassified here.  The same predicate is reused by
    # Controller admission so the context and routing layers cannot disagree.
    association_delivery_markers = (
        "实际注入", "注入为0", "注入是0", "候选很多但实际", "投递回执", "注入回执",
        "状态页", "未投递", "未注入", "对账", "正常吗", "链路断", "链路缺",
    )
    association_closure_request = is_association_closure_query(current)
    mixed_delivery_audit = any(marker in normalized for marker in association_delivery_markers)
    conceptual_association_query = bool(association_closure_request and not mixed_delivery_audit)
    if conceptual_association_query:
        is_mechanism_audit = False
    prior_compact = re.sub(r"\s+", "", prior_user).casefold()
    prior_evolution = bool(
        any(marker in prior_compact for marker in ("变迁", "演变", "迭代", "历程", "经历", "阶段"))
        and (
            any(marker in prior_compact for marker in ("最初", "最开始", "一开始", "起初", "初版", "早期", "以来", "至今"))
            or bool(re.search(r"从.{0,40}到(?:现在|目前|当前|如今|现状|最新)", prior_compact))
        )
    )
    used_context = bool((anaphoric or continuation or verification_followup or status_injection_followup or test_protocol_comparison or acceptance_followup) and rows)
    full_prompt = ""
    if acceptance_followup:
        context_anchor='\n'.join(f"{row['role']}: {row['content']}" for row in rows[-4:])
        full_prompt=(f"本轮原始问题：{current}\n仅供解析本次验收对象的前文：{context_anchor}\n"
                     "本轮是在追问前述任务是否完成、真实验收证据和执行责任；前文的通过结论是待核验陈述。"
                     "事实充分性与验收、协作指导的适用性应分别判断；保留当前用户问题，不扩大任务范围。")
    elif continuation:
        # This is a bounded semantic reconstruction of two user requests, not
        # a transcript replay and not an assistant instruction.  The adapter
        # labels it explicitly so the status page never claims that Codex
        # supplied a native model contract.
        context_anchor = prior_user or "\n".join(
            f"{row.get('role')}: {row.get('content')}"
            for row in rows[-4:]
            if str(row.get("content") or "").strip()
        )
        full_prompt = (
            f"前一项活动任务：{context_anchor}\n"
            f"本轮用户续问：{current}\n"
            "完整任务：保持前一项活动任务的对象、范围和当前执行状态，继续完成本轮要求；"
            "不得引入无关主题。"
        )[:1600]
    elif test_protocol_comparison:
        # The Hook consumes full_prompt on provider failure; routing hints
        # alone cannot restore the omitted object in the retrieval query.
        context_anchor = "\n".join(f"{row['role']}: {row['content']}" for row in rows[-3:])
        full_prompt = (
            f"本轮原始问题：{current}\n"
            f"仅供解析比较对象的前文记录：{context_anchor}\n"
            "核对前述自动测试与用户实际输入的原文、上下文、入口及回执差异；"
            "前文的完成结论尚待本轮核验，不能当作验收事实。"
        )[:1800]
    force_shapes = []
    if conceptual_association_query:
        # Do not force the narrow provenance-audit route.  Controller will add
        # the bounded system-map/synthesis routes and retain relation closure.
        force_shapes = ["system_map", "synthesis"]
    elif is_mechanism_audit:
        force_shapes = ["audit"]
    elif continuation and prior_evolution:
        # A detail follow-up inherits the previous evolution workload.  It
        # still goes through normal relevance and coverage admission; this only
        # prevents the router from shrinking it to a one-fact point lookup.
        force_shapes = ["timeline", "inventory"]
    # A temporal word inside "最开始进行语义分析" describes processing order;
    # it is not a request for the system's historical evolution.  Suppress the
    # timeline path only for this explicit context/mechanism audit shape.
    return {
        "schema": "hindsight.contextual_intent.v1",
        "used_context": used_context,
        "intent_mode": (
            "association_closure" if conceptual_association_query else
            "contextual_acceptance_followup" if acceptance_followup else
            "contextual_mechanism_audit" if is_mechanism_audit else
            "contextual_followup" if continuation else
            "current_utterance"
        ),
        "current_user_message": current[:600],
        "resolved_subjects": resolved_subjects,
        "guidance_requirements": ['acceptance_evidence','execution_responsibility'] if acceptance_followup else [],
        "routing_hints": {
            "force_shapes": force_shapes,
            "suppress_shapes": (["audit", "timeline"] if conceptual_association_query else ["timeline"] if is_mechanism_audit else []),
        },
        "full_prompt": full_prompt,
        "full_prompt_source": "contextual_followup_resolution" if full_prompt else "",
        "context_items": rows,
        "reason": (
            "当前句省略了验收对象并追问完成标准或执行责任；使用前述任务范围，单独检查适用的验收指导。"
            if acceptance_followup else
            "本轮用“这个”追问可见状态记录的 0 注入；必须把状态对象、Packet/回执和前文审计范围一并送入 Full Prompt 解析。"
            if status_injection_followup else
            "本轮是对前述 Full Prompt、实际注入和状态页审计的省略式验证追问；必须保留该审计对象后再解析。"
            if verification_followup else
            "本轮比较前述生产等价回放与用户真实输入；必须保留测试协议、Hook/Controller/Bank/Packet 与状态页回执范围后再解析。"
            if test_protocol_comparison else
            "本轮是对前一项活动任务的细化或继续执行请求；使用最近用户/助手任务片段补全对象、范围和状态。"
            if continuation and rows else
            "本轮要求解释图谱/星座关联闭包；保留结构关系、证据覆盖和泛节点抑制范围，不降级为单条实际注入审计。"
            if conceptual_association_query else
            "本轮含有上下文依赖表达；使用最近任务片段解析指代，并将其识别为记忆机制审计。"
            if is_mechanism_audit and rows else
            "本轮仍以当前用户原话独立判断；没有足够的上下文依赖证据。"
        ),
    }


def build_contextual_recall_query(
    prompt: str,
    envelope: dict,
    max_added_chars: int = 360,
    resolved_full_prompt: str = "",
) -> str:
    """Choose the semantic query sent to Hindsight.

    A verified agent/Qwen Full Prompt is the primary retrieval input on every
    turn.  The raw user sentence remains separately available to the planner
    for authority and admission decisions.  When no resolved Full Prompt is
    available, the bounded local envelope may add a search-only hint for a
    dependent turn; it never replays an unbounded transcript as answer context.
    """
    current = extract_user_request(str(prompt or ""))
    resolved = str(resolved_full_prompt or "").strip()
    if resolved:
        # Keep a generous but finite request body even if a provider returns a
        # very long contract.  The complete contract remains in the dedicated
        # X-Memory-Full-Prompt header and Controller runtime receipt.
        return _clip_utf8(resolved, 12000)
    if not isinstance(envelope, dict) or not envelope.get("used_context"):
        return current
    if envelope.get("intent_mode") in {"contextual_followup","contextual_acceptance_followup"} and envelope.get("full_prompt"):
        return str(envelope["full_prompt"])
    if envelope.get("intent_mode") != "contextual_mechanism_audit":
        return current
    subjects = [str(value).strip() for value in (envelope.get("resolved_subjects") or []) if str(value).strip()]
    rows = [row for row in (envelope.get("context_items") or []) if isinstance(row, dict)]
    snippets, remaining = [], max(80, min(480, int(max_added_chars or 360)))
    for row in rows[-2:]:
        content = " ".join(str(row.get("content") or "").split())
        if not content:
            continue
        excerpt = content[:remaining]
        snippets.append(excerpt)
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    if not subjects and not snippets:
        return current
    hint_parts = []
    if subjects:
        hint_parts.append("解析主题：" + "、".join(subjects[:8]))
    if snippets:
        hint_parts.append("仅用于解析本轮指代的最近上下文：" + " / ".join(snippets))
    return current + "\n\n[受控上下文检索提示] " + "；".join(hint_parts)


def select_live_context_reactivations(profile: dict, config: dict) -> tuple[list[str], dict]:
    """Select directly relevant older user statements from the live task.

    This is a precision reactivation, not a summary of the whole transcript.
    It runs only for explicit references to earlier parts of the task (or a
    genuinely large native context) and stays inside a dynamic token budget.
    """
    policy = config.get("contextMemoryCoordination") or {}
    if not bool(profile.get("enabled", True)):
        return [], {"live_context_reactivation_count": 0, "live_context_reactivation_tokens": 0}
    prompt = str(profile.get("prompt") or "")
    older = list(profile.get("older") or [])
    explicit = any(marker in prompt for marker in ("前面", "前边", "早些", "之前", "上文", "前几轮", "已经确定", "回顾当前任务"))
    long_context = int(profile.get("native_context_estimated_tokens") or 0) >= int(
        policy.get("longContextReactivationMinTokens") or 6000
    )
    if not older or not (explicit or long_context):
        return [], {"live_context_reactivation_count": 0, "live_context_reactivation_tokens": 0}

    similarity_threshold = float(policy.get("liveReactivationSimilarity") or 0.30)
    containment_threshold = float(policy.get("liveReactivationContainment") or 0.20)
    # The live-context budget is dynamic: ordinary continuation stays small;
    # explicit full/deep review or a genuinely large task can reopen more of
    # the old conversation.  The limit is token-based, never item-count based.
    base_tokens = int(policy.get("liveReactivationMaxTokens") or 700)
    hard_tokens = max(base_tokens, int(policy.get("liveReactivationHardMaxTokens") or 2400))
    max_tokens = base_tokens
    if any(marker in prompt for marker in ("全部", "完整", "全面", "逐项", "所有", "详细", "深度", "系统回顾")):
        max_tokens = max(max_tokens, int(policy.get("liveReactivationDeepTokens") or 1600))
    native_tokens = int(profile.get("native_context_estimated_tokens") or 0)
    if native_tokens >= int(policy.get("longContextReactivationMinTokens") or 6000):
        max_tokens = max(max_tokens, int(policy.get("liveReactivationLongContextTokens") or 1200))
    max_tokens = min(max_tokens, hard_tokens)
    ranked = []
    for index, message in enumerate(older):
        score = overlap_score(prompt, message)
        if score["similarity"] < similarity_threshold and score["containment"] < containment_threshold:
            continue
        ranked.append((max(score["similarity"], score["containment"]), index, message))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    selected, used = [], 0
    for _, _, message in ranked:
        tokens = estimate_tokens(message)
        if selected and used + tokens > max_tokens:
            continue
        selected.append(message)
        used += tokens
    return selected, {
        "live_context_reactivation_count": len(selected),
        "live_context_reactivation_tokens": used,
        "live_context_reactivation_budget_tokens": max_tokens,
    }


def format_live_context_reactivations(items: list[str]) -> str:
    if not items:
        return ""
    rows = "\n".join(f"- 较早的用户消息：{value}" for value in items)
    return (
        "\n<live_task_context_reactivation>\n"
        "以下内容来自当前任务较早的用户消息，不是新增长期记忆。只因本轮再次直接相关而重激活；"
        "它不能替代当前文件、当前状态或较新的用户纠正。\n"
        f"{rows}\n"
        "</live_task_context_reactivation>\n"
    )


def _best_overlap(text: str, pool: Iterable[str]) -> dict:
    best = {"similarity": 0.0, "containment": 0.0, "message_preview": ""}
    for message in pool:
        score = overlap_score(text, message)
        if max(score["similarity"], score["containment"]) > max(best["similarity"], best["containment"]):
            best = {**score, "message_preview": " ".join(message.split())[:180]}
    return best


def coordinate_items(items: list[dict], profile: dict, config: dict) -> tuple[list[dict], list[dict], dict]:
    """Return (kept, suppressed, metrics) without changing item order."""
    policy = config.get("contextMemoryCoordination") or {}
    if not bool(profile.get("enabled", True)):
        metrics = {
            "policy": profile.get("policy"),
            "enabled": False,
            "native_context_messages": profile.get("native_context_messages", 0),
            "native_context_estimated_tokens": profile.get("native_context_estimated_tokens", 0),
            "recent_user_messages": profile.get("recent_user_messages", 0),
            "older_user_messages": profile.get("older_user_messages", 0),
            "recent_duplicate_suppressed_count": 0,
            "estimated_duplicate_tokens_saved": 0,
            "older_context_reactivated_count": 0,
            "long_term_gap_fill_count": len(items or []),
            "native_context_replaced": False,
            "boundary": "上下文协同已关闭；Hook 仍不会删除或替换 Codex 原生任务上下文。",
        }
        return list(items or []), [], metrics
    similarity_threshold = float(policy.get("recentDuplicateSimilarity") or 0.84)
    containment_threshold = float(policy.get("recentDuplicateContainment") or 0.86)
    older_similarity = float(policy.get("olderReactivationSimilarity") or 0.72)
    older_containment = float(policy.get("olderReactivationContainment") or 0.78)
    kept, suppressed = [], []
    saved_tokens = 0
    reactivated = 0
    gap_fills = 0
    recent = profile.get("recent") or []
    older = profile.get("older") or []
    bypass = bool(profile.get("provenance_request"))

    for original in items or []:
        item = dict(original)
        metadata = dict(item.get("metadata") or {})
        text = _text(item)
        recent_match = _best_overlap(text, recent)
        older_match = _best_overlap(text, older)
        recent_duplicate = (
            recent_match["similarity"] >= similarity_threshold
            or recent_match["containment"] >= containment_threshold
        )
        if recent_duplicate and not bypass:
            coordination = {
                "disposition": "covered_by_recent_context",
                "plain_reason": "当前任务最近的用户消息已经包含同一事实，本轮不重复注入长期记忆。",
                "similarity": recent_match["similarity"],
                "containment": recent_match["containment"],
                "matched_live_context": recent_match["message_preview"],
            }
            metadata["_ccy_context_coordination"] = coordination
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected",
                "reason": coordination["plain_reason"],
                "context_gate": "recent_duplicate",
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            suppressed.append(item)
            saved_tokens += estimate_tokens(text)
            continue

        old_match = (
            older_match["similarity"] >= older_similarity
            or older_match["containment"] >= older_containment
        )
        if old_match:
            disposition = "reactivated_from_older_context"
            reason = "该事实位于当前长任务较早位置；本轮重新激活，降低压缩或长上下文位置偏差造成的遗漏。"
            reactivated += 1
            matched = older_match
        elif bypass:
            disposition = "required_for_provenance"
            reason = "本轮明确要求原话、时间或来源；即使当前上下文提到过，也保留证据记录。"
            matched = recent_match
            gap_fills += 1
        else:
            disposition = "fills_context_gap"
            reason = "当前任务最近上下文没有覆盖该信息；由 Hindsight 补充长期或跨任务事实。"
            matched = recent_match
            gap_fills += 1
        metadata["_ccy_context_coordination"] = {
            "disposition": disposition,
            "plain_reason": reason,
            "similarity": matched["similarity"],
            "containment": matched["containment"],
            "matched_live_context": matched["message_preview"],
        }
        item["metadata"] = metadata
        kept.append(item)

    metrics = {
        "policy": profile.get("policy"),
        "enabled": profile.get("enabled", True),
        "native_context_messages": profile.get("native_context_messages", 0),
        "native_context_estimated_tokens": profile.get("native_context_estimated_tokens", 0),
        "recent_user_messages": profile.get("recent_user_messages", 0),
        "older_user_messages": profile.get("older_user_messages", 0),
        "recent_duplicate_suppressed_count": len(suppressed),
        "estimated_duplicate_tokens_saved": saved_tokens,
        "older_context_reactivated_count": reactivated,
        "long_term_gap_fill_count": gap_fills,
        "native_context_replaced": False,
        "boundary": "Hook 只减少额外注入并重激活旧信息，不删除或替换 Codex 原生任务上下文。",
    }
    return kept, suppressed, metrics


def merge_metrics(*values: dict) -> dict:
    rows = [value for value in values if value]
    if not rows:
        return {}
    merged = dict(rows[0])
    for value in rows[1:]:
        for key in (
            "recent_duplicate_suppressed_count", "estimated_duplicate_tokens_saved",
            "older_context_reactivated_count", "long_term_gap_fill_count",
            "live_context_reactivation_count", "live_context_reactivation_tokens",
        ):
            merged[key] = int(merged.get(key) or 0) + int(value.get(key) or 0)
        if "live_context_reactivation_budget_tokens" in value:
            merged["live_context_reactivation_budget_tokens"] = max(
                int(merged.get("live_context_reactivation_budget_tokens") or 0),
                int(value.get("live_context_reactivation_budget_tokens") or 0),
            )
    return merged


def filter_identity_subject(query: str, items: list[dict], config: dict, explicit_evidence: bool = False) -> tuple[list[dict], list[dict]]:
    """Keep self-identity queries from admitting unrelated named-entity corrections."""
    compact = re.sub(r"\s+", "", str(query or "")).casefold()
    identity = config.get("authoritativeIdentity") or {}
    canonical = str(identity.get("name") or "").strip()
    aliases = [str(value).strip() for value in (identity.get("invalidAliases") or {}).keys()]
    is_identity = any(marker in compact for marker in ("我的名字", "我叫什么", "姓名", "用户姓名"))
    is_identity = is_identity or any(value and value.casefold() in compact for value in [canonical, *aliases])
    if not is_identity:
        return list(items or []), []

    qualified, rejected = [], []
    authority_seen = False
    for original in items or []:
        item = dict(original)
        metadata = dict(item.get("metadata") or {})
        text = _text(item)
        source = str(metadata.get("source") or "")
        subject_match = (
            source == "identity-authority"
            or any(value and value in text for value in [canonical, *aliases])
            or any(marker in text for marker in ("用户姓名", "正确姓名", "不是用户姓名", "并非用户姓名"))
        )
        if not subject_match:
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected",
                "reason": "本轮询问用户本人姓名；该候选描述的是其他客户或实体的名称纠正，主体不一致。",
                "context_gate": "identity_subject_mismatch",
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            rejected.append(item)
            continue
        if source == "identity-authority":
            authority_seen = True
            qualified.append(item)
            continue
        if authority_seen and not explicit_evidence:
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected",
                "reason": "版本化身份权威记录已完整回答本轮姓名问题；等价历史副本不重复注入。",
                "context_gate": "identity_authority_dedup",
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            rejected.append(item)
            continue
        qualified.append(item)
    return qualified, rejected
