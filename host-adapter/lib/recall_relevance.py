"""Shared, explainable content relevance policy (no vector-score cutoffs).

Classification is a deterministic lexical/concept fallback, not a calibrated
semantic probability. Scope/compatibility, provenance, and maturity are separate
gates. Discovery scores never stand in for an explained content relation.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


# host-adapter/lib -> host-adapter -> repository
POLICY_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "config" / "recall-policy.json"


def policy_manifest() -> dict[str, Any]:
    return json.loads(POLICY_MANIFEST_PATH.read_text(encoding="utf-8"))


def policy_defaults() -> dict[str, Any]:
    return copy.deepcopy(policy_manifest()["defaults"])


def _level(value: Any, *, inherit: bool = False) -> str:
    allowed = policy_manifest()["plane_overrides" if inherit else "levels"]
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("minimum_relevance_invalid")
    return value


def normalize_advanced_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("recall_advanced_invalid")
    defaults = policy_defaults()["recall_policy"]["advanced"]
    enums = policy_manifest()["advanced_enums"]
    result = {**defaults, **value}
    if set(value) - set(defaults):
        raise ValueError("recall_advanced_unknown_field")
    for key, default in defaults.items():
        if isinstance(default, bool):
            valid = type(result[key]) is bool
        else:
            valid = isinstance(result[key], str) and result[key] in enums[key]
        if not valid:
            raise ValueError("recall_advanced_invalid_" + key)
    return result


def normalize_recall_policy(settingsfield: Any = None) -> dict[str, Any]:
    if settingsfield is None:
        settingsfield = {}
    if not isinstance(settingsfield, dict):
        raise ValueError("recall_policy_invalid")
    defaults = policy_defaults()["recall_policy"]
    result = {key: _level(settingsfield.get(key, value), inherit=key != "default_min_relevance") for key, value in defaults.items() if key != "advanced"}
    result["advanced"] = normalize_advanced_policy(settingsfield.get("advanced", {}))
    return result


def resolve_min_relevance(runtime_settings: dict[str, Any] | None = None, plane: str = "user_memory", requested: str | None = None) -> dict[str, Any]:
    settings = runtime_settings or {}
    manifest = policy_manifest()
    recall = normalize_recall_policy(settings.get("recall_policy"))
    global_level = recall["default_min_relevance"]
    if plane == "external_rag":
        configured = _level((settings.get("rag") or {}).get("minimum_relevance", manifest["defaults"]["rag"]["minimum_relevance"]))
        plane_setting, configuration_source = configured, "external_rag"
    elif plane in {"user_memory", "agent_memory"}:
        plane_setting = recall[plane]
        configured = plane_setting if plane_setting != "inherit" else global_level
        configuration_source = "global_default" if plane_setting == "inherit" else "plane_override"
    else:
        raise ValueError("recall_plane_invalid")
    effective = configured
    reasons = ["configured_minimum_relevance"]
    applied = False
    if requested is not None:
        requested = _level(requested)
        if manifest["levels"].index(requested) <= manifest["levels"].index(configured):
            effective, applied = requested, True
            reasons.append("request_tightens_or_preserves_policy")
        else:
            reasons.append("request_would_loosen_policy")
    # EP eligibility switches apply to its User/Agent planes. External RAG
    # keeps independent retrieval semantics until it gains an explicit policy.
    advanced = manifest["defaults"]["recall_policy"]["advanced"] if plane == "external_rag" else recall["advanced"]
    return {"effective_level": effective, "policy_version": manifest["policy_version"], "plane": plane, "configured_level": configured, "configuration_source": configuration_source, "global_level": global_level, "plane_setting": plane_setting, "requested_level": requested, "requested_applied": applied, "reasons": reasons, "advanced": copy.deepcopy(advanced)}


def adaptive_recall_hint(policy: dict[str, Any], arguments: dict[str, Any], *, provider_status: str = "ok") -> dict[str, Any]:
    """Permitted paths only. The answering Agent must establish an evidence gap."""
    levels = policy_manifest()["levels"]
    current = _level(policy["effective_level"])
    floor = _level(policy["configured_level"])
    allowed = levels[:levels.index(floor) + 1]
    expansion = levels[levels.index(current) + 1:levels.index(floor) + 1]
    advanced = normalize_advanced_policy(policy.get("advanced", {}))
    enabled = advanced["adaptive_enabled"] and provider_status == "ok"
    # Only documented retrieval inputs are public; credentials/unknown inputs
    # cannot be echoed. Preserve every hard constraint in an expansion path.
    fields = {"query", "facets", "bank_alias", "budget", "max_tokens", "types", "temporal_window", "prefer_observations", "max_results", "force_deep", "model_family", "model_version", "capability_fingerprint", "toolchain", "task_archetype", "primary_context", "kind", "include_unverified", "check_id", "workspace_id", "limit"}
    preserved = {key: copy.deepcopy(value) for key, value in arguments.items() if key in fields}
    return {"decision_owner": "answering_agent", "enabled": enabled, "allowed_levels": allowed, "expansion_levels": expansion if enabled else [],
            "allowed_tool_arguments": [{**preserved, "minimum_relevance": level} for level in expansion] if enabled else [],
            "requires_evidence_gap_decision": True, "retrieval_performed": False,
            "evidence_gaps": ["missing_requested_subject_evidence", "unresolved_source_or_time_conflict", "missing_transferable_procedure_for_requested_step"],
            "stop_conditions": ["configured_floor_reached", "no_new_evidence_after_same_query", "provider_or_source_failure"],
            "boundary": "Hint only; preserve literal, subject, time, scope and permissions. Do not repeat identical queries or report a retrieval from this hint.",
            "blocked_reason": None if enabled else ("adaptive_disabled" if not advanced["adaptive_enabled"] else "provider_failure_not_semantic_zero")}


_CONTENT_FIELDS = ("text", "content", "title", "summary", "description", "abstract", "body", "chunk_text", "document_text", "failure_signature", "repair_actions", "preconditions", "procedure", "steps", "recommendation", "lessons", "trigger", "rule")
_STOP = set("a an and are as at be before by can code current do exact find for from has have how i identifier in is it its me mechanism memory of on or please query record records search test tests the then this to tool tools use using was were what when which with would you your verification verified generic agent agents assistant user host route routes recall research preference process experience experiences task tasks session project node nodes context phase scope failure failed fail error repair repaired fix recover recovery root cause".split())
_CJK_STOP = {"如何", "什么", "这个", "那个", "进行", "当前", "请问", "帮我", "查找", "搜索", "测试", "工具", "机制", "验证", "记忆", "记录", "相关", "问题", "结果", "过程", "修复", "恢复", "失败", "错误", "故障", "经验", "根因", "总结", "定位", "继续", "导致", "智能体", "用户", "任务", "阶段", "范围", "边界", "节点", "环境", "目前", "以前", "过去", "历史", "执行", "最小", "复测", "回归", "增加", "通过", "完成", "不能", "直接", "实际", "真实", "明确", "需要", "应该", "方式", "本轮", "至少", "一次", "共同", "规律", "可能", "建议", "原因", "方法", "系统"}
# Concept equivalences describe content relations, not provider score thresholds.
_CONCEPTS = {
    "pagination": ("pagination", "paginate", "paging", "分页"),
    "filter": ("filter", "filtering", "filtered", "筛选", "过滤"),
    "offset": ("offset", "偏移"),
    "count": ("count", "counts", "总数", "数量", "计数"),
    "render": ("render", "rendering", "preview", "渲染", "预览"),
    "overflow": ("overflow", "clipping", "clipped", "截断", "溢出", "裁切"),
    "presentation": ("presentation", "slides", "powerpoint", "pptx", "幻灯片", "演示文稿"),
    "dependency": ("dependency", "dependencies", "import", "导入", "依赖"),
    "drag": ("drag", "dragging", "draggable", "拖动", "拖拽"),
    "resize": ("resize", "resizing", "zoom", "zooming", "缩放"),
    "popup": ("popup", "modal", "dialog", "弹窗", "对话框"),
    "detail": ("detail", "details", "详情"),
    "projection": ("projection", "mapping", "投影", "映射"),
    "aggregation": ("aggregate", "aggregation", "聚合", "汇总"),
    "deduplication": ("duplicate", "duplicates", "duplicated", "deduplicate", "deduplication", "去重", "重复计数"),
    "coordinates": ("coordinate", "coordinates", "坐标"),
}
_PROBLEM_CONCEPTS = set(_CONCEPTS) - {"presentation"}
_GENERAL_RELATIONS = {
    "failure": ("failure", "failed", "fail", "error", "失败", "错误", "故障"),
    "repair": ("repair", "repaired", "fix", "recover", "recovery", "修复", "恢复"),
}


def _semantic_text(text: str, *, strip_paths: bool = True) -> str:
    """Keep delivered text intact; remove non-content only for matching."""
    text = re.sub(r"<environment_context\b[^>]*>.*?</environment_context>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<external_[\w:.-]+\b[^>]*>.*?</external_[\w:.-]+>", " ", text, flags=re.I | re.S)
    text = re.sub(r"</?(?:environment_context|external_[\w:.-]+)\b[^>]*>", " ", text, flags=re.I)
    # A sample prompt demonstrates a tool/test contract; it is not evidence
    # that the record executed the procedure mentioned inside that prompt.
    example_label = r"(?:测试\s*(?:Prompt|查询|提示词)|(?:test|example|sample)\s+(?:prompt|query)|示例\s*(?:问题|输入))\s*[：:]?\s*"
    text = re.sub(example_label + r"(?:[“\"][^”\"]*[”\"]|`[^`]*`)", " ", text, flags=re.I)
    text = re.sub(example_label + r"```[^\n]*\n.*?```", " ", text, flags=re.I | re.S)
    # Captured excerpts can start inside an unmatched fence. Remove explicitly
    # quoted prompt lines before pairing fences, so that truncation cannot
    # turn a prompt example into plain procedure evidence.
    prompt_start = r"(?:请(?:查找|搜索|比较|调用|实际调用)|这是一次|(?:please\s+)?(?:find|search|compare|invoke)\b)"
    text = re.sub(r"(?m)^\s*```(?:text|markdown)?\s*\n\s*" + prompt_start + r"[\s\S]*?^```[ \t]*(?:\n|$)", " ", text, flags=re.I)
    text = re.sub(r"(?m)(?:^\s*```(?:text|markdown)?\s*\n|^\s*>\s*)" + prompt_start + r"[^\n]*(?:\n```)?", " ", text, flags=re.I)
    def fenced_content(match: re.Match) -> str:
        body = match.group(1)
        # Prompts quoted as examples contain imperatives directed at a future
        # assistant. Keep executable code and descriptions of actual repairs.
        return " " if re.search(r"^\s*(?:请(?:查找|搜索|比较|调用|实际调用)|这是一次|(?:please\s+)?(?:find|search|compare|invoke)\b)", body, re.I | re.M) else match.group(0)
    text = re.sub(r"```[^\n]*\n(.*?)```", fenced_content, text, flags=re.S)
    if strip_paths:
        text = re.sub(r"(?<![\w])(?:[A-Za-z]:[\\/]|/)(?:[^\s<>\"'`()，。；]+)", " ", text)
    return text


def _general_terms(text: str) -> set[str]:
    lower = text.casefold()
    return {name for name, aliases in _GENERAL_RELATIONS.items() if any(alias in lower for alias in aliases)}


def _content(record: Any) -> str:
    if isinstance(record, str):
        return record
    if not isinstance(record, dict):
        return ""
    def readable(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(readable(item) for item in value)
        if isinstance(value, dict):
            return " ".join(readable(value.get(key)) for key in _CONTENT_FIELDS)
        return ""
    return " ".join(readable(record.get(key)) for key in _CONTENT_FIELDS).strip()


def _terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = {term for term in re.findall(r"[a-z0-9]+(?:[_./:-][a-z0-9]+)*", lowered) if len(term) > 1 and term not in _STOP}
    cjk_text = lowered
    for generic in _CJK_STOP:
        cjk_text = cjk_text.replace(generic, " ")
    for run in re.findall(r"[\u3400-\u9fff]+", cjk_text):
        # Never join across punctuation: that invents tokens absent from content.
        for width in (2, 3, 4):
            terms.update(run[i:i + width] for i in range(len(run) - width + 1) if run[i:i + width] not in _CJK_STOP)
    for name, aliases in _CONCEPTS.items():
        if any((alias in lowered if re.search(r"[\u3400-\u9fff]", alias) else re.search(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", lowered)) for alias in aliases):
            terms.add("concept:" + name)
    return terms


def _scope_terms(text: str) -> set[str]:
    """Subject evidence separate from the mechanism itself and query prose."""
    for aliases in _CONCEPTS.values():
        for alias in aliases:
            pattern = re.escape(alias) if re.search(r"[\u3400-\u9fff]", alias) else r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])"
            text = re.sub(pattern, " ", text, flags=re.I)
    return {term for term in _terms(text) if not term.startswith("concept:") and not re.fullmatch(r"[\u3400-\u9fff]{2}", term)}


def _concrete_procedure(content: str, answered_concepts: set[str]) -> bool:
    # Verbs act on a concrete subject elsewhere in the matching gate. Generic
    # success/failure/checklist words alone do not describe a repair procedure.
    action = r"计算|转换|读取|写入|绑定|映射|聚合|去重|排除|移除|传递|设置|解析|替换|累计|渲染|(?<!\w)(?:calculat\w*|convert\w*|read(?:s|ing)?|bind\w*|aggregat\w*|deduplicat\w*|remov\w*|pars\w*|replac\w*|filter\w*|append\w*|render\w*|add)(?!\w)"
    for clause in re.split(r"[。！？.!?\n]+", content):
        if re.search(action, clause, re.I) and answered_concepts & {term[8:] for term in _terms(clause) if term.startswith("concept:")}:
            return True
    # A problem heading and its nearby repair bullets often span paragraphs.
    # Restrict the action evidence to that local neighborhood, rather than any
    # action elsewhere in a long diagnostic record.
    for problem in answered_concepts:
        for alias in _CONCEPTS[problem]:
            pattern = re.escape(alias) if re.search(r"[\u3400-\u9fff]", alias) else r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])"
            for match in re.finditer(pattern, content, re.I):
                if re.search(action, content[max(0, match.start() - 160):match.end() + 160], re.I):
                    return True
    return False


def _problem_object_match(query: str, content: str, concept: str) -> bool:
    """Shared symptom words do not establish a shared affected object.

    Do not constrain transferable mechanisms to a task-family enum. Only
    distinguish explicitly incompatible senses of an overloaded symptom.
    """
    if concept != "overflow":
        return True
    visual = r"文字|文本框|网页|布局|排版|画布|字体|容器|text.?box|layout|font|canvas|viewport|css"
    budget = r"token|上下文|候选|召回|检索|retrieval|context|history|budget"
    for clause in re.split(r"[。！？.!?\n]+", content):
        if re.search(visual, query, re.I) and re.search(budget, clause, re.I) and not re.search(visual, clause, re.I):
            if any(re.search(re.escape(a), clause, re.I) for a in _CONCEPTS[concept]):
                # An independent visual procedure elsewhere still counts.
                others = [c for c in re.split(r"[。！？.!?\n]+", content)
                          if re.search(visual, c, re.I)]
                return any(_concrete_procedure(c, {concept}) for c in others)
    return True


def _literal_targets(query: str) -> list[str]:
    if not re.search(r"\b(?:exact|literal|verbatim|case[- ]sensitive)\b|精确|逐字|字面|完全匹配|原样|测试码|测试代码|确切", query, re.I):
        return []
    quoted = re.findall(r"`([^`]+)`|\"([^\"]+)\"|“([^”]+)”|'([^']+)'", query)
    targets = [next(part for part in group if part) for group in quoted]
    if not targets:
        targets = re.findall(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:[_./:-][A-Za-z0-9]+)+(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?=[A-Za-z0-9]*[0-9])[A-Z][A-Z0-9]{3,}(?![A-Za-z0-9_])", query)
    return list(dict.fromkeys(targets))


def _unfiltered_browse_intent(query: str) -> bool:
    if not re.search(r"全部|所有|全量|完整|\b(?:all|every|entire|complete)\b", query, re.I) or not re.search(r"历史|记忆|事实|经历|\b(?:histor(?:y|ical)|memories|memory|facts?|experiences?)\b", query, re.I):
        return False
    # A collection-wide request has no remaining subject after removing its
    # quantifier and browse wording. "All budget history" retains "budget".
    residual = re.sub(r"请|帮我|把|我的|我|个人|用户|全部|所有|全量|完整|历史|记忆|事实|经历|回顾|搜索|查找|找出来|列出|展示|检索|找出|给我|出来|和|与|都|的", " ", query)
    residual = re.sub(r"\b(?:please|find|show|list|retrieve|search|review|recall|all|every|entire|complete|my|me|mine|personal|user|histor(?:y|ical)|memories|memory|facts?|experiences?|and|the|of)\b", " ", residual, flags=re.I)
    return not re.search(r"[A-Za-z0-9\u3400-\u9fff]", residual)


def _short_subjects(query: str) -> list[str]:
    # Infer a subject slot from grammar, never from a list of names. A two-Han
    # name can follow a kinship phrase or precede temporal/possessive wording.
    prefix = r"(?:^|(?:请|帮我)?(?:回顾|查找|搜索|介绍|总结)|我(?:的)?(?:儿子|女儿|朋友|同事|客户|学生))"
    found = re.findall(prefix + r"([\u3400-\u9fff]{2})(?=过去|之前|此前|曾经|目前|现在|的(?:学习|工作|经历|偏好|进度|资料|历史))", query)
    return [term for term in dict.fromkeys(found) if term not in _CJK_STOP and term not in {"全部", "所有", "最近", "历史", "事实", "经历"}]


def _identifier_roots(query):
    identifiers = [word.casefold() for word in re.findall(r'\b[A-Z][A-Za-z0-9_.-]*\b', query)
                   if word.casefold() not in _STOP and word.casefold() not in {'review','summarize','compare','find','show','list'}]
    return list(dict.fromkeys(re.sub(r'\d+(?:\.\d+)*$', '', word).rstrip('-_.') or word for word in identifiers))


def subject_contract(query):
    """Concrete named scope shared by native matching and source clauses.

    Compound objects are alternatives for multi-object requests. Mechanism
    terms remain retrieval aspects; they do not rename a product/institution.
    This is textual scope evidence, never a verified formal project identity.
    """
    text=_semantic_text(str(query))
    subject_text=re.sub(r'版本演进|版本|演进|历史|先前发布对比|\b(?:versions?|evolution|history|releases?|prior|previous|earlier|past|compare|comparing|of|about)\b',' ',text,flags=re.I)
    subject_text=re.sub(r'\b([A-Z][A-Za-z_-]*)(\d+(?:\.\d+)*)\b',r'\1',subject_text)
    subject_text=re.sub(r'至|到|\b(?:between|to)\b',' ',subject_text,flags=re.I)
    compounds=[]
    for phrase in re.findall(r'\b[A-Za-z][A-Za-z0-9_-]*(?:[ \t]+[A-Za-z][A-Za-z0-9_-]*){1,6}',subject_text):
        words=phrase.split()
        if words[0].casefold() in _STOP:continue
        for part in re.split(r'\band\b|\bor\b|\bversus\b|\bvs\b',phrase,flags=re.I):
            tokens=part.split()
            if not tokens:continue
            if len({word.casefold() for word in tokens})==1:continue
            modifiers=[word for word in tokens[1:] if word[0].isupper() or word.casefold() not in _STOP and not any(word.casefold() in aliases for aliases in _CONCEPTS.values())]
            if modifiers:compounds.append(' '.join([tokens[0],*modifiers]))
    if re.search(r'版本|演进|历史',text):
        cleaned=subject_text
        for word in sorted(_CJK_STOP|{'请','查','回顾','比较','对比','梳理','盘点','介绍'},key=len,reverse=True):
            cleaned=cleaned.replace(word,' ')
        for phrase in re.findall(r'[\u3400-\u9fff]{4,40}',cleaned):
            compounds.extend(part for part in re.split(r'和|与|及',phrase) if len(part)>=4)
    identifiers=_identifier_roots(subject_text)
    if re.search(r'版本|演进|历史|\b(?:versions?|evolution|history|releases?)\b',text,re.I):
        identifiers=list(dict.fromkeys([*identifiers,*[phrase.split()[0].casefold() for phrase in compounds],
            *[word.casefold() for word in re.findall(r'\b[A-Za-z][A-Za-z_-]{1,}\b',subject_text) if word.casefold() not in _STOP and word.casefold() not in {'prior','previous','earlier','past'}]])) if not compounds else list(dict.fromkeys([*identifiers,*[phrase.split()[0].casefold() for phrase in compounds]]))
    return {'compounds':list(dict.fromkeys(compounds)),
            'identifiers':identifiers,'terms':_scope_terms(subject_text)}


def subject_witness(query,content,record=None):
    contract=subject_contract(query);text=_semantic_text(str(content)).casefold()
    matches=[phrase for phrase in contract['compounds'] if phrase.casefold() in text]
    verified_alias=False
    if isinstance(record,dict):
        verification=record.get('scope_verification') or {}
        context=record.get('primary_context') or {}
        if isinstance(verification,dict) and verification.get('status')=='verified' and verification.get('source') and isinstance(context,dict):
            canonical=verification.get('canonical_subject') or context.get('project') or context.get('subject')
            if canonical in contract['compounds'] and any(str(alias).casefold() in text for alias in verification.get('aliases') or [] if isinstance(alias,str) and alias):
                matches.append(canonical);verified_alias=True
    evidence=_scope_terms(text)
    evidence.update(root for root in contract['identifiers'] if re.search(r'(?<![a-z0-9_])'+re.escape(root)+r'(?![a-z_])',text))
    matched=bool(matches) if contract['compounds'] else bool(contract['terms']) and contract['terms']<=evidence and set(contract['identifiers'])<=evidence
    return {'matched':matched,'specific':bool(contract['compounds']),'matched_compounds':matches,
            'verified_alias':verified_alias,'contract':'literal_compound_or_explicit_verified_alias_not_formal_identity_inference'}


def classify_candidate(query: str, record: Any, main_query: str | None = None) -> dict[str, Any]:
    facet_query = _semantic_text(str(query or ""))
    main_text = _semantic_text(str(main_query or ""))
    # Explicit literal intent in the main request always constrains facets.
    literal_query = main_text if _literal_targets(main_text) else facet_query
    main_terms, facet_terms = _terms(main_text), _terms(facet_query)
    query = main_text if main_terms else facet_query
    content = _semantic_text(_content(record), strip_paths=not bool(_literal_targets(literal_query)))
    signals: dict[str, Any] = {"content_fields_only": True, "classifier": "content_relation_heuristic", "main_query_used": main_query is not None}
    def result(level: str, reason: str, score: float) -> dict[str, Any]:
        return {"level": level, "reasons": [reason], "score": score, "match_signals": signals}
    if not content:
        return result("unknown", "no_readable_candidate_content", 0.0)
    literals = _literal_targets(literal_query)
    if literals:
        # Code/identifier literals are case-sensitive; natural phrase literals
        # are case-insensitive unless the request explicitly demands case.
        def literal_found(target: str) -> bool:
            sensitive = bool(re.search(r"[_./:]|(?=.*[A-Za-z])(?=.*[0-9])", target)) or bool(re.search(r"case[- ]sensitive|区分大小写", query, re.I))
            haystack, needle = (content, target) if sensitive else (content.casefold(), target.casefold())
            return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])", haystack))
        found = [target for target in literals if literal_found(target)]
        signals.update({"literal_intent": True, "literal_targets": literals, "literal_matches": found})
        return result("strong", "explicit_literal_content_match", 1.0) if len(found) == len(literals) else result("none", "requested_literal_absent_from_content", 0.0)
    browse_query = main_text if main_text else facet_query
    if _unfiltered_browse_intent(browse_query):
        signals.update({"unfiltered_browse_intent": True, "scope_boundary": "caller_authorized_collection_only_not_source_claim_verification"})
        return result("strong", "explicit_unfiltered_collection_browse", 0.95)
    native_subject=subject_witness(query,content,record)
    signals['specific_subject_witness']=native_subject
    witness = record.get('source_context') if isinstance(record, dict) else None
    if isinstance(witness, dict) and witness.get('status') == 'source_read' and (witness.get('session_id') or witness.get('document_id')) and witness.get('source_revision') and any(
            isinstance(locator, dict) and locator.get('raw_line_sha256') and locator.get('source_path') and type(locator.get('byte_offset')) is int
            for locator in witness.get('locators') or []):
        context_terms = _terms(_semantic_text(str(witness.get('text') or '')))
        query_context = _scope_terms(query) & context_terms
        identifiers = subject_contract(query)['identifiers']
        context_text = str(witness.get('text') or '').casefold()
        subject_consistent = subject_witness(query,context_text,record)['matched']
        if identifiers and subject_consistent:
            query_context.update('identifier_family:'+word for word in identifiers)
        body_context = _terms(content) & context_terms
        # Source context may resolve a subject omitted in the extracted body.
        # Require an independent content relation to the same source context;
        # mere co-location (or a retrieval query stored in metadata) is insufficient.
        meaningful_body = {term for term in body_context if not re.fullmatch(r'[\u3400-\u9fff]{2}', term)}
        short_body = {term for term in body_context if re.fullmatch(r'[\u3400-\u9fff]{2}', term)}
        independent_short = any(a != b and a[1] != b[0] and b[1] != a[0] for a in short_body for b in short_body)
        meaningful_native = {term for term in _terms(query) & _terms(content) if not re.fullmatch(r'[\u3400-\u9fff]{2}', term)}
        missing_specific=native_subject['specific'] and not native_subject['matched']
        if subject_consistent and query_context and (meaningful_body or independent_short) and (not meaningful_native or missing_specific):
            signals.update(source_context_subject_support=sorted(query_context), source_context_body_support=sorted(body_context),
                           context_source_revision=witness['source_revision'], content_fields_only=False)
            return result('weak', 'source_context_supports_omitted_subject_and_body_relation', 0.4)
    if native_subject['specific'] and not native_subject['matched']:
        requested={term[8:] for term in _terms(query) if term.startswith('concept:') and term[8:] in _PROBLEM_CONCEPTS}
        answered=requested&{term[8:] for term in _terms(content) if term.startswith('concept:')}
        named_parent_overlap=set(_identifier_roots(query))&_scope_terms(content)
        if not (_concrete_procedure(content,answered) and not named_parent_overlap):
            return result('none','specific_subject_not_established_by_parent_or_partial_overlap',0.0)
    if native_subject['verified_alias']:
        return result('weak','explicit_verified_alias_scope_with_content',0.4)
    subjects = _short_subjects(browse_query)
    signals["short_subject_anchors"] = subjects
    if subjects and not any(subject in content for subject in subjects):
        return result("none", "requested_short_subject_absent_from_content", 0.0)
    query_terms = main_terms | facet_terms if main_terms else facet_terms
    content_terms = _terms(content)
    overlap = sorted(query_terms & content_terms)
    main_overlap, facet_overlap = sorted(main_terms & content_terms), sorted(facet_terms & content_terms)
    signals.update({"matched_terms": overlap, "query_term_count": len(query_terms), "main_matched_terms": main_overlap, "facet_matched_terms": facet_overlap, "main_has_subject_anchors": bool(main_terms), "matched_concepts": [term[8:] for term in overlap if term.startswith("concept:")], "generic_support_matches": sorted(_general_terms(query) & _general_terms(content)), "environment_and_path_anchors_removed": True})
    if not query_terms:
        # A deliberately broad recovery question can return weak background;
        # generic recovery words never relate two specific, different tasks.
        if signals["generic_support_matches"]:
            return result("weak", "broad_recovery_query_without_specific_subject", 0.3)
        return result("unknown", "query_has_no_meaningful_content_anchors", 0.0)
    if main_terms and not main_overlap:
        return result("none", "facet_relation_missing_main_subject", 0.0)
    if not overlap:
        return result("none", "no_meaningful_content_relation", 0.0)
    # On a specific multiword CJK task, accidental two-character overlap can
    # cross word boundaries (要求和 / 求和, 单元测试 / 单元素). Require a
    # phrase, an explained concept, or a complete Latin subject instead.
    substantial = [term for term in overlap if term.startswith("concept:") or not re.fullmatch(r"[\u3400-\u9fff]{2}", term)]
    if not substantial and len(query_terms) > 1:
        short_terms = [term for term in overlap if re.fullmatch(r"[\u3400-\u9fff]{2}", term)]
        independent = any(left != right and not (left[1] == right[0] or right[1] == left[0]) for left in short_terms for right in short_terms)
        if independent or any(subject in short_terms for subject in subjects):
            signals["independent_short_cjk_anchors"] = short_terms
            return result("weak", "meaningful_short_subject_or_independent_cjk_anchors", 0.4)
        return result("none", "only_ambiguous_cjk_atom_overlap", 0.0)
    coverage = len(overlap) / len(query_terms)
    signals["query_coverage"] = coverage
    query_problems = {term[8:] for term in query_terms if term.startswith("concept:") and term[8:] in _PROBLEM_CONCEPTS}
    answered = query_problems & {term[8:] for term in content_terms if term.startswith("concept:")}
    answered = {concept for concept in answered if _problem_object_match(query, content, concept)}
    scope_matches = sorted(_scope_terms(query) & _scope_terms(content))
    if "projection" in answered and "detail" in query_problems and "concept:detail" not in content_terms:
        # Projection is relational: projecting counts does not answer a request
        # to project detail content, though it can be optional background.
        answered.remove("projection")
    procedure = _concrete_procedure(content, answered)
    signals.update({"requested_problem_concepts": sorted(query_problems), "answered_problem_concepts": sorted(answered), "shared_subject_anchors": scope_matches, "concrete_procedure": procedure})
    if coverage == 1.0 or len(overlap) >= 2 and coverage >= 0.72:
        return result("strong", "direct_subject_or_procedure_content_match", 0.9 + min(coverage, 1) * 0.1)
    # A substantive answer to one requested subproblem is useful even when
    # most of a verbose query concerns other symptoms or source auditing.
    if answered and procedure:
        if answered == query_problems and scope_matches:
            return result("strong", "direct_requested_mechanism_and_subject_procedure", 0.95)
        return result("medium", "concrete_procedure_answers_requested_subproblem", 0.75)
    if len(overlap) >= 2 and coverage >= 0.35:
        return result("medium", "shared_subject_or_transferable_procedure", 0.6 + min(coverage, 1) * 0.2)
    return result("weak", "partial_content_relation:" + ",".join(overlap), 0.3 + min(coverage, 1) * 0.2)


def apply_relevance_policy(query: str, records: list[dict[str, Any]], policy: Any = None, *, main_query: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(policy, str):
        resolved = resolve_min_relevance({"recall_policy": {"default_min_relevance": policy}})
    elif isinstance(policy, dict) and "effective_level" in policy:
        resolved = dict(policy)
        _level(resolved["effective_level"])
        if "configured_level" in resolved:
            configured = _level(resolved["configured_level"])
            if policy_manifest()["levels"].index(resolved["effective_level"]) > policy_manifest()["levels"].index(configured):
                resolved["effective_level"] = configured
    else:
        resolved = resolve_min_relevance(policy)
    levels = policy_manifest()["levels"]
    advanced = normalize_advanced_policy(resolved.get("advanced", {}))
    resolved["advanced"] = advanced
    counts = {level: 0 for level in [*levels, "none", "unknown"]}
    exclusion_reasons: dict[str, int] = {}
    kept, decisions = [], []
    for record in records:
        relation = classify_candidate(query, record, main_query=main_query)
        content = _semantic_text(_content(record))
        execution = bool(record.get('failure_signature') and record.get('repair_actions')) or bool(record.get('verification_evidence') and record.get('phase') in {'recover', 'verify'} and record.get('outcome') in {'correct', 'recovered'})
        discussion = bool(re.search(r'讨论|提炼|设计.{0,12}(?:机制|skill)|(?:discuss|design|propos)\w*.{0,40}(?:mechanism|skill|policy)', content, re.I))
        relation['candidate_role'] = 'execution_case' if execution else 'mechanism_discussion' if discussion else 'source_claim'
        signals = relation["match_signals"]
        if signals.get("unfiltered_browse_intent"):
            relationship = "collection_navigation"
        elif signals.get("concrete_procedure") and not signals.get("shared_subject_anchors"):
            relationship = "transferable_method"
        elif relation["level"] == "weak":
            relationship = "background"
        elif relation["level"] in levels:
            relationship = "contextual_answer"
        else:
            relationship = "unresolved" if relation["level"] == "unknown" else "unrelated"
        scope_verification = record.get("scope_verification") or {}
        scope_status = record.get("scope_status") or (scope_verification.get("status") if isinstance(scope_verification, dict) else None) or "unknown"
        requested_scope = resolved.get("required_scope") or {}
        structured_scope = record.get("primary_context") or {}
        if requested_scope and scope_status == "unknown" and all(structured_scope.get(key) == value for key, value in requested_scope.items()):
            # A shared capture directory/project label resolves a structural
            # locator only; it cannot authenticate a formal project identity.
            scope_status = "structural_scope_resolved_not_fact_verified"
        # Generic methods have no project identity to verify. A scope supplied
        # by a caller still constrains their provenance when explicitly set.
        scope_required = bool(record.get("scope_required") or requested_scope or record.get("scope_status") or record.get("scope_verification")) and relationship != "transferable_method"
        temporal = record.get("time_validity") or record.get("temporal_status") or "unknown"
        if isinstance(temporal, dict):
            temporal = temporal.get("status", "unknown")
        historical = temporal in {"historical", "expired", "superseded", "past", "outdated"} or record.get("state") in {"superseded", "expired"} or bool(record.get("superseded_by"))
        navigation = relationship == "collection_navigation" or scope_required and scope_status != "verified"
        relation.update({"relationship": relationship, "scope_status": scope_status,
                         "temporal_role": "historical_reference" if historical else ("current" if temporal in {"current", "valid"} else "unknown"),
                         "truth_status": record.get("truth_status", "unknown"),
                         "evidence_role": "navigation_only" if navigation else ("method_reference" if relationship == "transferable_method" else "source_claim_requires_verification"),
                         # Relevance never authorizes execution. Keep an explicit
                         # downstream grant only when no independent gate blocks it.
                         "execution_eligible": record.get("execution_eligible") is True and not historical and not navigation})
        level = relation["level"]
        counts[level] += 1
        accepted = level in levels and levels.index(level) <= levels.index(resolved["effective_level"])
        exclusion_reason = None
        if not accepted:
            exclusion_reason = "below_minimum_relevance" if level in levels else (relation["reasons"][0] if relation["reasons"] else "classification_relation_unavailable")
        elif record.get("permission_status") in {"denied", "blocked"} or scope_status in {"mismatch", "denied"} or record.get("hard_scope_match") is False:
            exclusion_reason = "hard_scope_or_permission_denied"
        elif relationship == "transferable_method" and not advanced["allow_transferable_methods"]:
            exclusion_reason = "transferable_methods_disabled"
        elif relationship == "background" and not advanced["allow_background"]:
            exclusion_reason = "background_disabled"
        elif historical and advanced["historical_mode"] == "current_only":
            exclusion_reason = "historical_reference_disabled"
        elif scope_required and scope_status != "verified" and advanced["scope_unknown_mode"] == "require_verified":
            exclusion_reason = "scope_verification_required"
        if exclusion_reason:
            accepted = False
            exclusion_reasons[exclusion_reason] = exclusion_reasons.get(exclusion_reason, 0) + 1
        record_id = record.get("id") or record.get("process_memory_id") or record.get("chunk_id")
        decisions.append({"id": record_id, **relation, "kept": accepted, "exclusion_reason": exclusion_reason})
        if accepted:
            kept.append({**record, **{key: relation[key] for key in ("relationship", "scope_status", "temporal_role", "truth_status", "evidence_role", "execution_eligible", "candidate_role")}, "relevance_level": level, "relevance_score": relation["score"], "relevance_reasons": relation["reasons"], "relevance_match_signals": relation["match_signals"], "recall_strength": "navigation" if navigation else "direct" if level == "strong" else "weak_background"})
    wants_cases = bool(re.search(r'故障|修复|经验|实际|案例|复盘|failure|repair|fix|incident|case|retrospective', str(main_query or query), re.I))
    kept.sort(key=lambda record: (({'execution_case': 0, 'source_claim': 1, 'mechanism_discussion': 2}.get(record['candidate_role'], 1) if wants_cases else 0), -record["relevance_score"]))
    return kept, {**resolved, "reason": "explained_content_relation_before_pagination", "level_counts": counts, "kept_count": len(kept), "excluded_count": len(records) - len(kept), "exclusion_reasons": exclusion_reasons, "decisions": decisions, "score_semantics": "ordinal_content_relation_not_vector_probability"}
