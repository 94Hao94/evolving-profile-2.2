#!/usr/bin/env python3
"""Local query controller for Evolving Profile.

The service is a localhost-only, transparent Evolving Profile reverse proxy.  Recall
requests are classified by answer shape, expanded only when useful, executed
against the existing Hindsight API, deduplicated, time-aware ranked, and
returned in the stable Evolving Profile response schema.  Every non-recall endpoint
is proxied byte-for-byte.  The active Evolving Profile data plane stays untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

EP_ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = Path(os.environ.get("EVOLVING_PROFILE_STATE_ROOT", str(Path.home() / ".evolving-profile")))
HAM_SOURCE_ROOT = EP_ROOT / "ham-os"
if str(HAM_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(HAM_SOURCE_ROOT))
from ham.api import Api as HamApi
from ham.evidence_runtime import ClaimLedger, EvidenceCandidate, QueryContract

CUSTOM_SCRIPTS = EP_ROOT / "host-adapter"
if str(CUSTOM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CUSTOM_SCRIPTS))
# Keep sibling runtime modules importable when the Controller is loaded by
# absolute path (the same boundary used by the service and regression tests).
CONTROLLER_ROOT = Path(__file__).resolve().parent
if str(CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_ROOT))
GUIDANCE_SOURCE_ROOT = EP_ROOT / "guidance"
if str(GUIDANCE_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(GUIDANCE_SOURCE_ROOT))
from retrieval_quality import build_retrieval_contract, evaluate_evidence_quality, project_record_index  # noqa: E402
from lib.relevance import (  # noqa: E402
    POLICY as RELEVANCE_POLICY,
    admit_items as relevance_admit_items,
    cross_encoder_scores,
    source_class,
    explicit_user_source_request,
    recurrence_prevention_alignment,
    semantic_query_expansions,
    continuity_task_alignment,
    system_taxonomy_alignment,
    is_association_closure_query,
    operational_audit_query,
    filter_superseded_brand_constraints,
)
from lib.governance import (  # noqa: E402
    apply_recall_governance,
    requires_historical_bank_recall,
)
from lib.admission_contract import CONTEXT_FIELDS, decision_key, make_decision  # noqa: E402
from recall import filter_question_echo_evidence  # noqa: E402
from fast_bank import FastMemoryBank  # noqa: E402

try:
    import tiktoken
except ImportError:  # pragma: no cover - production runtime includes tiktoken
    tiktoken = None

# Bump whenever recall/admission semantics change. This value is part of the
# continuation-cache identity; leaving it stale serves pre-fix traces after a
# user refresh and makes a real repair appear to have had no effect.
# This value participates in recall and continuation cache keys.  Any change
# to admission semantics must advance it; otherwise a restarted service can
# replay a pre-fix decision and make the status page look as though a new rule
# had no effect.
# Bump the execution identity when delivery semantics change.  This prevents a
# background-only packet written by the pre-foreground-completion controller
# from being reused as if it had crossed the current Hook boundary.
VERSION = "1.49.0"
VERSION_LABEL = "1.49.0-brand-supersession"
HOME = Path.home()
DEFAULT_CONTRACT = STATE_ROOT / "memory-contract-v5.json"
DEFAULT_CODEX_RUNTIME_CONFIG = STATE_ROOT / "codex.json"
DEFAULT_POLICY = STATE_ROOT / "memory-access-policy-v3.json"
DEFAULT_AUDIT = STATE_ROOT / "audit/query-controller.jsonl"
DEFAULT_EFFECTIVENESS_AUDIT = STATE_ROOT / "audit/memory-effectiveness.jsonl"
# Small keyed projection for the owner-facing status page.  The append-only
# effectiveness ledger remains authoritative; this only prevents its 1 MB tail
# window from making an older visible trace falsely appear as “0 injected”.
DEFAULT_EFFECTIVENESS_RECEIPT_INDEX = STATE_ROOT / "control-plane/memory-effectiveness-receipts.json"
# The audit JSONL has no retention limit and individual trace rows deliberately
# carry a full Claim Ledger.  A 1 MB tail therefore can lose a recent broad
# recall after a burst of later writes.  This bounded execution-keyed copy is
# solely a status-page projection; it never participates in recall or answer
# construction.
DEFAULT_TRACE_INDEX = STATE_ROOT / "control-plane/recall-trace-index.json"
DEFAULT_STATUS = STATE_ROOT / "control-plane/query-controller-status.json"
DEFAULT_CONTINUATION_CACHE = STATE_ROOT / "control-plane/recall-continuation-cache.json"
# Read-only freshness receipt produced after the mental-model scheduler verifies
# a stable official generation.  It is not a duplicate memory store.
DEFAULT_MENTAL_MODEL_CACHE = STATE_ROOT / "control-plane/mental-model-cache.json"
# A derived, read-only lexical cache lets the foreground Hook return an
# auditable Packet when the official Recall endpoint is busy with a long
# vector/LLM operation. It is never an authority source and every row still
# crosses the normal Controller/Hook admission and time-governance gates.
DEFAULT_FAST_MEMORY_INDEX = STATE_ROOT / "control-plane/fast-memory-index.sqlite3"
DEFAULT_FAST_MEMORY_SNAPSHOT = STATE_ROOT / "backups/v2-cutover-20260812T191145/personal-memory.memory_units.jsonl"
# Direct user policies are a small auditable retrieval sidecar. They are not a
# second memory bank: each row points to its source Hindsight fact and remains
# explicitly provisional until independent later evidence upgrades it.
DEFAULT_DIRECT_POLICY_INDEX = STATE_ROOT / "directive-policy-index.json"
DIRECT_POLICY_SECTION_MAX_CHARS = 900
# Approved aliases are a read-only precision aid for recall.  This registry is
# not a second memory bank: it only tells the controller that verified names
# such as "trainer" and "导学复习训练师" refer to the same entity.
DEFAULT_ENTITY_ALIAS_REGISTRY = STATE_ROOT / "entity-resolution-v2.json"
# This is a small deterministic control-plane state, not a second memory bank or
# embedding index. It records the live project/source context that Hindsight must
# honor before it considers older semantic memories.
DEFAULT_PROJECT_STATE = STATE_ROOT / "control-plane/project-current-state.json"
DEFAULT_BACKUP_ROOT = HOME / "hindsight-backups"
DEFAULT_CLOUD_BACKUP_DIR = DEFAULT_BACKUP_ROOT / "wps-cloud-hindsight-backups"
DEFAULT_BACKUP_LOG = STATE_ROOT / "backup.log"
DEFAULT_BACKUP_ERROR_LOG = STATE_ROOT / "backup-error.log"
DEFAULT_RESTORE_DRILL_REPORT = STATE_ROOT / "system-restore-drill-report.json"
RECALL_PATH = re.compile(r"^/v1/default/banks/([^/]+)/memories/recall$")
UPSTREAM_QUERY_TOKEN_LIMIT = 500
SAFE_QUERY_TOKEN_LIMIT = 480
TRACE_PREVIEW_CHARS = 800
TRACE_RESULT_LIMIT = 8
TRACE_RESULT_PREVIEW_CHARS = 160
VALID_QUERY_SHAPES = frozenset((
    "point", "current", "timeline", "system_map", "inventory", "synthesis", "audit",
    "conflict", "missing", "procedure",
))
# Direct source recovery is intentionally narrow: it is enabled only for an
# explicit provenance/audit request with a sufficiently distinctive phrase.
# It never creates a second index or expands ordinary semantic recalls.
DIRECT_EVIDENCE_MIN_ANCHOR_CHARS = 12
DIRECT_EVIDENCE_MAX_ANCHOR_CHARS = 180
DIRECT_EVIDENCE_MAX_ITEMS_PER_BANK = 12
SOURCE_BANK_HINTS = {
    "qianwen-archive-v1": ("千问", "qianwen"),
    "doubao-archive-v1": ("豆包", "doubao"),
    "openclaw-trainer": ("openclaw", "小龙虾", "trainer", "训练师", "导学复习训练师"),
    "feishu-trainer-work-v1": ("飞书", "培训导师", "工作群", "工作"),
}
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "accept-encoding",
}

# Signals used by the deterministic working-set gate.  The gate does not ask a
# model and does not try to answer the user's question.  It only decides whether
# the current task already contains enough context to avoid a redundant
# long-term-memory lookup.
TASK_LOCAL_ACTION_TERMS = (
    "按这个执行", "按你的建议", "就按这个", "直接执行", "赶紧执行", "赶紧改",
    "开工", "继续", "接着", "改吧", "修改吧", "修复吧", "照这个做", "就这样",
    "可以可以", "没问题", "同意", "批准", "全部执行", "改成", "缩短", "调整",
    "删除", "增加", "写成", "优化",
)
CURRENT_WORK_ITEM_TERMS = (
    "刚才", "这个页面", "这个界面", "这个图", "这张图", "这个文件", "这份文件",
    "这段", "上面", "下面", "本次", "当前任务", "这些东西", "这个反馈闭环",
)
LONG_TERM_RECALL_TERMS = (
    "历史记录", "所有历史", "以前说过", "之前说过", "上个任务", "上一个任务",
    "另一个任务", "其他任务", "跨任务", "跨会话", "跨项目", "原话", "原文",
    "来源", "证据", "记录在哪", "时间线", "演变", "最初", "后来", "哪次",
    "什么时候说", "回顾", "记得吗", "记忆中", "hindsight里", "长期记忆",
)
DURABLE_STATE_SUBJECTS = (
    "trainer", "openclaw", "hindsight", "agentmemory", "小黛", "专家y", "秘书y",
    "hermes", "bank", "记忆库", "hook", "mcp", "服务", "配置", "模型", "通道",
    "备份", "数据库", "召回机制", "记忆机制",
)
STATE_QUESTION_TERMS = (
    "是否还", "还经过", "还使用", "现在是否", "目前是否", "当前是否", "现在怎么",
    "目前怎么", "当前怎么", "启用了吗", "停用了吗", "生效了吗", "还正常", "是什么状态",
)

SHAPE_TERMS = {
    "audit": (
        "原话", "原文", "来源", "证据", "哪次", "什么时候说", "何时说",
        "时间找出来", "出处", "逐字", "记录在哪", "邮件之前", "日志",
        "quote", "source", "evidence", "verbatim",
    ),
    "conflict": (
        "冲突", "矛盾", "纠正", "改口", "取代", "替代", "失效", "记错",
        "为什么错", "新旧", "前后不一致", "倒着", "反向", "逆向", "单向", "supersede", "contradict",
    ),
    "current": (
        "现在", "目前", "当前", "最新", "现状", "是否还", "已经", "仍然",
        "启用", "停用", "运行状态", "生效", "今天", "recent", "current",
    ),
    "inventory": (
        "哪些", "所有", "全部", "都做过", "完整列举", "尽量完整", "清单",
        "盘点", "版图", "覆盖范围", "有多少", "分别有哪些", "回顾以前",
        "everything", "all of", "inventory", "list every",
    ),
    "timeline": (
        "时间线", "演变", "过程", "先后", "前后", "最初", "最开始", "一开始", "起初", "后来", "阶段",
        "一路", "发展到", "历史变化", "timeline", "evolution",
    ),
    "system_map": (
        "分别指什么", "分别是什么", "分别指的是", "各自指什么", "各自是什么",
        "别名", "简称", "全称", "上下游", "角色", "职责", "不能混为一谈",
        "不是同一个", "边界", "组件关系",
    ),
    "missing": (
        "有没有记录", "没找到", "缺失", "遗漏", "记得吗", "是否提到",
        "为什么没召回", "找不到", "没整理出来", "unknown", "missing",
    ),
    "synthesis": (
        "为什么", "原因", "规律", "模式", "评价我", "分析我", "归纳",
        "综合", "心智模型", "偏好", "风格", "因果", "共同点", "反思",
        "synthesi", "pattern", "why",
    ),
    "procedure": (
        "怎么做", "怎样", "如何做", "步骤", "流程", "办法", "解决方案", "如何配置",
        "怎么处理", "怎么修", "最佳实践", "procedure", "how to",
    ),
}

PRIORITY = (
    "audit", "conflict", "current", "system_map", "inventory", "timeline",
    "missing", "synthesis", "procedure",
)

# Detection order and primary-plan dominance are intentionally separate.
# A phrase can mention "current" inside a request for a full timeline or
# inventory; the broader answer shape must own the budget and coverage receipt.
PRIMARY_ORDER = (
    "audit", "conflict", "system_map", "inventory", "timeline", "missing",
    "synthesis", "procedure", "current",
)

STRATEGY_BY_SHAPE = {
    "point": "semantic_point",
    "current": "current_state",
    "timeline": "temporal_sequence",
    "system_map": "system_taxonomy",
    "inventory": "open_set_inventory",
    "synthesis": "evidence_synthesis",
    "audit": "provenance_audit",
    "conflict": "version_conflict",
    "missing": "absence_check",
    "procedure": "procedural_reuse",
}

ROUTE_ORDER = (
    "point", "current", "timeline", "system_map", "inventory", "synthesis",
    "audit", "conflict", "missing", "procedure",
)

ROUTE_PURPOSE = {
    "point": "围绕一个明确问题做窄范围语义匹配",
    "current": "优先确认最新有效状态与已被替代内容",
    "timeline": "按起点、转折和最近状态还原演变过程",
    "system_map": "解释多个命名系统的定义、角色、别名、上下游和边界",
    "inventory": "跨来源、时间、主体和结果尽量完整盘点",
    "synthesis": "汇总多条证据、反例和适用范围形成归纳",
    "audit": "追到原话、来源、证据时间和版本",
    "conflict": "同时检索旧说法、新纠正和当前有效性",
    "missing": "用别名、相邻主题和多来源检查漏召回",
    "procedure": "复用历史步骤、结果、失败点和回退方法",
}

PROFILE_BY_SHAPE = {
    # Ordinary atomic lookup stays narrow to reduce unrelated recall.  A
    # contrastive point question is promoted dynamically in build_plan because
    # it must retrieve both sides of a distinction even though it remains a
    # single upstream call.
    # Unknown expressions must never be weaker than Hindsight's official
    # recommended mid baseline. Explicitly-known atomic lookups can still be
    # narrowed by client-side deterministic authority adapters.
    "point": ("mid", 1200, 1),
    "current": ("mid", 1100, 2),
    "timeline": ("high", 2000, 3),
    "system_map": ("high", 2800, 4),
    "inventory": ("high", 2400, 4),
    "synthesis": ("high", 2200, 3),
    "audit": ("high", 1800, 3),
    "conflict": ("high", 1800, 3),
    "missing": ("high", 1800, 4),
    # A single how-to question can stay in the mid lane.  Multi-part
    # incident/process questions are promoted in ``build_plan`` below so the
    # official Bank can return the complete discovery -> diagnosis -> repair
    # -> regression/recovery chain instead of only the first few summaries.
    "procedure": ("mid", 1500, 2),
}

# A procedural request becomes a coverage problem when it asks for several
# lifecycle stages.  The distinction is structural (number of independent
# stages), not a topic allow-list, so a new project/process receives the same
# treatment without widening every ordinary "how do I ...?" lookup.
PROCEDURE_DEPTH_MARKERS = (
    "完整流程", "处理流程", "如何发现", "如何定位", "如何修复", "同题复测",
    "相邻变体", "失败时", "失败后", "回退", "回滚", "根因", "排查", "闭环",
    "discovery", "diagnos", "repair", "regression", "rollback", "recovery",
)


def comprehensive_procedure_requested(query: str, shapes: list[str]) -> bool:
    """Whether a procedure request needs broad Bank evidence.

    ``procedure`` is intentionally not always deep: ordinary one-step
    instructions should remain quick.  Three distinct lifecycle markers (or
    an explicit complete-process phrase) open the high-budget procedural lane
    and its bounded facet queries.  This keeps recall generalized while
    preventing a fixed Hindsight-specific allow-list or blanket deep search.
    """
    if "procedure" not in shapes:
        return False
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    if not text:
        return False
    if any(marker.casefold() in text for marker in ("完整处理流程", "完整流程", "端到端处理流程")):
        return True
    hits = {marker for marker in PROCEDURE_DEPTH_MARKERS if marker.casefold() in text}
    return len(hits) >= 3


SYSTEM_TAXONOMY_MARKERS = (
    "分别指什么", "分别是什么", "分别指的是", "各自指什么", "各自是什么",
    "别名", "简称", "全称", "上下游", "角色", "职责", "不能混为一谈",
    "不是同一个", "组件关系", "不能混淆",
    # A causal-chain audit may ask for each component's responsibility and
    # failure effect instead of using the literal “分别是什么”.  It is still
    # a bounded system map: the named components, their roles and the failure
    # edge are the requested scope, not an open-ended synthesis inventory.
    "因果链", "每个组件负责", "组件负责什么", "哪个组件", "组件出问题", "组件导致",
)
SYSTEM_TAXONOMY_STOPWORDS = {
    "agent", "memory", "os", "query", "controller", "system", "bank", "hook",
    "adapter", "full", "prompt", "status", "page", "api", "the", "and", "with",
}


def named_system_taxonomy_question(query: str) -> bool:
    """Recognize a bounded map of explicitly named systems/components.

    Questions that ask what several named systems mean and how they relate are
    not open-set inventories.  Treating the words “哪些/分别” as a broad
    inventory caused unrelated observations to occupy the Packet and produced
    false coverage gaps for sources/actions/scope.  This structural detector
    uses the user's explicit ASCII identifiers plus taxonomy relation language;
    it is not a product allow-list.
    """
    from lib.relevance import is_transfer_event_query
    if is_transfer_event_query(query):
        return False
    text = full_prompt_text(query)
    compact = re.sub(r"\s+", "", text).casefold()
    if not any(marker.casefold() in compact for marker in SYSTEM_TAXONOMY_MARKERS):
        return False
    # A media/document task can mention versioned segments (``V20``/``V25``)
    # and a “角色设定” as ordinary content.  Those tokens are not named
    # systems, and a lone role/boundary noun is not a taxonomy question.  Keep
    # the taxonomy route for an explicit relation/question marker, or for at
    # least two role/boundary markers used with an actual question/contrast.
    weak_taxonomy_markers = ("角色", "职责", "边界")
    strong_taxonomy_markers = tuple(
        marker for marker in SYSTEM_TAXONOMY_MARKERS
        if marker not in weak_taxonomy_markers
    )
    weak_count = sum(marker in compact for marker in weak_taxonomy_markers)
    strong_present = any(marker.casefold() in compact for marker in strong_taxonomy_markers)
    question_present = any(marker in compact for marker in ("是什么", "指什么", "如何", "怎么", "区分", "关系"))
    if not strong_present and not (weak_count >= 2 and question_present):
        return False
    tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", text)
        if token.casefold() not in SYSTEM_TAXONOMY_STOPWORDS
        and not re.fullmatch(r"v\d+[a-z]?", token.casefold())
    }
    # Compound names such as Agent Memory OS and Query Controller are often
    # separated by punctuation/whitespace.  Count them only as names when the
    # full compound is explicitly present, avoiding “agent” or “query” alone.
    if "agentmemoryos" in compact:
        tokens.add("agentmemoryos")
    if "querycontroller" in compact:
        tokens.add("querycontroller")
    return len(tokens) >= 2


def semantic_plan_shape_supported(query: str, shape: str, detected_shapes: list[str]) -> bool:
    """Keep the optional planner advisory instead of letting it widen scope.

    The Qwen planner sees the resolved Full Prompt and is useful for paraphrases,
    but its answer is not a new source of intent.  In particular, a method
    question that mentions ``CASE``/``当前状态`` as examples is not thereby a
    request to enumerate a named-system taxonomy.  Expensive shapes are
    accepted only when the same structural evidence is present in the local
    detector; cheap shapes may be recovered from explicit question language.
    """
    candidate = str(shape or "").strip().casefold()
    detected = {str(value).strip().casefold() for value in detected_shapes or []}
    if candidate in detected:
        return True
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    if candidate == "system_map":
        return bool(
            named_system_taxonomy_question(query)
            or is_association_closure_query(query)
            or named_mechanism_stage_question(query)
        )
    if candidate == "timeline":
        return bool(evolution_question(query) or timeline_method_question(query))
    if candidate == "inventory":
        return "inventory" in detect_shapes(query)
    if candidate == "missing":
        return any(marker in text for marker in ("遗漏", "漏掉", "缺失", "找不到", "为什么没召回", "是否提到"))
    if candidate == "conflict":
        return any(marker in text for marker in ("冲突", "矛盾", "新旧", "替代", "superseded", "失效"))
    if candidate == "procedure":
        return any(marker in text for marker in ("如何", "怎么", "步骤", "流程", "办法", "处理", "排查", "修复"))
    if candidate == "synthesis":
        return any(marker in text for marker in ("为什么", "原因", "规律", "归纳", "综合", "判断逻辑", "共同点"))
    if candidate == "audit":
        return any(marker in text for marker in ("来源", "证据", "原话", "出处", "记录", "哪次", "什么时候"))
    if candidate == "current":
        return any(marker in text for marker in ("当前", "现在", "目前", "最新", "现状", "是否还", "运行状态"))
    if candidate == "point":
        return not (detected - {"point"})
    return False

# A long-running recall is allowed only when the user explicitly asks for a
# comprehensive historical reconstruction. Broad technical questions may still
# use several facets, but must not unexpectedly stall every Codex turn.
EXPLICIT_DEEP_RECALL_TERMS = (
    "深度召回", "深度回顾", "全面回顾", "完整回顾", "全量回顾",
    "调取全部", "调取所有", "全部历史", "所有历史", "完整历史",
    "查全部记录", "所有相关记录", "尽可能完整", "所有关键决定",
)


def explicit_deep_recall_requested(query: str, shapes: list[str]) -> bool:
    text = re.sub(r"\s+", "", str(query or "")).casefold()
    return (
        any(term in text for term in EXPLICIT_DEEP_RECALL_TERMS)
        and bool(set(shapes) & {"timeline", "inventory", "synthesis", "audit", "conflict", "missing"})
    )


EXPLICIT_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})(?:日|号)?"
    r"(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def explicit_temporal_anchor(query: str) -> dict[str, Any] | None:
    """Parse an unambiguous calendar date without guessing a year or timezone.

    Hindsight natively understands Chinese date wording.  ISO and slash dates
    were previously left only as literal text, so their temporal signal could
    be missed by a Chinese-oriented extraction path.  This helper preserves the
    user query and adds a deterministic query timestamp plus a Chinese display
    anchor.  Month/day without a year deliberately remains untouched: guessing
    a year would fabricate chronology.
    """
    match = EXPLICIT_DATE_PATTERN.search(full_prompt_text(query))
    if not match:
        return None
    try:
        year, month, day = (int(match.group(index)) for index in range(1, 4))
        hour = int(match.group(4) or 12)
        minute = int(match.group(5) or 0)
        second = int(match.group(6) or 0)
        value = datetime(year, month, day, hour, minute, second, tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        return None
    precision = "minute" if match.group(4) is not None else "day"
    normalized = f"{year}年{month}月{day}日"
    if precision == "minute":
        normalized += f"{hour:02d}:{minute:02d}"
    return {
        "source": match.group(0),
        "normalized": normalized,
        "query_timestamp": value.isoformat(),
        "precision": precision,
    }


def compact_for_literal_match(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def direct_evidence_anchor(query: str) -> str | None:
    """Extract one literal phrase for explicit provenance recovery.

    This is a narrow, deterministic fallback for questions such as “find the
    original source/time for: …”.  It avoids semantic rewriting and only uses
    a long enough anchor that can be verified by exact containment in a local
    Hindsight list response.  If there is no safe anchor, normal recall remains
    the only route.
    """
    original = full_prompt_text(query).strip()
    candidates = re.findall(r"[\"“‘「](.*?)[\"”’」]", original, flags=re.S)
    # The wrapper delimiter is the first one.  Historical source text can
    # itself contain JSON, URLs, or a conversation_id with additional colons;
    # using the last delimiter would reduce the anchor to a timestamp tail.
    if "：" in original:
        candidates.append(original.split("：", 1)[-1])
    elif ":" in original:
        candidates.append(original.split(":", 1)[-1])
    # When the user did not quote a sentence, remove the request/provenance
    # wrapper and retain the proposition itself. This is still literal (no
    # generated rewrite), but avoids trying to find the whole question—such as
    # “我以前在飞书里是否说过……请找原话、时间和来源”—inside the archive.
    proposition = re.sub(
        r"^(?:我)?(?:以前|之前|当时)?(?:在(?:飞书|豆包|千问|微信|培训导师)(?:里|中|群里)?)?"
        r"(?:是否|有没有)?(?:说过|提到过|问过)?",
        "",
        original,
        flags=re.I,
    )
    proposition = re.split(
        r"[？?，,；;。]*(?:请|麻烦|帮我)?(?:找|查|核对|给出|告诉我|查看)"
        r"[^，。；;!?！？]{0,36}(?:原话|原文|时间|来源|出处|证据)",
        proposition,
        maxsplit=1,
        flags=re.I,
    )[0]
    proposition = proposition.strip(" \t\r\n，,。；;：:？?")
    if proposition and proposition != original:
        candidates.append(proposition)
    candidates.append(original)
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip(" \t\r\n，,。；;：:")
        # A trailing date explains when the source occurred; it is not part of
        # the lexical claim that we need to locate.
        candidate = re.split(r"[，,；;。]\s*(?:发生(?:于|在)?|时间(?:是|为)?|日期(?:是|为)?)\s*20\d{2}[年./-]", candidate, maxsplit=1)[0].strip()
        compact = compact_for_literal_match(candidate)
        if not (DIRECT_EVIDENCE_MIN_ANCHOR_CHARS <= len(compact) <= DIRECT_EVIDENCE_MAX_ANCHOR_CHARS):
            continue
        if len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", candidate)) < DIRECT_EVIDENCE_MIN_ANCHOR_CHARS:
            continue
        return candidate
    return None


def select_direct_evidence_banks(
    query: str,
    evidence_bank: str | None,
    configured_banks: list[Any],
) -> tuple[list[str], str]:
    """Choose literal source banks without fanning out to every archive.

    The unified evidence bank is the default cross-source index.  A named raw
    archive is added only when the latest user request names that source.  An
    explicit all-source audit may use all configured archives, but ordinary
    provenance checks must not multiply reads across unrelated stores.
    """
    configured: list[str] = []
    for value in configured_banks:
        bank = str(value or "").strip()
        if bank and bank not in configured:
            configured.append(bank)
    selected: list[str] = []
    if evidence_bank and str(evidence_bank) in configured:
        selected.append(str(evidence_bank))
    elif evidence_bank:
        selected.append(str(evidence_bank))
    # ``query`` may be a verified Full Prompt made from a preceding request
    # plus a terse follow-up.  Looking only at its last paragraph erases the
    # origin-to-present scope precisely when a user says “再详细一点”.
    text = re.sub(r"\s+", "", str(query or "")).casefold()
    all_sources = any(term in text for term in (
        "所有来源", "全部来源", "跨来源全部", "每个来源", "所有原始来源",
    ))
    for bank in configured:
        if bank in selected:
            continue
        hints = SOURCE_BANK_HINTS.get(bank, ())
        if all_sources or any(hint.casefold() in text for hint in hints):
            selected.append(bank)
    reason = (
        "用户明确要求跨全部来源核对。" if all_sources
        else "默认先查统一证据库；只追加用户明确点名的原始来源。"
    )
    return selected, reason

# Stable observations and mental models are an interpretation sidecar: they
# never replace a current attachment or a direct user correction, but they can
# still provide durable preferences, quality standards and decision boundaries.
# The gate is deliberately semantic rather than based on a fixed list of task
# names, so it generalizes to new projects without injecting a profile into
# ordinary local UI edits.
STABLE_GUIDANCE_SIGNALS = (
    "为什么", "原因", "分析", "归纳", "规律", "模式", "偏好", "风格", "解释",
    "学习", "复习", "建议", "判断", "决策", "方案", "架构", "质量", "验收",
    "检查", "审计", "优化", "机制", "系统", "记忆", "召回", "注入", "hindsight",
    "关联", "完整", "遗漏", "一致", "风险", "最佳实践", "如何", "怎么做",
)
MENTAL_MODEL_CACHE_TTL_SECONDS = 300
MENTAL_MODEL_SECTION_MAX_CHARS = 1200


def stable_guidance_needed(query: str, shapes: list[str]) -> bool:
    """Whether stable observations/models can change *how* we answer.

    This does not decide current facts. It only opens a bounded sidecar for
    durable guidance when the user asks for explanation, strategy, quality,
    memory behavior or an explicitly connected/complete result.
    """
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    if not text or len(text) < 4:
        return False
    if re.search(r'(?:长期|稳定|既有|历史).{0,6}(?:约束|规则|偏好|标准|指导)',text):
        return True
    # An inventory request such as “某月都做了什么、分类列出” needs factual
    # coverage, not a broad personal mental model.  Treat it as stable-guidance
    # eligible only when the user explicitly asks for a durable pattern/rule;
    # otherwise sidecars can make a clean recall look rich while injecting
    # unrelated backup or business-framework chapters.
    shape_needs_guidance = bool(set(shapes) & {"system_map", "synthesis", "procedure", "missing", "timeline"}) or (
        "inventory" in shapes
        and any(marker in text for marker in ("偏好", "规律", "模式", "机制", "策略", "方法", "框架", "原则"))
    )
    signal_hits = sum(1 for marker in STABLE_GUIDANCE_SIGNALS if marker.casefold() in text)
    return shape_needs_guidance or signal_hits >= 2 or (
        signal_hits >= 1 and any(marker in text for marker in ("我的", "用户", "长期", "跨任务", "跨会话"))
    )


def verified_mental_model_rows(bank_id: str, live_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach cache-content agreement without overriding official staleness.

    Agreement is a cache identity check only. It neither verifies the model's
    claims nor proves that source updates or retractions have been processed.
    """
    receipt: dict[str, Any] = {
        "used": False, "verified_models": 0, "reason": "没有可用的新鲜心智模型验证回执。",
        "semantic_verification": "not_performed",
    }
    try:
        cache = load_json(DEFAULT_MENTAL_MODEL_CACHE)
        generated = datetime.fromisoformat(str(cache.get("generated_at") or "").replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds()
        if (
            str(cache.get("bank_id") or "") != bank_id
            or not bool(cache.get("generation_stable"))
            or int(cache.get("stale_count") or 0) != 0
            or bool(cache.get("refresh_due"))
            or age_seconds < 0 or age_seconds > 1800
        ):
            receipt["reason"] = "心智模型验证回执不满足同 Bank、稳定生成、未到刷新阈值和 30 分钟内新鲜度条件。"
            return live_rows, receipt
        cached_models = dict(cache.get("models") or {})
        patched: list[dict[str, Any]] = []
        verified = 0
        for live in live_rows:
            row = dict(live)
            cached = dict(cached_models.get(str(row.get("id") or "")) or {})
            live_hash = hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()
            cached_hash = hashlib.sha256(str(cached.get("content") or "").encode("utf-8")).hexdigest()
            if cached and live_hash == cached_hash and not bool(cached.get("is_stale")):
                # Matching cached text proves copy agreement, not source freshness.
                # Preserve the live upstream flag, including unknown/missing.
                row["_ccy_freshness_receipt"] = "cache_content_match_only"
                verified += 1
            patched.append(row)
        receipt.update({
            "used": verified > 0, "verified_models": verified,
            "age_seconds": round(age_seconds, 2),
            "reason": "缓存正文一致；不代表来源仍有效，不覆盖官方过期状态。" if verified else "验证回执内容与当前官方模型不一致。",
        })
        return patched, receipt
    except Exception as error:
        receipt["reason"] = f"读取心智模型验证回执失败：{error!r}"
        return live_rows, receipt


def guidance_query_terms(query: str) -> set[str]:
    """Small deterministic lexical bridge for choosing a model *section*.

    Hindsight's fact recall still does semantic/keyword/entity/temporal search.
    Mental models are served by a separate official endpoint, so this helper
    only picks a relevant section from the five already-governed models.
    """
    # Section selection is a semantic retrieval lane.  Preserve a resolved
    # Full Prompt's prior-context paragraphs; reducing it to the last line
    # makes broad observations and mental-model guidance look unrelated.
    compact = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    generic = {"这个", "那个", "这些", "问题", "什么", "怎么", "如何", "为什么", "是不是", "现在", "还有", "需要", "一下", "一个", "我们"}
    terms = {term for term in STABLE_GUIDANCE_SIGNALS if term.casefold() in compact and term not in generic}
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", compact))
    for i in range(max(0, len(cjk) - 1)):
        piece = cjk[i:i + 2]
        if piece not in generic:
            terms.add(piece)
    for token in re.findall(r"[a-z][a-z0-9_.-]{2,}", compact):
        terms.add(token)
    # Stable quality standards are commonly expressed through their concrete
    # *anti-pattern* ("not just unit tests", "see the real chain") whereas
    # the model section records the positive rule ("end-to-end completion",
    # evidence, regression, user-visible delivery).  This is a reusable
    # acceptance vocabulary bridge for selecting a candidate section only;
    # the later mental-model admission still requires shared quality anchors
    # and topic evidence, so it cannot inject the model into arbitrary tests.
    real_chain_signals = ("单测", "真实链路", "运行时", "真实交互", "用户可见", "端到端")
    acceptance_signals = ("测试", "验收", "验证", "证据", "报告", "完成", "回归", "链路")
    if any(signal in compact for signal in real_chain_signals) and any(signal in compact for signal in acceptance_signals):
        terms.update({"端到端", "真实完成", "回归", "根因", "用户可见", "运行时", "证据"})
    return terms


def compact_guidance_section(section: str, max_chars: int = 560) -> str:
    """Keep the actionable, bounded core of a qualified model section.

    Qualification still reads the full section. This is delivery-only
    compression so a long historical explanation does not evict the action,
    scope, and boundary that made the section useful for this turn.
    """
    limit=max(120,int(max_chars))
    source=str(section or "").strip()
    if len(source)<=limit:
        return source
    title=""
    heading=re.search(r"^#{1,3}\s+(.+)$",source,re.M)
    if heading:
        title="## "+heading.group(1).strip()+"\n"
        source=source[heading.end():].strip()
    sentences=[value.strip() for value in re.split(r"(?<=[。！？.!?])\s*|\n+",source) if value.strip()]
    action_markers=("真实入口","用户可见","最终结果","复测","回归","验收","验证","检查","修复")
    boundary_markers=("不能","不得","不应","适用","范围","边界","反例","当前事实","历史")
    condition_markers=("仅适用","只有","前提","授权","除非","不得","不适用","条件","范围","边界")
    ranked=[]
    for index,sentence in enumerate(sentences):
        score=3*sum(marker in sentence for marker in action_markers)+2*sum(marker in sentence for marker in boundary_markers)
        ranked.append((score,index,sentence))
    conditions=[(index,sentence) for _,index,sentence in ranked if any(marker in sentence for marker in condition_markers)]
    actions=[(index,sentence) for score,index,sentence in ranked if score and (index,sentence) not in conditions]
    chosen=[]
    used=len(title)
    for index,sentence in sorted(conditions,key=lambda row:row[0])+sorted(actions,key=lambda row:row[0]):
        if not sentence or used+len(sentence)>limit:
            continue
        chosen.append((index,sentence));used+=len(sentence)
    if not chosen and sentences:
        chosen=[(0,sentences[0][:max(0,limit-len(title))])]
    body="".join(sentence for _,sentence in sorted(chosen))
    return (title+body)[:limit]


def prioritize_guidance_for_budget(items: list[dict[str, Any]], *, guidance_needed: bool) -> list[dict[str, Any]]:
    """Place qualified model guidance before long factual history when needed."""
    if not guidance_needed:
        return list(items or [])
    def rank(row: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index,item=row
        item_type=str(item.get("type") or "")
        if item_type=="mental_model":
            return 0,index
        if item_type=="observation":
            return 1,index
        return 2,index
    return [item for _,item in sorted(enumerate(items or []),key=rank)]


def matched_direct_policy_anchors(query: str) -> list[str]:
    """Return active-policy anchors that make long-term recall materially useful.

    This runs before the working-set short circuit.  A local edit instruction
    normally stays local, but “modify this page and check every related place”
    should retrieve a matching reusable policy rather than silently bypassing
    Hindsight.  Generic words alone never trigger it.
    """
    try:
        compact = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
        generic = {"这个", "那个", "这些", "问题", "什么", "怎么", "如何", "为什么", "是否", "是不是", "现在", "需要", "一下", "我们", "相关"}
        index = load_json(DEFAULT_DIRECT_POLICY_INDEX)
        hits: list[str] = []
        platform_wide = {"hindsight", "agentmemory", "codex", "hermes", "memory", "记忆"}
        for row in index.get("policies") or []:
            if row.get("status") != "active_provisional":
                continue
            keys = [str(k).casefold() for k in (row.get("keywords") or []) if len(str(k).strip()) >= 2]
            matched = [key for key in keys if key not in generic and key in compact]
            # A single platform name is a scope marker, not sufficient evidence
            # that *this* direct policy answers the question.  Two distinct
            # platform markers (for example AgentMemory + Hindsight), or any
            # policy-specific anchor, remain meaningful.
            specific = [key for key in matched if key not in platform_wide]
            if specific or len(set(matched)) >= 2:
                hits.extend(matched)
        return sorted(set(hits))
    except Exception:
        return []


TYPES_BY_SHAPE = {
    "point": ["world", "experience", "observation"],
    "current": ["world", "experience"],
    "timeline": ["experience", "world"],
    "system_map": ["world", "experience", "observation"],
    "inventory": ["world", "experience", "observation"],
    "synthesis": ["observation", "world", "experience"],
    "audit": ["world", "experience"],
    "conflict": ["world", "experience"],
    "missing": ["world", "experience", "observation"],
    "procedure": ["experience", "world", "observation"],
}

EXPANSION_SUFFIXES = {
    "current": (
        "检索最近明确状态、有效配置、最新用户纠正和替代关系。",
        "检索已停止、不再使用或明确禁止的旧渠道与重复动作，并核对当前唯一有效目标。",
    ),
    "timeline": (
        "检索较早阶段、起点和前因。",
        "检索中间变化、关键转折和版本替代。",
        "检索最近状态、结果和仍未完成事项。",
    ),
    "system_map": (
        "定义分面：分别检索每个点名系统或组件的定义、职责、输入输出和正式定位。",
        "关系分面：检索别名、简称、全称、上下游、入口/中间层/底座关系及数据流边界。",
        "边界分面：检索不能混为一谈的相邻概念、替代/非替代关系、当前有效范围和明确排除项。",
        "证据分面：检索支撑这些定义和关系的世界事实、经历、观察与已核验心智模型。",
    ),
    "inventory": (
        "按不同来源、渠道和项目范围检索，避免只命中单一来源。",
        "按不同时间阶段、版本和变化检索，补齐早期与近期内容。",
        "按不同主体、工具、行为、结果和例外检索，扩大已知集合覆盖。",
    ),
    "synthesis": (
        "检索反复出现的模式、稳定偏好和因果线索。",
        "检索反例、适用边界、变化和替代解释。",
    ),
    "audit": (
        "检索带明确来源、证据时间、原话或文档标识的记录。",
        "检索同一命题的较新纠正、冲突版本和当前有效状态。",
    ),
    "conflict": (
        "检索旧状态、原始说法和当时适用范围。",
        "检索较新纠正、替代关系、有效期和当前状态。",
    ),
    "missing": (
        "使用同义表达、别名和间接提法检索可能漏召回的记录。",
        "按来源、时间、主体和相邻主题检查索引范围与已知空白。",
        "检查相关经历、事实、观察中是否存在分散证据。",
    ),
    "procedure": (
        "检索过去实际采用的步骤、结果、失败点和回退方法。",
        "步骤分面：检索如何发现问题、如何区分召回/准入/投递，并保留每一层的证据。",
        "处置分面：检索根因定位、修复动作、同题复测、相邻变体复测和通过标准。",
        "恢复分面：检索失败时的回退条件、回退动作、证据保留和避免误报完成的方法。",
    ),
}

CURRENT_STATE_MARKERS = (
    "当前", "现行", "目前", "现在", "最新", "已核验", "当前有效",
    "不再", "已替代", "已取代", "已停用", "已禁用", "仅作为",
    "只发", "仅向", "唯一", "移除", "关闭", "停止", "未单独", "不重复",
    "current", "effective", "superseded",
)

RETIREMENT_MARKERS = (
    "不再", "已替代", "已取代", "已停用", "已禁用", "只发", "仅向",
    "唯一", "移除", "关闭", "停止", "未单独", "不重复", "no longer",
    "superseded",
)


COVERAGE_BY_SHAPE = {
    "point": ["semantic"],
    "current": ["latest_state", "supersession"],
    # A chronological answer is not complete merely because it has several
    # dated documents.  It must establish an origin, distinct changes and the
    # present state; ``stage_coverage`` enforces that semantic contract.
    "timeline": ["earlier", "transitions", "latest", "stage_coverage"],
    "system_map": ["system_definitions", "role_relations", "alias_relations", "boundaries"],
    "inventory": ["sources", "time_ranges", "subjects", "actions_results"],
    "synthesis": ["supporting_evidence", "counter_evidence", "scope"],
    "audit": ["source", "evidence_time", "version"],
    "conflict": ["old_state", "new_state", "validity"],
    "missing": ["aliases", "sources", "time_ranges", "known_gaps"],
    "procedure": ["steps", "outcomes", "failure_modes"],
}

CONTINUITY_TERMS = (
    "继续刚才", "接着刚才", "继续上次", "接着上次", "上个任务", "上一个任务",
    "另一个任务", "另一个会话", "其他任务", "跨任务", "跨会话", "刚才那个",
    "前一个任务", "之前那个任务", "接着做", "继续做", "接管", "接手", "移交", "交接", "未完成项",
    "previous task", "other thread", "continue from",
)


def analyze_query_dimensions(query: str, shapes: list[str]) -> dict[str, Any]:
    """Create a composable five-dimensional plan instead of one hard label."""
    # ``query`` is normally the agent-resolved Full Prompt at this stage. Do
    # not reduce it to the last line: a preceding handoff/context paragraph
    # can be the only place that identifies the task being resumed.
    text = full_prompt_text(query).casefold()
    continuity_signals = [term for term in CONTINUITY_TERMS if term.casefold() in text]
    continuity = bool(continuity_signals)
    if continuity:
        temporal_scope = "recent_cross_thread"
    elif "timeline" in shapes:
        temporal_scope = "historical_timeline"
    elif "current" in shapes:
        temporal_scope = "current"
    elif any(marker in text for marker in ("以前", "历史", "过去", "曾经")):
        temporal_scope = "historical"
    else:
        temporal_scope = "unspecified"

    cross_domain = any(marker in text for marker in ("跨领域", "综合我", "各方面", "整体"))
    if "system_map" in shapes:
        breadth = "set"
    elif "inventory" in shapes:
        breadth = "open_set"
    elif cross_domain:
        breadth = "cross_domain"
    elif len(shapes) > 1:
        breadth = "set"
    else:
        breadth = "focused"

    if "audit" in shapes:
        evidence_need = "direct_sources"
    elif "conflict" in shapes:
        evidence_need = "versioned_sources"
    elif continuity:
        evidence_need = "summary_with_source_thread"
    else:
        evidence_need = "answer_evidence"

    reasoning_map = {
        "point": "direct_fact", "current": "current_state", "timeline": "temporal_order",
        "system_map": "system_taxonomy",
        "inventory": "open_set_coverage", "synthesis": "evidence_synthesis",
        "audit": "provenance_audit", "conflict": "conflict_resolution",
        "missing": "absence_check", "procedure": "procedure_reuse",
    }
    reasoning = list(dict.fromkeys(reasoning_map[shape] for shape in shapes))
    if continuity and "state_restore" not in reasoning:
        reasoning.insert(0, "state_restore")
    layers = ["hindsight"]
    if continuity:
        layers.insert(0, "handoff")
        layers.insert(1, "recent_activity")
    if "synthesis" in shapes:
        layers.insert(0, "stable_profile")
    if evidence_need in {"direct_sources", "versioned_sources"}:
        layers.append("source_ledger")
    return {
        "temporal_scope": temporal_scope,
        "breadth": breadth,
        "continuity": continuity,
        "continuity_signals": continuity_signals[:6],
        "evidence_need": evidence_need,
        "reasoning": reasoning,
        "layers": list(dict.fromkeys(layers)),
        "disclosure_level": "L2" if evidence_need in {"direct_sources", "versioned_sources"} else "L1",
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def recent_jsonl(path: Path, limit: int = 40, max_bytes: int = 1_048_576) -> list[dict[str, Any]]:
    """Read a bounded tail of a JSONL audit without scanning the full history."""
    if not path.exists() or limit <= 0:
        return []
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        take = min(size, max_bytes)
        handle.seek(size - take)
        raw = handle.read(take)
    lines = raw.splitlines()
    if size > take and lines:
        lines = lines[1:]
    rows: list[dict[str, Any]] = []
    for raw_line in reversed(lines):
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if value.get("event") in {"recall", "recall_failed"}:
            rows.append(value)
        if len(rows) >= limit:
            break
    return rows


def recent_jsonl_events(
    path: Path,
    limit: int = 200,
    max_bytes: int = 1_048_576,
) -> list[dict[str, Any]]:
    """Read recent JSONL events without imposing a recall-event filter."""
    if not path.exists() or limit <= 0:
        return []
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        take = min(size, max_bytes)
        handle.seek(size - take)
        raw = handle.read(take)
    lines = raw.splitlines()
    if size > take and lines:
        lines = lines[1:]
    rows: list[dict[str, Any]] = []
    for raw_line in reversed(lines):
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
        if len(rows) >= limit:
            break
    return rows


def events_for_execution_ids(
    path: Path,
    execution_ids: set[str],
    *,
    recent_events: list[dict[str, Any]] | None = None,
    max_full_scan_bytes: int = 64 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Return feedback for visible executions without letting a tail cap erase truth.

    The normal dashboard read is intentionally bounded to the last 1 MB.  A
    visible recall trace can, however, outlive that tail after a burst of later
    test/answer feedback.  In that case reporting ``0 injected`` is false: the
    Hook receipt still exists earlier in the append-only ledger.  Reuse the
    fast tail first, then do a targeted bounded scan only for the missing
    execution ids rendered on this page.
    """
    wanted = {str(value) for value in execution_ids if str(value)}
    rows = list(recent_events or [])
    if not wanted or not path.exists():
        return rows

    def event_execution_id(event: dict[str, Any]) -> str:
        return str(event.get("execution_id") or event.get("query_id") or "")

    seen = {event_execution_id(event) for event in rows}
    missing = wanted - seen
    try:
        if not missing or path.stat().st_size > max_full_scan_bytes:
            return rows
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event_execution_id(event) in missing:
                    rows.append(event)
    except OSError:
        return rows
    return rows


def receipt_index_events(path: Path, execution_ids: set[str]) -> list[dict[str, Any]]:
    """Read indexed receipts for visible execution ids without scanning a huge ledger."""
    wanted = {str(value) for value in execution_ids if str(value)}
    if not wanted:
        return []
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        payload = {}
    entries = payload.get("entries") if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return []
    rows: list[dict[str, Any]] = []
    for execution_id in wanted:
        for event in entries.get(execution_id) or []:
            if isinstance(event, dict):
                rows.append(dict(event))
    return rows


def upsert_receipt_index(
    path: Path,
    event: dict[str, Any],
    *,
    max_executions: int = 120,
) -> None:
    """Persist the latest full feedback receipts for recent visible turns.

    This is a bounded observability projection, not a second memory store and
    not a retrieval limit.  It retains every feedback stage for each retained
    execution so a click on 9998 can render the actual injected records.
    """
    execution_id = str(event.get("execution_id") or event.get("query_id") or "")
    if not execution_id:
        return
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        payload = {}
    entries = dict(payload.get("entries") or {}) if isinstance(payload, dict) else {}
    current = [dict(row) for row in entries.get(execution_id) or [] if isinstance(row, dict)]
    stage = str(event.get("stage") or "")
    # A Hook retry may post the same stage twice. Replace that stage so the
    # receipt remains deterministic while preserving injection + answer + fix.
    current = [row for row in current if str(row.get("stage") or "") != stage]
    # Store the browser projection, not a second 100+ MB copy of every nested
    # candidate/Packet field.  The append-only JSONL remains the immutable
    # source of truth.
    projected = status_trace_projection(
        {"memory_effectiveness": dict(event)}
    )
    projected_event = projected.get("memory_effectiveness") or dict(event)
    # ``status_trace_projection`` deliberately lifts the four-stage ledger to
    # the trace root so the browser can render it without duplicating the
    # nested effectiveness object.  The receipt index stores only the
    # effectiveness event, however; dropping that lifted field made a later
    # 9998 click show Hook/Packet input and delivery as zero even when the
    # immutable receipt had real stage-3/4 counts.  Keep the bounded stage
    # projection in the keyed receipt as well.  It is observability data only
    # and does not participate in recall or answer construction.
    if isinstance(projected.get("pipeline_stages"), dict):
        projected_event["pipeline_stages"] = projected["pipeline_stages"]
    if isinstance(projected.get("relevance_admission"), dict):
        projected_event["relevance_admission"] = projected["relevance_admission"]
    current.append(projected_event)
    current.sort(key=lambda row: str(row.get("at") or ""))
    entries[execution_id] = current
    ordered = sorted(
        entries.items(),
        key=lambda pair: max((str(row.get("at") or "") for row in pair[1]), default=""),
        reverse=True,
    )
    # Independent bounded lanes: a replay burst must not evict human turns.
    def diagnostic(pair):
        return any(row.get("execution_mode") in {"replay", "shadow_replay", "cassette_replay"}
                   or row.get("prompt_origin") in {"test_probe", "diagnostic", "fixture", "benchmark"}
                   for row in pair[1])
    ordered = ([pair for pair in ordered if not diagnostic(pair)][:max_executions]
               + [pair for pair in ordered if diagnostic(pair)][:max_executions])
    atomic_json(path, {
        "schema": 1,
        "meaning": "Recent execution-keyed observability receipts; source of truth remains memory-effectiveness.jsonl.",
        "updated_at": utc_now(),
        "entries": dict(ordered),
    })


def select_status_trace_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Reserve owner visibility independently from newer diagnostic bursts."""
    diagnostic=[];owners=[]
    for row in sorted(rows,key=lambda r:str(r.get("at") or ""),reverse=True):
        lane=diagnostic if row.get("execution_mode") in {"replay","shadow_replay","cassette_replay"} or row.get("prompt_origin") in {"test_probe","diagnostic","fixture","benchmark"} else owners
        lane.append(row)
    quota=min(len(diagnostic),min(30,limit//3))
    selected=owners[:max(1,limit-quota)]
    selected.extend(diagnostic[:max(0,limit-len(selected))])
    return sorted(selected,key=lambda r:str(r.get("at") or ""),reverse=True)


def trace_index_rows(path: Path, *, limit: int = 120) -> list[dict[str, Any]]:
    """Return the bounded trace projection for owner-facing observability.

    The append-only audit remains the source of truth.  This index only
    protects recent, already-recorded executions from being invisible when a
    few broad traces exceed the safe tail-read byte window.
    """
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        payload = {}
    entries = payload.get("entries") if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return []
    rows = [dict(row) for row in entries.values() if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return rows[:max(1, int(limit))]


def upsert_trace_index(path: Path, event: dict[str, Any], *, max_executions: int = 120) -> None:
    """Persist one recent recall trace for the 9998 status projection."""
    execution_id = str(event.get("execution_id") or event.get("query_id") or "")
    if not execution_id or str(event.get("event") or "") not in {"recall", "recall_failed"}:
        return
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        payload = {}
    entries = dict(payload.get("entries") or {}) if isinstance(payload, dict) else {}
    entries[execution_id] = status_trace_projection(event)
    ordered = sorted(
        entries.items(),
        key=lambda pair: str(pair[1].get("at") or ""),
        reverse=True,
    )
    def diagnostic(pair):
        row=pair[1]
        return row.get("execution_mode") in {"replay","shadow_replay","cassette_replay"} or row.get("prompt_origin") in {"test_probe","diagnostic","fixture","benchmark"}
    quota=max(1,int(max_executions))
    ordered=([pair for pair in ordered if not diagnostic(pair)][:quota]
             + [pair for pair in ordered if diagnostic(pair)][:quota])
    atomic_json(path, {
        "schema": 1,
        "meaning": "Recent execution-keyed trace projection; source of truth remains query-controller.jsonl.",
        "updated_at": utc_now(),
        "entries": dict(ordered),
    })


def join_effectiveness(
    traces: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach honest retrieval-to-answer feedback to recall traces.

    Injected memory is not automatically considered used.  If the answer does
    not expose enough evidence, the status stays ``unknown`` rather than being
    mislabeled as ignored.
    """
    by_query: dict[str, list[dict[str, Any]]] = {}
    for event in reversed(events):  # input is newest-first; apply oldest-first
        execution_id = str(event.get("execution_id") or event.get("query_id") or "")
        if execution_id:
            by_query.setdefault(execution_id, []).append(event)
    joined: list[dict[str, Any]] = []
    for original in traces:
        trace = dict(original)
        execution_id = str(trace.get("execution_id") or trace.get("query_id") or "")
        related = by_query.get(execution_id, [])
        modes={str(e.get('execution_mode') or '').casefold() for e in related if e.get('execution_mode')}
        if trace.get('execution_mode'):modes.add(str(trace['execution_mode']).casefold())
        if modes:trace['execution_mode']=next(iter(modes)) if len(modes)==1 else 'mixed_execution_origins'
        retrieved_items = {
            str(item.get("id") or item.get("chunk_id") or ""): dict(item)
            for item in (trace.get("selected_results") or [])
            if item.get("id") or item.get("chunk_id")
        }
        injected_items: dict[str, dict[str, Any]] = {}
        candidate_items: dict[str, dict[str, Any]] = {}
        rejected_items: dict[str, dict[str, Any]] = {}
        injected_ids: list[str] = []
        candidate_ids: list[str] = []
        rejected_ids: list[str] = []
        cited_ids: list[str] = []
        likely_used_ids: list[str] = []
        unknown_ids: list[str] = []
        ignored_ids: list[str] = []
        corrected_ids: list[str] = []
        context_memory_coordination: dict[str, Any] = {}
        full_prompt_resolution: dict[str, Any] = {}
        session_context_index: dict[str, Any] = {}
        session_context_bundle: dict[str, Any] = {}
        claim_delivery: dict[str, Any] = {}
        memory_packet: dict[str, Any] = {}
        packet_delivery: dict[str, Any] = {}
        pipeline_stages: dict[str, Any] = {}
        # Injection time is not an answer receipt.  Keeping those timestamps
        # separate prevents the UI from claiming an answer was evaluated before
        # Codex has actually completed its turn.
        updated_at = None
        answer_feedback_received = False
        answer_feedback_at = None
        answer_feedback_method = None
        for event in related:
            event_stage = str(event.get("stage") or "")
            if event_stage == "answer":
                answer_feedback_received = True
                answer_feedback_at = event.get("at") or answer_feedback_at
                answer_feedback_method = event.get("method") or answer_feedback_method
                updated_at = event.get("at") or updated_at
            elif event_stage == "correction":
                updated_at = event.get("at") or updated_at
            if isinstance(event.get("context_memory_coordination"), dict):
                context_memory_coordination = dict(event["context_memory_coordination"])
            if isinstance(event.get("full_prompt_resolution"), dict):
                # Keep the resolver receipt alongside the Hook-delivery
                # receipt.  The status page must distinguish a native agent
                # Full Prompt from Qwen context reconstruction and an explicit
                # fallback failure; otherwise it falsely calls the source
                # “historical/unknown”.
                full_prompt_resolution = dict(event["full_prompt_resolution"])
            if isinstance(event.get("session_context_index"), dict):
                session_context_index = dict(event["session_context_index"])
            if isinstance(event.get("session_context_bundle"), dict):
                session_context_bundle = dict(event["session_context_bundle"])
            if isinstance(event.get("claim_delivery"), dict):
                claim_delivery = dict(event["claim_delivery"])
            if isinstance(event.get("memory_packet"), dict):
                memory_packet = dict(event["memory_packet"])
            if isinstance(event.get("packet_delivery"), dict):
                packet_delivery = dict(event["packet_delivery"])
            if isinstance(event.get("pipeline_stages"), dict):
                pipeline_stages = dict(event["pipeline_stages"])
            for item in event.get("injected_items") or []:
                memory_id = str(item.get("id") or item.get("chunk_id") or "")
                if memory_id:
                    injected_items[memory_id] = dict(item)
            for item in event.get("candidate_items") or []:
                memory_id = str(item.get("id") or item.get("chunk_id") or "")
                if memory_id:
                    candidate_items[memory_id] = dict(item)
            for item in event.get("rejected_items") or []:
                memory_id = str(item.get("id") or item.get("chunk_id") or "")
                if memory_id:
                    rejected_items[memory_id] = dict(item)
            for field, target in (
                ("injected_ids", injected_ids),
                ("candidate_ids", candidate_ids),
                ("rejected_ids", rejected_ids),
                ("cited_ids", cited_ids),
                ("likely_used_ids", likely_used_ids),
                ("unknown_ids", unknown_ids),
                ("ignored_ids", ignored_ids),
                ("corrected_ids", corrected_ids),
            ):
                for value in event.get(field) or []:
                    value = str(value)
                    if value and value not in target:
                        target.append(value)
        # The default for every injected item with no stronger evidence is
        # unknown.  Never convert unknown into ignored by subtraction.
        classified = set(cited_ids) | set(likely_used_ids) | set(ignored_ids)
        for memory_id in injected_ids:
            if memory_id not in classified and memory_id not in unknown_ids:
                unknown_ids.append(memory_id)
        # Event payloads carry full candidate objects.  Older events did not
        # have explicit candidate/rejected ID arrays, so derive them from the
        # objects for a truthful mixed-history view.
        candidate_ids = list(dict.fromkeys(candidate_ids + list(candidate_items)))
        rejected_ids = list(dict.fromkeys(rejected_ids + list(rejected_items)))
        item_ids = list(dict.fromkeys(
            list(retrieved_items) + candidate_ids + rejected_ids + injected_ids + cited_ids + likely_used_ids
            + unknown_ids + ignored_ids + corrected_ids
        ))
        item_rows = []
        for memory_id in item_ids:
            item = dict(retrieved_items.get(memory_id) or injected_items.get(memory_id) or candidate_items.get(memory_id) or rejected_items.get(memory_id) or {})
            if memory_id in rejected_ids:
                status = "rejected_before_injection"
            elif memory_id in cited_ids:
                status = "cited"
            elif memory_id in likely_used_ids:
                status = "likely_used"
            elif memory_id in ignored_ids:
                status = "ignored"
            elif memory_id in injected_ids:
                status = "unknown"
            else:
                status = "retrieved_only"
            item.update({
                "id": memory_id,
                "retrieved": memory_id in retrieved_items,
                "injected": memory_id in injected_ids,
                "use_status": status,
                "corrected": memory_id in corrected_ids,
            })
            item_rows.append(item)
        trace["memory_effectiveness"] = {
            "event_name": "memory_effectiveness",
            "retrieved_count": len(retrieved_items),
            "candidate_count": len(set(candidate_ids)),
            "rejected_before_injection_count": len(set(rejected_ids)),
            "injected_count": len(set(injected_ids)),
            "cited_count": len(set(cited_ids)),
            "likely_used_count": len(set(likely_used_ids)),
            "unknown_count": len(set(unknown_ids)),
            "ignored_count": len(set(ignored_ids)),
            "corrected_count": len(set(corrected_ids)),
            "updated_at": updated_at,
            "answer_feedback_received": answer_feedback_received,
            "answer_feedback_at": answer_feedback_at,
            "answer_feedback_method": answer_feedback_method,
            "answer_observation_status": (
                "not_applicable" if not injected_ids else
                "awaiting_answer_feedback" if not answer_feedback_received else
                "visible_answer_evidence" if (cited_ids or likely_used_ids) else
                "answer_observed_without_attributable_reuse"
            ),
            "answer_observation_explanation": (
                "本次没有注入长期记忆，因此不适用回答利用判断。" if not injected_ids else
                "回答尚未结束或 Stop 回执尚未到达；不会把等待状态误标成已评估。" if not answer_feedback_received else
                "回答文字中出现可验证的记忆复用线索；这仍是可见证据，不等同读取模型内部注意力。" if (cited_ids or likely_used_ids) else
                "回答已被检查，但未发现可归因复用措辞；这不等同于记忆未注入或未被模型读取。"
            ),
            "items": item_rows,
            "context_memory_coordination": context_memory_coordination,
            "full_prompt_resolution": full_prompt_resolution,
            "session_context_index": session_context_index,
            "session_context_bundle": session_context_bundle,
            "claim_delivery": claim_delivery,
            "memory_packet": memory_packet,
            "packet_delivery": packet_delivery,
            "pipeline_stages": pipeline_stages,
            "note": "候选不等于注入；注入不等于使用。回答回执与注入时间分开记录；没有可见复用证据时保持“尚无法判断”，不倒推为忽略。",
        }
        joined.append(trace)
    return joined


def _status_item_projection(item: dict[str, Any]) -> dict[str, Any]:
    """Keep owner-visible evidence while dropping diagnostic expansion trees."""
    row = dict(item or {})
    admission = dict(row.get("admission") or (row.get("metadata") or {}).get("_ccy_admission") or {})
    if admission:
        keep = (
            "decision", "reason", "source_class", "semantic_relevance_score",
            "strong_hits", "weak_hits", "hits", "matched_concepts",
            "stable_guidance_anchors", "specific_topic_hits", "fixed_item_limit",
        )
        row["admission"] = {key: admission.get(key) for key in keep if key in admission}
    # Metadata is duplicated by the normalized top-level source/admission fields
    # and can contain the full graph/continuity diagnostic tree.
    row.pop("metadata", None)
    for key in ("text", "content"):
        if key in row and "text_preview" not in row:
            row["text_preview"] = " ".join(str(row.get(key) or "").split())[:800]
        row.pop(key, None)
    return row


def _status_effect_projection(effect: dict[str, Any], *, detail_limit: int) -> dict[str, Any]:
    projected = dict(effect or {})
    # Injection receipts repeat the same candidate objects at the event root,
    # in the four-stage ledger, and again in the joined effectiveness view.
    # Keep every delivered object, but retain only a compact audit sample of
    # candidates/rejections. Exact ID arrays and counts remain untouched.
    for field, preserve_all in (
        ("injected_items", True),
        ("candidate_items", False),
        ("rejected_items", False),
        ("deferred_items", False),
    ):
        values = [
            _status_item_projection(item)
            for item in list(projected.get(field) or [])
            if isinstance(item, dict)
        ]
        kept = values if preserve_all else values[:detail_limit]
        if values or field in projected:
            projected[field] = kept
        omitted = len(values) - len(kept)
        if omitted:
            projected[f"{field}_projection_omitted_count"] = omitted
    projected["items"] = [
        _status_item_projection(item) for item in list(projected.get("items") or [])
        if isinstance(item, dict)
    ]
    delivery = dict(projected.get("claim_delivery") or {})
    claims = list(delivery.pop("claims", []) or [])
    if claims:
        delivery["claims_projection_omitted_count"] = len(claims)
    if delivery:
        projected["claim_delivery"] = delivery
    packet = dict(projected.get("memory_packet") or {})
    # rendered_context duplicates every bundle as one large XML string.  The
    # status page renders bundles and their transport IDs directly.
    packet.pop("rendered_context", None)
    packet.pop("rendered_context_hash", None)
    compact_bundles = []
    for bundle in list(packet.get("bundles") or []):
        if not isinstance(bundle, dict):
            continue
        row = dict(bundle)
        row["evidence_summaries"] = [
            " ".join(str(value).split())[:800]
            for value in list(row.get("evidence_summaries") or [])[:4]
        ]
        if "summary" in row:
            row["summary"] = " ".join(str(row.get("summary") or "").split())[:800]
        compact_bundles.append(row)
    if compact_bundles:
        packet["bundles"] = compact_bundles
    if packet:
        projected["memory_packet"] = packet
    return projected


def status_trace_projection(value: dict[str, Any], *, detail_limit: int = 12) -> dict[str, Any]:
    """Return a bounded browser transport while preserving exact receipts.

    The append-only audit and execution-keyed trace index retain every row.
    The owner page needs exact counts and every actually delivered item, but it
    does not need dozens of multi-kilobyte rejected candidates repeated in the
    top-level stage ledger, relevance receipt, and effectiveness receipt.
    """
    row = dict(value or {})
    if isinstance(row.get("selected_results"), list):
        row["selected_results"] = [
            _status_item_projection(item)
            for item in row["selected_results"][:detail_limit]
            if isinstance(item, dict)
        ]
    receipt = dict(row.get("claim_receipt") or {})
    receipt_claims = list(receipt.pop("claims", []) or [])
    if receipt_claims:
        receipt["claim_ids"] = [
            str(claim.get("claim_id") or "")
            for claim in receipt_claims if isinstance(claim, dict) and claim.get("claim_id")
        ]
        receipt["claims_projection_omitted_count"] = len(receipt_claims)
    if receipt:
        row["claim_receipt"] = receipt
    effect = _status_effect_projection(
        dict(row.get("memory_effectiveness") or {}), detail_limit=detail_limit
    )
    # The Controller appends the retrieval/admission trace first; the Hook then
    # adds post-processing and packet-delivery receipts through effectiveness.
    # Merge both ledgers with the later Hook receipt taking precedence.  Picking
    # the first non-empty dict makes a truthful injected=1 row look like stage
    # 3/4 both processed zero items.
    stages = dict(row.get("pipeline_stages") or {})
    stages.update(dict(effect.get("pipeline_stages") or {}))
    compact_stages: dict[str, Any] = {}
    sample_fields = (
        "candidate_items", "sidecar_candidate_items", "qualified_items",
        "rejected_items", "deferred_items", "admitted_items", "not_delivered_items",
    )
    for name, raw_stage in dict(stages).items():
        if not isinstance(raw_stage, dict):
            compact_stages[name] = raw_stage
            continue
        stage = dict(raw_stage)
        for field in sample_fields:
            if not isinstance(stage.get(field), list):
                continue
            original = [
                _status_item_projection(item) if isinstance(item, dict) else item
                for item in stage[field]
            ]
            stage[field] = original[:detail_limit]
            omitted = len(original) - len(stage[field])
            if omitted:
                stage[f"{field}_projection_omitted_count"] = omitted
        # Every item that crossed the Memory Packet boundary remains auditable,
        # but its duplicated graph/continuity metadata is removed.
        if isinstance(stage.get("delivered_items"), list):
            stage["delivered_items"] = [
                _status_item_projection(item) if isinstance(item, dict) else item
                for item in stage["delivered_items"]
            ]
        compact_stages[name] = stage
    if compact_stages:
        row["pipeline_stages"] = compact_stages

    relevance = dict(row.get("relevance_admission") or {})
    relevance.update(dict(effect.get("relevance_admission") or {}))
    for field in ("rejected_items", "deferred_items"):
        if not isinstance(relevance.get(field), list):
            continue
        original = [
            _status_item_projection(item) if isinstance(item, dict) else item
            for item in relevance[field]
        ]
        relevance[field] = original[:detail_limit]
        omitted = len(original) - len(relevance[field])
        if omitted:
            relevance[f"{field}_projection_omitted_count"] = omitted
    if relevance:
        row["relevance_admission"] = relevance

    items = list(effect.get("items") or [])
    kept: list[dict[str, Any]] = []
    non_delivered = 0
    for item in items:
        if bool(item.get("injected")):
            kept.append(item)
        elif non_delivered < detail_limit:
            kept.append(item)
            non_delivered += 1
    effect["items"] = kept
    omitted = len(items) - len(kept)
    if omitted:
        effect["items_projection_omitted_count"] = omitted
    effect.pop("pipeline_stages", None)
    effect.pop("relevance_admission", None)
    row["memory_effectiveness"] = effect
    return row


def _encoding():
    if tiktoken is None:
        return None
    # Hindsight's recall endpoint enforces its 500-token ceiling with
    # cl100k_base.  Counting with a newer tokenizer can under-count Chinese
    # text and falsely mark an oversized upstream query as safe.
    return tiktoken.get_encoding("cl100k_base")


def query_token_count(value: str) -> int:
    encoder = _encoding()
    if encoder is not None:
        return len(encoder.encode(str(value or "")))
    # Conservative fallback: CJK characters usually consume at least one token;
    # ASCII text is approximated in four-character groups.
    text = str(value or "")
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def compact_query(value: str, max_tokens: int = SAFE_QUERY_TOKEN_LIMIT) -> tuple[str, dict[str, Any]]:
    """Bound a query while preserving both framing and the latest direct ask.

    Hindsight rejects queries above 500 tokens.  We reserve 20 tokens for
    tokenizer differences and keep the head plus a larger tail so dates/entities
    introduced early and the actual question commonly stated late both survive.
    """
    text = str(value or "").strip()
    before = query_token_count(text)
    if before <= max_tokens:
        return text, {"before_tokens": before, "after_tokens": before, "compacted": False}
    encoder = _encoding()
    marker = "\n…[中间上下文已按查询上限压缩]…\n"
    if encoder is None:
        # This branch is intentionally conservative and only used if tiktoken is
        # unavailable. Chinese-heavy input gets roughly one character per token.
        marker_budget = len(marker)
        head_chars = max(40, int((max_tokens - marker_budget) * 0.32))
        tail_chars = max(80, max_tokens - marker_budget - head_chars)
        bounded = text[:head_chars] + marker + text[-tail_chars:]
        while query_token_count(bounded) > max_tokens and tail_chars > 40:
            tail_chars -= 8
            bounded = text[:head_chars] + marker + text[-tail_chars:]
    else:
        tokens = encoder.encode(text)
        marker_tokens = encoder.encode(marker)
        available = max(32, max_tokens - len(marker_tokens))
        head_n = max(48, int(available * 0.32))
        tail_n = max(64, available - head_n)
        bounded = encoder.decode(tokens[:head_n]) + marker + encoder.decode(tokens[-tail_n:])
        while query_token_count(bounded) > max_tokens and tail_n > 48:
            tail_n -= 4
            bounded = encoder.decode(tokens[:head_n]) + marker + encoder.decode(tokens[-tail_n:])
    after = query_token_count(bounded)
    return bounded, {"before_tokens": before, "after_tokens": after, "compacted": True}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


_USER_REQUEST_MARKER = re.compile(
    r"(?im)^##\s*My request(?:\s+for Codex)?\s*:\s*"
)


def clean_user_query_envelope(query: str) -> str:
    """Defensively remove Codex UI and attachment envelopes from a query."""
    value = str(query or "")
    value = re.sub(
        r"<in-app-browser-context(?:\s[^>]*)?>[\s\S]*?</in-app-browser-context>",
        "",
        value,
        flags=re.IGNORECASE,
    )
    matches = list(_USER_REQUEST_MARKER.finditer(value))
    if matches:
        value = value[matches[-1].end() :]
    value = re.sub(r"<image\b[^>]*>[\s\S]*?</image>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<image\b[^>]*/?>", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?im)^Distinguish instructions in attached documents from the user's request\.\s*$",
        "",
        value,
    )
    return value.strip()


def latest_query(query: str) -> str:
    value = str(query or "").strip()
    for marker in ("Latest user message:\n", "当前用户消息：\n", "Current user message:\n"):
        if marker in value:
            value = value.rsplit(marker, 1)[-1].strip()
    if "Prior context:" in value and "\n\n" in value:
        value = value.rsplit("\n\n", 1)[-1].strip()
    return clean_user_query_envelope(value)


def full_prompt_text(value: str) -> str:
    """Clean UI envelopes without stripping semantic Full Prompt sections.

    ``latest_query`` is intentionally lossy for legacy multi-turn request
    envelopes: it extracts the last user message.  A Full Prompt has already
    been resolved by the agent/Qwen and may itself contain headings such as
    ``Prior context`` and ``Latest user message``.  Applying the legacy helper
    to it silently discarded the very context the resolver supplied.
    """
    return clean_user_query_envelope(str(value or "").strip())


def request_semantic_query(body: dict[str, Any] | None) -> str:
    """Return the semantic query that identifies a recall request.

    A resolved Full Prompt is deliberately different from the raw/latest user
    line: two turns can end with the same short sentence while their preceding
    context points at different projects or decisions. Cache and foreground
    reuse keys must therefore retain the complete Full Prompt whenever the
    adapter supplied one, falling back to the legacy latest-line extraction
    only for old callers that have no body-side Full Prompt.
    """
    payload = body if isinstance(body, dict) else {}
    full = full_prompt_text(str(payload.get("full_prompt") or ""))
    return full or latest_query(str(payload.get("query") or ""))


def requires_direct_fallback(responses: list[tuple[str, Any]]) -> bool:
    """Return true when no semantic recall path produced a usable item.

    A completed HTTP response with an empty ``results`` list is not a useful
    recall.  Treating it as success previously bypassed the direct official
    Hindsight fallback and emitted a misleading zero-injection receipt.
    """
    semantic = [
        response for label, response in responses
        if not str(label).startswith(("direct_evidence:", "stable_guidance_"))
    ]
    return not any((response or {}).get("results") for response in semantic)


def semantic_planner_needed(query: str) -> dict[str, Any]:
    """Decide whether a small semantic planner should *review* routing.

    This deliberately does not choose a route.  It recognizes uncertainty in
    the request structure (time window, breadth, comparison, multi-hop), then
    lets the configured Hindsight LLM propose a bounded plan.  The deterministic
    planner remains the safe fallback when the model is busy or unavailable.
    """
    text = re.sub(r"\s+", "", str(query or "")).casefold()
    # Origin-to-present evolution has a deterministic, test-covered structure
    # (stage facets plus closure). Asking the optional Qwen planner to
    # rediscover it makes the 0.8s Hook planning call fall back to Router V3,
    # which erases the very route we need.
    if evolution_question(query):
        return {
            "needed": False,
            "reason": "deterministic_evolution_route",
            "signals": {"time_scope": True, "coverage": True, "relation": True, "multi_part": False},
        }
    # Long-context continuation policy is already recognized structurally by
    # ``continuation_policy_question`` and has a deterministic procedure /
    # synthesis contract.  Calling the optional semantic planner here adds a
    # second Qwen round without improving recall, and in the real Hook path
    # can consume the entire foreground deadline.  Keep the route explicit so
    # the decision is observable in the trace and testable without a provider.
    if continuation_policy_question(query):
        return {
            "needed": False,
            "reason": "deterministic_continuation_policy_route",
            "signals": {"time_scope": False, "coverage": False, "relation": False, "multi_part": True},
        }
    time_scope = bool(re.search(
        r"(?:20\d{2}年)?\d{1,2}月(?:份)?|(?:20\d{2}[年./-])?\d{1,2}(?:月|[./-])\d{1,2}(?:日|号)?|"
        r"(?:本周|上周|本月|上月|去年|今年|前后|最初|最开始|一开始|起初|后来)", text,
    ))
    coverage = any(token in text for token in (
        "都", "全部", "所有", "分别", "完整", "清单", "盘点", "梳理", "回顾", "总结", "分类", "归类",
        # Natural completeness checks are coverage requests even when the user
        # does not repeat “清单”.  Keep this structural and only escalate when
        # combined with a time range or another ambiguity signal below.
        "有没有", "有无", "落下", "漏掉", "遗漏", "找全",
    ))
    relation = any(token in text for token in (
        "为什么", "关联", "相关", "前后", "影响", "区别", "对比", "是否", "漏", "遗漏",
    ))
    multi_part = sum(text.count(token) for token in ("包括", "以及", "分别", "还有", "同时", "并且")) >= 2
    needed = bool((time_scope and coverage) or (coverage and relation) or (time_scope and relation) or multi_part)
    return {
        "needed": needed,
        "reason": "ambiguous_scope_or_coverage" if needed else "deterministic_route_confident",
        "signals": {
            "time_scope": time_scope,
            "coverage": coverage,
            "relation": relation,
            "multi_part": multi_part,
        },
    }


def normalize_semantic_plan(value: Any, *, allowed_source: str = "") -> dict[str, Any] | None:
    """Validate an LLM or caller plan without granting it authority privileges."""
    if not isinstance(value, dict):
        return None
    source = str(value.get("source") or allowed_source or "").strip().casefold()
    if source not in {"agent", "qwen3.7-plus"}:
        return None
    shape = str(value.get("shape") or "").strip().casefold()
    try:
        confidence = float(value.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    if shape not in VALID_QUERY_SHAPES or confidence < 0.65 or confidence > 1:
        return None
    coverage = [
        str(item) for item in list(value.get("coverage_dimensions") or [])
        if str(item) in {x for values in COVERAGE_BY_SHAPE.values() for x in values}
    ]
    return {
        "source": source,
        "shape": shape,
        "confidence": round(confidence, 3),
        "coverage_dimensions": list(dict.fromkeys(coverage)),
        "reason": " ".join(str(value.get("reason") or "").split())[:300],
    }


class HindsightSemanticPlanner:
    """Small, serialized Qwen planner for only structurally ambiguous recalls."""

    def __init__(self, config: dict[str, Any]):
        self.config = dict(config.get("semanticPlanner") or {})
        self.lock = threading.BoundedSemaphore(1)

    @staticmethod
    def _dotenv(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" not in line or line.lstrip().startswith("#"):
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            pass
        return values

    def plan(self, query: str, trigger: dict[str, Any]) -> dict[str, Any] | None:
        if not self.config.get("enabled", False) or not trigger.get("needed"):
            return None
        if not self.lock.acquire(blocking=False):
            return None
        try:
            env = self._dotenv(Path(str(self.config.get("envFile") or STATE_ROOT / "profiles/evolving-profile-api.env")).expanduser())
            base_url = str(env.get("EVOLVING_PROFILE_API_LLM_BASE_URL") or "").rstrip("/")
            api_key = str(env.get("EVOLVING_PROFILE_API_LLM_API_KEY") or "")
            model = str(env.get("EVOLVING_PROFILE_API_LLM_MODEL") or "qwen3.7-plus")
            if not (base_url and api_key):
                return None
            prompt = (
                "你是 Hindsight 的查询规划器，不检索记忆、不回答用户。只根据用户当前问题，"
                "判断应该怎样召回长期记忆。输出严格 JSON："
                '{"shape":"point|current|timeline|system_map|inventory|synthesis|audit|conflict|missing|procedure",'
                '"confidence":0-1,"coverage_dimensions":["..."],"reason":"一句中文理由"}。'
                "inventory=在限定范围内尽量完整盘点（例如某月都做了什么、列条目、分分类）；"
                "timeline=只有用户明确要时间先后、演变、前因后果时才使用；synthesis=归纳规律；"
                "system_map=解释多个用户明确点名的系统/组件定义、角色、别名、上下游和边界；"
                "point=单一明确事实。时间范围本身不等于 timeline。不得因为出现‘所有’就忽略当前任务修改指令。"
                # The planner is part of the semantic path, so it must see the
                # resolved Full Prompt rather than only its final line. Raw
                # prompt provenance remains carried separately in the request
                # body and never becomes a routing permission.
                "用户问题：" + full_prompt_text(query)
            )
            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": int(self.config.get("maxCompletionTokens", 220)),
                "response_format": {"type": "json_object"},
                "enable_thinking": False,
            }
            request = urllib.request.Request(
                base_url + "/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=max(0.5, float(self.config.get("timeoutMs", 3500)) / 1000.0)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
            parsed = json.loads(content)
            parsed["source"] = "qwen3.7-plus"
            return normalize_semantic_plan(parsed)
        except Exception:
            return None
        finally:
            self.lock.release()


def working_set_decision(query: str) -> dict[str, Any]:
    """Decide whether the current task already supplies the needed context.

    This is intentionally conservative: only an explicit continuation/action
    that points at material already visible in the current task may bypass
    Hindsight.  Questions about durable state, history, provenance, another task
    or the user's stable profile continue to use long-term recall.
    """
    raw = latest_query(query)
    # A punctuation-only follow-up (for example "？") has no word character.
    # Sending it to Hindsight produces a deterministic upstream 422 and pollutes
    # the controller error counter without yielding any retrievable meaning.
    if not re.search(r"\w", raw, flags=re.UNICODE):
        return {
            "sufficient": True,
            "decision": "skip_nonsemantic_input",
            "reason": "当前消息没有可检索字词，不向 Hindsight 发出无效查询。",
            "signals": {
                "task_local_actions": [],
                "current_work_items": [],
                "long_term_requirements": [],
                "durable_state_subjects": [],
                "state_questions": [],
            },
        }
    text = re.sub(r"\s+", "", raw).casefold()
    local_actions = [term for term in TASK_LOCAL_ACTION_TERMS if term.casefold() in text]
    work_items = [term for term in CURRENT_WORK_ITEM_TERMS if term.casefold() in text]
    long_term = [term for term in LONG_TERM_RECALL_TERMS if term.casefold() in text]
    durable_subjects = [term for term in DURABLE_STATE_SUBJECTS if term.casefold() in text]
    state_questions = [term for term in STATE_QUESTION_TERMS if term.casefold() in text]
    explicit_profile = any(
        marker in text
        for marker in ("我的偏好", "我的风格", "对我的了解", "评价我", "分析我", "我以前")
    )
    durable_state_question = bool(durable_subjects and state_questions)
    # A phrase can look like an edit because it contains “不要/内容”, while
    # actually asking to explain the durable memory mechanism itself.  Require
    # two independent mechanism terms so ordinary UI edits remain fast, but a
    # question about injection admission, graph closure, or recall receipts
    # cannot be swallowed by the current-working-set shortcut.
    mechanism_terms = ("图谱", "星座", "召回", "检索", "注入", "回执", "相关性", "准入", "闭包", "心智模型", "观察", "实体别名")
    mechanism_hits = [term for term in mechanism_terms if term in text]
    durable_memory_mechanism_question = len(mechanism_hits) >= 2
    local_continuation = bool(local_actions and work_items)
    historical_cues = ("历史", "以前", "之前", "上次", "当时", "记忆", "跨任务", "跨会话", "原话", "来源", "证据")
    # A concrete edit instruction is already anchored by the visible task.  In
    # particular, “不要写……所有功能……” must not be reclassified as a request
    # to inventory every memory merely because it contains the word “所有”.
    # Do not use the single character “别” as an edit verb.  It also occurs in
    # ordinary words such as “分别”, which previously turned a durable profile
    # question (“分别有什么偏好？”) into a false current-task no-op.
    edit_verbs = ("不要", "写", "改", "修改", "调整", "删除", "增加", "保留", "做成", "修复", "优化", "设计")
    edit_targets = ("字眼", "表述", "功能", "页面", "界面", "图", "文件", "格式", "按钮", "布局", "内容", "版本")
    direct_task_instruction = bool(
        any(marker in text for marker in edit_verbs)
        and any(marker in text for marker in edit_targets)
        and not any(marker in text for marker in historical_cues)
        # “我要求你做视觉检查时，哪些方面必须核对？” describes a
        # durable quality rule.  It is not an instruction to alter the visible
        # page, even though it contains “做/页面”.
        and not any(marker in text for marker in ("哪些", "什么", "为什么", "如何", "怎样", "是否", "应当"))
    )
    # A terse continuation such as “还有哪些占比较大的空间” has no durable
    # subject or time anchor.  Searching a shared bank in that state produces
    # plausible-looking but cross-project noise.  Let the active task context
    # answer it; long-term recall resumes as soon as the user names history,
    # a project/entity, or a source.
    # A phrase such as “给客户的成品材料发出前要从哪些方面验收” is a
    # self-contained durable question, not a continuation merely because it
    # contains “哪些方面”.  Require an anaphoric/continuation cue as well as the
    # short generic phrase before the visible working set may bypass recall.
    anaphoric_followup = bool(
        re.match(r"^(?:那|那么|这个|这些|这种|其他|另外|还有|再|然后)", text)
        or any(marker in text for marker in ("除此之外", "别的呢", "其他呢", "还有呢"))
    )
    vague_followup = bool(
        len(text) <= 36
        and any(marker in text for marker in ("还有哪些", "还有什么", "哪些地方", "哪些方面", "占比较大", "还差什么"))
        and anaphoric_followup
        and not any(marker in text for marker in historical_cues)
        and not durable_state_question
    )
    # “你测试的怎么样了 / 进度怎么样了 / 完成了吗” is a follow-up to the
    # visible task, not a fresh historical lookup.  Without this guard the
    # controller made an unnecessary recall, then displayed a misleading
    # “0 injected” after relevance rejection.  Keep this deliberately narrow
    # so a named historical test still enters long-term recall.
    short_status_followup = bool(
        len(text) <= 24
        and any(marker in text for marker in ("测试的怎么样", "进度怎么样", "完成了吗", "结果怎么样", "弄好了吗"))
        and not any(marker in text for marker in historical_cues)
        and not durable_state_question
    )
    # A screenshot-backed follow-up such as “刷新了，还是这样啊” is an
    # observation about the immediately visible task state.  A bare semantic
    # recall for those six characters has no subject anchor and previously
    # either returned filename noise or, worse, was hidden behind an attachment
    # source guard.  Preserve a complete trace, but let the active task inspect
    # the observed state instead of querying the shared Bank.
    observed_status_followup = bool(
        len(text) <= 24
        and any(marker in text for marker in ("刷新了还是这样", "刷新后还是这样", "还是这样", "依旧这样", "仍然这样", "没变化", "没有变化"))
        and not any(marker in text for marker in historical_cues)
        and not durable_state_question
    )
    internal_maintenance = bool(
        raw.lstrip().startswith("## Memory Writing Agent")
        or raw.lstrip().startswith("<hindsight_checkpoint>")
    )
    # Approval phrases such as “按这个执行” already bind to the immediately
    # preceding assistant proposal even when the user does not repeat “刚才”.
    approved_action = any(
        marker in text
        for marker in ("按这个执行", "按你的建议", "按你推荐", "按推荐执行", "按照推荐", "就按这个", "全部执行", "直接执行")
    )
    sufficient = bool(
        (local_continuation or approved_action or direct_task_instruction or vague_followup or short_status_followup or observed_status_followup or internal_maintenance)
        and not long_term
        and not durable_memory_mechanism_question
        and not durable_state_question
        and not explicit_profile
    )
    if internal_maintenance:
        reason = "这是系统内部整理/恢复文本，不作为用户长期记忆召回；不向 Hindsight 发起读取。"
    elif vague_followup:
        reason = "问题没有给出历史、项目或实体锚点；先使用当前任务上下文，避免把共享库里看似相近但无关的内容注入。"
    elif short_status_followup:
        reason = "这是对当前任务执行进度或测试结果的短追问；直接使用当前任务状态，不发起会造成误导性‘0 注入’的长期记忆查询。"
    elif observed_status_followup:
        reason = "这是对当前截图或页面刚刷新后的短反馈；应检查当前可见状态并保留完整链路，不用无主题的长期记忆检索制造文件名噪声。"
    elif direct_task_instruction and sufficient:
        reason = "这是针对当前任务内容的具体修改指令；先使用当前对话和当前对象，避免“所有/哪些”等普通词误触发全库盘点。"
    elif sufficient:
        reason = "当前消息是在确认或继续处理本任务中已经可见的对象，不需要重复读取长期记忆。"
    elif long_term:
        reason = "问题明确要求历史、跨任务、来源或时间证据，需要长期记忆。"
    elif durable_memory_mechanism_question:
        reason = "问题同时涉及多个长期记忆机制（如图谱、召回、注入或回执）；需要读取真实历史规则和执行证据，不能当作当前页面的局部修改。"
    elif durable_state_question:
        reason = "问题在核对一个可长期变化的系统或角色状态，需要确认最新有效记录。"
    elif explicit_profile:
        reason = "问题要求稳定画像或跨经历判断，需要长期记忆证据。"
    else:
        reason = "当前工作集未能独立证明信息充分，保留一次聚焦长期记忆召回。"
    return {
        "sufficient": sufficient,
        "decision": "inspect_current_observed_state" if observed_status_followup else ("use_current_working_set" if sufficient else "consult_long_term_memory"),
        "reason": reason,
        "signals": {
            "task_local_actions": local_actions[:6],
            "current_work_items": work_items[:6],
            "direct_task_instruction": direct_task_instruction,
            "vague_followup": vague_followup,
            "short_status_followup": short_status_followup,
            "observed_status_followup": observed_status_followup,
            "anaphoric_followup": anaphoric_followup,
            "internal_maintenance": internal_maintenance,
            "long_term_requirements": long_term[:6],
            "durable_memory_mechanism_terms": mechanism_hits[:6],
            "durable_state_subjects": durable_subjects[:6],
            "state_questions": state_questions[:6],
        },
    }


def infer_role(user_agent: str, bank_id: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    bank = urllib.parse.unquote(bank_id or "").casefold()
    for role in ("secretary-y", "writer-master", "trainer", "expert", "peixun"):
        if role in bank:
            return role
    agent = (user_agent or "").casefold()
    if "hindsight-codex" in agent:
        return "codex"
    if "hermes" in agent or "hindsight-client-python" in agent:
        # The local Hermes adapter uses the official Python Hindsight client.
        # Codex and OpenClaw identify themselves explicitly, so this generic
        # Python integration signature is unambiguous on this installation.
        return "hermes"
    if "openclaw" in agent:
        return "openclaw"
    return "default"


def origin_to_present_structure(query: str) -> bool:
    """Recognize a generic request for an evolution across a time span.

    This is deliberately structural: it does not name a product, project or
    component.  A request that explicitly spans an origin and the present is
    a timeline even when it omits the literal word "timeline".
    """
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    origin = ("最初", "最开始", "一开始", "起初", "初版", "原始版本", "早期")
    present = ("现在", "目前", "当前", "如今", "现状", "最新")
    if any(marker in text for marker in origin) and any(marker in text for marker in present):
        return True
    if re.search(r"从.{0,40}到(?:现在|目前|当前|如今|现状|最新)", text):
        return True
    # “自官方下载安装以来” is a common Chinese origin-to-now form.  It
    # contains neither the literal “从” nor “现在”, but “以来/至今” supplies
    # the same closed temporal interval.  Whether it is truly an evolution
    # request is still checked by ``evolution_question`` below, so this does
    # not turn an ordinary date statement into a timeline route.
    return bool(re.search(r"自.{0,40}(?:以来|至今)", text))


def timeline_method_question(query: str) -> bool:
    """Recognize a policy question about maintaining a version timeline.

    ``timeline`` is also the answer shape for an actual origin-to-present
    history.  The two requests have different coverage contracts: a method
    question such as “which historical conclusions should remain and how do
    we mark superseded versions?” needs temporal/validity evidence, but it
    cannot prove an *origin stage* that the user never asked us to reconstruct.
    Requiring ``stage_coverage`` for that shape caused an otherwise complete
    policy answer to remain permanently incomplete.  Keep this structural and
    conservative: an explicit origin-to-present/evolution request always wins
    and still requires the origin stage.
    """
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    temporal = any(marker in text for marker in (
        "时间线", "版本", "状态变更", "状态变化", "有效期", "生效时间",
        "证据时间", "superseded", "替代关系", "历史结论",
    ))
    method = any(marker in text for marker in (
        "如何", "应如何", "怎样", "哪些必须", "哪些可以", "去重", "避免",
        "保留", "标记", "仲裁", "处理", "规则", "策略",
    ))
    if not (temporal and method):
        return False
    # A genuine reconstruction of an origin-to-present evolution keeps the
    # stricter stage contract, even when it also asks for the maintenance
    # policy.  ``evolution_question`` is defined below, so use only the
    # structural origin check here and let the caller apply the evolution
    # guard after both helpers are available.
    return not origin_to_present_structure(query)


def evolution_question(query: str) -> bool:
    """Whether an origin-to-present request asks for changes, not a date."""
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    change_requested = any(marker in text for marker in (
        "变迁", "演变", "变化", "发展", "迭代", "历程", "经历", "阶段", "多少次", "一路",
    ))
    if origin_to_present_structure(query):
        return change_requested
    # Natural comparison prompts often enumerate temporal stages instead of
    # using a literal “从…到…” span: official/original, later/after adoption,
    # and current.  Requiring both a comparison/change relation and at least
    # two stage groups keeps this structural rather than product-specific.
    stage_groups = (
        ("官方", "官方版", "原始", "初版", "起初", "早期"),
        ("后续", "后来", "之后", "接入后", "升级后", "改造后"),
        ("现在", "当前", "目前", "如今", "现状", "最新"),
    )
    stage_count = sum(any(marker in text for marker in group) for group in stage_groups)
    comparison = any(marker in text for marker in ("比较", "对比", "区别", "差异"))
    return stage_count >= 2 and comparison and change_requested


def timeline_query_signal(query: str) -> bool:
    """Return whether the Full Prompt actually asks for temporal evidence.

    ``阶段`` is frequently used as an incidental noun (for example,
    “选题梳理阶段已结束”).  Treating that single word as a timeline request
    makes the Hook run ``filter_timeline_evidence`` and can discard otherwise
    qualified literature, implementation or status evidence.  Temporal
    admission therefore needs a relation/interval signal, while preserving
    explicit origin-to-present, version, date and before/after questions.
    """
    if evolution_question(query):
        return True
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    explicit = (
        "时间线", "时间轴", "变迁", "演变", "演进", "历程", "历史变化",
        "先后", "前后", "起点到", "从官方", "官方以来", "发展历程",
        "版本演进", "版本变化", "状态变更", "状态变化", "替代关系",
        "最开始", "最初", "一开始", "起初", "后来", "接入后", "升级后",
    )
    if any(marker in text for marker in explicit):
        return True
    if "阶段" in text and any(marker in text for marker in (
        "阶段变化", "阶段演进", "阶段历程", "不同阶段", "各阶段",
        "阶段分别", "阶段有哪些", "按阶段", "分阶段", "阶段顺序",
    )):
        return True
    # An operational audit often says “之前总结的…现在开始” while
    # comparing today's receipts.  Those words are not a request to rebuild a
    # product history; keep the audit candidates out of the timeline filter
    # unless it used one of the explicit temporal relations above.
    if operational_audit_query(query):
        return False
    if "之前" in text and any(marker in text for marker in ("现在", "后来", "目前", "如今")):
        return True
    date_signals = re.findall(
        # Require a Chinese month marker or a full ISO date.  The previous
        # ``\d{1,2}-\d{1,2}`` branch classified ordinary ranges such as
        # “400-800 条” and “3-5 个方向” as two dates, opening the timeline
        # filter for a literature feasibility prompt.
        r"(?:20\d{2}年)?\d{1,2}月\d{1,2}(?:日|号)?|"
        r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}|"
        r"(?<!\d)\d{1,2}[./]\d{1,2}(?!\d)|"
        r"(?:今天|昨天|前天|本周|上周|本月|上月)",
        text,
    )
    return len(date_signals) >= 2 or ("分别" in text and "之前" in text)


def continuation_policy_question(query: str) -> bool:
    """Detect an abstract long-context continuation policy question.

    Words such as ``当前状态`` and ``证据`` often occur as examples of the
    activity-task envelope.  They must not by themselves turn a question about
    how to recover a continuation into a live-status/provenance audit.  This
    detector relies on the discourse structure (continuation markers + method
    request + context-recovery subject), so it generalizes beyond Hindsight's
    component names and does not depend on a benchmark phrase.
    """
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    continuation_hits = sum(
        marker in text for marker in (
            "长任务", "续办", "续写", "继续", "不要停", "接着做", "把新的补上",
            "最近三轮", "远距离上下文",
        )
    )
    method_hits = sum(
        marker in text for marker in (
            "为什么不能", "为什么不", "如何结合", "怎么结合", "判断逻辑",
            "避免误判", "避免把", "不能仅依赖", "不能只依赖",
        )
    )
    context_hits = sum(
        marker in text for marker in (
            "上下文", "fullprompt", "活动任务", "任务证据", "意图",
        )
    )
    direct_audit = any(
        marker in text for marker in (
            "找出原话", "原文出处", "具体哪一条", "实际注入", "投递回执",
        )
    )
    return continuation_hits >= 2 and method_hits >= 1 and context_hits >= 1 and not direct_audit


def detect_shapes(query: str) -> list[str]:
    text = full_prompt_text(query).casefold()
    found = [
        shape for shape in PRIORITY
        if any(term.casefold() in text for term in SHAPE_TERMS[shape])
    ]
    # ``证据``/``来源`` also occur as ordinary domain-workflow nouns (for
    # example, a literature plan that says not to invent paper evidence).  Do
    # not let that incidental wording promote the whole request to the
    # provenance-audit profile.  An audit route needs either an explicit
    # original/source request, a runtime reconciliation, or a ledger-style
    # source/time/version proposition.  This keeps expensive audit facets and
    # their strict admission gate from swallowing otherwise valid task
    # evidence, without weakening explicit source or runtime audits.
    if "audit" in found:
        compact_text = re.sub(r"\s+", "", text)
        explicit_raw = any(marker in compact_text for marker in (
            "原话", "原文", "逐字", "出处", "哪次", "什么时候说", "记录在哪",
            "我的原文", "具体哪条原话",
        ))
        ledger_request = (
            any(marker in compact_text for marker in ("来源", "证据时间", "版本", "证据链", "原始事实", "权威"))
            and any(marker in compact_text for marker in ("核对", "审计", "追溯", "按证据", "依据", "证明", "验收"))
            and any(marker in compact_text for marker in ("历史", "状态", "结论", "记录", "命题", "结果", "当前", "时间"))
        )
        if not operational_audit_query(query) and not explicit_raw and not ledger_request:
            found = [shape for shape in found if shape != "audit"]
    # Media/document prompts often contain a noun such as “角色设定” or
    # “边界” without asking for a system taxonomy.  The old keyword scan
    # promoted those prompts to ``system_map`` and then applied the strict
    # named-component/relationship gate, which could turn a perfectly valid
    # V20–V25 file operation into an empty Packet.  Keep the map lane only
    # when there is a named-system taxonomy/association request or at least
    # two independent relation markers; a lone role/boundary noun is subject
    # context, not a component map.
    if "system_map" in found:
        relation_markers = (
            "分别指什么", "分别是什么", "各自指什么", "各自是什么", "别名", "简称", "全称",
            "上下游", "职责", "不能混为一谈", "不是同一个", "组件关系", "因果链",
            "每个组件负责", "组件负责什么", "哪个组件", "组件出问题", "组件导致",
            "角色", "边界",
        )
        relation_signal_count = sum(marker in text for marker in relation_markers)
        if not (
            named_system_taxonomy_question(query)
            or is_association_closure_query(query)
            or named_mechanism_stage_question(query)
            or relation_signal_count >= 2
        ):
            found = [shape for shape in found if shape != "system_map"]
    # In a long-context policy question, “当前状态/证据” usually describes
    # the activity-task envelope rather than a request for the live runtime or
    # verbatim provenance.  Keep the durable method/synthesis lanes and avoid
    # adding expensive status/audit coverage from incidental examples.
    if continuation_policy_question(query):
        found = [shape for shape in found if shape not in {"audit", "current"}]
        for shape in ("procedure", "synthesis"):
            if shape not in found:
                found.append(shape)
    # A named-system taxonomy question (“X/Y/Z分别是什么、别名/上下游/边界”)
    # is a bounded mapping, not an inventory of every memory containing
    # “哪些/分别”.  Give it its own route and remove the broad inventory lane;
    # the route still performs multi-facet retrieval and relation closure.
    if named_system_taxonomy_question(query):
        found = [shape for shape in found if shape != "inventory"]
        if "system_map" not in found:
            found.append("system_map")
    # Structural signals generalize beyond a fixed topic vocabulary. They only
    # affect retrieval shape; they never invent a domain or an answer.
    enumeration_signals = sum(text.count(token) for token in ("包括", "以及", "分别", "还有", "等等"))
    if enumeration_signals >= 2 and "inventory" not in found:
        found.append("inventory")
    if ("之前" in text and any(x in text for x in ("现在", "后来", "目前"))) and "timeline" not in found:
        found.append("timeline")
    if origin_to_present_structure(query) and "timeline" not in found:
        found.append("timeline")
    if evolution_question(query) and "timeline" not in found:
        found.append("timeline")
    # Two or more explicit dates/periods are a timeline request even when the
    # user never says the word "时间线".  This keeps the classifier generic and
    # fixes questions such as “8月4日和8月5日之前分别做了什么”.
    date_signals = re.findall(
        r"(?:20\d{2}[年./-])?\d{1,2}(?:月|[./-])\d{1,2}(?:日|号)?|"
        r"(?:今天|昨天|前天|本周|上周|本月|上月)",
        text,
    )
    if (len(date_signals) >= 2 or ("分别" in text and "之前" in text)) and "timeline" not in found:
        found.append("timeline")
    # A bounded-period review such as “梳理一下8月份都干了什么，列出条目分好类”
    # asks for coverage across that period even when it does not use the exact
    # word “清单”.  Treat the combination of a period, review verb and coverage
    # cue as inventory.  This is structural routing, not a new keyword-only
    # topic rule, so ordinary “整理一个文件” is unaffected.
    period_review = bool(re.search(r"(?:20\d{2}年)?\d{1,2}月(?:份)?", text)) and (
        any(marker in text for marker in ("梳理", "回顾", "总结", "列出", "归类", "分类", "盘点"))
        or any(marker in text for marker in ("有没有", "有无", "落下", "漏掉", "遗漏", "找全"))
    ) and any(marker in text for marker in ("都", "全", "全部", "分别", "条目", "有没有", "有无", "落下", "漏掉", "遗漏", "找全"))
    if period_review and "inventory" not in found:
        found.append("inventory")
    # A bounded request for the user's recent visible entries (for example
    # “查看最近十几条我主动正常的条目”) is an inventory even when it omits
    # “清单/盘点”.  Require three independent signals so ordinary questions
    # containing only “最近” or “记录” do not widen into a full-bank read.
    recent_entry_review = bool(
        any(marker in text for marker in ("最近", "近期", "今天", "昨天", "本周", "本月"))
        and any(marker in text for marker in ("条目", "记录", "回合", "几条", "多少条"))
        and any(marker in text for marker in ("查看", "查找", "检查", "审查", "看看", "有没有问题", "是否有问题", "正常"))
    )
    if recent_entry_review and "inventory" not in found:
        found.append("inventory")
    # Shared-versus-private is a reusable relationship structure, not a
    # trainer-specific shortcut.  It needs synthesis coverage so the answer
    # includes both the common layer and the bounded role-local layer.
    shared_terms = ("共享", "共用", "跨角色", "跨智能体")
    private_terms = ("私有", "专属", "自己的", "本角色")
    if any(term in text for term in shared_terms) and any(term in text for term in private_terms) and "synthesis" not in found:
        found.append("synthesis")
    # “哪些方面必须核对” asks for the quality criteria of one concrete
    # operation.  It is not a request to enumerate the whole memory bank.  If
    # treated as inventory, the broad-recall profile can surface plausible but
    # unrelated historical items and then reject every item at injection time.
    # Keep this structural: the same safeguard applies to any bounded
    # “which aspects must be checked/verified” question, not merely to visual
    # inspection.
    bounded_criteria_question = bool(
        any(marker in text for marker in ("哪些方面", "哪些项", "哪些维度"))
        and any(marker in text for marker in ("必须核对", "需要核对", "必须检查", "需要检查", "应当核对", "应当检查", "验收"))
        and not any(marker in text for marker in ("全部列举", "完整列举", "尽量完整", "盘点", "清单", "有多少", "所有记录"))
    )
    if bounded_criteria_question:
        found = [shape for shape in found if shape != "inventory"]
    # Re-apply the temporal relation guard after all structural additions.
    # This is intentionally at the end: origin/date logic above may add a
    # timeline shape even when the initial keyword scan did not.  A lone
    # incidental “阶段” must never activate the destructive Hook filter.
    if "timeline" in found and not timeline_query_signal(query):
        found = [shape for shape in found if shape != "timeline"]
    return [shape for shape in PRIORITY if shape in found] or ["point"]


def build_route_decisions(query: str, shapes: list[str], primary: str) -> list[dict[str, Any]]:
    """Return the complete route catalogue plus evidence for this decision.

    The dashboard can therefore render inactive routes as well as the routes
    actually selected.  This is deterministic audit metadata and adds no model
    call or retrieval call.
    """
    text = full_prompt_text(query).casefold()
    enumeration_signals = sum(text.count(token) for token in ("包括", "以及", "分别", "还有", "等等"))
    timeline_structure = (
        "之前" in text and any(x in text for x in ("现在", "后来", "目前"))
    ) or origin_to_present_structure(query)
    decisions: list[dict[str, Any]] = []
    for shape in ROUTE_ORDER:
        signals: list[str] = []
        if shape != "point":
            for term in SHAPE_TERMS.get(shape, ()):
                if term.casefold() in text and term not in signals:
                    signals.append(term)
        if shape == "inventory" and enumeration_signals >= 2 and not signals:
            signals.append(f"枚举结构信号×{enumeration_signals}")
        if shape == "system_map" and named_system_taxonomy_question(query) and not signals:
            signals.append("命名系统＋定义/关系/边界结构")
        if shape == "timeline" and timeline_structure and not signals:
            signals.append("起点＋当前的时序结构")
        active = shape in shapes
        if shape == "point" and active:
            signals.append("未命中更宽问题形态")
        if active:
            reason = (
                f"命中：{'、'.join(signals[:6])}" if signals
                else "由混合问题的结构信号命中"
            )
            if shape == primary and len(shapes) > 1:
                reason += "；按主路径优先级承担预算与覆盖检查"
        else:
            reason = "本次没有检测到该类关键词或结构信号"
        budget, max_tokens, max_queries = PROFILE_BY_SHAPE[shape]
        decisions.append({
            "shape": shape,
            "strategy": STRATEGY_BY_SHAPE[shape],
            "active": active,
            "primary": shape == primary,
            "signals": signals[:6],
            "reason": reason,
            "purpose": ROUTE_PURPOSE[shape],
            "profile": {
                "budget": budget,
                "max_tokens": max_tokens,
                "max_queries": max_queries,
            },
            "memory_types": TYPES_BY_SHAPE[shape],
            "coverage_dimensions": COVERAGE_BY_SHAPE[shape],
        })
    return decisions


def is_negative_current_check(query: str, shapes: list[str]) -> bool:
    """Detect a current-state request that also asks what no longer applies."""
    if "current" not in shapes:
        return False
    text = full_prompt_text(query).casefold()
    return any(
        marker in text
        for marker in (
            "是否还", "不再", "没有再", "不重复", "重复", "停用", "禁用",
            "取消", "废弃", "不走", "不经过", "no longer", "still use",
        )
    )


def is_contrastive_point_check(query: str, shapes: list[str]) -> bool:
    """Detect a one-fact question that explicitly contrasts two alternatives."""
    if shapes != ["point"]:
        return False
    text = full_prompt_text(query).casefold()
    return any(
        marker in text
        for marker in (
            "还是", "而非", "而不是", "不是", "区别", "差别", "相比",
            " versus ", " vs ", "rather than", "instead of", "difference between",
        )
    )


def build_queries(query: str, shapes: list[str], max_queries: int) -> list[str]:
    # ``query`` is the resolved Full Prompt on the production path.  Do not
    # reduce it to the last paragraph: that would leave the audit fields
    # correct while every actual Bank facet silently loses the context used to
    # disambiguate the user's short question.
    original = full_prompt_text(query)
    values = [original]
    # Reusable domain-ontology bridges improve recall of novel paraphrases.
    # They only generate candidates; the relevance gate below still compares
    # every candidate with the original user intent before injection.
    for bridge in semantic_query_expansions(original):
        values.append(f"{original}\n同义检索分面：{bridge}")
    # Round-robin across matched answer shapes. A mixed request such as
    # "all original statements and their dates" needs both inventory coverage
    # and provenance, not four paraphrases of only the first detected shape.
    suffix_groups = [EXPANSION_SUFFIXES.get(shape, ()) for shape in shapes]
    depth = max((len(group) for group in suffix_groups), default=0)
    for index in range(depth):
        for group in suffix_groups:
            if index < len(group):
                values.append(f"{original}\n记忆检索维度：{group[index]}")
    unique: list[str] = []
    for item in values:
        item = item.strip()
        if item and item not in unique:
            unique.append(item)
    return unique[:max_queries]


def evolution_anchor_terms(query: str) -> list[str]:
    """Extract explicit, user-written identifiers for an evolution question."""
    original = full_prompt_text(query)
    # These tokens are Full Prompt framing, not user subjects.  Letting them
    # consume the tiny graph/evolution anchor budget made a resolved prompt
    # seed ``Prior``/``context``/``Latest`` instead of the named product or
    # project.  They remain in the auditable Full Prompt; they are only
    # excluded from literal entity probes.
    ignored = {
        "from", "into", "with", "that", "this", "the", "and", "version",
        "prior", "context", "latest", "user", "message", "assistant",
        "current", "prompt", "request", "background", "system",
    }
    terms = [
        value for value in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", original)
        if value.casefold() not in ignored
    ]
    # Never split arbitrary Chinese prose into pseudo-entities. A quoted term
    # is an explicit user-written anchor and is safe to reuse for retrieval.
    terms.extend(re.findall(r"[《「『\"“]([^》」』\"”]{2,32})[》」』\"”]", original))
    return list(dict.fromkeys(str(value).strip() for value in terms if str(value).strip()))[:3]


def build_graph_seed_queries(query: str, limit: int = 3) -> list[str]:
    """Build graph probes from stable anchors before conversational fragments.

    The official graph endpoint is literal.  A natural-language prefix such as
    ``那你告诉我`` is therefore a poor seed and must never consume the small
    graph probe budget ahead of an explicit product, project, quoted term or
    approved alias written by the user.
    """
    original = full_prompt_text(query)
    compact = _entity_normalize(original)
    generic = {
        "那你告诉我", "最开始官方", "经历了大概多少次", "经历多少次",
        "明显变迁", "从最开始", "到现在", "什么", "为什么", "怎么",
        "所有", "全部", "当前", "历史", "记忆", "图谱", "实体", "问题",
        # Full Prompt framing fragments are not graph entities.  Without this
        # guard a sentence such as “状态页曾记录…当前需要核对” consumed the
        # third literal probe before the concrete task name reached Hindsight.
        "状态页", "状态页曾", "前文", "用户消息", "当前需要", "需要核",
        "月", "全部资料", "全部信息", "相关资料",
    }
    prioritized: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip()
        if not value or value in generic or value in prioritized:
            return
        prioritized.append(value)

    # Explicit ASCII/quoted anchors are unambiguous user-written identifiers.
    # Keep the historical priority when a question names several concrete
    # products/files (for example ``Codex``/``WPS``/``Cloud``).  A Chinese-only
    # relation question often has just one ASCII product (``Hindsight``),
    # though; in that case reserve the remaining probes for explicit noun
    # phrases such as ``同一项目``/``文件名``/``客户别名`` instead of letting
    # conversational fragments (``当同一项目``/``里使用简称``) consume them.
    explicit_anchors = evolution_anchor_terms(original)
    for value in explicit_anchors:
        add(value)
        if len(explicit_anchors) >= max(1, int(limit)) and len(prioritized) >= max(1, int(limit)):
            break

    # The official graph endpoint is literal.  Extract short, user-written
    # Chinese relation/entity phrases before the broad stop-word cleanup.  The
    # suffix list is semantic (entity/alias/file/scope terms), not a project
    # allow-list, so it generalizes to new projects and domains.
    chinese_anchor_patterns = (
        r"同一[\u4e00-\u9fff]{0,6}(?:项目|任务|主体|实体|系统)",
        # Keep the discriminative file/customer anchors ahead of the shorter
        # generic ``简称`` token; each is an exact lexical seed, never a
        # fragment such as ``录里使用简称``.
        r"客户别名|文件名|目录名|项目简称",
        r"实体归一化|范围边界|关系闭包|图谱|星座",
        r"简称",
    )
    chinese_anchors: list[str] = []
    for pattern in chinese_anchor_patterns:
        for value in re.findall(pattern, original):
            cleaned = str(value or "").strip(" ，。；：！？、()（）[]【】")
            if cleaned and cleaned not in chinese_anchors and _usable_entity_form(cleaned):
                chinese_anchors.append(cleaned)
    # If several explicit ASCII anchors already fill the probe budget, retain
    # that stable behavior.  Otherwise add the concrete Chinese phrases now.
    if len(prioritized) < max(1, int(limit)):
        for value in chinese_anchors:
            add(value)
            if len(prioritized) >= max(1, int(limit)):
                break
    # Approved aliases preserve canonical entity governance rather than treating
    # every similar string as a real entity.
    for canonical, forms in load_approved_entity_aliases().items():
        known_forms = [canonical, *forms]
        if any(_usable_entity_form(str(form)) and _entity_normalize(str(form)) in compact for form in known_forms):
            add(canonical)
    # Chinese names are lower-confidence than explicit identifiers, but can
    # still be useful after stop words and generic phrasing are removed.
    split_text = re.sub(
        r"(?:不要|用户|用户要|用户希望|用户要求|那你告诉我|告诉我|请|帮我|我想|我要|希望|要求|要|想|接管|交接|之前|之后|做过的|做的|做|说明|梳理|回顾|总结|核对|验证|查看|看看|查询|问|准确|大概|多少|明显|经历|发生|变迁|或者|以及|和|与|及|、|是|不是|是否|把|将|在|对|从|到|关于|为什么|如何|什么|关联|关系|联动|全称|别名|方案|记录|历史|当前|相关|完整|一起|之间|同时|并且|状态页|前文|用户消息|需要|曾|的|地|得|一下|年|月)",
        " ", original,
    )
    for value in re.findall(r"[\u4e00-\u9fff]{2,16}", split_text):
        if value not in generic and _usable_entity_form(value):
            add(value)
    # A literal full-query fallback is useful only when no concrete anchor was
    # extracted.  Once one or more anchors exist, sending the whole sentence
    # would spend a scarce graph probe on conversational framing (for example
    # “或者准确说有多少明显”) rather than on another endpoint.  This is a
    # shape-based rule, not a project/entity allow-list.
    if not prioritized:
        add(original)
    return prioritized[:max(1, int(limit))]


def build_evolution_queries(query: str, max_queries: int) -> list[str]:
    """Build a diverse origin-to-present plan without hard-coding a product."""
    original = full_prompt_text(query)
    anchor = " ".join(evolution_anchor_terms(original)) or original
    facets = (
        "阶段检索：最初、官方基础、早期定位与首次接入。",
        "阶段检索：升级、迁移、重构、引入、替代与关键转折；每条只保留可区分的阶段。",
        "阶段检索：实体图谱、观察、心智模型、召回治理、审计或可观测性等能力演进。",
        "阶段检索：当前正式架构、运行边界、仍未完成项与不能据此声称完成的验收条件。",
    )
    values = [original, *[f"{anchor}\n{facet}" for facet in facets]]
    return list(dict.fromkeys(values))[:max_queries]


def is_evolution_stage_candidate(query: str, text: str) -> bool:
    """Whether a record proves one stage of a named system's evolution."""
    anchors = [_entity_normalize(value) for value in evolution_anchor_terms(query)]
    normalized = _entity_normalize(text)
    subject = any(anchor and anchor in normalized for anchor in anchors)
    stage_terms = (
        "最初", "最开始", "一开始", "官方", "基础", "早期", "接入", "引入", "迁移", "重构",
        "升级", "替代", "切换", "改为", "扩展", "图谱", "实体", "观察", "心智模型", "审计",
        "回放", "adapter", "hook", "controller", "agent memory os", "当前", "正式", "未完成",
    )
    # A date or a phrase such as “改为” alone is not a product evolution: it
    # also describes cosmetic renames, screenshots and one-off content edits.
    # Require a system-capability/architecture fact in addition to the named
    # subject and a temporal transition.  This remains generic across agents
    # and products, while keeping “important changes” focused on what can
    # change recall, retention, state or operational behavior.
    structural_terms = (
        "系统", "架构", "机制", "记忆", "bank", "实体", "关系", "召回", "检索", "retain", "recall",
        "reflect", "observation", "mentalmodel", "模型", "hook", "adapter", "controller", "api", "mcp",
        "配置", "数据库", "迁移", "重构", "升级", "切换", "替代", "接入", "生产", "接口", "版本", "治理",
        "审计", "注入", "图谱", "工作流", "控制平面",
    )
    return bool(
        subject
        and any(term in normalized for term in stage_terms)
        and any(term in normalized for term in structural_terms)
    )


def named_mechanism_stage_question(query: str) -> bool:
    """Recognize a bounded question about one named system-change stage.

    This is intentionally narrower than an origin-to-present evolution
    question.  A user may ask only what a quoted/named component's *introduction
    stage* solved, what mechanisms it added, and how it was validated.  Those
    are real historical evidence needs even though the wording does not span
    from an origin all the way to the present.  Requiring an explicit anchor,
    a transition marker, and a mechanism/problem/verification relation keeps
    this from elevating generic architecture chatter.
    """
    text = _entity_normalize(full_prompt_text(query))
    if not evolution_anchor_terms(query):
        return False
    stage_markers = (
        "阶段", "引入", "接入", "迁移", "重构", "升级", "改造", "上线", "切换", "替代", "演进",
    )
    relation_markers = (
        "解决", "机制", "问题", "作用", "验证", "边界", "原因", "改进", "效果", "缺口",
    )
    return any(marker in text for marker in stage_markers) and any(marker in text for marker in relation_markers)


def mechanism_stage_evidence_alignment(query: str, text: str) -> dict[str, Any]:
    """Test whether one record proves a named mechanism-stage question.

    The evidence must name the same mechanism and say something independently
    useful about its operation, recall behaviour, guardrail, or validation. A
    bare co-mention of the component therefore still fails.  There is no
    result-count limit: every independently qualifying record is retained.
    """
    normalized = _entity_normalize(text)
    anchors = [_entity_normalize(value) for value in evolution_anchor_terms(query)]
    subject_hits = [anchor for anchor in anchors if anchor and anchor in normalized]
    mechanism_terms = (
        "召回", "检索", "注入", "读取", "编排", "路由", "覆盖", "缓存", "超时", "降级", "过滤",
        "校验", "验证", "审计", "hook", "adapter", "controller", "接口", "bank", "记忆", "上下文",
    )
    outcome_terms = (
        "避免", "解决", "确保", "防止", "一致", "完整", "通过", "成功", "失败", "边界", "透明",
        "回退", "熔断", "质量", "权威", "来源", "冲突", "当前", "正式", "运行",
    )
    mechanism_hits = [term for term in mechanism_terms if term in normalized]
    outcome_hits = [term for term in outcome_terms if term in normalized]
    return {
        "qualified": bool(subject_hits and mechanism_hits and outcome_hits),
        "subject_hits": subject_hits,
        "mechanism_hits": mechanism_hits,
        "outcome_hits": outcome_hits,
    }


def evolution_evidence_alignment(query: str, text: str) -> dict[str, Any]:
    """Judge whether one record proves an evolution stage, not just the topic.

    An origin-to-present request legitimately admits many records, but it does
    not make every record that mentions the system relevant.  The record must
    independently establish an origin/foundation, a concrete change, or the
    present architecture.  This is a semantic boundary, not an item limit.
    """
    normalized = _entity_normalize(text)
    anchors = [_entity_normalize(value) for value in evolution_anchor_terms(query)]
    subject_hits = [anchor for anchor in anchors if anchor and anchor in normalized]
    origin_terms = (
        "最初", "最开始", "一开始", "起初", "初版", "官方", "基础", "早期", "原生", "主架构",
    )
    change_terms = (
        "接入", "引入", "迁移", "重构", "升级", "替代", "切换", "改为", "扩展", "整合",
        "适配", "分层", "增加", "新增", "改动", "变更", "优化", "修复", "上线", "停用",
    )
    current_terms = ("当前", "现在", "目前", "正式", "生产", "已启用", "已生效", "运行")
    structural_terms = (
        "系统", "架构", "机制", "记忆", "bank", "实体", "关系", "召回", "检索", "retain", "recall",
        "reflect", "observation", "mentalmodel", "模型", "hook", "adapter", "controller", "api", "mcp",
        "配置", "数据库", "治理", "审计", "注入", "图谱", "工作流", "控制平面", "agentmemory",
    )
    origin_hits = [term for term in origin_terms if term in normalized]
    change_hits = [term for term in change_terms if term in normalized]
    # “supports multiple integration methods” describes a capability catalog,
    # not a dated integration event.  Keep an actual Hook/agent integration
    # eligible, but do not let this lexical overlap fill an evolution slot.
    if "接入方式" in normalized and "接入" in change_hits:
        change_hits.remove("接入")
    current_hits = [term for term in current_terms if term in normalized]
    structural_hits = [term for term in structural_terms if term in normalized]
    # A retrospective timeline needs evidence that a transition actually
    # happened.  The bank also contains many valuable *requirements* and
    # concerns ("用户要求…", "用户担忧…"), but those are not independently
    # verifiable system stages unless the same record says it was completed,
    # enabled or otherwise realized.  Keeping them in the result set used to
    # make an open evolution answer look comprehensive while spending most of
    # its context on plans that never became production changes.
    planning_prefixes = (
        "用户要求", "用户希望", "用户提出", "用户指出", "用户认为", "用户担忧", "用户询问", "用户偏好", "用户决定", "用户设定",
        "助手计划", "助理计划", "建议", "拟", "准备",
    )
    realization_terms = (
        "已完成", "完成", "已实现", "实现", "已上线", "上线", "已启用", "启用",
        "已生效", "生效", "正式启用", "正式切换", "生产切换", "生产运行", "已切换", "切换成功", "部署完成",
        "验证通过", "测试通过", "落地", "运行中",
    )
    is_requested_only = any(normalized.startswith(prefix) for prefix in planning_prefixes)
    # Do not mistake “未实现 / 未完成 / 未生效” for a positive delivery event.
    # Normalization removes spaces, so checking the immediate negative prefix
    # is both language-robust and deterministic.
    realization_hits = [
        term for term in realization_terms
        if term in normalized
        and not any(f"未{term}" in normalized or f"没有{term}" in normalized or f"尚未{term}" in normalized for _ in (0,))
    ]
    # A Bank also contains answers *about* Hindsight.  They help a live
    # conversation, but are not primary evidence for an evolution timeline:
    # otherwise a prior assistant explanation, recommendation or user
    # question recursively becomes a historical change.  Keep such a record
    # only when it itself records a completed/active change.
    narrative_prefixes = (
        "助手解释", "助理解释", "助手建议", "助理建议", "助手澄清", "助理澄清",
        "示例用户询问", "用户询问", "用户要求", "用户希望", "用户提出", "用户指出",
        "用户认为", "用户担忧", "用户偏好", "用户决定", "用户设定", "回答用户",
        "对用户当前场景的判断", "当前问题",
    )
    is_narrative_only = any(normalized.startswith(prefix) for prefix in narrative_prefixes)
    evidence_state = (
        "requested_not_realized"
        if (is_requested_only or is_narrative_only) and not realization_hits
        else "realized_change"
    )
    qualified = bool(
        subject_hits and structural_hits and (origin_hits or change_hits or current_hits)
        and evidence_state != "requested_not_realized"
    )
    return {
        "qualified": qualified,
        "evidence_state": evidence_state,
        "subject_hits": subject_hits,
        "origin_hits": origin_hits,
        "change_hits": change_hits,
        "current_hits": current_hits,
        "realization_hits": realization_hits,
        "structural_hits": structural_hits,
    }


def attachment_requires_source_first(query: str) -> bool:
    """Whether an attached/open source must suppress historic-memory recall.

    An attachment is authoritative for assertions *about its current content*.
    It is not, by itself, an authority for a question that diagnoses the memory
    system or an execution trace.  In the latter case the screenshot is useful
    evidence, but the answer necessarily depends on controller receipts, prior
    traces and (when relevant) durable Hindsight memory.  Treating every
    attachment as a source-first boundary caused those diagnostic turns to show
    a false-looking ``0 recalled / 0 injected`` outcome.
    """
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    # A terse status follow-up is about the immediately visible outcome, not a
    # claim that an attachment is the only authority.  Let the working-set gate
    # retain it as a continuation; otherwise the UI lies that it intentionally
    # skipped Hindsight because of a file it has not actually inspected.
    status_followup_markers = (
        "刷新了还是这样", "刷新后还是这样", "还是这样", "依旧这样", "仍然这样", "没变化", "没有变化",
        # A screenshot-qualified “this injection is zero” is an execution
        # audit, not a request whose answer can come from the attachment alone.
        # Let the Full Prompt and historical receipts participate before any
        # candidate is rejected.
        "这个注入是0", "这个注入为0", "实际注入是0", "实际注入为0", "0条注入",
    )
    if any(marker in text for marker in status_followup_markers):
        return False
    test_mismatch_markers = ("测试", "回归", "实际输入", "真实输入")
    comparison_markers = ("区别", "不一样", "成功", "失败", "真实", "实际")
    if any(marker in text for marker in test_mismatch_markers) and any(marker in text for marker in comparison_markers):
        return False
    diagnostic_markers = (
        "为什么", "原因", "不全", "缺失", "异常", "问题", "排查", "检查", "审计", "修复", "验证",
    )
    mechanism_markers = (
        "实际链路", "链路", "状态页", "召回", "检索", "注入", "回执", "controller", "hindsight", "hook", "记忆机制",
        "回归", "测试", "实际输入", "真实输入", "测试输入", "测试和我",
    )
    is_mechanism_diagnosis = (
        any(marker in text for marker in diagnostic_markers)
        and any(marker in text for marker in mechanism_markers)
    )
    return not is_mechanism_diagnosis


def build_plan(
    query: str,
    role: str,
    policy: dict[str, Any],
    controller_config: dict[str, Any],
    *,
    client: str = "unknown",
    bank_id: str = "",
    recall_profile: str = "",
    runtime_context: dict[str, Any] | None = None,
    semantic_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_context = dict(runtime_context or {})
    # The agent-provided Full Prompt is the semantic retrieval input.  The raw
    # turn remains only for the local working-set signal, so a terse follow-up
    # does not force Hindsight to infer meaning from a fixed transcript window.
    # The request query is the resolved Full Prompt.  The Hook may provide the
    # extracted user wording separately so the audit/UI can distinguish the
    # two without weakening semantic retrieval.  Legacy callers omit it and
    # retain the previous query-as-raw behavior.
    raw_user_prompt = str(runtime_context.get("raw_user_prompt") or query or "")
    contextual_intent = dict(runtime_context.get("contextual_intent") or {})
    full_prompt = full_prompt_text(str(runtime_context.get("full_prompt") or ""))
    full_prompt_source = str(runtime_context.get("full_prompt_source") or "").strip()
    if not full_prompt and contextual_intent.get('used_context') and contextual_intent.get('full_prompt'):
        full_prompt=full_prompt_text(str(contextual_intent['full_prompt']))
        full_prompt_source='contextual_intent_fallback'
    if full_prompt:
        query = full_prompt
    else:
        full_prompt_source = "fallback_context_envelope"
    semantic_query_text = full_prompt or latest_query(query)
    working_set = working_set_decision(raw_user_prompt)
    # The context resolver has already proved that a short continuation refers
    # to the active task.  The raw phrase (for example "继续优化") contains no
    # artifact nouns, so the lexical working-set detector would otherwise send
    # a broad shared-Bank probe and manufacture a misleading zero-injection
    # result. Preserve the user's current task scope while keeping explicit
    # historical/provenance requests on the normal recall path.
    if (contextual_intent.get("used_context")
            and contextual_intent.get("intent_mode") == "contextual_followup"
            and not any(marker in raw_user_prompt for marker in ("历史", "以前", "当时", "原话", "来源", "跨任务", "跨会话"))):
        working_set = dict(working_set)
        working_set.update({
            "sufficient": True,
            "decision": "use_current_working_set",
            "reason": "Full Prompt 已由当前任务上下文解析为续办；直接沿用当前任务对象，避免对共享 Bank 发起无主题探测。",
            "context_resolved_continuation": True,
        })
    direct_policy_anchors = matched_direct_policy_anchors(query)
    # A resolved continuation may contain policy words copied from the active
    # task (for example "真实回执" inside the previous audit). Those words are
    # context evidence, not a new request to retrieve the policy from the
    # shared Bank. Keep the current-task decision authoritative unless the raw
    # user turn explicitly asks for historical/source evidence.
    if (working_set.get("context_resolved_continuation")
            and not any(marker in raw_user_prompt for marker in ("历史", "以前", "当时", "原话", "来源", "跨任务", "跨会话"))):
        direct_policy_anchors = []
    if working_set["sufficient"] and direct_policy_anchors:
        working_set = dict(working_set)
        working_set.update({
            "sufficient": False,
            "decision": "augment_current_working_set_with_direct_policy",
            "reason": "当前任务本身可续办，但命中用户已审核的跨任务直接政策锚点；补入相关政策，不做全库泛搜。",
            "direct_policy_anchor_hits": direct_policy_anchors,
        })
    if working_set["sufficient"]:
        working_set = dict(working_set)
        working_set.update({
            "decision": ("use_current_working_set" if working_set.get("context_resolved_continuation") else "augment_current_working_set"),
            "reason": ("Full Prompt 已由当前任务上下文解析为续办；沿用当前任务对象，不对共享 Bank 发起无主题探测。"
                       if working_set.get("context_resolved_continuation") else
                       "当前任务上下文可用于消解指代，但不能替代长期记忆；本轮仍执行有界混合检索并只补充缺口。"),
        })
    # Every Full Prompt receives a bounded Bank probe.  A rich current context
    # changes the selection target; it is never a zero-recall shortcut.
    memory_action = "focused_recall"
    # Weak words such as “现在” and “哪些” describe many ordinary editing
    # instructions.  Once the working-set gate has established that the user is
    # continuing the visible task, they must not independently open a broad
    # memory route.
    shapes = detect_shapes(query)
    # Keep conceptual graph/constellation questions out of the narrow
    # concrete-delivery audit lane.  Full Prompts commonly mention “注入、
    # 来源、时间、冲突” while asking how to build the association closure;
    # treating the word “为什么” as a delivery audit makes the status page
    # show only 来源核验/新旧冲突 and hides the requested graph route.  A
    # concrete “实际注入为 0 / 回执缺失” question remains an audit.  This is
    # deliberately query-shape based, not a Hindsight/project allow-list.
    association_closure_request = is_association_closure_query(semantic_query_text)
    association_delivery_markers = (
        "实际注入", "注入为0", "注入是0", "候选很多但实际", "投递回执", "注入回执",
        "状态页", "未投递", "未注入", "对账", "正常吗", "链路断", "链路缺",
    )
    association_query_compact = re.sub(r"\s+", "", semantic_query_text).casefold()
    association_mixed_delivery_audit = any(
        marker.casefold() in association_query_compact for marker in association_delivery_markers
    )
    conceptual_association_query = bool(
        association_closure_request and not association_mixed_delivery_audit
    )
    # A bounded context-intent envelope may resolve a reference such as
    # “刚才这条是否合格”.  It cannot alter the user's literal request or grant
    # any access; it only adds/removes retrieval *shapes* with an auditable
    # reason.  This prevents a processing-order phrase like “最开始做语义
    # 分析” from being mistaken for a request to reconstruct a timeline.
    if bool(contextual_intent.get("used_context")):
        hints = dict(contextual_intent.get("routing_hints") or {})
        suppress = {str(shape) for shape in hints.get("suppress_shapes") or [] if str(shape) in VALID_QUERY_SHAPES}
        force = [str(shape) for shape in hints.get("force_shapes") or [] if str(shape) in VALID_QUERY_SHAPES]
        if conceptual_association_query:
            # Context resolver and Controller must agree even when an older
            # Hook sends an envelope with ``force_shapes=["audit"]``.
            suppress.add("audit")
            force = [shape for shape in force if shape != "audit"]
            contextual_intent = dict(contextual_intent)
            contextual_intent["intent_mode"] = "association_closure"
            contextual_intent["routing_hints"] = {
                **hints,
                "force_shapes": list(dict.fromkeys([*force, "system_map", "synthesis"])),
                "suppress_shapes": sorted(suppress),
            }
            contextual_intent["association_closure"] = {
                "requested": True,
                "conceptual": True,
                "mixed_delivery_audit": False,
            }
        shapes = [shape for shape in shapes if shape not in suppress]
        shapes = list(dict.fromkeys([*shapes, *force])) or ["point"]
    accepted_semantic_plan = normalize_semantic_plan(semantic_plan)
    semantic_plan_review = {
        "received": bool(semantic_plan),
        "accepted": None,
        "shape": (str(semantic_plan.get("shape") or "").strip().casefold() if isinstance(semantic_plan, dict) else ""),
        "reason": "not_provided",
    }
    if accepted_semantic_plan:
        if not semantic_plan_shape_supported(
            query, accepted_semantic_plan["shape"], shapes
        ):
            # A provider is allowed to explain a paraphrase, not to introduce
            # an expensive route whose required slots are absent from the
            # user's structural request.  This is the guard that prevents a
            # generic continuation-policy question from becoming a
            # system-map/alias audit and timing out the Hook.
            semantic_plan_review = {
                "received": True,
                "accepted": False,
                "shape": accepted_semantic_plan["shape"],
                "reason": "unsupported_shape_without_structural_signal",
            }
            accepted_semantic_plan = None
        else:
            semantic_plan_review = {
                "received": True,
                "accepted": True,
                "shape": accepted_semantic_plan["shape"],
                "reason": "shape_supported_by_current_prompt_structure",
            }
    if accepted_semantic_plan:
        # An agent or the bounded Qwen planner may choose the retrieval shape,
        # but it cannot grant itself raw access, suppress a required recall, or
        # erase explicitly detected audit/conflict semantics.
        preserved = [shape for shape in shapes if shape in {"audit", "conflict", "current"}]
        if evolution_question(query) and "timeline" not in preserved:
            preserved.append("timeline")
        shapes = list(dict.fromkeys([accepted_semantic_plan["shape"], *preserved]))
    if conceptual_association_query:
        # A provider plan is advisory.  It cannot reintroduce the misleading
        # audit veto for a conceptual association question, and it must leave
        # at least one structural association route in the foreground.
        shapes = [shape for shape in shapes if shape != "audit"]
        if "system_map" not in shapes:
            shapes.append("system_map")
        if "synthesis" not in shapes:
            shapes.append("synthesis")
    # A semantic planner may conservatively return ``inventory`` for a named
    # role/alias question.  The deterministic taxonomy guard remains the
    # authority for this structural boundary and must survive that review.
    if named_system_taxonomy_question(query):
        shapes = [shape for shape in shapes if shape != "inventory"]
        if "system_map" not in shapes:
            shapes.insert(0, "system_map")
    # “相关/全部/联动/遗漏” asks for a connected evidence set, not merely a
    # higher item limit.  Add a synthesis facet and record an explicit closure
    # receipt so unrelated neighbours cannot masquerade as coverage.
    # A relationship request must open the official graph lane, not merely add
    # more semantic paraphrases.  These are intentionally concrete requests
    # for identity, handoff, dependency or closure; broad words such as
    # “相关” alone do *not* activate it, because they would turn ordinary
    # questions into noisy graph-neighbour recalls.
    closure_markers = (
        "关联闭包", "关联性", "相关联", "所有关联", "全部关联", "联动", "不要遗漏", "前后都", "相关页面",
        "实体关系", "实体别名", "别名", "全称", "是不是同一个", "是否同一个", "前后步骤", "上下游", "因果链", "派生链", "证据边界", "关系", "关联", "依赖关系", "依赖链", "相互依赖",
        "接管", "迁移任务", "回滚约束",
    )
    # An evolution question is itself a relationship question: the answer must
    # connect origin, transitions, replacements and present state.  Requiring
    # graph discovery here avoids a shallow pile of same-era progress logs.
    relation_closure_required = (
        any(marker in semantic_query_text for marker in closure_markers) or evolution_question(semantic_query_text)
    )
    if relation_closure_required and "synthesis" not in shapes and "system_map" not in shapes:
        shapes.append("synthesis")
    normalized_profile = str(recall_profile or "").strip().casefold()
    # An origin-to-present question is intrinsically deep even if the user did
    # not type the literal words “深度召回”.  Returning only the first two
    # stage facets and deferring the rest to a background continuation made the
    # current answer weaker than a manual Bank audit.  It now receives the same
    # foreground deadline and completion rule as an explicit deep request.
    explicit_deep_recall = (
        normalized_profile == "deep"
        or explicit_deep_recall_requested(query, shapes)
        or evolution_question(query)
    )
    # ``procedure`` normally stays cheap, but a request that names discovery,
    # diagnosis, repair, regression and rollback is not a one-step how-to. It
    # needs the wider official retrieval lane to recover distributed evidence
    # (including records that only appear under the high Bank budget).
    procedure_depth = comprehensive_procedure_requested(query, shapes)
    if explicit_deep_recall:
        memory_action = "deep_recall"
    elif working_set.get("sufficient") and contextual_intent.get("intent_mode") == "contextual_followup":
        memory_action = "noop_long_term"
    dimensions = analyze_query_dimensions(query, shapes)
    primary = (
        "system_map"
        if conceptual_association_query and "system_map" in shapes
        else next((shape for shape in PRIMARY_ORDER if shape in shapes), "point")
    )
    budget, max_tokens, _ = PROFILE_BY_SHAPE[primary]
    max_queries = max(PROFILE_BY_SHAPE[shape][2] for shape in shapes)
    negative_current = is_negative_current_check(query, shapes)
    contrastive_point = is_contrastive_point_check(query, shapes)
    if contrastive_point:
        # Preserve one-call latency while allowing enough returned evidence to
        # keep both contrasted alternatives and their direct differentiator.
        budget = "mid"
        max_tokens = max(max_tokens, 1100)
    if negative_current:
        # Establishing both the active path and the retired/forbidden path is a
        # version-resolution task even when phrased as a current-state question.
        # Use the conflict-grade depth, while keeping the three facets parallel.
        max_queries = max(max_queries, 3)
        budget = "high"
        max_tokens = max(max_tokens, 1800)
    if dimensions["continuity"]:
        # Restore state from the deterministic handoff first, then use
        # Hindsight for durable background and conflict checks.
        max_queries = max(max_queries, 3)
        budget = "mid"
        max_tokens = max(max_tokens, 1800)
    if explicit_deep_recall:
        # Explicit deep recall may legitimately need a long evidence chain.
        # The outer hook can return immediately with a continuation, so do not
        # turn an item-count cap into an artificial ceiling here.
        budget = "high"
        max_queries = max(max_queries, 4)
        max_tokens = max(max_tokens, 4000)
    if procedure_depth:
        budget = "high"
        max_queries = max(max_queries, 5)
        max_tokens = max(max_tokens, 2800)
    if evolution_question(query):
        # Origin-to-present is a coverage problem, not a terse point lookup.
        # Reserve independent stage facets even when the user did not say
        # "深度检索" explicitly; final admission remains relevance- and
        # token-budget-governed, so ordinary questions do not become noisy.
        budget = "high"
        max_queries = max(max_queries, 5)
        max_tokens = max(max_tokens, 4000)
    # Ontology bridges are candidate-generation evidence, not optional
    # decoration.  A point query normally has one facet, so without reserving
    # this second slot the bridge was constructed and then immediately sliced
    # away.  That made “一个单词是什么意思，是否值得长期记忆” miss the
    # explicitly stored write-boundary rule even though direct Bank recall
    # could find it.
    if semantic_query_expansions(query):
        max_queries = max(max_queries, 2)
    max_tokens = max(max_tokens, *(PROFILE_BY_SHAPE[shape][1] for shape in shapes))
    if negative_current:
        max_tokens = max(max_tokens, 1800)
    # An operational audit explicitly compares logs/scripts/chain evidence,
    # the Bank candidate set and the packet that crossed the Hook boundary.
    # The ordinary audit profile (2,400 tokens) is enough for a single status
    # explanation but silently truncates the independent candidate set for an
    # open-set comparison.  Raise only this structural workload to the normal
    # per-query ceiling and reserve five bounded facets; this is not a fixed
    # item-count grant and does not affect ordinary questions.
    operational_audit = operational_audit_query(semantic_query_text)
    if operational_audit:
        budget = "high"
        max_queries = max(max_queries, 5)
        max_tokens = max(max_tokens, 4096)
    role_policy = (policy.get("roles") or {}).get(
        role, (policy.get("roles") or {}).get("default", {})
    )
    allowed = list(role_policy.get("allowedLevels") or ["core"])
    levels = list(role_policy.get("initialLevels") or ["core"])
    # Raw user archives are a provenance tool, not a generic recall booster.
    # A system audit that mentions “evidence / recall / injection” must not
    # silently open Doubao, Qianwen or Feishu originals.
    evidence_required = explicit_user_source_request(semantic_query_text)
    temporal_anchor = explicit_temporal_anchor(query)
    literal_anchor = direct_evidence_anchor(query) if evidence_required else None
    if primary in {"inventory", "timeline", "conflict", "missing"}:
        for level in ("facts", "events"):
            if level in allowed and level not in levels:
                levels.append(level)
    if evidence_required and "raw" in allowed and "raw" not in levels:
        levels.append("raw")

    shared_bank = policy.get("sharedBank")
    role_bank = role_policy.get("roleBank")
    evidence_bank = policy.get("evidenceBank")
    source_evidence_banks, source_evidence_bank_reason = select_direct_evidence_banks(
        query,
        str(evidence_bank) if evidence_bank else None,
        list(policy.get("directEvidenceSourceBanks") or []),
    )
    banks = [value for value in (shared_bank, role_bank) if value]
    if "raw" in levels and evidence_bank:
        banks.append(evidence_bank)

    raw_queries = (
        build_evolution_queries(query, max_queries)
        if evolution_question(query)
        else build_queries(query, shapes, max_queries)
    )
    if dimensions["continuity"]:
        # A handoff is a coverage request, not a generic point lookup. The
        # previous implementation appended these lanes *after* ``max_queries``
        # had already been reached, then sliced them away; the Controller
        # appeared to plan a handoff while every upstream call still used the
        # first semantic facet. Build the bounded lanes explicitly so the
        # concrete artifact and its lifecycle both reach the Bank.
        handoff_scope = continuity_task_alignment(semantic_query_text, "")
        artifact_terms = list(handoff_scope.get("requested_artifact_terms") or [])
        artifact_label = "、".join(artifact_terms) if artifact_terms else "具体工件"
        # A semantic sentence can still rank a concrete handoff record below
        # later diagnostic summaries.  Add one deterministic lexical lane
        # built from the named subject/artifact and lifecycle slots.  It is
        # deliberately derived from the request (no project ID or fixed
        # record allow-list), so paraphrases generalize while the official
        # Bank can return the exact migration record when it exists.
        anchor_terms = [
            value for value in evolution_anchor_terms(semantic_query_text)
            if value.casefold() not in {"hindsight", "agentmemory", "controller", "hook", "mcp"}
        ]
        if "codex" in semantic_query_text.casefold() and "codex" not in {v.casefold() for v in anchor_terms}:
            anchor_terms.insert(0, "Codex")
        lifecycle_terms = [
            "交接", "已完成工作", "当前进度", "未完成事项", "验证", "回退条件"
        ]
        handoff_anchor = " ".join(dict.fromkeys([*anchor_terms, *artifact_terms, *lifecycle_terms]))
        continuity_facets = (
            semantic_query_text + f"\n记忆检索维度：只找{artifact_label}交接的实际迁移记录、来源任务、已完成项和未完成项。",
            semantic_query_text + "\n记忆检索维度：只找同一交接任务的验证结果、当前状态、回退办法和失败/修复证据；排除其他服务迁移。",
            handoff_anchor + "\n记忆检索维度：恢复同一交接任务的实际记录、当前状态、未完成事项、验证证据和回退条件；排除其他服务的迁移。",
        )
        max_queries = max(max_queries, 4)
        raw_queries = list(dict.fromkeys([semantic_query_text, *continuity_facets]))[:max_queries]
    queries: list[str] = []
    query_metrics: list[dict[str, Any]] = []
    for item in raw_queries:
        bounded, metric = compact_query(
            item,
            int(controller_config.get("maxQueryTokens", SAFE_QUERY_TOKEN_LIMIT)),
        )
        queries.append(bounded)
        query_metrics.append(metric)
    cross_domain = any(
        marker in semantic_query_text
        for marker in ("跨领域", "综合我", "结合我", "联系我的", "各方面", "整体")
    )
    prefer_observations = (
        any(shape in {"system_map", "inventory", "synthesis", "procedure"} for shape in shapes)
        and not any(shape in {"current", "audit", "conflict"} for shape in shapes)
    )
    # A provenance/current-state cue normally suppresses broad mental-model
    # chapters so they cannot compete with the authoritative fact.  An
    # explicit question about the observation/model layer is different: the
    # user is asking for the admission/quality framework itself, so the
    # sidecar must remain available even when that question also mentions
    # current facts, evidence, or original wording.  The sidecar is still
    # subject to freshness, section, relevance, and budget gates below.
    explicit_guidance_request = any(
        marker in semantic_query_text for marker in ("观察", "心智模型")
    )
    mental_policy = (
        "suppress" if any(shape in {"current", "audit", "conflict"} for shape in shapes) and not explicit_guidance_request
        else "allow" if any(shape in {"system_map", "inventory", "timeline", "synthesis", "missing"} for shape in shapes) or explicit_guidance_request
        else "routed_only"
    )
    # Cross-task handoff is an evidence-restoration request. Stable guidance
    # can explain a decision later, but must not occupy the factual handoff
    # context that needs task state, artifacts and rollback facts.
    if dimensions["continuity"]:
        mental_policy = "suppress"
    # Guidance is a low-cost, broad candidate probe for every Full Prompt.
    # Candidate presence does not mean injection: card-level applicability is
    # still checked after factual coverage and temporal adjudication.
    stable_guidance_enabled = True
    guidance_sidecar = {
        "enabled": stable_guidance_enabled,
        "observation": stable_guidance_enabled,
        # A concrete handoff needs task facts first.  Do not let the generic
        # memory/cognition models occupy its Packet merely because they share
        # words such as “Hindsight” or “迁移”; ordinary and broad synthesis
        # queries still receive the independent mental-model sidecar.
        "mental_model": stable_guidance_enabled and mental_policy != "suppress",
        "reason": (
            "本轮是跨任务接管；先恢复同一任务的状态、工件和回退证据，稳定观察/心智模型不能替代这些事实。"
            if dimensions["continuity"] else
            "当前事实仍以本轮权威来源为准；仅补入能改变解释、质量标准或决策边界的稳定观察与心智模型。"
            if stable_guidance_needed(query, shapes) else
            "本轮是局部执行或当前工作集追问，稳定观察/心智模型不能改变答案，不重复注入。"
        ),
    }
    types = []
    for shape in shapes:
        for memory_type in TYPES_BY_SHAPE[shape]:
            if memory_type not in types:
                types.append(memory_type)
    coverage_dimensions = []
    for shape in shapes:
        for dimension in COVERAGE_BY_SHAPE[shape]:
            if dimension not in coverage_dimensions:
                coverage_dimensions.append(dimension)
    if "current" in shapes and not negative_current:
        # A plain “what is current?” request only needs the latest valid state.
        # Supersession becomes mandatory only when the user contrasts an old
        # route or explicitly asks whether it is still used.
        coverage_dimensions = [
            value for value in coverage_dimensions if value != "supersession"
        ]
    if dimensions["continuity"]:
        for dimension in ("latest_state", "source_thread", "open_items"):
            if dimension not in coverage_dimensions:
                coverage_dimensions.append(dimension)
    if relation_closure_required and not evolution_question(query):
        # A named system map already declares its scope through the explicit
        # list of systems/components in the Full Prompt.  Adding the generic
        # inventory ``scope`` slot here made the packet claim that a taxonomy
        # answer was incomplete even when every named system, role and
        # boundary had been covered.  Keep graph closure dimensions for the
        # map (entities/decisions are still useful relation evidence), but do
        # not require an open-set scope receipt that this workload never
        # asked for.  Other relation/synthesis workloads retain the normal
        # scope check.
        relation_dimensions = (
            ("related_entities", "related_decisions")
            if "system_map" in shapes
            else ("related_entities", "related_decisions", "scope")
        )
        for dimension in relation_dimensions:
            if dimension not in coverage_dimensions:
                coverage_dimensions.append(dimension)
    # Origin-to-present and named mechanism-stage questions need the same
    # explicit evidence slots as a handoff.  Otherwise the static stage
    # queries can return a plausible timeline while silently omitting version
    # transitions or the decisions that caused them.  These slots are derived
    # from the query shape, not from a product-specific record list.
    if evolution_question(query) or named_mechanism_stage_question(query):
        for dimension in ("version", "counter_evidence", "related_decisions", "related_entities", "scope"):
            if dimension not in coverage_dimensions:
                coverage_dimensions.append(dimension)
    # A bounded system-map question may also contain causal words such as
    # “因果链/出问题”.  ``detect_shapes`` quite correctly keeps the synthesis
    # lane for the failure explanation, but synthesis's generic open-set
    # ``scope`` slot is not an additional requirement when the user already
    # enumerated the components.  The map's ``boundaries`` dimension carries
    # the applicable/excluded scope; retaining both made an otherwise complete
    # named-chain answer report ``coverage_complete=false`` and trigger an
    # unnecessary second recall.  Do not apply this relaxation to inventory or
    # evolution workloads, where an explicit open scope is part of the ask.
    if (
        "system_map" in shapes
        and "inventory" not in shapes
        and not evolution_question(query)
        and not named_mechanism_stage_question(query)
    ):
        coverage_dimensions = [value for value in coverage_dimensions if value != "scope"]
    # A handoff/evolution Full Prompt can legitimately require several
    # independent evidence facets.  The original three continuity lanes cover
    # state and rollback, but they do not necessarily retrieve version
    # transitions, counter-evidence, or the decisions affected by the handoff.
    # Open a bounded, dimension-driven lane for any of those *declared*
    # coverage slots instead of assuming that a broad result set implies
    # completeness.  The lane is derived from the plan's required dimensions,
    # not from a fixed record allow-list, so it generalizes to other projects.
    coverage_facet_hints = {
        "system_definitions": "只找本题点名的每个系统/组件定义、职责、输入输出和正式定位，不扩展到未点名产品。",
        "role_relations": "只找点名系统之间的入口、接入层、中间编排层、记忆底座、Packet/回执等上下游关系。",
        "alias_relations": "只找点名名称的别名、简称、全称和哪些名称不是同一产品/组件的证据。",
        "boundaries": "只找这些名称不能混为一谈的职责边界、替代/非替代关系、当前有效范围和排除项。",
        "version": "只找版本、阶段、变更顺序、已替代/停用状态及其时间证据。",
        "counter_evidence": "只找反例、失败、冲突、不适用边界和回退证据；不要把规划中的要求当成已完成事实。",
        "related_decisions": "只找该任务关联的决定、影响对象、前后依赖与为何采用/放弃某方案的证据。",
        "related_entities": "只找同一任务涉及的实体、别名、上下游对象及图谱关系证据。",
        "scope": "只找适用范围、前置条件、排除范围和不应套用的相邻任务。",
    }
    coverage_route_required = bool(
        dimensions["continuity"]
        or negative_current
        or evolution_question(query)
        or any(shape in {"system_map", "inventory", "timeline", "audit", "missing", "conflict", "synthesis"} for shape in shapes)
    )
    if coverage_route_required:
        facet_order = (
            ("version", "counter_evidence", "related_decisions", "related_entities", "scope",
             "system_definitions", "role_relations", "alias_relations", "boundaries")
            if dimensions["continuity"] else
            ("system_definitions", "role_relations", "alias_relations", "boundaries", "version",
             "counter_evidence", "related_decisions", "related_entities", "scope")
        )
        coverage_facets = [
            semantic_query_text + "\n记忆检索维度：" + coverage_facet_hints[dimension]
            for dimension in facet_order
            if dimension in coverage_dimensions and coverage_facet_hints.get(dimension)
        ]
        if coverage_facets:
            # Keep three foreground lanes for latency; the remaining lanes
            # are explicit, auditable escalation work.  A coverage request
            # may use more than the ordinary point-query budget, but never an
            # unbounded fan-out.
            max_queries = max(max_queries, min(8, len(raw_queries) + len(coverage_facets)))
            raw_queries = list(dict.fromkeys([*raw_queries, *coverage_facets]))[:max_queries]
            queries = []
            query_metrics = []
            for item in raw_queries:
                bounded, metric = compact_query(
                    item,
                    int(controller_config.get("maxQueryTokens", SAFE_QUERY_TOKEN_LIMIT)),
                )
                queries.append(bounded)
                query_metrics.append(metric)
    initial_query_count = 1
    if relation_closure_required:
        max_queries = max(max_queries, 3)
        max_tokens = max(max_tokens, 2200)
        closure_facet = semantic_query_text + "\n记忆检索维度：关联闭包；找出同一实体、同一决定、前后步骤与受影响对象，保留关系证据和不适用边界。"
        if closure_facet not in raw_queries:
            raw_queries.append(closure_facet)
        raw_queries = raw_queries[:max_queries]
        queries=[]; query_metrics=[]
        for item in raw_queries:
            bounded, metric = compact_query(item, int(controller_config.get("maxQueryTokens", SAFE_QUERY_TOKEN_LIMIT)))
            queries.append(bounded); query_metrics.append(metric)
    if dimensions["continuity"]:
        # State restoration needs the task anchor plus its explicit lifecycle
        # facets (completed/current, pending, rollback).  Running only two of
        # those lanes foreground made the Hook return one good record while
        # the equally relevant handoff evidence arrived later in background.
        # This is a coverage dimension, not an item-count increase.
        initial_query_count = min(3, len(queries))
    elif dimensions["breadth"] in {"open_set", "cross_domain"} or "system_map" in shapes or relation_closure_required or procedure_depth:
        initial_query_count = min(2, len(queries))
    # Ontology bridges are specifically for novel paraphrases.  Execute the
    # original and its first bridge together; treating the bridge as optional
    # escalation lets a superficially plausible first hit incorrectly declare
    # coverage complete and suppress the actually relevant vocabulary.
    if semantic_query_expansions(query) and not dimensions["continuity"]:
        initial_query_count = min(2, len(queries))
    initial_queries = queries[:initial_query_count]
    escalation_queries = queries[initial_query_count:]
    coverage_required = bool(
        negative_current
        or any(
            shape in {"system_map", "inventory", "timeline", "audit", "missing", "conflict", "synthesis"}
            for shape in shapes
        )
        or procedure_depth
    )
    # A coverage-bearing plan with several facets is semantically a deep read
    # even when the user did not type the literal word "深度".  The old
    # deadline selector only used ``explicit_deep_recall``; long audit/
    # conflict/inventory prompts therefore received the 18s complex budget,
    # timed out during escalation, and relied on a partial cache packet.  Use
    # the existing deep contract for this structural case.  The condition is
    # deliberately based on route shape/facet breadth, not a project name or
    # a fixed prompt, so ordinary one-facet questions keep their fast path.
    coverage_budget_route = bool(
        coverage_required
        and (
            explicit_deep_recall
            or procedure_depth
            or len(queries) > 3
            or primary in {"audit", "conflict", "inventory", "timeline", "system_map"}
        )
    )
    missing_evidence_can_change_answer = bool(
        escalation_queries
        and (
            explicit_deep_recall
            or negative_current
            or dimensions["continuity"]
            or coverage_required
        )
    )
    route_decisions = build_route_decisions(query, shapes, primary)
    association_receipt = {
        "requested": bool(association_closure_request),
        "conceptual": bool(conceptual_association_query),
        "mixed_delivery_audit": bool(association_mixed_delivery_audit),
        "route": "system_map+synthesis" if conceptual_association_query else "concrete_delivery_audit" if association_mixed_delivery_audit else "not_requested",
    }
    for decision in route_decisions:
        if not decision.get("active"):
            continue
        decision["association_closure"] = association_receipt
        if conceptual_association_query and decision.get("shape") == "system_map":
            decision["signals"] = list(dict.fromkeys(["图谱/星座关联闭包", *list(decision.get("signals") or [])]))[:6]
            decision["reason"] = (
                str(decision.get("reason") or "")
                + "；本题是概念性关联闭包，先沿实体/别名、图谱/星座和关系边扩展，再用命题覆盖与泛节点抑制收束"
            )
        elif conceptual_association_query and decision.get("shape") == "synthesis":
            decision["signals"] = list(dict.fromkeys(["关联证据归纳", *list(decision.get("signals") or [])]))[:6]
            decision["reason"] = (
                str(decision.get("reason") or "")
                + "；归并关联路径、来源时序、Claim Bundle 与反泛节点证据"
            )
    if procedure_depth:
        for decision in route_decisions:
            if decision.get("shape") == "procedure":
                decision["profile"] = {"budget": "high", "max_tokens": max_tokens, "max_queries": max_queries}
                decision["reason"] = (
                    str(decision.get("reason") or "")
                    + "；命中多阶段流程结构，升级为高预算覆盖召回"
                )
    plan = {
        "schema": 1,
        "controller_version": VERSION,
        # Every invocation gets a unique execution identity.  The stable
        # fingerprint remains available for grouping repeated phrasings without
        # causing feedback from one execution to attach to another.
        "query_id": uuid.uuid4().hex,  # compatibility alias for execution_id
        "execution_id": None,
        "query_fingerprint": hashlib.sha256(semantic_query_text.encode("utf-8")).hexdigest()[:16],
        "input_query": semantic_query_text,
        "raw_user_prompt": full_prompt_text(raw_user_prompt),
        "prompt_origin": str(runtime_context.get("prompt_origin") or ""),
        "full_prompt": full_prompt or semantic_query_text,
        "full_prompt_source": full_prompt_source,
        "evaluation_as_of": str(runtime_context.get("evaluation_as_of") or ""),
        "role": role,
        "access_class": role_policy.get("accessClass", "C"),
        "client": client,
        "planner": accepted_semantic_plan or {
            "source": "deterministic",
            "reason": "本次由本地结构与当前工作集判断；没有采用外部语义规划。",
            "confidence": 1.0,
            "coverage_dimensions": [],
        },
        "semantic_plan_review": semantic_plan_review,
        "requested_bank": bank_id,
        "memory_action": memory_action,
        "operational_audit": operational_audit,
        "working_set": working_set,
        "direct_policy_anchor_hits": direct_policy_anchors,
        "primary_shape": primary,
        "matched_shapes": shapes,
        "strategies": [STRATEGY_BY_SHAPE[s] for s in shapes],
        "route_decisions": route_decisions,
        "association_closure": association_receipt,
        "contextual_intent": contextual_intent,
        "budget": budget,
        "max_tokens": max_tokens,
        "types": types,
        "relation_closure": {"required": relation_closure_required, "max_hops": 2, "coverage_gap_stop": True, "status": "pending" if relation_closure_required else "not_required", "reason": "由 Required Slot 覆盖缺口驱动实体/决定/前后步骤关联闭包；资源预算限制图扩展，不替代语义覆盖判断。" if relation_closure_required else "本轮没有关系覆盖缺口；仍执行混合 Bank 与 Guidance 探测。"},
        "graph_route": {
            "enabled": relation_closure_required,
            "endpoint": "official_memory_graph",
            "max_hops": 2,
            "max_nodes": 80,
            "max_candidates": 10,
            "max_tokens": 1200,
            "latency_ms": 6000,
            "max_seed_queries": 3,
            "coverage_gap_stop": True,
            "reason": "图谱以 Required Slot 缺口决定是否继续扩展；只保留能解释路径、填补覆盖且通过时间/来源治理的候选。" if relation_closure_required else "本轮未要求关系闭包；不读取图谱邻居，但不跳过混合 Bank 与 Guidance 探测。",
        },
        "prefer_observations": prefer_observations,
        "queries": initial_queries,
        "query_metrics": query_metrics[:initial_query_count],
        "escalation_queries": escalation_queries,
        "escalation_query_metrics": query_metrics[initial_query_count:],
        "input_query_tokens": query_token_count(semantic_query_text),
        "query_compacted": any(item.get("compacted") for item in query_metrics),
        "upstream_query_token_limit": UPSTREAM_QUERY_TOKEN_LIMIT,
        "query_count": len(initial_queries),
        "planned_query_count": len(queries),
        # A deep evolution request must complete its stage coverage before this
        # answer is formed.  The outer Hook now has a compatible deep budget;
        # background continuation remains only a recovery path, never the sole
        # route to the facts needed by the current response.
        "foreground_escalation_allowed": True,
        "coverage_dimensions": coverage_dimensions,
        "coverage_required": coverage_required,
        "scope_claim": "indexed_scope_only",
        "negative_current_check": negative_current,
        "contrastive_point_check": contrastive_point,
        "requires_direct_evidence": evidence_required,
        "explicit_temporal_anchor": temporal_anchor,
        "direct_evidence_anchor": literal_anchor,
        "mental_model_policy": mental_policy,
        "guidance_sidecar": guidance_sidecar,
        "cross_domain": cross_domain,
        "levels": levels,
        "allowed_levels": allowed,
        "shared_bank": shared_bank,
        "role_bank": role_bank,
        "evidence_bank": evidence_bank,
        "direct_evidence_banks": source_evidence_banks,
        "direct_evidence_bank_reason": source_evidence_bank_reason,
        "relevance_admission_config": dict(controller_config.get("relevanceAdmission") or {}),
        "banks": list(dict.fromkeys(banks)),
        "auto_expand": bool(role_policy.get("autoExpand")),
        # The budget covers initial facets and any escalation together.
        "deadline_ms": int(
            controller_config.get(
                "deepDeadlineMs" if (explicit_deep_recall or coverage_budget_route) else (
                    "complexDeadlineMs" if len(queries) > 1 else "pointDeadlineMs"
                ),
                175000 if (explicit_deep_recall or coverage_budget_route) else (8000 if len(queries) > 1 else 2500),
            )
        ),
        "explicit_deep_recall": explicit_deep_recall,
        "coverage_budget_route": coverage_budget_route,
        "procedure_depth": procedure_depth,
        "recall_profile": normalized_profile or "controller",
        "fallback": "direct_hindsight_recall",
        "dimensions": dimensions,
        "temporal_scope": dimensions["temporal_scope"],
        "breadth": dimensions["breadth"],
        "continuity": dimensions["continuity"],
        "evidence_need": dimensions["evidence_need"],
        "reasoning": dimensions["reasoning"],
        "layers": dimensions["layers"],
        "disclosure_level": dimensions["disclosure_level"],
        "initial_budget": budget,
        "initial_max_tokens": max_tokens,
        "escalate_if_missing": {
            "enabled": missing_evidence_can_change_answer,
            "budget": "high",
            "max_tokens": min(4000, max(max_tokens, 2800 if dimensions["continuity"] else max_tokens)),
            "coverage_requirements": coverage_dimensions,
            "reason": (
                "缺失证据可能改变答案，才允许追加召回。"
                if missing_evidence_can_change_answer
                else "当前答案形态不值得为缺口追加召回。"
            ),
        },
        "value_of_information": {
            "may_escalate": missing_evidence_can_change_answer,
            "criterion": "只有缺失证据可能改变结论时，才执行下一次召回。",
        },
    }
    if accepted_semantic_plan:
        for dimension in accepted_semantic_plan["coverage_dimensions"]:
            if dimension not in plan["coverage_dimensions"]:
                plan["coverage_dimensions"].append(dimension)
    # A policy/maintenance question can use the temporal timeline route
    # without asking Hindsight to reconstruct an actual origin-to-present
    # history.  Do not leave the generic ``stage_coverage`` requirement on the
    # plan in that case: it has no truthful origin slot to fill and would make
    # a complete answer look perpetually incomplete.  Explicit evolution
    # questions are excluded by ``evolution_question`` and retain the strict
    # origin-stage requirement.
    if timeline_method_question(query) and not evolution_question(query):
        plan["coverage_dimensions"] = [
            value for value in plan["coverage_dimensions"]
            if value != "stage_coverage"
        ]
        for decision in plan["route_decisions"]:
            if decision.get("shape") == "timeline":
                decision["coverage_dimensions"] = [
                    value for value in decision.get("coverage_dimensions") or []
                    if value != "stage_coverage"
                ]
    # A newly supplied attachment/open file or an explicit user correction is
    # a stronger source than semantic memory. Return immediately with a visible
    # source-first receipt: the agent already has the source in this task and
    # must inspect it before older values may influence a current-state answer.
    attachment_source_guard = bool(runtime_context.get("source_authority")) and attachment_requires_source_first(query)
    # A current correction normally has precedence over historical memory, but
    # an explicit zero-injection/coverage diagnosis is itself a durable
    # architecture question. Keep the correction as the highest-priority
    # constraint while allowing Bank evidence to explain why relevant layers
    # were rejected; otherwise the system hides the exact history needed to
    # diagnose its own false zeros.
    historical_scope = requires_historical_bank_recall(query)
    source_guard = bool(attachment_source_guard or runtime_context.get("user_correction")) and not historical_scope
    project_state = dict(runtime_context.get("project_state") or {})
    if source_guard:
        guard_kind = "authoritative_source" if runtime_context.get("source_authority") else "explicit_user_correction"
        plan.update({
            "memory_action": "source_first",
            "primary_shape": "current",
            "matched_shapes": ["current"],
            "strategies": ["authoritative_source_guard"],
            "queries": [],
            "query_metrics": [],
            "escalation_queries": [],
            "escalation_query_metrics": [],
            "query_count": 0,
            "planned_query_count": 0,
            "coverage_dimensions": ["authoritative_source"],
            "coverage_required": False,
            "scope_claim": "current_source_inspection_required",
            "mental_model_policy": "sidecar_only" if guidance_sidecar.get("enabled") else "suppress",
            "value_of_information": {
                "may_escalate": False,
                "criterion": "先读取当前权威来源；旧记忆不能回答当前版本事实。若本轮需要解释或决策，稳定观察/心智模型只作为侧车补充。",
            },
            "working_set": {
                "sufficient": True,
                "decision": "inspect_authoritative_source",
                "reason": "本轮出现当前附件/打开文件或用户明确纠正；先以当前来源为准，不读取旧语义记忆。",
                "signals": {"source_kind": runtime_context.get("source_kind"), "source_label": runtime_context.get("source_label")},
            },
            "source_guard": {
                "active": True,
                "kind": guard_kind,
                "reason": "当前来源优先于旧记忆；待当前文件或本轮纠正被任务实际处理后，再按需补充项目历史。",
                "source_label": runtime_context.get("source_label") or "本轮用户明确纠正",
            },
        })
        for decision in plan["route_decisions"]:
            decision["active"] = decision["shape"] == "current"
            decision["primary"] = decision["shape"] == "current"
            if decision["shape"] == "current":
                decision["strategy"] = "authoritative_source_guard"
                decision["reason"] = "命中：当前附件/打开文件或明确纠正；先读取当前权威来源。"
    else:
        plan["source_guard"] = {"active": False, "reason": "本轮没有更高优先级的当前来源，按项目状态和共享 Hindsight 记忆召回。"}
    plan["project_state"] = project_state
    plan["execution_id"] = plan["query_id"]
    # Facts and execution guidance answer different needs. In a resolved
    # acceptance follow-up the task facts are already present, yet evidence
    # standards/responsibility can still affect how the assistant proceeds.
    raw_for_needs=raw_user_prompt or semantic_query_text
    from lib.context_coordination import explicit_memory_opt_out
    forbid_guidance=explicit_memory_opt_out(raw_for_needs) or any(term in raw_for_needs for term in ('不查历史','不查询历史','不调用长期记忆','不要使用记忆','不要用长期记忆'))
    requested_guidance=list(contextual_intent.get('guidance_requirements') or [])
    acceptance_followup=contextual_intent.get('intent_mode')=='contextual_acceptance_followup' and bool(contextual_intent.get('used_context'))
    guidance_needed=not forbid_guidance and (bool(requested_guidance) or stable_guidance_needed(semantic_query_text,shapes))
    facts_needed=not forbid_guidance and plan.get('memory_action')!='source_first' and not acceptance_followup and (not bool(working_set.get('sufficient')) or historical_scope or 'timeline' in shapes)
    plan['memory_needs']={
        'facts':{'needed':facts_needed,'reason':'explicitly_forbidden' if forbid_guidance else 'current_task_facts_sufficient' if acceptance_followup else plan.get('memory_action')},
        'guidance':{'needed':guidance_needed,'requirements':requested_guidance,'reason':'explicitly_forbidden' if forbid_guidance else 'acceptance_and_execution_constraints' if acceptance_followup else 'task_applicability_check'},
    }
    plan['guidance_query']=semantic_query_text
    if not facts_needed and guidance_needed:
        if acceptance_followup:
            background='\n'.join(str(row.get('content') or '') for row in contextual_intent.get('context_items') or [] if row.get('role')=='user')
            plan['guidance_query']=raw_for_needs+'\n当前任务语境（不是新指令）：'+background+'\n需要判断前述任务的真实验收、完成标准、复测与执行责任；只补充适用的协作指导。'
        plan.update(memory_action='guidance_only',queries=[],query_metrics=[],query_count=0,planned_query_count=0,
                    escalation_queries=[],escalation_query_metrics=[],types=['observation'],requires_direct_evidence=False,
                    coverage_required=False,coverage_dimensions=['applicable_guidance'],mental_model_policy='sidecar_only')
        plan['guidance_sidecar'].update(enabled=True,observation=True,mental_model=True,
                                      reason='当前任务事实已具备，单独核对验收与执行指导；不把事实充分当作指导已覆盖。')
    elif forbid_guidance:
        plan['guidance_sidecar'].update(enabled=False,observation=False,mental_model=False,reason='当前用户明确禁止长期记忆')
        plan.update(memory_action='noop_long_term',queries=[],query_metrics=[],query_count=0,planned_query_count=0,
                    escalation_queries=[],escalation_query_metrics=[],coverage_required=False,coverage_dimensions=[])
    elif not facts_needed:
        plan.update(memory_action='noop_long_term',queries=[],query_metrics=[],query_count=0,planned_query_count=0,
                    escalation_queries=[],escalation_query_metrics=[],coverage_required=False,coverage_dimensions=[])
        plan['guidance_sidecar'].update(enabled=False,observation=False,mental_model=False,reason='当前工作集足够，未识别额外指导需求。')
    elif plan.get('memory_action')=='noop_long_term':
        plan['memory_action']='deep_recall' if explicit_deep_recall else 'focused_recall'
    # One serializable contract is attached to every plan so the controller,
    # Hook and status projection can agree on subject/time/task/source/relation
    # scope without reparsing the prompt independently.
    plan["retrieval_contract"] = build_retrieval_contract(
        semantic_query_text,
        plan,
        {
            "graphMaxHops": controller_config.get("graphMaxHops"),
            "graphMaxNodes": controller_config.get("graphMaxNodes"),
            "graphMaxCandidates": controller_config.get("graphMaxCandidates"),
            "graphMaxTokens": controller_config.get("graphMaxTokens"),
            "graphLatencyMs": controller_config.get("graphLatencyMs"),
            "graphMaxSeedQueries": controller_config.get("graphMaxSeedQueries"),
        },
    )
    graph_budget = plan["retrieval_contract"]["graph_budget"]
    plan["relation_closure"]["max_hops"] = graph_budget["max_hops"]
    plan["graph_route"].update(graph_budget)
    return plan


def evaluate_coverage(results: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    """Return an evidence-backed coverage receipt, not a result-count proxy."""
    required = list(plan.get("coverage_dimensions") or [])
    full_prompt = str(plan.get("full_prompt") or plan.get("input_query") or "")
    continuation_policy = continuation_policy_question(full_prompt)
    text = "\n".join(
        str(item.get("text") or item.get("content") or "") for item in results
    ).casefold()
    metadata = [item.get("metadata") or {} for item in results]
    timestamps = [timestamp_value(item) for item in results if timestamp_value(item)]
    types = {str(item.get("type") or item.get("memory_type") or "") for item in results}
    documents = {str(item.get("document_id") or "") for item in results if item.get("document_id")}
    source_values = {
        str(value)
        for row in metadata
        for key, value in row.items()
        if key.casefold() in {"source", "session_id", "session_ids", "project", "transcript_path", "evidence_time"}
        and value not in (None, "")
    }
    current_hit = any(marker.casefold() in text for marker in CURRENT_STATE_MARKERS)
    retirement_hit = any(marker.casefold() in text for marker in RETIREMENT_MARKERS)
    # Evolution prompts often name the starting point as “原始架构” or
    # “官方下载安装到现在” rather than using the literal “最初”.  Treat
    # those explicit origin anchors as stage evidence too; otherwise the
    # claim-level receipt can be complete while the generic coverage receipt
    # incorrectly reports a missing ``stage_coverage`` slot.
    origin_hit = any(marker in text for marker in (
        "最初", "最开始", "一开始", "起初", "初版", "原始版本", "原始架构",
        "初始架构", "官方版本", "官方下载安装到", "官方安装", "早期", "基础版",
    ))
    open_hit = any(marker in text for marker in ("未完成", "下一步", "待处理", "待继续", "剩余", "还需要"))
    transition_hit = any(marker in text for marker in ("后来", "之后", "改为", "变成", "替代", "阶段", "转折"))
    version_hit = bool(
        transition_hit
        or retirement_hit
        or "版本" in text
        or re.search(r"(?:^|[^a-z])v?\d+(?:\.\d+){1,3}(?:$|[^0-9])", text)
    )
    failure_hit = any(marker in text for marker in ("失败", "错误", "问题", "回退", "未通过"))
    # Counter-evidence is often written as a diagnosis rather than the
    # literal word “反例” (for example “表现不佳 / 未能 / 缺失 / 不称职”).
    # Recognize those evidence-bearing negative forms while keeping the
    # requirement scoped to a synthesis/audit plan; this avoids declaring
    # every ordinary “问题” mention a contradiction.
    counter_hit = any(marker in text for marker in (
        "反例", "但是", "例外", "边界", "不适用", "另一种解释",
        "表现不佳", "未能", "缺失", "不称职", "不足", "误判", "失败",
    ))
    decision_hit = any(marker in text for marker in (
        "关联决定", "相关决定", "决定", "优先级", "采用", "放弃",
        "取代", "替代", "策略", "职责", "规则", "方案", "为何选择",
    ))
    explicit_current = current_hit or any(
        str(row.get(key) or "").casefold() in {"current", "active", "verified", "effective", "latest"}
        for row in metadata
        for key in ("validity", "status", "verification_status")
    )
    # Long-task continuation guidance is frequently stored as a compact
    # method rule rather than a project-specific incident.  Its evidence
    # vocabulary is therefore different from a generic procedure/timeline:
    # ``任务锚点/普通续写`` establishes scope, ``分层/结合/读取`` establishes
    # steps, and ``修复/避免误判`` establishes outcome and counter-evidence.
    # Keep this override behind the same structural query detector used by
    # routing, and require more than one returned record so a single broad
    # Full Prompt heading cannot manufacture coverage.
    continuation_scope_hit = any(marker in text for marker in (
        "任务锚点", "任务证据", "活动任务", "同一任务", "普通续写",
        "任务身份", "任务边界", "长任务",
    ))
    continuation_steps_hit = any(marker in text for marker in (
        "分层", "结合", "读取", "检索", "统一构造", "并行", "索引",
        "规划", "重排", "定范围", "定状态", "远距离上下文", "最近三轮",
        "full prompt",
    ))
    continuation_outcome_hit = any(marker in text for marker in (
        "修复", "解决", "确保", "准确", "完成", "通过", "生效", "验收",
    ))
    continuation_counter_hit = any(marker in text for marker in (
        "不能", "不应", "避免", "误判", "短路", "缺失", "不完整", "问题",
        "失败", "不称职",
    ))
    continuation_evidence = {
        "enabled": continuation_policy,
        "scope": continuation_scope_hit,
        "steps": continuation_steps_hit,
        "outcomes": continuation_outcome_hit,
        "counter_evidence": continuation_counter_hit,
        "result_count_gate": len(results) >= 2,
    }
    # Prefer explicit stage labels produced by a future stage projection.  Raw
    # memories can still provide a conservative fallback based on independent
    # change families, but repeated progress updates with the same change verb
    # are one family, not artificial completeness.
    explicit_stages = {
        str(row.get("evolution_stage") or row.get("stage_id") or "").strip()
        for row in metadata
        if str(row.get("evolution_stage") or row.get("stage_id") or "").strip()
    }
    transition_families = {
        marker for marker in (
            "接入", "引入", "迁移", "重构", "升级", "替代", "改为", "停用", "扩展", "分层",
            "整合", "适配", "回放", "审计", "图谱", "实体", "观察", "心智模型",
        ) if marker in text
    }
    stage_family_count = len(explicit_stages or transition_families)
    # A named-system taxonomy question has its own evidence receipt.  The
    # generic inventory slots (sources/actions/scope) describe open-set
    # collection and are not a valid proxy for whether every named component
    # was explained.  Keep these checks structural and vocabulary-based: a
    # definition/role record must say what a component does, the relation
    # record must expose the hand-off, aliases must be explicit, and the
    # boundary record must distinguish non-equivalence or non-substitution.
    system_definition_hit = any(marker in text for marker in (
        "定义", "含义", "定位", "负责", "职责", "架构", "内核", "编排层",
        "接入层", "入口", "底座", "数据流", "组件",
    ))
    system_role_hit = any(marker in text for marker in (
        "上下游", "入口", "接入层", "中间层", "编排", "底座", "数据库",
        "数据流", "连接", "调用", "负责", "输入", "输出", "→",
    ))
    system_alias_hit = any(marker in text for marker in (
        "别名", "简称", "全称", "即", "统称", "不是同一", "不等于", "≠",
    ))
    system_boundary_hit = any(marker in text for marker in (
        "边界", "不是同一个", "不是同一", "不等于", "不替代", "仅", "只作为",
        "不能", "不拥有", "不自动", "不应", "≠",
    ))
    checks = {
        "semantic": bool(results),
        "latest_state": explicit_current or ("latest_state" not in required and bool(timestamps)),
        "supersession": retirement_hit,
        "earlier": len(set(timestamps)) >= 2,
        "transitions": transition_hit,
        "latest": explicit_current,
        "stage_coverage": origin_hit and explicit_current and stage_family_count >= 3,
        "sources": len(source_values) >= 1 or len(documents) >= 2,
        "time_ranges": len(set(timestamps)) >= 2,
        "subjects": len(results) >= 2,
        "actions_results": any(kind in types for kind in ("experience", "world")) and bool(results),
        "supporting_evidence": len(results) >= 2,
        "counter_evidence": counter_hit,
        "scope": any(marker in text for marker in ("范围", "场景", "条件", "只在", "适用于")),
        "source": bool(source_values or documents),
        "evidence_time": bool(timestamps) or any("evidence_time" in row for row in metadata),
        "version": version_hit or any("version" in row for row in metadata),
        "old_state": retirement_hit or transition_hit,
        "new_state": current_hit,
        "validity": current_hit or retirement_hit or any(
            key in row for row in metadata for key in ("validity", "valid_from", "valid_to", "status")
        ),
        "aliases": len(results) >= 2,
        "known_gaps": any(marker in text for marker in ("未知", "没有记录", "缺失", "未找到", "无法确认")),
        "steps": any(marker in text for marker in ("步骤", "首先", "然后", "最后", "流程")),
        "outcomes": any(marker in text for marker in ("结果", "完成", "通过", "生效", "成功")),
        "failure_modes": failure_hit,
        "source_thread": any(
            key in row for row in metadata for key in ("session_id", "session_ids", "project", "transcript_path")
        ) or any("codex" in value.casefold() for value in documents | source_values),
        "open_items": open_hit,
        "related_entities": len({str(x) for item in results for x in (item.get("entities") or []) if x}) >= 2 or any("关联" in str(item.get("text") or item.get("content") or "") for item in results),
        "related_decisions": len(results) >= 2 and (
            transition_hit
            or decision_hit
            or any("影响" in str(item.get("text") or item.get("content") or "") for item in results)
        ),
        "system_definitions": system_definition_hit,
        "role_relations": system_role_hit,
        "alias_relations": system_alias_hit,
        "boundaries": system_boundary_hit,
    }
    if continuation_policy and len(results) >= 2:
        checks["scope"] = checks["scope"] or continuation_scope_hit
        checks["steps"] = checks["steps"] or continuation_steps_hit
        checks["outcomes"] = checks["outcomes"] or continuation_outcome_hit
        checks["counter_evidence"] = checks["counter_evidence"] or continuation_counter_hit
        checks["failure_modes"] = checks["failure_modes"] or continuation_counter_hit
    covered = [dimension for dimension in required if checks.get(dimension, bool(results))]
    missing = [dimension for dimension in required if dimension not in covered]
    return {
        "required": required,
        "covered": covered,
        "missing": missing,
        "complete": not missing,
        "evidence": {
            "result_count": len(results),
            "distinct_documents": len(documents),
            "source_receipts": len(source_values),
            "timestamped_results": len(timestamps),
            "memory_types": sorted(types),
            "origin_evidence": origin_hit,
            "current_evidence": explicit_current,
            "stage_family_count": stage_family_count,
            "stage_families": sorted(explicit_stages or transition_families),
            "continuation_policy_evidence": continuation_evidence,
        },
        "relation_closure_status": "complete" if not any(x in missing for x in ("related_entities", "related_decisions")) else ("not_applicable" if not plan.get("relation_closure",{}).get("required") else "incomplete"),
    }


def _file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _backup_timestamp(value: str) -> datetime | None:
    match = re.search(r"(20\d{6}-\d{6})", value)
    if not match:
        return None
    try:
        # Backup filenames are emitted by the local scheduled script in local
        # wall-clock time.  Treating them as UTC made a 03:10 backup appear to
        # have completed at 11:10 in China and could even yield a negative age.
        return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").astimezone()
    except ValueError:
        return None



def _parse_audit_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def token_savings_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    """Summarise adaptive recall *budget* avoided from the local audit ledger.

    This is deliberately not presented as provider billing or generated-output
    tokens.  It is the deterministic context budget avoided relative to using
    the current full-coverage upper bound (2,400 tokens) for every recall.
    """
    audit_path = STATE_ROOT / "audit" / "recall-semantic.jsonl"
    baseline = 2400
    now = datetime.now().astimezone()
    mode = (query.get("range") or ["today"])[0]
    start: datetime | None = None
    end: datetime | None = None
    if mode == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif mode == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif mode == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif mode == "custom":
        def _date(name: str, end_of_day: bool = False) -> datetime | None:
            raw = (query.get(name) or [""])[0]
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d").astimezone()
                return parsed.replace(hour=23, minute=59, second=59) if end_of_day else parsed
            except ValueError:
                return None
        start, end = _date("from"), _date("to", True)

    rows: list[dict[str, Any]] = []
    if audit_path.exists():
        try:
            with audit_path.open("r", encoding="utf-8", errors="replace") as audit_stream:
              for line in audit_stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                at = _parse_audit_time(row.get("at"))
                if not at or (start and at < start) or (end and at > end):
                    continue
                maximum = row.get("max_tokens")
                if not isinstance(maximum, (int, float)) or maximum < 0:
                    continue
                used_budget = min(int(maximum), baseline)
                rows.append({
                    "at": at.isoformat(), "profile": str(row.get("profile") or "未标注"),
                    "budget": str(row.get("budget") or "未标注"), "max_tokens": used_budget,
                    "saved": max(0, baseline - used_budget),
                    "query_shape": row.get("query_shape"),
                    "strategy_count": len(row.get("query_strategies") or []),
                })
        except OSError:
            pass
    groups: dict[str, dict[str, int]] = {}
    for row in rows:
        group = groups.setdefault(row["profile"], {"calls": 0, "budget_tokens": 0, "saved_tokens": 0})
        group["calls"] += 1; group["budget_tokens"] += row["max_tokens"]; group["saved_tokens"] += row["saved"]
    rows.sort(key=lambda row: row["at"], reverse=True)
    return {
        "schema": 1, "metric": "adaptive_recall_budget_avoided", "baseline_tokens_per_recall": baseline,
        "metric_explanation": "相对每次都按 2400 token 全覆盖召回，动态路由未分配的上下文预算。不是模型账单，也不等同于模型实际输出。",
        "range": mode, "from": start.isoformat() if start else None, "to": end.isoformat() if end else None,
        "calls": len(rows), "allocated_tokens": sum(row["max_tokens"] for row in rows),
        "saved_tokens": sum(row["saved"] for row in rows), "full_coverage_tokens": len(rows) * baseline,
        "by_profile": [{"profile": name, **value} for name, value in sorted(groups.items(), key=lambda kv: (-kv[1]["saved_tokens"], kv[0]))],
        "recent_examples": rows[:12], "generated_at": now.isoformat(),
    }


def backup_status_payload() -> dict[str, Any]:
    """Build a cheap, non-secret backup receipt for the local control plane."""
    daily = DEFAULT_BACKUP_ROOT / "daily"
    local_sets: dict[str, dict[str, Any]] = {}
    for path in daily.glob("*") if daily.exists() else []:
        stamp = re.search(r"(20\d{6}-\d{6})", path.name)
        if not path.is_file() or not stamp:
            continue
        local_sets.setdefault(stamp.group(1), {"timestamp": stamp.group(1), "files": []})["files"].append(_file_info(path))
    local_rows = sorted(local_sets.values(), key=lambda row: row["timestamp"], reverse=True)[:10]
    for row in local_rows:
        names = [item["name"] for item in row["files"]]
        row["complete_set"] = (
            any(name.startswith("hindsight-full-") and name.endswith(".zip") for name in names)
            and any(name.startswith("hindsight-config-") and name.endswith(".tar.gz") for name in names)
            and any(name.startswith("SHA256SUMS-") for name in names)
        )
        row["total_bytes"] = sum(int(item["bytes"]) for item in row["files"])

    cloud_rows = []
    for folder in sorted(DEFAULT_CLOUD_BACKUP_DIR.glob("hindsight-backup-*"), reverse=True)[:10] if DEFAULT_CLOUD_BACKUP_DIR.exists() else []:
        if not folder.is_dir():
            continue
        files = [_file_info(path) for path in sorted(folder.iterdir()) if path.is_file()]
        names = [item["name"] for item in files]
        cloud_rows.append({
            "timestamp": folder.name.removeprefix("hindsight-backup-"),
            "path": str(folder),
            "files": files,
            "total_bytes": sum(int(item["bytes"]) for item in files),
            "complete_set": (
                sum(name.endswith(".enc") for name in names) >= 2
                and any(name.startswith("SHA256SUMS-") for name in names)
            ),
        })

    latest_local = next((row for row in local_rows if row["complete_set"]), None)
    latest_cloud = next((row for row in cloud_rows if row["complete_set"]), None)
    latest_time = _backup_timestamp((latest_local or {}).get("timestamp", ""))
    age_hours = None
    if latest_time:
        age_hours = round((datetime.now().astimezone() - latest_time).total_seconds() / 3600, 2)
    error_tail = ""
    try:
        error_tail = "\n".join(DEFAULT_BACKUP_ERROR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
    except OSError:
        pass
    latest_epoch = latest_time.timestamp() if latest_time else 0
    # The backup script intentionally redirects stderr into backup-error.log;
    # tar's harmless “Removing leading /” and package download progress also
    # land there.  Do not call those failures.  Require an actual failure token
    # in the recent tail and independently confirm that the matching run ended
    # with the script's completed receipt.
    meaningful_error_lines = [
        line for line in error_tail.splitlines()
        if re.search(r"(?:^|\b)(?:error|failed|fatal|traceback|no space left|permission denied|checksum mismatch)(?:\b|:)", line, re.I)
        and not re.search(r"no errors detected", line, re.I)
    ]
    recent_error = bool(
        meaningful_error_lines
        and DEFAULT_BACKUP_ERROR_LOG.exists()
        and DEFAULT_BACKUP_ERROR_LOG.stat().st_mtime > latest_epoch
    )
    backup_log_tail = ""
    try:
        backup_log_tail = "\n".join(DEFAULT_BACKUP_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-120:])
    except OSError:
        pass
    latest_stamp = (latest_local or {}).get("timestamp", "")
    log_completed = bool(
        latest_stamp
        and f"hindsight-full-{latest_stamp}.zip" in backup_log_tail
        and '"status": "completed"' in backup_log_tail
    )
    restore_drill_matches = False
    try:
        drill = json.loads(DEFAULT_RESTORE_DRILL_REPORT.read_text(encoding="utf-8"))
        restore_drill_matches = bool(
            drill.get("status") == "passed"
            and latest_stamp
            and f"hindsight-full-{latest_stamp}.zip" in str(drill.get("database_backup") or "")
            and f"hindsight-config-{latest_stamp}.tar.gz" in str(drill.get("configuration_backup") or "")
            and (drill.get("database_restore") or {}).get("status") == "passed"
            and not drill.get("production_routing_changed")
        )
    except (OSError, ValueError, TypeError):
        pass
    healthy = bool(
        latest_local and latest_cloud and age_hours is not None and age_hours <= 48
        and (log_completed or restore_drill_matches) and not recent_error
    )
    now = datetime.now().astimezone()
    next_run = now.replace(hour=3, minute=10, second=0, microsecond=0)
    if next_run <= now:
        from datetime import timedelta
        next_run += timedelta(days=1)
    return {
        "schema": 1,
        "generated_at": utc_now(),
        "overall_status": "healthy" if healthy else "attention",
        "status_explanation": (
            "本地完整集和 WPS 同步目录加密完整集均存在，最近一次不超过 48 小时。"
            if healthy else "最近备份、完整集或错误日志至少有一项需要查看。"
        ),
        "schedule": {"local_time": "每天 03:10", "next_run_at": next_run.isoformat()},
        "retention": {
            "local": "最近7个日备份 + 第2/3/4周锚点 + 上月锚点",
            "wps_cloud": "最近2个加密完整备份集",
        },
        "encryption": "WPS 副本：AES-256-CBC + PBKDF2-SHA256(iter=600000)；密钥不在本接口展示",
        "local": {"directory": str(daily), "latest": latest_local, "recent_sets": local_rows, "age_hours": age_hours},
        "wps_cloud": {
            "directory": str(DEFAULT_CLOUD_BACKUP_DIR),
            "local_sync_directory_readable": DEFAULT_CLOUD_BACKUP_DIR.is_dir(),
            "local_sync_directory_writable": os.access(DEFAULT_CLOUD_BACKUP_DIR, os.W_OK),
            "remote_receipt_scope": "这里只验证 WPS 同步目录中的本地副本；云端最终同步状态以 WPS 客户端为准",
            "latest": latest_cloud,
            "recent_sets": cloud_rows,
        },
        "verification": {
            "matching_run_completed": log_completed,
            "matching_restore_drill_passed": restore_drill_matches,
            "local_complete_set": bool(latest_local),
            "wps_directory_complete_set": bool(latest_cloud),
            "recent_failure_signal": recent_error,
        },
        "latest_error_after_backup": recent_error,
        "error_tail": "\n".join(meaningful_error_lines[-8:]) if recent_error else "",
    }


def result_key(item: dict[str, Any]) -> str:
    for key in ("id", "chunk_id"):
        if item.get(key):
            return f"{key}:{item[key]}"
    text = str(item.get("text") or item.get("content") or "")
    return "text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def partition_deterministic_runtime_authorities(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate executable current-state facts from semantic Bank evidence.

    These rows are created only after a source-specific query recognizer has
    matched and their values are read from live configuration or runtime
    endpoints. Running them through the ordinary cross-encoder gate can
    reject the most authoritative answer merely because synthetic rows have
    no embedding provenance. The explicit admission class avoids granting a
    suffix-based exemption to arbitrary ``*-authority`` memories.
    """
    authorities: list[dict[str, Any]] = []
    ordinary: list[dict[str, Any]] = []
    for raw in results:
        item = dict(raw or {})
        metadata = dict(item.get("metadata") or {})
        if metadata.get("admission_class") != "deterministic_runtime_authority":
            ordinary.append(item)
            continue
        admission = dict(metadata.get("_ccy_admission") or {})
        admission.update({
            "decision": "qualified",
            "reason": "当前问题命中专用运行态识别器；该事实由本机可执行配置或实时端点生成。",
            "source_class": "deterministic_runtime_authority",
        })
        metadata["_ccy_admission"] = admission
        item["metadata"] = metadata
        authorities.append(item)
    return authorities, ordinary


def source_first_authority_items(
    query: str, runtime_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return one query-specific executable authority for a Hook source-first turn."""
    runtime_config = load_json(DEFAULT_CODEX_RUNTIME_CONFIG)
    candidates = apply_recall_governance(
        full_prompt_text(query),
        [],
        runtime_config,
        contextual_intent=(runtime_context or {}).get("contextual_intent"),
        raw_user_prompt=str((runtime_context or {}).get("raw_user_prompt") or ""),
    )
    by_source = {
        str((item.get("metadata") or {}).get("source") or ""): dict(item)
        for item in candidates
        if str((item.get("metadata") or {}).get("source") or "").endswith("-authority")
    }
    # More specific live sources take precedence over the generic local
    # runtime snapshot when wording overlaps (for example a current backup
    # question also says Hindsight).
    for source in (
        "backup-runtime-authority",
        "coding-plan-runtime-authority",
        "identity-authority",
        "wps-sync-runtime-authority",
        "unknown-attribute-policy-authority",
        "smalltalk-memory-policy-authority",
        "local-edit-memory-policy-authority",
        "local-runtime-authority",
    ):
        if source in by_source:
            return [by_source[source]]
    return []


def controller_admission_inputs(
    ranked: list[dict[str, Any]],
    admitted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    deferred: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the exact, de-duplicated Controller admission denominator.

    ``ranked`` contains fused Bank/graph candidates.  Stable observations,
    direct policies and mental-model sections are evaluated by explicit
    sidecar gates and can therefore enter ``admitted``/``rejected`` without
    appearing in that ranked list.  They are still real Controller inputs and
    must be counted and shown once.  Later partition rows replace the ranked
    copy so their final admission reason is preserved without changing order.
    """
    ordered_keys: list[str] = []
    by_key: dict[str, dict[str, Any]] = {}
    ranked_keys: set[str] = set()
    for raw in ranked or []:
        item = dict(raw.get("item") or raw) if isinstance(raw, dict) else {}
        if not item:
            continue
        key = result_key(item)
        if key not in by_key:
            ordered_keys.append(key)
        by_key[key] = item
        ranked_keys.add(key)
    for group in (admitted or [], rejected or [], deferred or []):
        for raw in group:
            item = dict(raw or {}) if isinstance(raw, dict) else {}
            if not item:
                continue
            key = result_key(item)
            if key not in by_key:
                ordered_keys.append(key)
            by_key[key] = item
    inputs = [by_key[key] for key in ordered_keys]
    sidecars = [by_key[key] for key in ordered_keys if key not in ranked_keys]
    return inputs, sidecars


def normalize_admission_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Give deterministic authority rows a marked, stable local identity.

    Upstream Bank records must already carry an ID. Some executable local
    authority rows intentionally do not because they are generated at request
    time. A content-derived `authority:` ID is safe for their audit binding;
    it is explicitly marked and never represents a Bank memory ID.
    """
    row = dict(item or {})
    if row.get("id") or row.get("chunk_id"):
        return row
    metadata = dict(row.get("metadata") or {})
    row["metadata"] = metadata
    if not str(row.get("type") or "").strip():
        row["type"] = "invalid_candidate"
    source = str(metadata.get("source") or "")
    deterministic = metadata.get("admission_class") == "deterministic_runtime_authority" or source.endswith("-authority")
    prefix = "authority" if deterministic else "invalid"
    material = "\x1f".join((source, str(row.get("type") or ""), str(row.get("text") or row.get("content") or "")))
    row["id"] = prefix + ":" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    metadata["_ccy_admission_identity"] = {
        "generated": True,
        "kind": "deterministic_authority" if deterministic else "invalid_candidate",
        "source": source or "unknown",
        "bank_memory_id": False,
    }
    return row


def finalize_admission(
    context: dict[str, Any], candidates: list[dict[str, Any]], *,
    admitted: list[dict[str, Any]], rejected: list[dict[str, Any]], deferred: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind every Controller candidate to one final semantic decision.

    This records the existing admission pipeline. It does not create a second
    relevance algorithm for the Hook to disagree with later.
    """
    def keys(rows: list[dict[str, Any]]) -> set[str]:
        return {decision_key(dict(item or {})) for item in rows if isinstance(item, dict)}

    admitted_keys, rejected_keys, deferred_keys = keys(admitted), keys(rejected), keys(deferred)
    ordered: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for raw in candidates or []:
        item = dict(raw or {})
        key = decision_key(item)
        if key in seen:
            continue
        seen.add(key)
        ordered.append((key, normalize_admission_candidate(item)))

    decisions: list[dict[str, Any]] = []
    for original_key, item in ordered:
        key = original_key
        metadata = dict(item.get("metadata") or {})
        prior = dict(metadata.get("_ccy_admission") or item.get("admission") or {})
        invalid_identity = (metadata.get("_ccy_admission_identity") or {}).get("kind") == "invalid_candidate"
        if invalid_identity or key in rejected_keys:
            action = "reject"
        elif key in deferred_keys:
            action = "defer"
        elif key in admitted_keys:
            action = "admit"
        else:
            action = "reject"
        reason = str(prior.get("decision") or ("invalid_candidate_identity" if invalid_identity else {"admit": "qualified", "defer": "deferred_for_token_budget", "reject": "not_selected"}[action]))
        detail = str(prior.get("reason") or ("候选缺少上游可验证身份，只保留拒绝审计，不允许进入 Packet。" if invalid_identity else "Controller final admission ledger."))
        decisions.append(make_decision(item, context, action, reason, reason_detail=detail, decider="controller"))
    return {
        "items": [item for _, item in ordered],
        "decisions": decisions,
        "normalized_items_by_input_key": {key: item for key, item in ordered},
    }


def admission_binding_context(
    runtime_context: dict[str, Any] | None, *, execution_id: str, prompt_sha256: str,
) -> dict[str, Any]:
    """Build a complete binding without masking a partially broken Hook turn.

    The production Hook supplies all four per-turn identity values.  Direct
    read-only Controller callers do not have a Hook at all, so they receive an
    explicit diagnostic identity rather than an invalid empty binding.  A
    partially populated Hook identity remains incomplete and is rejected by
    the contract instead of being silently rewritten as a diagnostic run.
    """
    runtime = dict(runtime_context or {})
    binding = {
        "session_id": str(runtime.get("session_id") or ""),
        "turn_id": str(runtime.get("turn_id") or ""),
        "hook_invocation_id": str(runtime.get("invocation_id") or ""),
        "execution_id": str(execution_id or ""),
        "prompt_sha256": str(prompt_sha256 or ""),
        "policy_version": RELEVANCE_POLICY,
        "source_revision": str(runtime.get("source_revision") or ""),
    }
    hook_identity = (
        binding["session_id"],
        binding["turn_id"],
        binding["hook_invocation_id"],
        binding["source_revision"],
    )
    if not any(hook_identity):
        diagnostic_key = binding["execution_id"] or hashlib.sha256(
            binding["prompt_sha256"].encode("utf-8")
        ).hexdigest()[:24]
        marker = f"diagnostic:{diagnostic_key}"
        binding.update({
            "session_id": marker,
            "turn_id": marker,
            "hook_invocation_id": marker,
            "execution_id": binding["execution_id"] or marker,
            "prompt_sha256": binding["prompt_sha256"] or marker,
            "source_revision": "diagnostic:no-hook-source-revision",
            "binding_mode": "diagnostic_no_hook",
        })
    else:
        binding["binding_mode"] = (
            "hook" if all(binding[field] for field in CONTEXT_FIELDS) else "hook_incomplete"
        )
    return binding


def timestamp_value(item: dict[str, Any]) -> float:
    raw = item.get("mentioned_at") or (item.get("metadata") or {}).get("evidence_time")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def filter_results_as_of(results: list[dict[str, Any]], as_of: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return evidence that existed at ``as_of`` plus later excluded rows.

    Evaluation must not retrieve an answer that was retained *after* the
    question under test.  Rows without an interpretable timestamp remain: an
    unknown time must not be silently treated as a later fact.
    """
    try:
        cutoff = datetime.fromisoformat(str(as_of).replace("Z", "+00:00")).timestamp()
    except Exception:
        return list(results or []), []
    kept, excluded = [], []
    for item in results or []:
        value = timestamp_value(item)
        (excluded if value and value > cutoff else kept).append(item)
    return kept, excluded


ENTITY_STOP_NAMES = frozenset((
    "user", "用户", "assistant", "助手", "实体", "项目", "任务", "系统", "记忆", "记录",
    "内容", "问题", "当前", "历史", "来源", "事实", "经历", "观察", "心智模型",
))
# These describe the retrieval mechanism itself, not a subject the user wants
# evidence about. They are deliberately excluded from entity anchor admission.
ENTITY_CONTEXT_NAMES = frozenset(("hindsight", "codex", "openclaw", "memoryquerycontroller"))


def _entity_normalize(value: Any) -> str:
    """Canonical comparison form used only for entity anchors, never for merges."""
    return re.sub(r"[\s_\-‐‑‒–—]+", "", str(value or "")).casefold()


def _entity_names(value: Any) -> list[str]:
    """Extract bounded display names from upstream Hindsight entity payloads."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        values = [value.get("canonical_name"), value.get("name"), value.get("label")]
        aliases = value.get("aliases") or []
        if isinstance(aliases, list):
            values.extend(aliases)
        return [str(item).strip() for item in values if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        names: list[str] = []
        for item in value:
            names.extend(_entity_names(item))
        return names
    return []


def load_approved_entity_aliases(path: Path = DEFAULT_ENTITY_ALIAS_REGISTRY) -> dict[str, list[str]]:
    """Read only explicitly approved aliases; model candidates never enter recall routing."""
    raw = load_json(path)
    approved = raw.get("approved_aliases") if isinstance(raw, dict) else {}
    if not isinstance(approved, dict):
        return {}
    output: dict[str, list[str]] = {}
    for canonical, aliases in approved.items():
        canonical_name = str(canonical or "").strip()
        if not canonical_name:
            continue
        values = [canonical_name]
        if isinstance(aliases, list):
            values.extend(str(alias).strip() for alias in aliases if str(alias or "").strip())
        output[canonical_name] = list(dict.fromkeys(values))
    return output


def _usable_entity_form(value: str) -> bool:
    compact = _entity_normalize(value)
    if not compact or compact in ENTITY_STOP_NAMES or compact in ENTITY_CONTEXT_NAMES:
        return False
    # Very short Chinese words are too ambiguous.  ASCII/model/file identifiers
    # can be short if they include a digit or a separator, but require >=3 chars.
    # Explicit family/person anchors are meaningful even at two CJK chars;
    # ordinary short fragments remain rejected by the generic guard.
    if len(compact) >= 3 or compact in {"老婆", "妻子", "优优", "女儿", "儿子"}:
        return True
    return bool(re.search(r"[0-9._/]", str(value))) and len(compact) >= 2


def resolve_query_entity_anchors(query: str, entities: dict[str, Any]) -> list[dict[str, str]]:
    """Resolve only entities named in this user request or a verified alias.

    A query without a concrete entity deliberately receives no entity gate: this
    avoids turning generic conceptual questions into false-negative retrievals.
    """
    compact_query = _entity_normalize(query)
    if not compact_query:
        return []
    aliases = load_approved_entity_aliases()
    candidates: list[tuple[str, str, str, str]] = []
    # Upstream entity entries are evidence from the current Hindsight response.
    for key, raw in (entities or {}).items():
        names = _entity_names(raw)
        canonical = next((name for name in names if name), str(key or "").strip())
        entity_id = str(raw.get("id") or key) if isinstance(raw, dict) else str(key)
        for name in names or [canonical]:
            candidates.append((canonical, entity_id, name, "upstream_entity"))
    # Registry entries only add aliases for names already surfaced upstream, so
    # an unrelated global alias cannot expand every query.
    upstream_canonicals = {_entity_normalize(row[0]) for row in candidates if row[0]}
    for canonical, forms in aliases.items():
        if _entity_normalize(canonical) not in upstream_canonicals:
            continue
        for form in forms:
            candidates.append((canonical, canonical, form, "approved_alias"))
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for canonical, entity_id, form, source in candidates:
        if not _usable_entity_form(form):
            continue
        normalized = _entity_normalize(form)
        if normalized and normalized in compact_query:
            identity = (_entity_normalize(canonical), normalized)
            if identity in seen:
                continue
            seen.add(identity)
            found.append({
                "id": str(entity_id)[:120],
                "canonical_name": str(canonical)[:160],
                "matched_form": str(form)[:160],
                "match_source": source,
            })
    return sorted(found, key=lambda row: (-len(_entity_normalize(row["matched_form"])), row["canonical_name"]))[:8]


def entity_anchor_adjustment(item: dict[str, Any], anchors: list[dict[str, str]]) -> tuple[float, dict[str, Any]]:
    """Softly prioritize exact entity evidence; do not impose an item-count cap.

    A matching entity receives a substantial ranking bonus. A candidate carrying
    only a different concrete entity is gently demoted rather than rejected: it
    may still be a valid shared rule, and semantic admission remains the final
    relevance judge.
    """
    if not anchors:
        return 0.0, {"required": False, "matched": False, "reason": "本次问题没有明确命名可验证实体；不按实体过滤。"}
    item_names = list(dict.fromkeys(_entity_names(item.get("entities") or [])))
    text = _entity_normalize(str(item.get("text") or item.get("content") or ""))
    anchor_forms = {
        _entity_normalize(value)
        for anchor in anchors for value in (anchor.get("canonical_name"), anchor.get("matched_form"))
        if _entity_normalize(value)
    }
    matched = []
    for anchor in anchors:
        forms = [_entity_normalize(anchor.get("canonical_name")), _entity_normalize(anchor.get("matched_form"))]
        if any(form and (
            any(form == _entity_normalize(name) or (len(form) >= 4 and form in _entity_normalize(name)) for name in item_names)
            or form in text
        ) for form in forms):
            matched.append(anchor.get("canonical_name") or anchor.get("matched_form"))
    concrete_names = [name for name in item_names if _usable_entity_form(name)]
    conflicts = [name for name in concrete_names if _entity_normalize(name) not in anchor_forms]
    if matched:
        return 0.18, {
            "required": True, "matched": True,
            "matched_anchors": list(dict.fromkeys(str(x) for x in matched if x)),
            "candidate_entities": concrete_names[:12],
            "reason": "候选含本次明确提到的实体或已批准别名，优先保留。",
        }
    if conflicts:
        return -0.12, {
            "required": True, "matched": False,
            "candidate_entities": concrete_names[:12],
            "conflicting_entity_names": conflicts[:12],
            "reason": "候选只带有其他具体实体；降序但不直接删除，仍由语义、时间和来源规则复核。",
        }
    return 0.0, {
        "required": True, "matched": False,
        "candidate_entities": concrete_names[:12],
        "reason": "候选未标具体实体，保留给语义与来源规则判断，避免漏掉通用但相关的规则。",
    }


def trace_result_summaries(results: list[dict[str, Any]], *, limit: int | None = TRACE_RESULT_LIMIT) -> list[dict[str, Any]]:
    """Keep a bounded, display-only receipt of the memories already returned.

    This never performs another lookup.  It records enough provenance to make a
    recall trace understandable without copying full memories into the audit.
    """
    summaries: list[dict[str, Any]] = []
    selected = results if limit is None else results[:max(0, int(limit))]
    for item in selected:
        metadata = item.get("metadata") or {}
        scores = item.get("scores") or {}
        text = " ".join(str(item.get("text") or item.get("content") or "").split())
        summaries.append({
            "id": str(item.get("id") or item.get("chunk_id") or "")[:80],
            "type": str(item.get("type") or item.get("memory_type") or "unknown")[:40],
            "text_preview": text[:TRACE_RESULT_PREVIEW_CHARS],
            "document_id": str(item.get("document_id") or "")[:120],
            "source": str(metadata.get("source") or "")[:80],
            "mentioned_at": item.get("mentioned_at") or item.get("occurred_start"),
            "score": scores.get("final") or item.get("score"),
            "admission": dict(metadata.get("_ccy_admission") or {}),
            "entities": _entity_names(item.get("entities") or [])[:12],
            "entity_anchor": dict(metadata.get("_ccy_entity_anchor") or {}),
            "graph_evidence": dict(metadata.get("_ccy_graph_evidence") or {}),
        })
    return summaries


def foreground_reranker_budget(remaining_seconds: float, config: dict[str, Any]) -> float:
    """Bound the optional local reranker without stealing receipt delivery.

    Official Bank/graph retrieval plus deterministic source, entity, relation
    and time governance remain authoritative.  The cross-encoder only refines
    ordering, so it receives a small independent budget and always leaves time
    to construct and transport the auditable Controller receipt.
    """
    remaining = max(0.0, float(remaining_seconds or 0.0))
    reserve = max(0.5, float(config.get("rerankerReceiptReserveSeconds", 1.5)))
    cap = max(0.0, float(config.get("rerankerForegroundMaxSeconds", 3.0)))
    available = max(0.0, remaining - reserve)
    return round(min(cap, available), 3)


def foreground_semantic_reranker_enabled(config: dict[str, Any]) -> bool:
    """The CPU cross-encoder is benchmark-gated and off in live Hook traffic.

    Production already uses Hindsight hybrid retrieval plus RRF, entity,
    source, time and graph governance.  A local model may be re-enabled for a
    measured experiment, but never enters the interactive path implicitly.
    """
    return bool(config.get("foregroundSemanticRerankerEnabled", False))


# PyTorch inference cannot be cancelled once a worker thread has entered the
# model.  Creating one executor per request therefore turns harmless foreground
# timeouts into an unbounded CPU backlog during real consecutive prompts.  A
# process-wide singleflight keeps at most one optional reranker inference alive;
# Bank, graph, source and time governance continue immediately when it is busy.
_RERANK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hindsight-reranker")
_RERANK_SINGLEFLIGHT_LOCK = threading.Lock()
_RERANK_INFLIGHT = None


def deadline_cross_encoder_scores(
    query: str,
    texts: list[str],
    *,
    model_name: str,
    batch_size: int,
    timeout: float,
) -> tuple[list[float | None], str | None]:
    """Run the optional reranker without accumulating timed-out work."""
    global _RERANK_INFLIGHT
    count = len(texts)
    with _RERANK_SINGLEFLIGHT_LOCK:
        previous = _RERANK_INFLIGHT
        if previous is not None and not previous.done():
            return [None] * count, "skipped: optional local cross-encoder inference already running"
        if previous is not None:
            # Consume a completed/failed result solely to release references;
            # it belongs to an older query and is never reused for this one.
            try:
                previous.result(timeout=0)
            except Exception:
                pass
        future = _RERANK_EXECUTOR.submit(
            cross_encoder_scores, query, texts,
            model_name=model_name, batch_size=batch_size,
        )
        _RERANK_INFLIGHT = future
    try:
        scores, error = future.result(timeout=max(0.01, float(timeout)))
        return scores, error
    except FuturesTimeoutError:
        return [None] * count, "skipped: optional local cross-encoder exceeded its foreground sub-budget"
    except Exception as error:
        return [None] * count, f"skipped: optional local cross-encoder failed: {error!r}"
    finally:
        if future.done():
            with _RERANK_SINGLEFLIGHT_LOCK:
                if _RERANK_INFLIGHT is future:
                    _RERANK_INFLIGHT = None


def _claim_stage_for_result(item: dict[str, Any], contract: QueryContract) -> str | None:
    """Conservatively classify a returned record for the delivery receipt.

    This is not a second ranking decision.  It only makes the controller's
    already-admitted result set auditable as distinct claims, preserving the
    source record IDs needed for the Hook to report real delivery later.
    """
    metadata = item.get("metadata") or {}
    explicit = str(metadata.get("evolution_stage") or metadata.get("stage_id") or "").strip()
    if explicit:
        return explicit
    text = str(item.get("text") or item.get("content") or "")
    compact = text.casefold()
    if contract.workload != "evolution":
        return None
    if any(term in text for term in ("官方", "初始", "最初", "早期", "基础版")):
        return "origin"
    if any(term in compact for term in ("hook", "controller", "adapter")):
        return "hooks_controller"
    if any(term in text for term in ("当前", "现行", "正式")) or "agent memory os" in compact:
        return "current"
    if any(term in text for term in ("图谱", "星座", "实体", "观察", "心智模型")):
        return "graph_constellation"
    return "stage"


def build_claim_receipt(query: str, results: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    """Project controller results into source-preserving, delivery-neutral claims.

    Crucially, the Controller has *not* injected anything.  The Hook later
    attaches `actual_hook_injected_claim_ids` after it has actually placed the
    selected records in Codex's context.  Keeping that boundary explicit fixes
    the former UI ambiguity between search, admission and real delivery.
    """
    contract = QueryContract.from_request(query, execution_id=str(plan.get("execution_id") or plan.get("query_id") or ""))
    ledger = ClaimLedger(contract)
    for item in results:
        metadata = item.get("metadata") or {}
        text = str(item.get("text") or item.get("content") or "").strip()
        record_id = str(item.get("id") or item.get("chunk_id") or "").strip()
        if not text or not record_id:
            continue
        graph = dict(metadata.get("_ccy_graph_evidence") or {})
        ledger.add(EvidenceCandidate(
            record_id=record_id,
            text=text,
            channel=("official_graph" if graph.get("endpoint") else "official_recall"),
            entities=tuple(_entity_names(item.get("entities") or [])),
            stage=_claim_stage_for_result(item, contract),
            authority=0.9 if str(metadata.get("source") or "").endswith("authority") else 0.7,
            tokens=max(1, query_token_count(text)),
            relation_path=tuple(str(x) for x in (graph.get("path") or []) if str(x)),
            occurred_at=str(item.get("mentioned_at") or item.get("occurred_start") or "") or None,
            source_uri=str(metadata.get("source_uri") or "") or None,
        ))
    claims = []
    for claim in ledger.claims():
        claims.append({
            "claim_id": claim.claim_key,
            "statement_preview": claim.statement[:360],
            "slot": claim.slot,
            "evidence_ids": list(claim.evidence_ids),
            "channels": list(claim.channels),
            "entities": list(claim.entities),
            "relation_paths": [list(path) for path in claim.relation_paths],
            "authority": claim.authority,
        })
    coverage = ledger.coverage()
    return {
        "schema": "ham.controller_claim_receipt.v1",
        "execution_id": contract.execution_id,
        "query_contract": {
            "workload": contract.workload,
            "required_slots": list(contract.required_slots),
            "canonical_entities": list(contract.canonical_entities),
            "required_relation_closure": contract.required_relation_closure,
        },
        "candidate_record_count": len(results),
        "admitted_claim_count": len(claims),
        "claims": claims,
        "coverage": {
            "status": coverage.status,
            "covered_slots": list(coverage.covered_slots),
            "missing_slots": list(coverage.missing_slots),
        },
        # This intentionally remains empty in Controller output.  The Hook is
        # the only component allowed to state what crossed into agent context.
        "actual_hook_injected_claim_ids": [],
    }


# Retrieval is intentionally broad; prompt injection is not.  This second
# admission layer applies to every controller client (Codex, Hermes and
# OpenClaw) before results leave the local service.
ADMISSION_GENERIC_TERMS = {
    "这个", "那个", "这些", "问题", "什么", "怎么", "如何", "为什么", "是不是",
    "没有", "还有", "很多", "一些", "之前", "现在", "历史", "聊天", "记录", "对应",
    "实际", "当前", "到底", "因为", "看到", "压根", "一个", "多找", "信息", "我们",
}
ADMISSION_SIGNAL_TERMS = (
    "hindsight", "agentmemory", "codex", "openclaw", "hermes", "trainer", "小黛", "飞书", "wps",
    "记忆", "注入", "召回", "检索", "读取", "回答", "相关", "上下文", "观察", "心智模型", "实体",
    "测试", "证据", "时间线", "项目", "方案", "预算", "报价", "费用", "图表", "表格", "word", "excel", "ppt",
    # Structural-delivery anchors are short in Chinese but highly meaningful
    # when they co-occur.  They prevent a real dependency such as “岗位三”
    # from being rejected merely because the stored memory omits the school
    # name repeated in the user request.
    "岗位", "角色", "方向", "职责", "模块", "功能", "课程", "实施", "验收", "成果", "联动", "关联", "完整",
    # These are concrete subject anchors, not generic stop words.  Retaining
    # them prevents generic architecture/mental-model memories from winning a
    # query whose decisive subject is health, a visual trace, a word meaning,
    # or a recall-limit policy.
    "视觉", "链路", "健康", "医疗", "单词", "词义", "上限", "截断", "固定",
)

def _admission_compact(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _query_admission_signals(query: str) -> list[str]:
    compact = _admission_compact(query)
    signals = [term for term in ADMISSION_SIGNAL_TERMS if term in compact]
    for term in re.findall(r"[a-z][a-z0-9_.-]{2,}", compact):
        if term not in signals and term not in ADMISSION_GENERIC_TERMS:
            signals.append(term)
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", compact))
    for index in range(max(0, len(cjk) - 2)):
        term = cjk[index:index + 3]
        if any(generic in term for generic in ADMISSION_GENERIC_TERMS):
            continue
        if term not in signals:
            signals.append(term)
    return signals[:32]


def broad_period_inventory_month(query: str) -> int | None:
    """Return month only for a *scope-free* calendar completeness request.

    Calendar membership is a valid relevance signal only when the user asks to
    check an entire period rather than a named subject inside that period.  The
    latter must still pass ordinary topical admission: “8月的 Word 修改记录”
    must not pull every August memory.

    Natural Chinese completeness requests often omit formal review verbs, e.g.
    “8月份还有没有落下的事情，你再看看”.  We recognize them by removing the
    calendar/completeness envelope and requiring no subject residue.  This is a
    general structural rule, not a phrase-specific shortcut.
    """
    text = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    match = re.search(r"(?:20\d{2}年)?(\d{1,2})月(?:份)?", text)
    if not match:
        return None
    month = int(match.group(1))
    if not 1 <= month <= 12:
        return None
    review = any(marker in text for marker in ("梳理", "回顾", "总结", "列出", "归类", "分类", "盘点"))
    completeness = any(marker in text for marker in (
        "都干了什么", "都做了什么", "全部", "所有", "条目", "有没有", "有无",
        "落下", "漏掉", "遗漏", "找全", "没完成", "未完成",
    ))
    if not (review or completeness):
        return None
    # Remove only structural phrases. Any remaining topic-like content means
    # this is a scoped query and regular proposition relevance must decide.
    residual = text
    residual = re.sub(r"(?:20\d{2}年)?\d{1,2}月(?:份)?", "", residual)
    structural_phrases = (
        "都干了什么", "都做了什么", "还有没有", "没完成", "未完成", "有没有", "没错", "帮我",
        "看看", "一下", "全部", "所有", "条目", "梳理", "回顾", "总结", "列出", "归类", "分类", "盘点",
        "落下", "漏掉", "遗漏", "找全", "事情", "的事", "内容", "工作", "任务", "还有", "有无", "是否",
        "好的", "请", "你", "再", "下", "都", "吗", "呢", "嗯", "的",
    )
    for phrase in sorted(structural_phrases, key=len, reverse=True):
        residual = residual.replace(phrase, "")
    residual = re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", residual)
    # A named entity, English product/file token, or other content remains:
    # retain ordinary relevance gating instead of elevating the whole month.
    return month if not residual else None


def explicit_identity_endpoints(query: str) -> list[str]:
    """Return user-named endpoints for an identity/alias question.

    This is intentionally a small structural guard, not a company-name list.
    It applies only when the user asks whether entities are the same, what a
    canonical name is, or whether a spelling is an alias.  In that setting a
    candidate that merely co-mentions one company is not useful evidence.
    """
    raw = full_prompt_text(query)
    identity_markers = ("是不是同一个", "是否同一个", "是否同一", "不是同一个", "实体别名", "别名", "全称", "简称", "更名")
    if not any(marker in raw for marker in identity_markers):
        return []
    split = re.sub(
        r"(?:和|与|及|、|是|不是|是否|请|把|将|在|对|从|到|关于|为什么|如何|什么|关联|关系|联动|全称|别名|简称|更名|方案|记录|历史|当前|相关|不要|完整|一起|之间|以及|同时|并且|的|地|得)",
        " ", raw,
    )
    stop = {"用户", "问题", "内容", "信息", "记忆", "图谱", "实体", "要求", "应该", "什么", "怎样", "为什么", "这个", "那个", "所有", "全部", "当前", "历史", "方案", "观察", "心智模型", "正确身份", "相关偏好", "证据来源", "冲突记录", "治理", "字符串替换", "连成", "记录"}
    values = [
        value for value in re.findall(r"[\u4e00-\u9fff]{2,16}", split)
        if value not in stop and _usable_entity_form(value)
    ]
    return list(dict.fromkeys(values))[:4]


def personal_identity_correction_alignment(query: str, candidate: str) -> dict[str, Any]:
    """Recognize an explicit ASR/spelling correction without treating it as an alias.

    This covers a narrow fact pattern: the user names a possibly misrecognized
    form and asks for the correct personal name.  It deliberately requires the
    candidate to state both the error relation and the canonical-name outcome;
    conditional metadata observations cannot become an identity answer.
    """
    question = re.sub(r"\s+", "", full_prompt_text(query))
    text = re.sub(r"\s+", "", str(candidate or ""))
    wrong_match = re.search(r"(?:写成|误写为|识别为|叫做|名字是)\s*([\u4e00-\u9fff]{2,8})", question)
    wrong_form = wrong_match.group(1) if wrong_match else ""
    requested = (
        bool(wrong_form)
        and any(marker in question for marker in ("姓名", "名字", "真名"))
        and any(marker in question for marker in ("语音识别", "错字", "误写", "转写错误"))
    )
    correction_hits = [
        marker for marker in ("语音识别", "错字", "误写", "转写错误", "识别错误")
        if marker in text
    ]
    canonical_hits = [
        marker for marker in ("正确姓名", "真实姓名", "姓名是", "姓名为", "不是用户姓名", "并非用户姓名")
        if marker in text
    ]
    uncertain = any(marker in text for marker in ("若身份证", "如果身份证", "可能是", "待确认", "请确认"))
    qualified = bool(
        requested
        and wrong_form in text
        and correction_hits
        and canonical_hits
        and not uncertain
    )
    return {
        "qualified": qualified,
        "wrong_form": wrong_form,
        "correction_hits": correction_hits,
        "canonical_hits": canonical_hits,
        "uncertain": uncertain,
    }


def long_task_delivery_alignment(query: str, candidate: str) -> dict[str, Any]:
    """Match the durable long-task delivery contract without broad task leakage."""
    question = re.sub(r"\s+", "", full_prompt_text(query))
    text = re.sub(r"\s+", "", str(candidate or ""))
    requested = "长任务" in question and any(
        marker in question for marker in ("推进", "交付", "怎么做", "如何", "应该")
    )
    chain = {
        "similar_case_scan": any(marker in text for marker in ("同类扫描", "同类问题")),
        "root_cause": "根因" in text or "根因修复" in text,
        "regression": any(marker in text for marker in ("回归验证", "回归复测")),
        "autonomous_progress": any(marker in text for marker in ("主动报告阶段", "当前动作", "剩余工作", "最终复核")),
    }
    qualified = requested and "长任务" in text and sum(chain.values()) >= 3
    return {
        "qualified": qualified,
        "requested": requested,
        "chain": chain,
        "chain_strength": sum(chain.values()),
    }


def promote_long_task_delivery_contract(
    query: str,
    ranked: list[dict[str, Any]],
    admitted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep one minimum-evidence closure for an explicit long-task request."""
    delivery_matches: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for row in ranked:
        item = dict(row.get("item") or {})
        if not item or source_class(item) in {"raw_evidence", "direct_policy"}:
            continue
        alignment = long_task_delivery_alignment(
            query, str(item.get("text") or item.get("content") or "")
        )
        if alignment.get("qualified"):
            delivery_matches.append((
                float(alignment.get("chain_strength") or 0) * 10.0
                + float(row.get("score") or 0),
                item,
                alignment,
            ))
    if not delivery_matches:
        return admitted, rejected
    _, item, alignment = max(delivery_matches, key=lambda value: value[0])
    key = result_key(item)
    if key in {result_key(value) for value in admitted}:
        return admitted, rejected
    metadata = dict(item.get("metadata") or {})
    metadata["_ccy_admission"] = {
        "decision": "qualified_long_task_delivery_contract",
        "reason": "当前问题明确询问长任务推进与交付；候选同时给出同类扫描、根因修复、回归验证和进度透明要求。",
        "long_task_delivery": alignment,
        "policy": RELEVANCE_POLICY,
        "fixed_item_limit": False,
    }
    item["metadata"] = metadata
    return admitted + [item], [value for value in rejected if result_key(value) != key]


def is_entity_governance_query(query: str) -> bool:
    """Whether the question is about the *governance mechanism* itself.

    This differs from a named-entity identity check.  There may be no concrete
    company/person endpoint to require, but records must still demonstrate the
    alias/canonical-name/evidence relation rather than merely contain a broad
    word such as "governance".
    """
    compact = re.sub(r"\s+", "", full_prompt_text(query))
    return sum(marker in compact for marker in ("实体", "别名", "全称", "规范名", "实体治理", "冲突记录")) >= 2


def is_injection_coverage_query(query: str) -> bool:
    """Recognize the durable no-fixed-cap / coverage-closure policy question."""
    compact = re.sub(r"\s+", "", full_prompt_text(query))
    return "注入" in compact and sum(marker in compact for marker in ("固定", "条数", "上限", "相关性", "覆盖", "闭包", "闭合")) >= 2


def admit_controller_results(query: str, ranked: list[dict[str, Any]], plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply source-aware relevance to every candidate; never count-cap it."""
    # The ranking layer historically passed ``{"item": memory, ...}`` rows,
    # while the live merged response deliberately exposes plain memory rows.
    # Normalise at this boundary so the same admission policy governs both
    # paths.  Without it, wiring this function into the real post-merge path
    # raised KeyError("item") and turned a relevance fix into a 502.
    ranked = [
        row if isinstance(row, dict) and isinstance(row.get("item"), dict)
        else {"item": dict(row)}
        for row in (ranked or []) if isinstance(row, dict)
    ]
    # Admission is downstream of the resolved Full Prompt.  Using only the
    # final user line here would undo context-aware retrieval and reject a
    # candidate whose subject appears in the preceding handoff context.
    intent = full_prompt_text(query)
    deep = bool(
        plan.get("explicit_deep_recall")
        or plan.get("primary_shape") in {"inventory", "synthesis", "timeline", "system_map"}
    )
    admitted, rejected = relevance_admit_items(
        intent,
        [dict(row["item"]) for row in ranked],
        deep=deep,
        preserve_controller_decision=False,
    )
    # Keep the Controller's claim receipt aligned with the Hook's final
    # post-processing.  A historical prompt echo that has no independent
    # answer-side evidence must be rejected here, before it can be counted as
    # a Controller-qualified claim and later appear as ``not_delivered`` when
    # the Hook correctly removes it.  The shared helper preserves substantive
    # governance rules (priority/scope/expiry markers) while dropping only
    # question-only echoes.
    admitted, question_echo_rejected = filter_question_echo_evidence(intent, admitted)
    rejected.extend(question_echo_rejected)
    # Evolution queries ask for a connected sequence, so a record describing
    # the same named system at a different stage may have low lexical overlap
    # with the final wording. Promote only the conjunction of an explicit
    # user-written subject and a concrete stage/current-state marker. This is
    # a general temporal-relation rule, not a product-specific quantity bump.
    if evolution_question(intent):
        known = {result_key(item) for item in admitted}
        promoted: list[dict[str, Any]] = []
        for row in ranked:
            item = dict(row["item"])
            key = result_key(item)
            if key in known or source_class(item) == "raw_evidence":
                continue
            text = str(item.get("text") or item.get("content") or "")
            if not is_evolution_stage_candidate(intent, text):
                continue
            metadata = dict(item.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "qualified_evolution_stage",
                "reason": "用户请求从起点到当前的演进；候选同时命中用户明确对象和可验证的阶段/当前状态证据。",
                "evolution_anchors": evolution_anchor_terms(intent),
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
            }
            item["metadata"] = metadata
            promoted.append(item)
            known.add(key)
        if promoted:
            promoted_keys = {result_key(item) for item in promoted}
            rejected = [item for item in rejected if result_key(item) not in promoted_keys]
            admitted.extend(promoted)
        # A broad deep read can return dozens of records that merely contain
        # the system name.  Do not make a fixed Top-K out of that abundance;
        # retain every independent origin/change/current proof and reject only
        # records that cannot occupy any evolution slot on their own.
        evolution_retained: list[dict[str, Any]] = []
        evolution_rejected: list[dict[str, Any]] = []
        for item in admitted:
            alignment = evolution_evidence_alignment(
                intent, str(item.get("text") or item.get("content") or "")
            )
            if alignment["qualified"]:
                evolution_retained.append(item)
                continue
            blocked = dict(item)
            metadata = dict(blocked.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or blocked.get("admission") or {})
            admission.update({
                "decision": "rejected_evolution_topic_only",
                "reason": "本题要求系统从起点到当前的演进；候选虽可能提及主题，但未独立证明起点/基础、具体变迁或当前架构，不能占用演进证据槽位。",
                "evolution_alignment": alignment,
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
            })
            metadata["_ccy_admission"] = admission
            blocked["metadata"] = metadata
            evolution_rejected.append(blocked)
        admitted = evolution_retained
        rejected.extend(evolution_rejected)
    # A bounded question about a named component's introduction/upgrade stage
    # has the same evidence need as a timeline *slot*, but does not necessarily
    # mention an origin-to-present span.  The ordinary lexical gate is too
    # strict here: an authoritative record can explain the component's routing
    # or recall effect without repeating the user's words "what problem did it
    # solve".  Promote only records that independently name the component and
    # prove both an operational mechanism and an outcome/guardrail.
    if named_mechanism_stage_question(intent):
        # Current-policy weak background may already be in admitted. Upgrade
        # its relationship receipt only when the same stage verifier proves
        # independent mechanism and outcome evidence; never reopen rejections.
        for index, existing in enumerate(admitted):
            metadata = dict(existing.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or {})
            if admission.get("policy") != RELEVANCE_POLICY or admission.get("relevance_strength") != "weak":
                continue
            alignment = mechanism_stage_evidence_alignment(intent, str(existing.get("text") or existing.get("content") or ""))
            if not alignment["qualified"]:
                continue
            admission.update(decision="qualified_mechanism_stage", relevance_strength="direct",
                             mechanism_stage_alignment=alignment,
                             reason="同一命名机制已独立说明运行关系及结果/验证；升级关系准入回执，不宣称事实或宿主送达已核验。")
            metadata["_ccy_admission"] = admission
            admitted[index] = {**existing, "metadata": metadata}
        known = {result_key(item) for item in admitted}
        promoted: list[dict[str, Any]] = []
        for row in ranked:
            item = dict(row["item"])
            key = result_key(item)
            if key in known or source_class(item) == "raw_evidence":
                continue
            alignment = mechanism_stage_evidence_alignment(
                intent, str(item.get("text") or item.get("content") or "")
            )
            if not alignment["qualified"]:
                continue
            metadata = dict(item.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "qualified_mechanism_stage",
                "reason": "该候选命中用户明确点名的机制，并独立说明其运行/召回机制及结果、边界或验证；可作为阶段证据注入。",
                "mechanism_stage_alignment": alignment,
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
            }
            item["metadata"] = metadata
            promoted.append(item)
            known.add(key)
        if promoted:
            promoted_keys = {result_key(item) for item in promoted}
            rejected = [item for item in rejected if result_key(item) not in promoted_keys]
            admitted.extend(promoted)
    # A terse request such as “周报也想办法预防再出问题” names a concrete
    # workflow but can share only two Chinese characters with its history.
    # Once the candidate pool proves that workflow has a failure/repair chain,
    # do not let a high semantic score re-admit a *different* workflow (for
    # example 晨报) simply because it also discusses preventing failures.  The
    # common relevance gate remains in force; this is an additional subject
    # closure guard, not a topic-specific result cap.
    recurrence_by_key = {
        result_key(dict(row["item"])): recurrence_prevention_alignment(intent, str((row["item"] or {}).get("text") or (row["item"] or {}).get("content") or ""))
        for row in ranked
    }
    if any(value.get("qualified") for value in recurrence_by_key.values()):
        retained: list[dict[str, Any]] = []
        recurrence_rejected: list[dict[str, Any]] = []
        for item in admitted:
            evidence = recurrence_by_key.get(result_key(item)) or recurrence_prevention_alignment(
                intent, str(item.get("text") or item.get("content") or "")
            )
            if evidence.get("qualified"):
                retained.append(item)
                continue
            metadata = dict(item.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected_recurrence_subject_mismatch",
                "reason": "本题已定位到同一工作流的故障/修复链；候选虽谈及失败或防错，但未命中该工作流主体，不能跨流程注入。",
                "recurrence_prevention_alignment": evidence,
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            recurrence_rejected.append(item)
        admitted = retained
        rejected.extend(recurrence_rejected)
        # A candidate can already have failed the ordinary relevance gate
        # before this workflow-closure guard runs.  Preserve the more useful
        # cause in its receipt as well: the status page must explain that it
        # was excluded because it belongs to a different workflow, rather
        # than leaving the user with an opaque generic "rejected" label.
        for item in rejected:
            evidence = recurrence_by_key.get(result_key(item)) or recurrence_prevention_alignment(
                intent, str(item.get("text") or item.get("content") or "")
            )
            if evidence.get("qualified"):
                continue
            metadata = dict(item.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected_recurrence_subject_mismatch",
                "reason": "本题已定位到同一工作流的故障/修复链；候选虽谈及失败或防错，但未命中该工作流主体，不能跨流程注入。",
                "recurrence_prevention_alignment": evidence,
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
    # An identity/alias question needs evidence *about the relation*, not every
    # historical item that happens to mention one endpoint.  This is a semantic
    # admission rule rather than an item limit: any number of records can pass
    # when each ties a named endpoint to an identity/canonical-name relation.
    identity_correction = personal_identity_correction_alignment(intent, "")
    if identity_correction.get("wrong_form"):
        known = {result_key(item) for item in admitted}
        promoted: list[dict[str, Any]] = []
        seen_wrong_forms: set[str] = set()
        for row in ranked:
            item = dict(row["item"])
            key = result_key(item)
            if key in known or source_class(item) in {"raw_evidence", "direct_policy"}:
                continue
            alignment = personal_identity_correction_alignment(
                intent, str(item.get("text") or item.get("content") or "")
            )
            wrong_form = str(alignment.get("wrong_form") or "")
            if not alignment.get("qualified") or wrong_form in seen_wrong_forms:
                continue
            metadata = dict(item.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "qualified_identity_correction",
                "reason": "候选明确把本轮点名的语音/拼写错误与规范姓名关联，并排除了错误别名。",
                "identity_correction": alignment,
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
            }
            item["metadata"] = metadata
            promoted.append(item)
            known.add(key)
            seen_wrong_forms.add(wrong_form)
        if promoted:
            promoted_keys = {result_key(item) for item in promoted}
            rejected = [item for item in rejected if result_key(item) not in promoted_keys]
            admitted.extend(promoted)
    admitted, rejected = promote_long_task_delivery_contract(
        intent, ranked, admitted, rejected
    )
    endpoints = explicit_identity_endpoints(intent)
    # A named-system taxonomy question explicitly asks for definitions,
    # aliases, upstream/downstream roles and non-equivalence boundaries.  It
    # is broader than a two-endpoint identity check; applying the latter here
    # would discard valid role/architecture records that do not repeat an
    # alias phrase.  Its dedicated taxonomy gate already requires structural
    # role/boundary evidence, so keep the identity guard for true identity
    # lookups only.
    if endpoints and not named_system_taxonomy_question(intent):
        relation_terms = ("独立", "同一", "不是", "别名", "全称", "统一", "简称", "更名", "主体", "身份")
        retained: list[dict[str, Any]] = []
        identity_rejected: list[dict[str, Any]] = []
        for item in admitted:
            text = _entity_normalize(str(item.get("text") or item.get("content") or ""))
            endpoint_hits = [endpoint for endpoint in endpoints if _entity_normalize(endpoint) in text]
            relation_hits = [term for term in relation_terms if term in text]
            if endpoint_hits and relation_hits:
                retained.append(item)
                continue
            metadata = dict(item.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected_identity_relation",
                "reason": "本题要求实体同一性/别名关系；候选只共现名称，未同时证明命名端点与身份、别名、全称或独立主体关系。",
                "identity_endpoints": endpoints,
                "identity_endpoint_hits": endpoint_hits,
                "identity_relation_hits": relation_hits,
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            identity_rejected.append(item)
        admitted = retained
        rejected.extend(identity_rejected)
    # A mechanism-level alias-governance question has no named endpoint, so
    # the identity guard above must not reduce it to unrelated policy snippets.
    # Promote every structured candidate that actually establishes at least two
    # governance anchors.  This is proposition gating, not a result-count
    # override: records on canonical names, aliases, evidence provenance,
    # conflict/merge handling or entity normalization all remain eligible;
    # generic "data governance" records do not.
    if is_entity_governance_query(intent):
        governance_anchors = ("实体", "别名", "全称", "规范名", "归并", "合并", "证据", "冲突", "来源", "字符串替换", "命名")
        known = {result_key(item) for item in admitted}
        promoted: list[dict[str, Any]] = []
        for row in ranked:
            item = dict(row["item"])
            key = result_key(item)
            if key in known or source_class(item) in {"raw_evidence", "direct_policy"}:
                continue
            text = _entity_normalize(str(item.get("text") or item.get("content") or ""))
            hits = sorted({anchor for anchor in governance_anchors if anchor in text})
            if len(hits) < 2:
                continue
            metadata = dict(item.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "qualified_entity_governance_relation",
                "reason": "本题询问实体别名治理机制；该候选同时说明命名/别名与证据、归并或冲突处理关系。",
                "entity_governance_anchors": hits,
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
            }
            item["metadata"] = metadata
            promoted.append(item)
            known.add(key)
        if promoted:
            promoted_keys = {result_key(item) for item in promoted}
            rejected = [item for item in rejected if result_key(item) not in promoted_keys]
            admitted.extend(promoted)
    # The question "do not use a fixed injection count" is a current durable
    # policy, not merely a historical description of an older 4/6/8 rule.
    # Admit every record that proves the coverage/relevance/closure mechanism;
    # favour current-policy language so an old implementation note can only be
    # presented as historical context, never as the operative instruction.
    if is_injection_coverage_query(intent):
        anchors = ("注入", "固定", "条数", "上限", "相关性", "覆盖", "闭包", "闭合", "关联")
        current_markers = ("不设", "反对", "不靠", "覆盖完整性", "停止标准")
        known = {result_key(item) for item in admitted}
        promoted: list[dict[str, Any]] = []
        for row in ranked:
            item = dict(row["item"])
            key = result_key(item)
            if key in known or source_class(item) == "raw_evidence":
                continue
            text = _entity_normalize(str(item.get("text") or item.get("content") or ""))
            hits = sorted({anchor for anchor in anchors if anchor in text})
            if len(hits) < 3:
                continue
            metadata = dict(item.get("metadata") or {})
            current = any(marker in text for marker in current_markers)
            metadata["_ccy_admission"] = {
                "decision": "qualified_injection_coverage_policy",
                "reason": "本题要求取消固定条数，以相关性、覆盖完整性和关联闭包决定注入；该候选直接说明该机制。",
                "injection_coverage_anchors": hits,
                "current_policy_language": current,
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
            }
            item["metadata"] = metadata
            # Preserve every relevant history item, but rank the operative rule
            # first so the injected context cannot accidentally revive the old
            # fixed-count implementation.
            item.setdefault("scores", {})["final"] = float((item.get("scores") or {}).get("final") or item.get("score") or 0.0) + (0.35 if current else 0.0)
            promoted.append(item)
            known.add(key)
        if promoted:
            promoted_keys = {result_key(item) for item in promoted}
            rejected = [item for item in rejected if result_key(item) not in promoted_keys]
            admitted.extend(sorted(promoted, key=lambda item: not bool(((item.get("metadata") or {}).get("_ccy_admission") or {}).get("current_policy_language"))))
    # For a topic-free calendar-period review, calendar membership *is* the
    # requested relevance criterion. Promote only timestamped structured-memory
    # candidates from that exact month; raw archives still require an explicit
    # provenance request. The existing adaptive token budget, deduplication and
    # governance stages still apply afterwards.
    requested_month = broad_period_inventory_month(intent)
    if plan.get("primary_shape") == "inventory" and requested_month is not None:
        known = {result_key(item) for item in admitted}
        promoted = []
        for row in ranked:
            item = dict(row["item"])
            key = result_key(item)
            if key in known or source_class(item) == "raw_evidence":
                continue
            timestamp = timestamp_value(item)
            if not timestamp or datetime.fromtimestamp(timestamp, timezone.utc).month != requested_month:
                continue
            metadata = dict(item.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "qualified",
                "reason": f"用户请求{requested_month}月的无主题全量回顾；候选具有同月时间戳，按时间范围准入。",
                "source_class": source_class(item),
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
                "temporal_inventory_match": {"month": requested_month},
            }
            item["metadata"] = metadata
            promoted.append(item)
            known.add(key)
        if promoted:
            rejected = [item for item in rejected if result_key(item) not in known]
            admitted.extend(promoted)
    return admitted, rejected

def fast_recovery_admission(
    query: str, items: list[dict[str, Any]], plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the same Controller admission to derived-cache recovery rows."""
    ranked = [
        {
            "item": dict(item or {}),
            "score": float(
                ((item.get("scores") or {}).get("final"))
                or item.get("score")
                or 0.0
            ),
        }
        for item in items or []
        if isinstance(item, dict)
    ]
    return admit_controller_results(query, ranked, plan)


def contextual_guidance_match(task_context: dict[str, Any] | None, text: str) -> bool:
    """Match a terse follow-up to its evidenced acceptance task context."""
    context=dict(task_context or {})
    requirements=set(context.get("guidance_requirements") or [])
    full_prompt=re.sub(r"\s+", "", str(context.get("full_prompt") or "")).casefold()
    body=re.sub(r"\s+", "", str(text or "")).casefold()
    acceptance=bool({"acceptance_evidence","execution_responsibility"}&requirements)
    system_scope=any(term in full_prompt for term in ("hindsight","记忆","召回","注入","状态页","修复"))
    action_chain=sum(term in body for term in ("真实入口","用户可见","最终结果","复测","回归","验收"))
    boundary=any(term in body for term in ("不能","不得","不应","证据","完成"))
    return bool(context.get("used_context")) and acceptance and system_scope and action_chain>=2 and boundary


def qualify_stable_guidance_observation(query: str, item: dict[str, Any], *, task_context: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """Admit a stable observation only when it changes *how* the task is done.

    This is intentionally narrower than an ordinary observation recall: a sidecar
    cannot turn a nearby personal-profile sentence into task context.  It must
    share a durable decision/quality topic with the query and preserve at least
    two independent anchors (or one Hindsight/memory anchor plus one durable
    decision anchor).  There is no item-count cap; all qualified sections still
    pass through the common dynamic token budget.
    """
    if str(item.get("type") or "") != "observation":
        return False, {"reason": "稳定侧车仅接收观察，不把事实或经历提升为通用偏好。"}
    text = re.sub(r"\s+", "", str(item.get("text") or item.get("content") or "")).casefold()
    compact_query = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    if not text or not compact_query:
        return False, {"reason": "问题或观察内容为空，不能证明稳定指导相关。"}
    # Named-system definition/relationship questions use a dedicated sidecar
    # gate.  A generic observation about reliability or collaboration must not
    # enter merely because it mentions “Agent” or “记忆”; it needs one of the
    # requested systems plus independent role/boundary evidence.
    if named_system_taxonomy_question(query):
        alignment = system_taxonomy_alignment(query, text)
        qualified = bool(alignment.get("qualified"))
        return qualified, {
            "shared_stable_anchors": alignment.get("shared_systems") or [],
            "specific_stable_relations": (alignment.get("role_markers") or []) + (alignment.get("boundary_markers") or []),
            "memory_anchor": bool(alignment.get("shared_systems")),
            "durable_anchor": bool(alignment.get("role_markers") or alignment.get("boundary_markers")),
            "reason": (
                "系统关系问题的观察侧车命中点名系统及其角色/边界证据，可补充定义与分层背景。"
                if qualified else
                "系统关系问题的观察没有同时命中点名系统和独立角色/边界证据，不注入。"
            ),
        }
    durable = (
        "偏好", "风格", "质量", "验收", "检查", "审计", "优化", "机制", "系统",
        "记忆", "召回", "注入", "hindsight", "关联", "完整", "遗漏", "一致", "风险",
        "决策", "判断", "方案", "架构", "学习", "解释", "交付", "测试", "验证",
    )
    query_hits = {term for term in durable if term in compact_query}
    shared = sorted(term for term in query_hits if term in text)
    asks_for_observation = "观察" in compact_query
    asks_for_mental_model = "心智模型" in compact_query
    # Naming a governed memory type is itself an explicit memory-domain
    # request.  The previous gate only treated Hindsight/记忆/召回/注入 as a
    # memory anchor, so a valid question such as “心智模型的准入标准是什
    # 么” caused every observation to be labelled “宽泛相似”.  This opens the
    # sidecar; the type-specific checks below still decide each item.
    explicit_guidance_request = asks_for_observation or asks_for_mental_model
    memory_anchor = explicit_guidance_request or any(
        term in compact_query and term in text for term in ("hindsight", "记忆", "召回", "注入")
    )
    durable_anchor = any(term in compact_query and term in text for term in (
        "质量", "验收", "检查", "审计", "优化", "完整", "遗漏", "一致", "风险", "决策", "判断", "架构", "方案",
    ))
    query_memory_scope = any(term in compact_query for term in (
        "hindsight", "记忆", "召回", "注入", "观察", "心智模型"
    ))
    specific_groups = (
        ("观察", ("观察",)),
        ("心智模型", ("心智模型",)),
        ("注入", ("注入", "召回", "hook", "retain")),
        ("质量", ("质量", "验收", "审计", "验证", "复查", "回归")),
        ("决策", ("决策", "判断", "边界", "取舍")),
        ("纠正", ("纠正", "当前来源", "附件", "截图")),
    )
    specific_hits = [name for name, variants in specific_groups if name in compact_query and any(v in text for v in variants)]
    # If the question is explicitly about memory behavior, a generic word such
    # as “决策” or “质量” is not enough: the observation must also carry the
    # same memory-domain anchor *and* one queried mechanism/quality relation.
    # This removes unrelated Trainer/project facts that happen to share one
    # broad quality word, without setting a fixed item limit.
    qualified = (
        bool(memory_anchor) and (bool(specific_hits) or len(shared) >= 3)
        if query_memory_scope else
        (len(shared) >= 2 or (memory_anchor and durable_anchor) or (asks_for_observation and (memory_anchor or durable_anchor)))
    )
    if contextual_guidance_match(task_context,text):
        qualified=True
    return qualified, {
        "shared_stable_anchors": shared,
        "specific_stable_relations": specific_hits,
        "memory_anchor": memory_anchor,
        "durable_anchor": durable_anchor,
        "reason": (
            "稳定观察同时命中当前问题的记忆/决策质量锚点，可补充做事标准而不替代当前事实。"
            if qualified else
            "稳定观察只存在宽泛相似，未同时证明与当前问题的记忆或决策质量关系，不注入。"
        ),
    }


def qualify_direct_policy(query: str, item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Admit direct user policies only when their own declared anchors match.

    A direct instruction is useful immediately but must never leak into an
    unrelated task merely because it contains broad words such as “quality”.
    """
    if str(item.get("type") or "") != "direct_policy":
        return False, {"reason": "不是直接政策侧车。"}
    from lib.guidance_provenance import guidance_source_decision
    source_review=guidance_source_decision(item)
    if not source_review['allowed']:
        return False,{'reason':'用户政策来源未通过核对：'+str(source_review['reason']),'source_review':source_review}
    metadata = dict(item.get("metadata") or {})
    if not metadata.get("direct_policy_gate") or metadata.get("policy_status") != "active_provisional":
        return False, {"reason": "直接政策未通过有效状态或来源校验。"}
    if explicit_user_source_request(query):
        return False, {
            "direct_policy_anchor_hits": [],
            "shared_stable_anchors": [],
            "reason": "当前问题要求用户原话、时间或来源；直接政策是加工后的规则，不可作为原话证据注入。",
        }
    compact_query = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    generic = {"这个", "那个", "这些", "问题", "什么", "怎么", "如何", "为什么", "是否", "是不是", "现在", "需要", "一下", "我们", "进行", "相关"}
    keywords = [str(x).casefold() for x in (metadata.get("direct_policy_keywords") or []) if len(str(x).strip()) >= 2]

    def exact_ascii_anchor(anchor: str, value: str) -> bool:
        # ``memory`` must not match merely because the user wrote
        # ``AgentMemory``.  Without token boundaries, an unrelated policy such
        # as “do not filter API keys in memory” leaks into an AgentMemory
        # architecture question.  Chinese anchors remain substring based,
        # while ASCII identifiers retain their word boundaries.
        if re.fullmatch(r"[a-z0-9_-]+", anchor):
            return bool(re.search(rf"(?<![a-z0-9_-]){re.escape(anchor)}(?![a-z0-9_-])", value))
        return anchor in value

    matched = sorted({x for x in keywords if x not in generic and exact_ascii_anchor(x, compact_query)})
    text = re.sub(r"\s+", "", str(item.get("text") or "")).casefold()
    # The keyword list is a candidate-generation aid, not a scope grant.  A
    # policy may contain a broad platform anchor (for example “长期记忆”) in
    # its wording while being explicitly limited to one concrete workflow
    # (for example Codex 文件夹迁移).  Before this guard such a row could leak
    # into an unrelated question when one broad keyword matched.  Require the
    # declared applicability scope itself to leave at least one meaningful
    # anchor in the resolved Full Prompt.  The scope is copied into the
    # sidecar text as “适用范围：…；边界：…”, and newer callers may also provide
    # it as structured metadata.  This is deliberately lexical and
    # scope-first; it does not impose an item count or a fixed product list.
    declared_scope = str(metadata.get("direct_policy_scope") or "").strip()
    if not declared_scope:
        scope_match = re.search(r"适用范围[:：](.*?)(?:边界[:：]|$)", text)
        declared_scope = str(scope_match.group(1) if scope_match else "").strip()
    if declared_scope:
        scope_compact = re.sub(r"\s+", "", declared_scope).casefold()
        # File/document nouns and workflow glue are not an applicability grant.
        # The previous bigram extractor treated ``教学`` inside a project name
        # as enough to admit a policy scoped to ``知识解释与教学内容`` and
        # treated ``Word 文档`` as enough to admit a school-leader-only policy.
        # Keep the scope check lexical and model-free, but extract meaningful
        # phrases first and require either one strong phrase or two independent
        # short domain phrases.  This generalises across projects without a
        # project-name allow-list.
        generic_scope_terms = {
            "适用", "范围", "所有", "涉及", "包括", "涵盖", "覆盖", "面向", "针对", "用于",
            "场景", "相关", "方面", "工作", "任务", "内容", "要求", "操作", "回答", "核对",
            "规则", "标准", "方法", "过程", "结果", "助手", "用户", "具体", "一个", "这", "该", "本",
            "必须", "需要", "可以", "应当", "应该", "以及", "与", "和", "及", "或", "的", "中", "上", "下",
            "先", "后", "再", "并", "且", "等", "制作", "输出", "配置", "设置", "使用", "完成", "执行",
            "文档", "文件", "页面", "材料", "方案", "文本", "记录", "数据", "信息", "说明", "说明文档",
            "生成", "编辑", "修改", "修订", "交付", "发送", "验收", "检查", "复核", "处理", "提升",
            # File formats are transport nouns, just like ``文档``/``文件``;
            # they cannot by themselves authorize a policy scoped to a named
            # audience or workflow.
            "word", "ppt", "pptx", "excel", "pdf", "doc", "docx", "xls", "xlsx", "csv", "md", "txt", "zip", "py", "js", "mjs", "sh",
        }
        scope_split_re = re.compile(
            r"[，,、；;：:。.!！？?（）()【】\[\]/|]+|"
            r"适用范围|适用于|所有|涉及|包括|涵盖|覆盖|面向|针对|用于|场景|任务|方面|工作|相关|"
            r"内容|要求|操作|回答|核对|规则|标准|方法|过程|结果|具体|一个|必须|需要|可以|应当|应该|"
            r"以及|与|和|及|或|的|中|上|下|这|该|本|先|后|再|并|且|等"
        )
        scope_segments: list[str] = []
        for raw_segment in scope_split_re.split(scope_compact):
            segment = raw_segment.strip()
            if not segment:
                continue
            # Strip transport nouns from either side, retaining the domain
            # phrase (e.g. ``校领导内部汇报材料`` -> ``校领导内部汇报``).
            changed = True
            while changed:
                changed = False
                for suffix in ("文档", "文件", "页面", "材料", "方案", "文本", "记录", "信息", "制作", "输出", "配置", "设置"):
                    if segment.endswith(suffix) and len(segment) > len(suffix):
                        segment = segment[:-len(suffix)]
                        changed = True
                for prefix in ("默认", "标准", "具体", "当前"):
                    if segment.startswith(prefix) and len(segment) > len(prefix):
                        segment = segment[len(prefix):]
                        changed = True
            if segment and segment not in generic_scope_terms:
                scope_segments.append(segment)
        # ASCII identifiers are already useful as complete anchors; for CJK,
        # phrases of three or more characters are strong.  Two-character
        # phrases are retained as weak anchors and need a second independent
        # match, preventing a lone ``教学``/``文档`` substring from leaking a
        # narrowly scoped rule while preserving domains such as ``周报`` or
        # ``音乐`` when the scope states them explicitly.
        scope_ascii_anchors = {
            value for value in re.findall(r"[a-z][a-z0-9_.-]{2,}", scope_compact)
            if value not in generic_scope_terms
        }
        scope_cjk_anchors = {
            value for value in scope_segments
            if re.search(r"[\u3400-\u9fff]", value) and value not in generic_scope_terms
        }
        strong_scope_anchors = {
            value for value in scope_ascii_anchors | scope_cjk_anchors
            if not re.fullmatch(r"[\u3400-\u9fff]{2}", value)
        }
        weak_scope_anchors = {
            value for value in scope_cjk_anchors
            if re.fullmatch(r"[\u3400-\u9fff]{2}", value)
        }
        strong_hits = sorted({anchor for anchor in strong_scope_anchors if anchor in compact_query})
        weak_hits = sorted({anchor for anchor in weak_scope_anchors if anchor in compact_query})
        # A weak two-character domain can be enough when the query repeats it
        # as an explicit lexical unit (rather than embedding it in a longer
        # project name), or when two independent weak terms jointly identify a
        # scope such as ``打印 + 图表``.  Requiring the same short term to have
        # a non-CJK boundary is intentionally conservative; strong phrases
        # remain substring matched for Chinese natural language.
        explicit_weak_hits = []
        for anchor in weak_hits:
            if re.search(rf"(?:^|[^\u3400-\u9fff]){re.escape(anchor)}(?:$|[^\u3400-\u9fff])", compact_query):
                explicit_weak_hits.append(anchor)
        scope_hits = sorted(set(strong_hits + explicit_weak_hits + (weak_hits if len(weak_hits) >= 2 else [])))
        scope_specific_hits = sorted(set(strong_hits + weak_hits))
        scope_qualified = bool(strong_hits or len(set(weak_hits)) >= 2 or explicit_weak_hits)
        # Teaching/explanation is a legitimate broad domain, but it often
        # appears as a substring of a project name (for example
        # ``智慧教学督导系统``).  An explicit explanation request is therefore
        # accepted only when the declared scope itself names explanation or
        # teaching output; the project-name substring alone still fails.
        explanation_scope = any(term in scope_compact for term in ("知识解释", "教学内容", "教学输出", "教学"))
        explanation_request = any(
            term in compact_query
            for term in ("解释", "含义", "意思", "如何理解", "举例", "打比方", "是什么")
        )
        if explanation_scope and explanation_request and not scope_qualified:
            scope_qualified = True
            scope_hits = sorted(set(scope_hits + ["explicit_explanation_request"]))
        if not scope_hits:
            return False, {
                "direct_policy_anchor_hits": matched,
                "shared_stable_anchors": [],
                "scope_anchor_hits": [],
                "scope_specific_anchors": scope_specific_hits,
                "scope_strong_anchor_hits": strong_hits,
                "scope_weak_anchor_hits": weak_hits,
                "declared_scope": declared_scope,
                "reason": f"直接政策的适用范围没有命中当前 Full Prompt（声明范围：{declared_scope or '未提供'}）；不能仅凭政策正文中的宽泛关键词跨场景注入。",
            }
        if not scope_qualified:
            return False, {
                "direct_policy_anchor_hits": matched,
                "shared_stable_anchors": [],
                "scope_anchor_hits": scope_hits,
                "scope_specific_anchors": scope_specific_hits,
                "scope_strong_anchor_hits": strong_hits,
                "scope_weak_anchor_hits": weak_hits,
                "declared_scope": declared_scope,
                "reason": "适用范围只命中嵌在更长项目名中的弱词，未形成独立域锚点；不跨场景注入。",
            }
    else:
        scope_hits = []
        scope_specific_hits = []
        strong_hits = []
        weak_hits = []
    # Entity-alias governance is answered by the official entity records and
    # graph.  A direct policy that merely says "governance" or "evidence" is
    # not a substitute; it must itself declare an alias/canonical/entity anchor.
    if is_entity_governance_query(query):
        entity_policy_anchors = ("实体", "别名", "全称", "规范名", "归并", "命名")
        policy_entity_hits = [anchor for anchor in entity_policy_anchors if anchor in text and anchor in compact_query]
        if not policy_entity_hits:
            return False, {
                "direct_policy_anchor_hits": matched,
                "shared_stable_anchors": [],
                "reason": "本题是实体别名治理；该直接政策没有实体、别名或规范名锚点，不能以宽泛‘治理/证据’替代官方实体关系记录。",
            }
    # A deliberately explicit query about policy/instructions can use a policy
    # from the same declared domain, but still needs a non-generic anchor.
    durable_hits = [x for x in ("规则", "要求", "偏好", "规范", "关联", "完整", "遗漏", "一致", "检查", "验收", "回归", "修改") if x in compact_query and x in text]
    platform_wide = {"hindsight", "agentmemory", "codex", "hermes", "memory", "记忆", "记忆系统", "系统", "架构"}
    specific_keywords = [value for value in keywords if value not in platform_wide]
    specific_matched = [value for value in matched if value not in platform_wide]
    delivery_policy = any(term in text for term in ("交付", "发送", "投递"))
    delivery_request = (
        any(term in compact_query for term in ("交付", "发送", "投递", "发给"))
        or ("文件" in compact_query and "文件夹" not in compact_query and "目录" not in compact_query)
    )
    if delivery_policy and not delivery_request:
        return False, {
            "direct_policy_anchor_hits": matched,
            "shared_stable_anchors": durable_hits,
            "reason": "该直接政策只约束文件交付/发送；问题虽提到同一智能体，但没有请求交付动作，不能注入。",
        }
    # Policies that prescribe the fields of a data/record model must be
    # requested as schema work.  “Evidence” is an important quality word, but
    # it is not a license to add a policy about refresh timestamps, nullability
    # or record fields to every validation report.
    schema_policy = any(term in text for term in ("字段", "非空内容", "刷新时间", "失效条件", "数据模型", "数据结构"))
    schema_request = any(term in compact_query for term in ("字段", "非空", "刷新时间", "失效条件", "数据模型", "数据结构", "schema"))
    if schema_policy and not schema_request:
        return False, {
            "direct_policy_anchor_hits": matched,
            "shared_stable_anchors": durable_hits,
            "reason": "该直接政策约束记录/数据模型字段；当前只是要求验收证据，并未请求字段或生命周期设计，不能因共享‘证据’一词注入。",
        }
    # Explanation/teaching guidance is intentionally narrower than a project
    # name containing a teaching-related character sequence.  For example, a
    # query about the ``智慧教学督导系统`` document is not a request to explain
    # a difficult concept.  Require an explicit explanation signal before a
    # policy scoped to knowledge teaching can enter the packet.
    explanation_policy = any(term in text for term in ("知识解释", "教学内容输出", "复杂专业知识", "举例和打比方"))
    explanation_request = any(
        term in compact_query
        for term in ("解释", "含义", "意思", "如何理解", "举例", "打比方", "是什么", "为什么")
    )
    if explanation_policy and not explanation_request:
        return False, {
            "direct_policy_anchor_hits": matched,
            "shared_stable_anchors": durable_hits,
            "scope_anchor_hits": scope_hits,
            "declared_scope": declared_scope,
            "reason": "该直接政策只约束知识解释/教学输出；当前没有明确解释请求，不能因项目名中的‘教学’弱词注入。",
        }
    # Applicability statements are part of a direct policy, not decorative
    # text.  In particular, a print-only black/white chart rule used to enter
    # any PPT task merely because both mention "图表".  Respect both halves:
    # explicit print scope requires a print request, and an explicit
    # screen/digital exclusion blocks it from a screen-edit request.
    print_scoped = "打印" in text or "印刷" in text
    screen_excluded = bool(re.search(r"不适用.{0,18}(?:屏幕|数字媒体|屏显|在线展示)", text))
    print_requested = any(term in compact_query for term in ("打印", "印刷", "纸质", "黑白输出", "装订"))
    if print_scoped and not print_requested:
        return False, {
            "direct_policy_anchor_hits": matched,
            "shared_stable_anchors": durable_hits,
            "reason": "该直接政策的适用范围限定为打印/印刷输出；当前问题没有打印请求，不能因同样提到图表而注入。",
        }
    if screen_excluded and any(term in compact_query for term in ("ppt", "屏幕", "演示", "数字媒体", "在线")):
        return False, {
            "direct_policy_anchor_hits": matched,
            "shared_stable_anchors": durable_hits,
            "reason": "该直接政策明确排除屏幕或数字展示；当前是屏幕/PPT任务，不能注入。",
        }
    # A policy that declares a concrete operating domain (keys, backup,
    # publishing, a named artifact, etc.) needs that domain to be present in
    # the question.  The shared umbrella “记忆系统” only gets a candidate into
    # the audit lane; it cannot authorize injection by itself.
    qualified = (
        bool(specific_matched)
        or (not specific_keywords and (len(set(matched)) >= 2 or len(durable_hits) >= 2))
    )
    return qualified, {
        "direct_policy_anchor_hits": matched,
        "shared_stable_anchors": durable_hits,
        "scope_anchor_hits": scope_hits,
        "declared_scope": declared_scope,
        "reason": "用户直接政策命中当前问题的具体锚点；作为待验证做事规则注入。" if qualified else "直接政策未命中当前问题的具体锚点；不因宽泛表述注入。",
    }


def admit_direct_policy_sidecar(
    query: str, ranked: list[dict[str, Any]], admitted: list[dict[str, Any]], rejected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    known = {result_key(item) for item in admitted}
    promoted: list[dict[str, Any]] = []
    sidecar_rejected: list[dict[str, Any]] = []
    for row in ranked:
        if "stable_guidance_direct_policies" not in set(row.get("sources") or []):
            continue
        item = dict(row.get("item") or {})
        key = result_key(item)
        if key in known:
            continue
        ok, detail = qualify_direct_policy(query, item)
        metadata = dict(item.get("metadata") or {})
        metadata["_ccy_admission"] = {
            "decision": "qualified" if ok else "rejected",
            "reason": detail["reason"],
            "source_class": "direct_policy",
            "policy": RELEVANCE_POLICY,
            "fixed_item_limit": False,
            "direct_policy_sidecar": True,
            "direct_policy_anchor_hits": detail.get("direct_policy_anchor_hits") or [],
            "stable_guidance_anchors": detail.get("shared_stable_anchors") or [],
            "evidence_status": metadata.get("evidence_status"),
        }
        item["metadata"] = metadata
        if ok:
            promoted.append(item); known.add(key)
        else:
            sidecar_rejected.append(item)
    if promoted:
        promoted_keys = {result_key(item) for item in promoted}
        rejected = [item for item in rejected if result_key(item) not in promoted_keys]
        admitted.extend(promoted)
    rejected.extend(sidecar_rejected)
    return admitted, rejected


def qualify_stable_mental_model(query: str, item: dict[str, Any], *, task_context: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """Require a mental-model section to prove a stable decision contribution."""
    if str(item.get("type") or "") != "mental_model":
        return False, {"reason": "不是心智模型章节。"}
    from lib.guidance_provenance import guidance_source_decision
    source_review=guidance_source_decision(item)
    if not source_review['allowed']:
        return False,{'reason':'心智模型来源链需要复核。','source_review':source_review}
    metadata = dict(item.get("metadata") or {})
    if (bool(metadata.get("is_stale")) and not (metadata.get('source_revalidated_reference') and (metadata.get('source_revalidation') or {}).get('allowed'))) or not metadata.get("mental_model_section_gate"):
        return False, {"reason": "心智模型未经过新鲜度或章节选择校验。"}
    # Provenance wrappers are not semantic evidence of applicability.
    text = re.sub(r"\s+", "", str(metadata.get('semantic_body') or item.get("text") or item.get("content") or "")).casefold()
    compact_query = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
    # The cognition model can contain a generic-looking “冲突与未决项”
    # section.  It remains a learning/teaching framework and must not enter a
    # delivery or acceptance question merely because both mention a boundary.
    if (str(metadata.get("mental_model_id") or "") == "liuzhongyang-cognition-learning"
            and not any(term in compact_query for term in ("学习", "复习", "背诵", "术语", "知识", "讲解", "教学", "训练", "因果"))):
        return False, {
            "reason": "认知学习模型只用于学习、解释和训练问题；当前交付/验收任务不采用其通用未决项。",
            "applicability_scope": "cognition_model_outside_scope",
        }
    learning_scope=('学习目标','背诵','复习','陌生英文术语','知识解释','学习方法','教学方法')
    if sum(term in text for term in learning_scope)>=2 and not any(term in compact_query for term in ('学习','复习','背诵','术语','知识讲解','教学')):
        return False,{'reason':'该章节约束学习或知识解释；当前问题是其他任务的执行与验收，泛词“记忆/边界”不构成适用性桥接。','applicability_scope':'learning_outside_scope'}
    if named_system_taxonomy_question(query):
        alignment = system_taxonomy_alignment(query, text)
        qualified = bool(alignment.get("qualified"))
        return qualified, {
            "shared_stable_anchors": alignment.get("shared_systems") or [],
            "specific_topic_hits": (alignment.get("role_markers") or []) + (alignment.get("boundary_markers") or []),
            "reason": (
                "系统关系问题的心智模型章节命中点名系统及其角色/边界证据，只补充分工与限制。"
                if qualified else
                "系统关系问题的心智模型章节没有同时命中点名系统和独立角色/边界证据，不注入。"
            ),
        }
    durable = (
        "质量", "验收", "检查", "审计", "优化", "机制", "系统", "记忆", "召回", "注入", "hindsight",
        "hook", "mcp", "agentmemory", "完整", "遗漏", "一致", "风险", "决策", "判断", "方案", "架构", "学习", "解释", "交付", "测试", "验证",
        "证据", "范围", "边界", "反例", "适用", "门槛", "拒绝", "降级", "回归",
    )
    shared = sorted(term for term in durable if term in compact_query and term in text)
    asks_for_model = "心智模型" in compact_query
    asks_for_observation = "观察" in compact_query
    memory_scope_terms = ("hindsight", "记忆", "召回", "注入", "hook", "mcp", "agentmemory")
    query_memory_scope = any(term in compact_query for term in memory_scope_terms)
    # An explicit request to use/evaluate a mental model (or its evidence
    # layer, observation) is already a memory-domain anchor.  Requiring an
    # additional literal “Hindsight/记忆/召回” token made model guidance
    # disappear from otherwise clear questions such as “心智模型何时应
    # 注入？”.  Freshness and section/topic checks remain mandatory.
    memory_anchor = asks_for_model or asks_for_observation or any(
        term in compact_query and term in text for term in memory_scope_terms
    )
    # When the request names a concrete topic (for example WPS/backup), a
    # model section that only repeats the system-wide word “Hindsight/记忆” is
    # not useful.  Require one non-framework Chinese bigram or identifier from
    # the actual question.  This keeps the relevant “数据备份与资产安全” section
    # while rejecting unrelated collaboration/cognition chapters.
    framework = set(durable) | {"什么", "如何", "是否", "当前", "用户", "需要", "一个", "问题", "一下", "我们", "里面", "这个", "那个", "哪些"}
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", compact_query))
    specific_terms = {
        cjk[index:index + 2]
        for index in range(max(0, len(cjk) - 1))
        if cjk[index:index + 2] not in framework
    }
    specific_terms.update(token for token in re.findall(r"[a-z][a-z0-9_.-]{2,}", compact_query) if token not in {"hindsight", "memory", "codex", "agentmemory", "hermes"})
    topic_hits = sorted(term for term in specific_terms if term in text)
    requires_topic_match = bool(specific_terms) and not asks_for_model
    # “不要只信单测、要看真实链路” is a stable acceptance standard, not a
    # narrow product topic.  A section that explicitly defines real completion
    # through runtime evidence, root-cause repair and end-to-end regression is
    # precisely the reusable decision framework for that request.  Demand the
    # chain rather than a single broad word so unrelated quality prose stays
    # out.
    real_chain_request = (
        any(term in compact_query for term in ("真实链路", "端到端", "真实交互", "运行时"))
        and any(term in compact_query for term in ("单测", "测试", "验收", "回归", "证据"))
    )
    real_completion_model = (
        any(term in text for term in ("真实完成", "端到端", "运行时行为"))
        and any(term in text for term in ("根因", "回归", "测试结果", "硬证据"))
    )
    qualified = (
        bool(memory_anchor) and (len(shared) >= 1 or asks_for_model)
        if query_memory_scope else
        (len(shared) >= 2 or (asks_for_model and len(shared) >= 1))
    )
    if real_chain_request and real_completion_model:
        qualified = True
    if requires_topic_match and not topic_hits:
        qualified = False
    if contextual_guidance_match(task_context,text):
        qualified = True
    return qualified, {
        "shared_stable_anchors": shared,
        "specific_topic_hits": topic_hits[:12],
        "reason": (
            "该已核验心智模型章节命中当前问题的稳定决策/质量锚点，只作为做事框架补充。"
            if qualified else
            "心智模型章节没有足够的当前决策/质量锚点；不因名称相似而注入。"
        ),
    }


def admit_stable_mental_model_sidecar(
    query: str, ranked: list[dict[str, Any]], admitted: list[dict[str, Any]], rejected: list[dict[str, Any]], *, task_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    known = {result_key(item) for item in admitted}
    promoted: list[dict[str, Any]] = []
    sidecar_rejected: list[dict[str, Any]] = []
    for row in ranked:
        if "stable_guidance_mental_models" not in set(row.get("sources") or []):
            continue
        item = dict(row.get("item") or {})
        key = result_key(item)
        if key in known:
            continue
        ok, detail = qualify_stable_mental_model(query, item, task_context=task_context)
        metadata = dict(item.get("metadata") or {})
        metadata["_ccy_admission"] = {
            "decision": "qualified" if ok else "rejected",
            "reason": detail["reason"],
            "source_class": "mental_model",
            "policy": RELEVANCE_POLICY,
            "fixed_item_limit": False,
            "stable_guidance_sidecar": True,
            "stable_guidance_anchors": detail.get("shared_stable_anchors") or [],
            "mental_model_section_gate": True,
        }
        item["metadata"] = metadata
        if ok:
            promoted.append(item)
            known.add(key)
        else:
            sidecar_rejected.append(item)
    if promoted:
        promoted_keys = {result_key(item) for item in promoted}
        rejected = [item for item in rejected if result_key(item) not in promoted_keys]
        admitted.extend(promoted)
    rejected.extend(sidecar_rejected)
    return admitted, rejected


def admit_stable_guidance_sidecar(
    query: str, ranked: list[dict[str, Any]], admitted: list[dict[str, Any]], rejected: list[dict[str, Any]], *, task_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Promote qualified observation sidecar candidates with an explicit receipt."""
    known = {result_key(item) for item in admitted}
    promoted: list[dict[str, Any]] = []
    sidecar_rejected: list[dict[str, Any]] = []
    for row in ranked:
        if "stable_guidance_observations" not in set(row.get("sources") or []):
            continue
        item = dict(row.get("item") or {})
        key = result_key(item)
        if key in known:
            continue
        ok, detail = qualify_stable_guidance_observation(query, item, task_context=task_context)
        if not ok:
            metadata = dict(item.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "rejected",
                "reason": detail["reason"],
                "source_class": "observation",
                "policy": RELEVANCE_POLICY,
                "fixed_item_limit": False,
                "stable_guidance_sidecar": True,
                "stable_guidance_anchors": detail.get("shared_stable_anchors") or [],
            }
            item["metadata"] = metadata
            sidecar_rejected.append(item)
            continue
        metadata = dict(item.get("metadata") or {})
        metadata["_ccy_admission"] = {
            "decision": "qualified",
            "reason": detail["reason"],
            "source_class": source_class(item),
            "policy": RELEVANCE_POLICY,
            "fixed_item_limit": False,
            "stable_guidance_sidecar": True,
            "stable_guidance_anchors": detail["shared_stable_anchors"],
        }
        item["metadata"] = metadata
        promoted.append(item)
        known.add(key)
    if promoted:
        promoted_keys = {result_key(item) for item in promoted}
        rejected = [item for item in rejected if result_key(item) not in promoted_keys]
        admitted.extend(promoted)
    rejected.extend(sidecar_rejected)
    return admitted, rejected


def merge_recall_responses(
    responses: list[tuple[str, dict[str, Any]]],
    plan: dict[str, Any],
    ranking_deadline_at: float | None = None,
) -> dict[str, Any]:
    aggregate: dict[str, dict[str, Any]] = {}
    entities: dict[str, Any] = {}
    primary = plan["primary_shape"]
    # Every admission/governance pass below must see the same resolved Full
    # Prompt that drove the upstream facets. ``latest_query`` is reserved for
    # raw legacy display fields; using it here would silently drop preceding
    # handoff context a second time after retrieval already used it.
    semantic_admission_query = full_prompt_text(
        str(plan.get("full_prompt") or plan.get("input_query") or "")
    )
    if plan.get('memory_action')=='guidance_only':
        semantic_admission_query=str(plan.get('guidance_query') or semantic_admission_query)
    timestamps = [
        timestamp_value(item)
        for _, response in responses
        for item in (response.get("results") or [])
        if timestamp_value(item)
    ]
    oldest = min(timestamps) if timestamps else 0.0
    newest = max(timestamps) if timestamps else 0.0
    for _, response in responses:
        entities.update(response.get("entities") or {})
    entity_anchors = resolve_query_entity_anchors(str(plan.get("input_query") or ""), entities)

    for source, response in responses:
        for rank, raw in enumerate(response.get("results") or [], 1):
            item = dict(raw)
            key = result_key(item)
            scores = item.get("scores") or {}
            base = float(scores.get("final") or item.get("score") or 0.0)
            rrf = 1.0 / (60.0 + rank)
            bonus = 0.0
            item_type = str(item.get("type") or "")
            entity_delta, entity_receipt = entity_anchor_adjustment(item, entity_anchors)
            metadata_for_entity = dict(item.get("metadata") or {})
            metadata_for_entity["_ccy_entity_anchor"] = entity_receipt
            item["metadata"] = metadata_for_entity
            bonus += entity_delta
            if primary == "current" and item_type == "world":
                bonus += 0.05
            if primary == "timeline" and item_type == "experience":
                bonus += 0.04
            if primary in {"inventory", "synthesis"} and item_type == "observation":
                bonus += 0.025
            if source.startswith("direct_evidence:"):
                item.setdefault("metadata", {})["_ccy_direct_evidence"] = True
                # Exact containment verifies provenance, not topical relevance.
                # Keep only a small tie-breaker; source-aware admission below
                # still rejects an authentic but unrelated quotation.
                bonus += 0.05
            ts = timestamp_value(item)
            if primary in {"current", "conflict"} and newest and ts:
                span = newest - oldest
                recency = 1.0 if span <= 0 else (ts - oldest) / span
                bonus += 0.10 * max(0.0, min(1.0, recency))
            metadata = item.get("metadata") or {}
            validity = str(
                metadata.get("validity")
                or metadata.get("status")
                or metadata.get("verification_status")
                or ""
            ).casefold()
            if primary in {"current", "conflict"}:
                if validity in {"current", "active", "verified", "effective"}:
                    bonus += 0.04
                elif validity in {"superseded", "obsolete", "invalid", "expired"}:
                    bonus -= 0.08
                # Current-state answers must prefer explicit status and
                # supersession statements over merely similar older facts.
                # These are generic validity markers, not topic keywords.
                normalized_text = str(
                    item.get("text") or item.get("content") or ""
                ).casefold()
                marker_hits = sum(
                    1 for marker in CURRENT_STATE_MARKERS
                    if marker.casefold() in normalized_text
                )
                # Explicit exclusivity/retirement language often carries the
                # actual answer to "is the old path still used?".  Give such
                # statements enough weight to survive multi-facet fusion, while
                # retaining a cap so lexical markers cannot overwhelm semantic
                # relevance and time evidence.
                bonus += min(0.27, 0.09 * marker_hits)
                if plan.get("negative_current_check"):
                    retirement_hits = sum(
                        1 for marker in RETIREMENT_MARKERS
                        if marker.casefold() in normalized_text
                    )
                    bonus += min(0.30, 0.15 * retirement_hits)
            if metadata.get("verification_status") == "verified":
                bonus += 0.03
            base_component = base + bonus
            # Fusion must not depend on which concurrent facet finishes first.
            # The original query (facet:0) gets the stronger RRF weight; every
            # expansion contributes a smaller coverage vote.  The best semantic
            # score/validity bonus is selected independently of arrival order.
            rrf_component = (
                5.0 if source in {"facet:0", "direct_fallback"} else 2.0
            ) * rrf
            if key not in aggregate:
                aggregate[key] = {
                    "item": item,
                    "best_base": base_component,
                    "rrf_score": rrf_component,
                    "sources": [source],
                }
            else:
                if base_component > aggregate[key]["best_base"]:
                    aggregate[key]["best_base"] = base_component
                    aggregate[key]["item"] = item
                # A normal semantic facet can reach the same canonical memory
                # before the official graph lane.  Keep the graph provenance
                # on the merged record even when the semantic copy has a
                # higher score; otherwise the final Hook receipt falsely says
                # that no injected memory came through the relationship graph.
                graph_evidence = (item.get("metadata") or {}).get("_ccy_graph_evidence")
                existing_metadata = dict((aggregate[key]["item"].get("metadata") or {}))
                if graph_evidence and not existing_metadata.get("_ccy_graph_evidence"):
                    existing_metadata["_ccy_graph_evidence"] = dict(graph_evidence)
                    aggregate[key]["item"]["metadata"] = existing_metadata
                aggregate[key]["rrf_score"] += rrf_component
                if source not in aggregate[key]["sources"]:
                    aggregate[key]["sources"].append(source)

    for row in aggregate.values():
        row["score"] = row["best_base"] + row["rrf_score"]
    relevance_cfg = dict(plan.get("relevance_admission_config") or {})
    semantic_texts = [
        str(row["item"].get("text") or row["item"].get("content") or "")
        for row in aggregate.values()
    ]
    # The cross encoder refines ordering but never authorizes a memory by
    # itself.  It must yield to the Hook's hard interactive deadline: if CPU
    # contention makes it slow, retain the source/graph/time-governed
    # candidates and return a visible receipt instead of losing the chain.
    remaining = None if ranking_deadline_at is None else max(0.0, ranking_deadline_at - time.monotonic())
    reranker_candidate_cap = max(1, int(relevance_cfg.get("rerankerForegroundMaxCandidates", 32)))
    reranker_wait = None if remaining is None else foreground_reranker_budget(remaining, relevance_cfg)
    if remaining is not None and not foreground_semantic_reranker_enabled(relevance_cfg):
        semantic_scores, semantic_error = [None] * len(semantic_texts), (
            "skipped: foreground semantic reranker disabled; production uses RRF plus deterministic governance"
        )
    elif remaining is not None and len(semantic_texts) > reranker_candidate_cap:
        semantic_scores, semantic_error = [None] * len(semantic_texts), (
            "skipped: optional local cross-encoder candidate set exceeds "
            f"foreground cap ({len(semantic_texts)}>{reranker_candidate_cap})"
        )
    elif reranker_wait is not None and reranker_wait < 0.35:
        semantic_scores, semantic_error = [None] * len(semantic_texts), "skipped: controller deadline reserved for Hook receipt"
    elif ranking_deadline_at is None:
        semantic_scores, semantic_error = cross_encoder_scores(
            str(plan.get("input_query") or ""), semantic_texts,
            model_name=str(relevance_cfg.get("rerankerModel") or "BAAI/bge-reranker-base"),
            batch_size=int(relevance_cfg.get("batchSize") or 32),
        )
    else:
        semantic_scores, semantic_error = deadline_cross_encoder_scores(
            str(plan.get("input_query") or ""), semantic_texts,
            model_name=str(relevance_cfg.get("rerankerModel") or "BAAI/bge-reranker-base"),
            batch_size=int(relevance_cfg.get("batchSize") or 32),
            timeout=max(0.1, reranker_wait),
        )
    for row, semantic_score in zip(aggregate.values(), semantic_scores):
        metadata = dict(row["item"].get("metadata") or {})
        if semantic_score is not None:
            metadata["semantic_relevance_score"] = round(float(semantic_score), 6)
            # Semantic relevance influences order but does not decide admission
            # by itself; graph/time/source evidence remains useful.
            row["score"] += 0.65 * float(semantic_score)
        row["item"]["metadata"] = metadata
    ranked = sorted(aggregate.values(), key=lambda row: row["score"], reverse=True)
    # Provenance requests are different from ordinary answer synthesis: the
    # caller explicitly asked to inspect the source record.  Put exact,
    # locally-verified literal matches first so the normal output budget cannot
    # crowd them out with many merely similar semantic candidates.  The list
    # may intentionally contain more than one source record; retaining that
    # ambiguity is more honest than silently choosing the newest duplicate.
    if primary == "audit":
        direct_rows = [
            row for row in ranked
            if any(str(source).startswith("direct_evidence:") for source in row["sources"])
        ]
        if direct_rows:
            direct_keys = {id(row) for row in direct_rows}
            ranked = direct_rows + [row for row in ranked if id(row) not in direct_keys]
    max_tokens = int(plan.get("max_tokens") or 1200)
    max_chars = max(1600, max_tokens * 4)
    # Sidecars are not normal fact candidates.  Keep them out of the generic
    # retriever admission, then evaluate each under its explicit stable-guidance
    # rule.  This prevents an arbitrary model heading or an unrelated profile
    # sentence from becoming eligible merely because it shares one broad word.
    primary_ranked = [
        row for row in ranked
        if not ({"stable_guidance_observations", "stable_guidance_mental_models", "stable_guidance_direct_policies"} & set(row.get("sources") or []))
    ]
    relevance_admitted, relevance_rejected = admit_controller_results(
        semantic_admission_query, primary_ranked, plan
    )
    # The source-first guard protects current attachments/corrections from being
    # overwritten by old factual memories.  Separately labelled stable
    # observations and fresh mental-model sections may only add quality/decision
    # guidance under their own anchor tests; they share the same governance and
    # dynamic token budget afterwards.
    relevance_admitted, relevance_rejected = admit_stable_guidance_sidecar(
        semantic_admission_query, ranked, relevance_admitted, relevance_rejected,
        task_context=plan.get("contextual_intent"),
    )
    relevance_admitted, relevance_rejected = admit_direct_policy_sidecar(
        semantic_admission_query, ranked, relevance_admitted, relevance_rejected
    )
    relevance_admitted, relevance_rejected = admit_stable_mental_model_sidecar(
        semantic_admission_query, ranked, relevance_admitted, relevance_rejected,
        task_context=plan.get("contextual_intent"),
    )
    # Apply the same current-state/supersession ledger for every controller
    # client (Codex, Hermes, Cloud Code, Feishu agents), not only in the outer
    # Codex Hook.  This prevents a semantically similar but retired route from
    # being injected as if it were current.
    before_governance = list(relevance_admitted)
    # Policies/models describe how to reason, not past/current project facts.
    # They should not be discarded by a factual supersession ledger; their own
    # provenance and anchor gates above remain mandatory.
    pre_governance_sidecars = [
        item for item in before_governance
        if str(item.get("type") or "") in {"mental_model", "direct_policy"}
        and str(((item.get("metadata") or {}).get("_ccy_admission") or {}).get("decision") or "") == "qualified"
    ]
    factual_before_governance = [item for item in before_governance if item not in pre_governance_sidecars]
    runtime_config = load_json(DEFAULT_CODEX_RUNTIME_CONFIG)
    governed = apply_recall_governance(
        semantic_admission_query,
        factual_before_governance,
        runtime_config,
        contextual_intent=plan.get("contextual_intent"),
        raw_user_prompt=str(plan.get("raw_user_prompt") or ""),
    )
    # Re-attach qualified guidance after factual current-state governance.
    governed.extend(pre_governance_sidecars)
    before_keys = {result_key(item): item for item in before_governance}
    governed_existing_keys = {
        result_key(item) for item in governed
        if result_key(item) in before_keys
    }
    governance_filtered = [
        item for key, item in before_keys.items() if key not in governed_existing_keys
    ]
    for item in governance_filtered:
        metadata = dict(item.get("metadata") or {})
        admission = dict(metadata.get("_ccy_admission") or {})
        admission.update({
            "decision": "rejected_current_conflict",
            "reason": "命中已核验的时序替代规则：旧状态保留在历史库中，但不注入当前状态回答。",
        })
        metadata["_ccy_admission"] = admission
        item["metadata"] = metadata
    relevance_rejected.extend(governance_filtered)
    # Direct-policy and mental-model sidecars have already passed their own
    # source-specific gates above.  The generic retriever gate is designed for
    # factual memory chunks and would otherwise reject a qualified policy/model
    # merely because its synthetic type has no embedding provenance.
    governed_runtime_authorities, governed = partition_deterministic_runtime_authorities(governed)
    # A concrete cross-task handoff is scoped by the named artifact.  The
    # executable local-runtime snapshot is authoritative for live Hindsight
    # configuration questions, but it is not evidence about an unrelated
    # folder migration.  Keep the row in the rejection receipt instead of
    # letting the deterministic prefix bypass the same Full Prompt boundary
    # that governs Bank candidates.
    handoff_scope = continuity_task_alignment(
        semantic_admission_query, ""
    )
    if handoff_scope.get("required") and handoff_scope.get("artifact_scope_required"):
        retained_runtime_authorities: list[dict[str, Any]] = []
        for item in governed_runtime_authorities:
            alignment = continuity_task_alignment(
                semantic_admission_query,
                str(item.get("text") or item.get("content") or ""),
            )
            if alignment.get("passes"):
                retained_runtime_authorities.append(item)
                continue
            metadata = dict(item.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected_handoff_artifact_scope",
                "reason": "当前是具体工件的跨任务交接；本机运行态权威虽可信，但未命中 Full Prompt 指定的文件夹/目录/路径范围，不能冒充交接事实。",
                "continuity_task_alignment": alignment,
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            relevance_rejected.append(item)
        governed_runtime_authorities = retained_runtime_authorities
    governed_sidecars = [
        item for item in governed
        if str(item.get("type") or "") in {"mental_model", "direct_policy"}
        and str(((item.get("metadata") or {}).get("_ccy_admission") or {}).get("decision") or "") == "qualified"
    ]
    governed_primary = [item for item in governed if item not in governed_sidecars]
    relevance_admitted, governance_admission_rejected = relevance_admit_items(
        semantic_admission_query,
        governed_primary,
        deep=bool(plan.get("explicit_deep_recall") or plan.get("primary_shape") in {"inventory", "synthesis", "timeline"}),
        preserve_controller_decision=True,
    )
    # The final generic recheck deliberately rejects broad stale predictions.
    # An explicit long-task delivery contract is a stricter structural rule
    # with four independently required actions, so restore that one minimum
    # closure after the generic pass rather than losing it on cache recovery.
    relevance_admitted, governance_admission_rejected = promote_long_task_delivery_contract(
        semantic_admission_query,
        [
            {
                "item": item,
                "score": float(((item.get("scores") or {}).get("final")) or item.get("score") or 0.0),
            }
            for item in governed_primary
        ],
        relevance_admitted,
        governance_admission_rejected,
    )
    # Live authority is generated only after a dedicated recognizer matches;
    # it therefore precedes ordinary semantic evidence and participates in the
    # same dynamic token budget without being dependent on embedding scores.
    relevance_admitted = governed_runtime_authorities + [
        item for item in relevance_admitted
        if result_key(item) not in {result_key(value) for value in governed_runtime_authorities}
    ]
    known_after_governance = {result_key(item) for item in relevance_admitted}
    for item in governed_sidecars:
        if result_key(item) not in known_after_governance:
            relevance_admitted.append(item)
            known_after_governance.add(result_key(item))
    relevance_rejected.extend(governance_admission_rejected)
    selected: list[dict[str, Any]] = []
    deferred_for_budget: list[dict[str, Any]] = []
    used_chars = 0
    guidance_needed=bool((plan.get("memory_needs") or {}).get("guidance",{}).get("needed"))
    for item in prioritize_guidance_for_budget(relevance_admitted,guidance_needed=guidance_needed):
        text = str(item.get("text") or item.get("content") or "")
        # The first qualified item is always retained.  Thereafter the only
        # volume guard is the plan's adaptive token budget.  No per-profile or
        # per-document item limit is allowed: related evidence can span many
        # pages, files and hops.
        if selected and used_chars + len(text) > max_chars:
            metadata = dict(item.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "deferred_for_token_budget",
                "reason": "与当前问题相关；本轮自适应上下文预算已用完，未因条数上限被拒绝。",
                "budget_chars": max_chars,
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            deferred_for_budget.append(item)
            continue
        selected.append(item)
        used_chars += len(text)

    controller_input_items, controller_sidecar_items = controller_admission_inputs(
        ranked,
        relevance_admitted,
        relevance_rejected,
        deferred_for_budget,
    )
    runtime_context = dict(plan.get("_runtime_context") or {})
    admission_context = admission_binding_context(
        runtime_context,
        execution_id=str(plan.get("execution_id") or ""),
        prompt_sha256=str(
            runtime_context.get("user_prompt_fingerprint")
            or plan.get("query_fingerprint")
            or ""
        ),
    )
    finalized_admission = finalize_admission(
        admission_context,
        controller_input_items,
        admitted=selected,
        rejected=relevance_rejected,
        deferred=deferred_for_budget,
    )
    normalized_by_input_key = finalized_admission["normalized_items_by_input_key"]
    controller_input_items = finalized_admission["items"]
    selected = [
        normalized_by_input_key.get(decision_key(item), item)
        for item in selected
    ]

    selected_entity_matches = [
        item for item in selected
        if ((item.get("metadata") or {}).get("_ccy_entity_anchor") or {}).get("matched")
    ]
    entity_resolution = {
        "required": bool(entity_anchors),
        "anchors": entity_anchors,
        "candidate_count": len(aggregate),
        "selected_match_count": len(selected_entity_matches),
        "selected_match_ids": [str(item.get("id") or item.get("chunk_id") or "")[:80] for item in selected_entity_matches[:16]],
        "policy": "明确实体或已批准别名只影响排序与可解释性；不设置条数上限，不因未带实体标签直接丢弃通用相关规则。",
        "status": "matched" if selected_entity_matches else ("no_explicit_entity" if not entity_anchors else "no_entity_matched"),
    }

    merged = {
        "results": selected,
        "entities": entities,
        "query_controller": {
            "version": VERSION,
            "admission_contract": {
                "schema": "hindsight.admission_contract.v1",
                "controller_policy": RELEVANCE_POLICY,
                "hook_revision": str(runtime_context.get("admission_revision") or ""),
                "mode": str(runtime_context.get("admission_contract_mode") or "legacy"),
                "meaning": "候选、Controller 准入、Hook 传输保护、Packet 投递和回执使用同一回合绑定账本；缓存键也包含 Hook 准入版本。",
            },
            "query_id": plan["query_id"],
            "execution_id": plan["execution_id"],
            "query_fingerprint": plan["query_fingerprint"],
            "memory_action": plan.get("memory_action", "focused_recall"),
            "evaluation_as_of": plan.get("evaluation_as_of") or None,
            "evaluation_excluded_count": int(plan.get("evaluation_excluded_count") or 0),
            "working_set": plan.get("working_set"),
            "planner": plan.get("planner"),
            "value_of_information": plan.get("value_of_information"),
            "primary_shape": primary,
            "procedure_depth": bool(plan.get("procedure_depth")),
            "recall_profile": plan.get("recall_profile"),
            "deadline_ms": plan.get("deadline_ms"),
            "strategies": plan["strategies"],
            "route_decisions": plan["route_decisions"],
            "query_count": len(responses),
            "coverage_dimensions": plan["coverage_dimensions"],
            "scope_claim": plan["scope_claim"],
            "result_count": len(selected),
            "relevance_admission": {
                "candidate_count": len(controller_input_items),
                "fused_candidate_count": len(ranked),
                "sidecar_candidate_count": len(controller_sidecar_items),
                "qualified_count": len(relevance_admitted),
                # Retained for older dashboard readers; it now means qualified,
                # never a count-capped admission set.
                "admitted_count": len(relevance_admitted),
                "injected_eligible_count": len(selected),
                "deferred_for_token_budget_count": len(deferred_for_budget),
                "current_conflict_filtered_count": len(governance_filtered),
                "budget_max_tokens": max_tokens,
                "budget_max_chars": max_chars,
                "budget_used_chars": used_chars,
                "deferred_items": trace_result_summaries(deferred_for_budget, limit=None),
                "rejected_count": len(relevance_rejected),
                "rejected_items": trace_result_summaries(relevance_rejected, limit=None),
                "policy": RELEVANCE_POLICY,
                "semantic_reranker": str(relevance_cfg.get("rerankerModel") or "BAAI/bge-reranker-base"),
                "semantic_reranker_error": semantic_error,
                "meaning": "先用当前用户问题核对意图分面、来源类型、独立主题锚点和本地语义相关性，再用已核验时间线排除旧状态；权威/原话只证明真实来源，不能绕过主题相关性。所有合格候选按排序装入动态 token 预算，不设固定条数上限。",
            },
            "admission_decisions": finalized_admission["decisions"],
            "project_state": plan.get("project_state"),
            "source_guard": plan.get("source_guard"),
            "contextual_intent": plan.get("contextual_intent"),
            "guidance_sidecar": dict(plan.get("guidance_sidecar") or {}),
            "entity_resolution": entity_resolution,
            "pipeline_stages": {
                "schema": "hindsight.controller_pipeline_stages.v1",
                "retrieval": {
                    "raw_result_count": sum(len(response.get("results") or []) for _, response in responses),
                    "fused_candidate_count": len(ranked),
                    "sidecar_candidate_count": len(controller_sidecar_items),
                    "controller_input_count": len(controller_input_items),
                    "candidate_items": trace_result_summaries(
                        [dict(row.get("item") or {}) for row in ranked], limit=None
                    ),
                    "sidecar_candidate_items": trace_result_summaries(
                        controller_sidecar_items, limit=None
                    ),
                    "meaning": "各真实分面返回后先去重融合；另列稳定观察、直接规则和心智模型侧路候选。两者共同构成 Controller 的真实准入输入，均不代表已注入。",
                },
                "controller_admission": {
                    "input_count": len(controller_input_items),
                    "qualified_count": len(relevance_admitted),
                    "eligible_after_budget_count": len(selected),
                    "rejected_count": len(relevance_rejected),
                    "deferred_count": len(deferred_for_budget),
                    "qualified_items": trace_result_summaries(relevance_admitted, limit=None),
                    "rejected_items": trace_result_summaries(relevance_rejected, limit=None),
                    "deferred_items": trace_result_summaries(deferred_for_budget, limit=None),
                    "semantic_reranker_error": semantic_error,
                    "policy": RELEVANCE_POLICY,
                },
            },
        },
    }
    # Hindsight clients validate RecallResponse and ignore unknown top-level
    # keys. Mirror the routing receipt into the official ``trace`` field so
    # Hermes can close the retrieval-to-answer usefulness loop.
    merged["trace"] = {"query_controller": dict(merged["query_controller"])}
    return merged


def header_value(headers: dict[str, str], name: str, default: str = "") -> str:
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target:
            return str(value or default)
    return default


def decode_header_value(value: str) -> str:
    try:
        return urllib.parse.unquote(str(value or ""))
    except Exception:
        return str(value or "")


def runtime_context_from_headers(headers: dict[str, str]) -> dict[str, Any]:
    """Extract only local, deterministic context supplied by a Hook/adapter."""
    cwd = decode_header_value(header_value(headers, "X-Memory-Project-Cwd"))[:1024]
    source_label = decode_header_value(header_value(headers, "X-Memory-Source-Label"))[:480]
    source_kind = header_value(headers, "X-Memory-Source-Kind").strip().casefold()
    # ``Source-Kind`` used to be the only authority signal.  A reused adapter
    # client could therefore carry an attachment header into a later ordinary
    # text turn, even though that turn supplied no current source at all.  The
    # Hook now sends an explicit per-invocation presence bit; honor it when
    # present and retain the legacy behavior only for older adapters that omit
    # the bit entirely.
    source_present = header_value(headers, "X-Memory-Source-Present").strip()
    correction = header_value(headers, "X-Memory-User-Correction").strip() == "1"
    source_authority = source_kind in {"attachment", "open_file", "explicit_current_source"}
    if source_present in {"0", "1"}:
        source_authority = source_present == "1" and source_authority
    project_key = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16] if cwd else ""
    execution_mode = header_value(headers, "X-Memory-Execution-Mode").strip().casefold()
    prompt_origin = header_value(headers, "X-Memory-Prompt-Origin").strip().casefold()
    if prompt_origin not in {"user_direct", "agent_generated", "agent_tool_call", "test_probe"}:
        prompt_origin = ""
    # Adapters that can expose the agent's resolved task meaning may supply a
    # Full Prompt.  It is not a transcript and is retained as one opaque,
    # auditable semantic input.  Legacy hooks simply omit it and the plan
    # records the fallback source instead of pretending an agent produced it.
    full_prompt = decode_header_value(header_value(headers, "X-Memory-Full-Prompt"))
    full_prompt_source = header_value(headers, "X-Memory-Full-Prompt-Source").strip() or ""
    if len(full_prompt) > 48000:
        full_prompt = ""
        full_prompt_source = "rejected_oversize_header"
    contextual_intent: dict[str, Any] = {}
    raw_contextual_intent = decode_header_value(header_value(headers, "X-Memory-Context-Intent"))
    if raw_contextual_intent and len(raw_contextual_intent) <= 4096:
        try:
            candidate = json.loads(raw_contextual_intent)
            if isinstance(candidate, dict) and str(candidate.get("schema") or "") == "hindsight.contextual_intent.v1":
                # Never accept arbitrary transcript-shaped fields through a
                # request header. The Hook sends only a bounded interpretation
                # receipt for resolving the current user turn.
                contextual_intent = candidate
        except (TypeError, ValueError):
            contextual_intent = {}
    return {
        "project_key": project_key,
        "project_cwd": cwd,
        "session_id": header_value(headers, "X-Memory-Session-Id")[:160],
        "turn_id": header_value(headers, "X-Memory-Turn-Id")[:160],
        # These three values form the immutable per-turn join key used by the
        # Hook receipt, Controller trace, feedback ledger and 9998 projection.
        # Do not rely on a timestamp/window join: parallel turns may carry the
        # same wording and complete in a different order.
        "invocation_id": header_value(headers, "X-Memory-Invocation-Id")[:160],
        "user_prompt_fingerprint": header_value(headers, "X-Memory-User-Prompt-Fingerprint")[:96],
        "source_revision": header_value(headers, "X-Memory-Source-Revision")[:160],
        "admission_contract_mode": header_value(headers, "X-Memory-Admission-Mode").casefold()[:32] or "legacy",
        "source_authority": source_authority,
        "source_present": source_present in {"0", "1"} and source_present == "1",
        "source_kind": source_kind,
        "source_label": source_label,
        "source_fingerprint": header_value(headers, "X-Memory-Source-Fingerprint")[:96],
        "user_correction": correction,
        "execution_mode": execution_mode or "production",
        "prompt_origin": prompt_origin,
        "planning_only": header_value(headers, "X-Memory-Plan-Only").strip() == "1",
        # Evaluation-only boundary. Production callers never set it; clean
        # replay callers use it to prevent a retained answer from becoming its
        # own historical evidence on the next run.
        "evaluation_as_of": header_value(headers, "X-Memory-Evaluation-As-Of")[:64],
        "contextual_intent": contextual_intent,
        "full_prompt": full_prompt,
        "full_prompt_source": full_prompt_source,
        # The Hook owns the final candidate-to-Packet admission. Keep its
        # revision in the request contract so a cache cannot replay a result
        # produced under a superseded delivery policy.
        "admission_revision": header_value(headers, "X-Memory-Admission-Revision")[:160],
    }


def runtime_context_from_request(headers: dict[str, str], body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge body-side semantic contract fields over legacy headers.

    Full Prompt is content, not transport metadata.  The header remains a
    compatibility path for older adapters, while the JSON body is the
    authoritative path for long contracts and avoids proxy header limits.
    """
    context = runtime_context_from_headers(headers)
    payload = body if isinstance(body, dict) else {}
    body_full_prompt = str(payload.get("full_prompt") or "").strip()
    if body_full_prompt:
        context["full_prompt"] = body_full_prompt
        context["full_prompt_source"] = str(
            payload.get("full_prompt_source") or context.get("full_prompt_source") or "body_contract"
        ).strip()
    body_raw_prompt = str(payload.get("raw_user_prompt") or "").strip()
    if body_raw_prompt:
        context["raw_user_prompt"] = body_raw_prompt
    return context


class ProjectStateStore:
    """Atomic, local-only current-project authority ledger.

    The ledger deliberately stores only the current task/source pointers and
    correction receipts. Durable facts still live in the shared Hindsight bank.
    """
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        try:
            state = load_json(path) if path.exists() else {}
        except Exception:
            state = {}
        self.state = state if isinstance(state, dict) else {}
        self.state.setdefault("schema", 1)
        self.state.setdefault("projects", {})

    def touch(self, context: dict[str, Any], query: str) -> dict[str, Any]:
        key = str(context.get("project_key") or "")
        if not key:
            return {"available": False, "reason": "当前调用没有提供项目上下文。"}
        now = utc_now()
        with self.lock:
            projects = self.state.setdefault("projects", {})
            row = dict(projects.get(key) or {})
            row.update({
                "project_key": key,
                "project_cwd": str(context.get("project_cwd") or row.get("project_cwd") or "")[:1024],
                "last_seen_at": now,
                "last_session_id": str(context.get("session_id") or row.get("last_session_id") or "")[:160],
                "last_prompt_preview": latest_query(query)[:480],
            })
            sources = list(row.get("sources") or [])[-12:]
            if context.get("source_authority"):
                source = {
                    "fingerprint": str(context.get("source_fingerprint") or hashlib.sha256(str(context.get("source_label") or "").encode()).hexdigest()[:16]),
                    "label": str(context.get("source_label") or "当前附件/打开文件")[:480],
                    "kind": str(context.get("source_kind") or "attachment"),
                    "seen_at": now,
                    "status": "source_first_pending_inspection",
                }
                if not sources or sources[-1].get("fingerprint") != source["fingerprint"]:
                    sources.append(source)
                else:
                    sources[-1] = source
                row["active_source"] = source
                row["authority_mode"] = "source_first"
            # An attached/open current source remains the authority even when
            # the wording also looks like a correction (for example: "以这份
            # 最新附件为准").  Record ordinary corrections only when there is
            # no newer source object to inspect first.
            if context.get("user_correction") and not context.get("source_authority"):
                corrections = list(row.get("corrections") or [])[-24:]
                corrections.append({"at": now, "preview": latest_query(query)[:360], "kind": "explicit_user_correction"})
                row["corrections"] = corrections
                row["authority_mode"] = "current_user_correction"
            row["sources"] = sources
            projects[key] = row
            # Keep the ledger bounded even after long-term use.
            if len(projects) > 120:
                oldest = sorted(projects, key=lambda item: str((projects[item] or {}).get("last_seen_at") or ""))[:-120]
                for item in oldest:
                    projects.pop(item, None)
            atomic_json(self.path, self.state)
            return self._summary(row)

    def record_correction(self, event: dict[str, Any]) -> None:
        key = str(event.get("project_key") or "")
        if not key:
            return
        with self.lock:
            row = dict((self.state.setdefault("projects", {})).get(key) or {})
            if not row:
                return
            rejected = list(row.get("rejected_memory_ids") or [])
            for value in event.get("corrected_ids") or []:
                if value and value not in rejected:
                    rejected.append(str(value)[:160])
            row["rejected_memory_ids"] = rejected[-100:]
            row["last_correction_feedback_at"] = utc_now()
            row["authority_mode"] = "current_user_correction"
            self.state["projects"][key] = row
            atomic_json(self.path, self.state)

    def rejected_ids(self, context: dict[str, Any]) -> set[str]:
        key = str(context.get("project_key") or "")
        with self.lock:
            row = (self.state.get("projects") or {}).get(key) or {}
            return {str(value) for value in (row.get("rejected_memory_ids") or []) if value}

    def summary(self, context: dict[str, Any]) -> dict[str, Any]:
        key = str(context.get("project_key") or "")
        with self.lock:
            row = ((self.state.get("projects") or {}).get(key) or {}) if key else {}
            return self._summary(row) if row else {"available": False, "reason": "当前项目尚无实时状态记录。"}

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        source = dict(row.get("active_source") or {})
        return {
            "available": bool(row),
            "project_key": row.get("project_key"),
            "project_cwd": row.get("project_cwd"),
            "last_seen_at": row.get("last_seen_at"),
            "last_session_id": row.get("last_session_id"),
            "authority_mode": row.get("authority_mode") or "shared_memory_fallback",
            "active_source": source or None,
            "source_count": len(row.get("sources") or []),
            "correction_count": len(row.get("corrections") or []),
            "rejected_memory_count": len(row.get("rejected_memory_ids") or []),
            "last_prompt_preview": row.get("last_prompt_preview") or "",
        }


class ControllerState:
    def __init__(self, status_path: Path):
        self.lock = threading.Lock()
        self.status_path = status_path
        self.started_at = utc_now()
        self.values: dict[str, Any] = {
            "status": "starting",
            "version": VERSION,
            "started_at": self.started_at,
            "plans": 0,
            "recalls": 0,
            "noop_recalls": 0,
            "multi_query_recalls": 0,
            "fallbacks": 0,
            "errors": 0,
            "active_reads": 0,
            "effectiveness_events": 0,
            "functional_status": "starting",
            "last_plan": None,
            "last_error": None,
            "last_error_at": None,
        }

    def update(self, **changes: Any) -> None:
        with self.lock:
            for key, value in changes.items():
                if key.startswith("inc_"):
                    name = key[4:]
                    self.values[name] = int(self.values.get(name) or 0) + int(value)
                else:
                    self.values[key] = value
            self.values["updated_at"] = utc_now()
            atomic_json(self.status_path, self.values)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.values)


class MemoryQueryController:
    def __init__(
        self,
        contract_path: Path,
        policy_path: Path,
        upstream_url: str,
        audit_path: Path,
        status_path: Path,
        effectiveness_path: Path = DEFAULT_EFFECTIVENESS_AUDIT,
    ):
        self.contract_path = contract_path
        self.policy_path = policy_path
        self.upstream_url = upstream_url.rstrip("/")
        self.audit_path = audit_path
        self.effectiveness_path = effectiveness_path
        self.effectiveness_receipt_index_path = DEFAULT_EFFECTIVENESS_RECEIPT_INDEX
        self.effectiveness_receipt_index_lock = threading.Lock()
        self.trace_index_path = DEFAULT_TRACE_INDEX
        self.trace_index_lock = threading.Lock()
        self.project_states = ProjectStateStore(DEFAULT_PROJECT_STATE)
        self.ham_api = HamApi(contract_path=contract_path)
        self.status = ControllerState(status_path)
        self.reload()
        # The same Hindsight-owned Qwen model is used only after deterministic
        # routing detects structural ambiguity.  It is serialized and optional:
        # a busy/erroring model must never block or downgrade normal recall.
        self.semantic_planner = HindsightSemanticPlanner(self.config)
        self.read_semaphore = threading.BoundedSemaphore(
            int(self.config.get("globalReadLimit", 4))
        )
        fast_cfg = dict(self.config.get("fastRecall") or {})
        self.fast_recall_enabled = bool(fast_cfg.get("enabled", False))
        self.fast_bank = FastMemoryBank(
            snapshot_path=Path(str(fast_cfg.get("snapshotPath") or DEFAULT_FAST_MEMORY_SNAPSHOT)).expanduser(),
            index_path=Path(str(fast_cfg.get("indexPath") or DEFAULT_FAST_MEMORY_INDEX)).expanduser(),
        )
        # Build/open the derived index before accepting the first Hook request.
        # A one-time local parse is preferable to allowing the first real user
        # turn to hit the same zero-memory race that this lane is designed to
        # prevent. Unit/test contracts leave this feature disabled.
        fast_ready = self.fast_bank.ensure_ready() if self.fast_recall_enabled else False
        self.mental_model_lock = threading.Lock()
        self.mental_model_cache: dict[str, dict[str, Any]] = {}
        # Same request fan-in prevents Hooks, role adapters and retries from
        # multiplying identical work.  The stable key includes bank, query,
        # requested memory types and recall profile; unrelated questions never
        # share a response.
        self.inflight_lock = threading.Lock()
        self.inflight_requests: dict[str, dict[str, Any]] = {}
        # A Hook can deliver the same user turn twice through adjacent adapters
        # (for example a focused first pass followed by an automatic deep pass).
        # Keep a tiny in-memory foreground result cache so the second delivery
        # reuses the first real receipt instead of creating a false zero-injection
        # trace. This is intentionally short-lived and never persisted as memory.
        self.foreground_reuse_lock = threading.Lock()
        self.foreground_reuse_cache: dict[str, dict[str, Any]] = {}
        self.continuation_lock = threading.Lock()
        self.continuation_persist_lock = threading.Lock()
        self.continuation_cache_path = Path(
            str(self.config.get("continuationCachePath") or DEFAULT_CONTINUATION_CACHE)
        ).expanduser()
        self.continuation_cache = (
            load_json(self.continuation_cache_path)
            if self.continuation_cache_path.exists() and self.continuation_cache_path.stat().st_size <= int(self.config.get("continuationCacheMaxBytes",16*1024*1024))
            else {"schema": 1, "items": {}}
        )
        if not isinstance(self.continuation_cache, dict):
            self.continuation_cache = {"schema": 1, "items": {}}
        self.continuation_cache.setdefault("schema", 1)
        self.continuation_cache.setdefault("items", {})
        self.continuation_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hindsight-continuation")
        self.status.update(
            status="healthy",
            functional_status="healthy",
            relevance_policy=RELEVANCE_POLICY,
            continuation_cache_identity="controller_version+relevance_policy+bank+query+types+budget+profile+session",
            upstream_url=self.upstream_url,
            contract_schema=self.contract.get("schema"),
            contract_sha256=hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            fast_recall={
                "enabled": self.fast_recall_enabled,
                "ready": fast_ready,
                "index_path": str(self.fast_bank.index_path),
                "snapshot_path": str(self.fast_bank.snapshot_path),
                "load_error": self.fast_bank.load_error or None,
            },
        )

    def _append_audit_event(self, event: dict[str, Any]) -> None:
        """Append the immutable audit, then refresh its bounded status view."""
        append_jsonl(self.audit_path, event)
        if str(event.get("event") or "") in {"recall", "recall_failed"}:
            # A few narrow unit tests construct a bare controller via
            # ``__new__``. Keep their temporary audit isolated while retaining
            # the production default initialized in ``__init__``.
            trace_index_path = getattr(
                self, "trace_index_path", self.audit_path.with_suffix(".trace-index.json")
            )
            trace_index_lock = getattr(self, "trace_index_lock", None)
            if trace_index_lock is None:
                trace_index_lock = threading.Lock()
                self.trace_index_lock = trace_index_lock
            with trace_index_lock:
                upsert_trace_index(trace_index_path, event)

    def reload(self) -> None:
        self.contract = load_json(self.contract_path)
        if self.contract.get("schema") != 5:
            raise ValueError("memory contract schema must be 5")
        self.policy = load_json(self.policy_path)
        self.config = dict(self.contract.get("queryController") or {})

    def plan(
        self,
        query: str,
        role: str,
        client: str,
        bank_id: str = "",
        recall_profile: str = "",
        runtime_context: dict[str, Any] | None = None,
        agent_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime_context = dict(runtime_context or {})
        # A cassette replay must be observational: it can read the current
        # project state but cannot advance a project cursor or persist a
        # synthetic correction.  This makes replay results comparable to a
        # real Hook without changing a user's next production decision.
        if str(runtime_context.get("execution_mode") or "").casefold() in {"replay", "shadow_replay", "cassette_replay"} or runtime_context.get('prompt_origin')=='test_probe':
            runtime_context["project_state"] = self.project_states.summary(runtime_context)
        else:
            runtime_context["project_state"] = self.project_states.touch(runtime_context, query)
        # First produce the fully deterministic plan.  This is both the fast
        # path and the authority baseline: no external hint can turn a no-op
        # working-set instruction into a broad memory read or change source
        # access.  Codex/Hermes may supply a validated semantic plan on a
        # later/explicit pass; otherwise Qwen reviews only uncertain structure.
        deterministic_plan = build_plan(
            query, role, self.policy, self.config, client=client, bank_id=bank_id,
            recall_profile=recall_profile, runtime_context=runtime_context,
        )
        trigger = semantic_planner_needed(query)
        allowed_agent_sources = set(
            (self.config.get("semanticPlanner") or {}).get("agentPlan", {}).get("acceptedSources") or []
        )
        accepted_agent_plan = (
            normalize_semantic_plan(agent_plan, allowed_source="agent")
            if role in allowed_agent_sources else None
        )
        selected_semantic_plan = accepted_agent_plan
        # The Hook first asks for a plan under a sub-second budget.  That
        # request is an admission/routing probe, not the recall itself: never
        # wait for the optional Qwen semantic planner here or a valid
        # context-aware deterministic plan degrades to the legacy router.
        # The subsequent recall request is still allowed to use Qwen.
        planning_only = bool(runtime_context.get("planning_only"))
        if (not planning_only and selected_semantic_plan is None
                and deterministic_plan.get("memory_action") != "noop_long_term"):
            selected_semantic_plan = self.semantic_planner.plan(query, trigger)
        plan = deterministic_plan
        if selected_semantic_plan is not None and deterministic_plan.get("memory_action") != "noop_long_term":
            plan = build_plan(
                query, role, self.policy, self.config, client=client, bank_id=bank_id,
                recall_profile=recall_profile, runtime_context=runtime_context,
                semantic_plan=selected_semantic_plan,
            )
        planner = dict(plan.get("planner") or {})
        planner["trigger"] = trigger
        planner["agent_plan_received"] = bool(agent_plan)
        planner["semantic_plan_review"] = plan.get("semantic_plan_review")
        plan["planner"] = planner
        self.status.update(
            inc_plans=1,
            functional_status="healthy",
            last_error=None,
            last_error_at=None,
            last_plan={
                "at": utc_now(),
                "query_id": plan["query_id"],
                "execution_id": plan["execution_id"],
                "query_fingerprint": plan["query_fingerprint"],
                "role": role,
                "client": client,
                "primary_shape": plan["primary_shape"],
                "query_count": plan["query_count"],
                "input_query_tokens": plan["input_query_tokens"],
                "query_compacted": plan["query_compacted"],
                "project_state": plan.get("project_state"),
                "source_guard": plan.get("source_guard"),
                "planner": plan.get("planner"),
            },
        )
        return plan

    def upstream(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        url = self.upstream_url + path
        outgoing = {
            key: value for key, value in headers.items()
            if key.casefold() not in HOP_HEADERS
        }
        outgoing["X-Memory-Controller-Bypass"] = "1"
        req = urllib.request.Request(url, data=body, headers=outgoing, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()

    def upstream_json(
        self,
        path: str,
        value: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        with self.read_semaphore:
            self.status.update(inc_active_reads=1)
            try:
                status, _, body = self.upstream(
                    "POST",
                    path,
                    json.dumps(value, ensure_ascii=False).encode("utf-8"),
                    headers,
                    timeout,
                )
                if status < 200 or status >= 300:
                    raise RuntimeError(f"upstream HTTP {status}: {body[:500]!r}")
                return json.loads(body.decode("utf-8"))
            finally:
                self.status.update(inc_active_reads=-1)

    def anchored_evidence_lookup(
        self,
        bank_id: str,
        anchor: str,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Resolve an explicit literal anchor using Hindsight's existing list API.

        This is a source/provenance fallback, not a second retrieval index.  We
        only retain candidates that literally contain the normalized anchor, so
        a similar-but-different current fact cannot silently replace an older
        source record.  Ambiguous matches are returned together for the caller
        to see; the controller never declares one of them “the” source.
        """
        normalized = compact_for_literal_match(anchor)
        path = (
            "/v1/default/banks/"
            + urllib.parse.quote(bank_id, safe="")
            + "/memories/list?"
            + urllib.parse.urlencode({"q": anchor, "limit": 12, "offset": 0})
        )
        started = time.monotonic()
        receipt: dict[str, Any] = {
            "bank": bank_id,
            "anchor": anchor[:DIRECT_EVIDENCE_MAX_ANCHOR_CHARS],
            "endpoint": "official_memory_list",
            "status": "completed",
            "candidate_count": 0,
            "literal_match_count": 0,
            "elapsed_ms": 0.0,
            "error": None,
        }
        try:
            with self.read_semaphore:
                self.status.update(inc_active_reads=1)
                try:
                    status, _, body = self.upstream("GET", path, None, headers, timeout)
                finally:
                    self.status.update(inc_active_reads=-1)
            if status < 200 or status >= 300:
                raise RuntimeError(f"upstream HTTP {status}: {body[:300]!r}")
            payload = json.loads(body.decode("utf-8"))
            items = list(payload.get("items") or [])
            # The official list call is itself bounded to 12 candidates.  For
            # an explicit provenance request, keep every exact match it returns:
            # truncating to three could hide the sought original behind nearby
            # duplicates, even though all four were locally verified.
            matches = [
                item for item in items
                if normalized in compact_for_literal_match(str(item.get("text") or item.get("content") or ""))
            ][:DIRECT_EVIDENCE_MAX_ITEMS_PER_BANK]
            receipt.update({
                "candidate_count": len(items),
                "literal_match_count": len(matches),
                "matched_ids": [str(item.get("id") or item.get("chunk_id") or "")[:80] for item in matches],
            })
            return matches, receipt
        except Exception as error:
            receipt.update({"status": "failed", "error": repr(error)})
            return [], receipt
        finally:
            receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)

    def graph_closure_lookup(
        self,
        bank_id: str,
        query: str,
        headers: dict[str, str],
        timeout: float,
        seed_memory_ids: list[str] | None = None,
        seed_entity_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Discover relationship candidates through Hindsight's official graph.

        The graph is an evidence source, not a permission to inject every
        neighbour.  We read a bounded query-filtered subgraph, then fetch its
        canonical memory records.  Those records go through exactly the same
        semantic admission, temporal governance and adaptive-token selection
        as normal recall results.  This avoids the previous blind spot where
        constellation/graph data existed only in the UI and entity rank bonus.
        """
        started = time.monotonic()
        graph_cfg = self.config
        graph_max_hops = max(1, min(3, int(graph_cfg.get("graphMaxHops", 2))))
        graph_max_nodes = max(10, min(120, int(graph_cfg.get("graphMaxNodes", 80))))
        graph_max_candidates = max(1, min(20, int(graph_cfg.get("graphMaxCandidates", 10))))
        graph_max_seed_queries = max(1, min(5, int(graph_cfg.get("graphMaxSeedQueries", 3))))
        graph_latency_ms = max(500, min(12000, int(graph_cfg.get("graphLatencyMs", 6000))))
        timeout = min(max(0.25, float(timeout)), graph_latency_ms / 1000.0)
        receipt: dict[str, Any] = {
            "enabled": True,
            "endpoint": "official_memory_graph",
            "status": "completed",
            "node_count": 0,
            "edge_count": 0,
            "candidate_count": 0,
            "selected_count": 0,
            "max_hops_examined": 0,
            "budget": {"max_hops": graph_max_hops, "max_nodes": graph_max_nodes,
                       "max_candidates": graph_max_candidates, "latency_ms": graph_latency_ms,
                       "max_seed_queries": graph_max_seed_queries},
            "selected_ids": [],
            "error": None,
            "reason": "通过官方记忆图谱发现与本题实体、决定或流程相连的候选；候选仍须通过统一相关性和时序治理。",
        }
        try:
            # The graph endpoint's q filter is intentionally literal enough
            # that a full natural-language question can return no nodes even
            # when its named entity is richly connected.  Query the original
            # request *and* verified concrete anchors (aliases/ASCII names),
            # then union the bounded subgraphs.  This is not vector expansion:
            # every extra seed is visibly present in the user's request.
            # Extract concrete, user-written relation endpoints before the
            # whole sentence.  Official graph filtering is literal; for
            # “云深处科技和宇树科技是不是同一实体” the useful seeds are the
            # two names, not the entire interrogative sentence.
            relation_text = full_prompt_text(query)
            graph_stop_names = {"用户", "问题", "内容", "信息", "记忆", "图谱", "实体", "要求", "应该", "什么", "怎样", "为什么", "这个", "那个", "所有", "全部", "当前", "历史", "方案", "观察", "心智模型"}
            # The helper deliberately orders explicit anchors and approved
            # aliases before generic prose.  The old inline extraction put
            # generic Chinese fragments first and could remove “Hindsight”
            # before the three-probe cap was applied.
            graph_queries = build_graph_seed_queries(relation_text, limit=3)
            # Keep the graph lane bounded.  The endpoint builds a potentially
            # dense subgraph; four literal probes plus four chunk probes could
            # consume the entire 25s Codex Hook budget and leave no Output
            # receipt.  User-written endpoints take priority, followed by one
            # full-question fallback, never an unbounded seed fan-out.
            graph_queries = list(dict.fromkeys(value for value in graph_queries if value.strip()))[:graph_max_seed_queries]
            receipt["seed_queries"] = [value[:120] for value in graph_queries]
            seed_memory_ids = list(dict.fromkeys(str(value or "") for value in (seed_memory_ids or []) if str(value or "")))[:4]
            receipt["seed_memory_ids"] = seed_memory_ids
            # Keep first-pass entities only as an audit receipt.  They are not
            # graph query seeds: a broad semantic hit can name unrelated
            # tooling and would make the graph expansion drift off-topic.
            receipt["first_pass_entities"] = list(dict.fromkeys(str(value)[:160] for value in (seed_entity_names or []) if str(value or "")))[:16]
            all_nodes: dict[str, dict[str, Any]] = {}
            all_edges: dict[str, dict[str, Any]] = {}
            graph_requests: list[dict[str, str]] = [{"q": graph_query, "limit": str(graph_max_nodes)} for graph_query in graph_queries]
            # A completed semantic facet supplies canonical memory IDs.  Asking
            # the official graph for those seed nodes is more reliable than
            # using an entire Chinese natural-language sentence as a literal
            # graph filter, and is a genuine one/two-hop traversal rather than
            # a second semantic search.
            # A semantic seed graph lookup is used only as a fallback when the
            # user did not write a concrete entity/anchor.  When anchors are
            # present, adding arbitrary first-pass chunks both wastes the
            # latency reserve and can drift to co-mentioned projects.
            if not graph_requests:
                graph_requests.extend({"chunk_id": memory_id, "limit": "80"} for memory_id in seed_memory_ids)
            for graph_params in graph_requests:
                if time.monotonic() >= started + max(0.25, timeout):
                    break
                graph_path = (
                    "/v1/default/banks/"
                    + urllib.parse.quote(bank_id, safe="")
                    + "/graph?"
                    + urllib.parse.urlencode(graph_params)
                )
                with self.read_semaphore:
                    self.status.update(inc_active_reads=1)
                    try:
                        status, _, raw = self.upstream("GET", graph_path, None, headers, max(0.25, started + max(0.25, timeout) - time.monotonic()))
                    finally:
                        self.status.update(inc_active_reads=-1)
                if status < 200 or status >= 300:
                    continue
                graph = json.loads(raw.decode("utf-8"))
                for row in list(graph.get("nodes") or []):
                    data = dict(row.get("data") or {})
                    node_id = str(data.get("id") or "")
                    if node_id:
                        all_nodes[node_id] = data
                for row in list(graph.get("edges") or []):
                    data = dict(row.get("data") or {})
                    edge_id = str(data.get("id") or "")
                    if edge_id:
                        all_edges[edge_id] = data
            nodes = [{"data": value} for value in all_nodes.values()]
            edges = [{"data": value} for value in all_edges.values()]
            receipt["node_count"] = len(nodes)
            receipt["edge_count"] = len(edges)
            node_by_id = {
                str((row.get("data") or {}).get("id") or ""): dict(row.get("data") or {})
                for row in nodes
                if str((row.get("data") or {}).get("id") or "")
            }
            degree: dict[str, int] = {node_id: 0 for node_id in node_by_id}
            for edge in edges:
                data = edge.get("data") or {}
                source, target = str(data.get("source") or ""), str(data.get("target") or "")
                if source in degree:
                    degree[source] += 1
                if target in degree and target != source:
                    degree[target] += 1
            # The filtered graph already anchors candidates to the request,
            # but a broad seed such as ``Codex`` can contribute thousands of
            # high-degree nodes.  Selecting only by degree used to crowd the
            # concrete ``文件夹迁移任务`` nodes out of the ten-node budget.
            # Rank lexical endpoint/relation support first, then graph degree;
            # this preserves graph discovery while keeping the candidate cap
            # deterministic and topic-specific.
            endpoint_terms = [
                _entity_normalize(value) for value in graph_queries
                if _usable_entity_form(value) and _entity_normalize(value) not in graph_stop_names
            ]
            relation_cues = [
                value for value in ("全称", "别名", "同一", "独立", "关系", "关联", "步骤", "回滚", "当前", "历史", "观察", "心智模型", "来源", "时间", "同步", "备份", "注入", "检索", "交付", "渲染", "依赖", "纠正", "替代")
                if value in relation_text
            ]

            def node_support(node_id: str) -> tuple[int, int, int, int, str]:
                node_text = _entity_normalize(str(node_by_id[node_id].get("text") or node_by_id[node_id].get("label") or ""))
                endpoint_hits = sum(1 for term in endpoint_terms if term and term in node_text)
                relation_hits = sum(1 for cue in relation_cues if cue in node_text)
                return (
                    -endpoint_hits,
                    -relation_hits,
                    -int(bool(degree.get(node_id, 0))),
                    -degree.get(node_id, 0),
                    str(node_by_id[node_id].get("date") or ""),
                )

            candidate_ids = sorted(
                node_by_id,
                key=node_support,
                reverse=False,
            )[:graph_max_candidates]
            receipt["candidate_count"] = len(candidate_ids)
            receipt["max_hops_examined"] = graph_max_hops if any(degree.get(node_id, 0) for node_id in candidate_ids) else 0
            def fetch_memory(memory_id: str) -> dict[str, Any] | None:
                memory_path = (
                    "/v1/default/banks/"
                    + urllib.parse.quote(bank_id, safe="")
                    + "/memories/"
                    + urllib.parse.quote(memory_id, safe="")
                )
                with self.read_semaphore:
                    self.status.update(inc_active_reads=1)
                    try:
                        status, _, raw = self.upstream("GET", memory_path, None, headers, max(0.25, started + max(0.25, timeout) - time.monotonic()))
                    finally:
                        self.status.update(inc_active_reads=-1)
                if status < 200 or status >= 300:
                    return None
                item = json.loads(raw.decode("utf-8"))
                metadata = dict(item.get("metadata") or {})
                metadata["_ccy_graph_evidence"] = {
                    "endpoint": "official_memory_graph",
                    "node_id": memory_id,
                    "graph_degree": degree.get(memory_id, 0),
                    "max_hops_examined": receipt["max_hops_examined"],
                    "reason": "该记忆位于本题官方图谱筛出的关联子图内；仍需统一相关性和时序复核。",
                }
                item["metadata"] = metadata
                return item
            items: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=min(4, len(candidate_ids) or 1)) as pool:
                futures = [pool.submit(fetch_memory, memory_id) for memory_id in candidate_ids]
                for future in as_completed(futures):
                    item = future.result()
                    if item:
                        items.append(item)
            # A graph edge proves that two memories are connected, not that
            # either answers this exact relation.  Require an explicit entity
            # endpoint plus a requested relation/scope cue (or two explicit
            # endpoints) before a graph candidate joins normal ranking.  This
            # blocks neighbours such as an unrelated image rule that happens
            # to mention the same company.
            graph_supported: list[dict[str, Any]] = []
            for item in items:
                text = _entity_normalize(str(item.get("text") or item.get("content") or ""))
                endpoint_hits = sum(1 for term in endpoint_terms if term and term in text)
                relation_hits = sum(1 for cue in relation_cues if cue in text)
                if endpoint_hits >= 2 or (endpoint_hits >= 1 and relation_hits >= 1) or (not endpoint_terms and relation_hits >= 2):
                    graph_supported.append(item)
            receipt["fetched_count"] = len(items)
            receipt["relevance_supported_count"] = len(graph_supported)
            items = graph_supported
            receipt["selected_count"] = len(items)
            receipt["selected_ids"] = [str(item.get("id") or item.get("chunk_id") or "")[:80] for item in items]
            return items, receipt
        except Exception as error:
            receipt.update({"status": "failed", "error": repr(error)})
            return [], receipt
        finally:
            receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)

    def stable_mental_model_sidecar(
        self,
        bank_id: str,
        query: str,
        plan: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Select fresh *sections* of official mental models as guidance.

        The official Recall API intentionally accepts only world/experience/
        observation. Mental models have their own official endpoint. This
        method respects that boundary, ignores stale models and creates normal
        candidate records so the common relevance and dynamic-budget gates can
        still reject them. It is a cache, not a second memory store.
        """
        receipt: dict[str, Any] = {
            "enabled": True, "endpoint": "official_mental_models_list",
            "status": "completed", "candidate_count": 0, "selected_count": 0,
            "excluded_stale": 0, "cache_hit": False, "reason": plan.get("guidance_sidecar", {}).get("reason"),
        }
        now = time.monotonic()
        with self.mental_model_lock:
            cached = dict(self.mental_model_cache.get(bank_id) or {})
        rows = cached.get("items") if now - float(cached.get("at") or 0) < MENTAL_MODEL_CACHE_TTL_SECONDS else None
        if rows is not None:
            receipt["cache_hit"] = True
        else:
            path = "/v1/default/banks/" + urllib.parse.quote(bank_id, safe="") + "/mental-models?detail=content&limit=100"
            try:
                with self.read_semaphore:
                    self.status.update(inc_active_reads=1)
                    try:
                        status, _, raw = self.upstream("GET", path, None, headers, max(0.25, timeout))
                    finally:
                        self.status.update(inc_active_reads=-1)
                if status < 200 or status >= 300:
                    raise RuntimeError(f"upstream mental-model list HTTP {status}: {raw[:300]!r}")
                payload = json.loads(raw.decode("utf-8"))
                rows = list(payload.get("items") or [])
                rows, freshness_receipt = verified_mental_model_rows(bank_id, rows)
                receipt["freshness_receipt"] = freshness_receipt
                with self.mental_model_lock:
                    self.mental_model_cache[bank_id] = {"at": now, "items": rows}
            except Exception as error:
                receipt.update({"status": "failed", "error": repr(error)})
                return [], receipt
        terms = guidance_query_terms(query)
        candidate_rows = []
        for model in rows or []:
            if bool(model.get("is_stale")):
                receipt.setdefault('stale_models_considered',0);receipt['stale_models_considered']+=1
            content = str(model.get("content") or "")
            if not content:
                continue
            # Preserve a title with each independently admitted section.
            title_match = re.search(r"^#\s+(.+)$", content, re.M)
            model_title = str(model.get("name") or (title_match.group(1).strip() if title_match else model.get("id") or "心智模型"))
            # Some consolidated models use bold-labelled paragraphs instead of
            # Markdown ``##`` headings. Split both forms so one matching
            # Hindsight bullet cannot inject unrelated cashflow/video/HR
            # material from the same broad risk chapter.
            sections = re.split(r"(?=^##\s+|^\s*(?:[-*]\s+)?\*\*[^*\n]{2,80}\*\*\s*[:：])", content, flags=re.M)
            for section in sections:
                section = section.strip()
                if not section:
                    continue
                heading = re.search(r"^#{1,3}\s+(.+)$", section, re.M)
                section_title = heading.group(1).strip() if heading else model_title
                haystack = (model_title + "\n" + section_title + "\n" + section).casefold()
                body = re.sub(r"^#{1,3}\s+.+?(?:\n|$)", "", section, count=1, flags=re.M).casefold()
                body_hits = [term for term in terms if term and term.casefold() in body]
                title_hits = [term for term in terms if term and term.casefold() in (model_title + " " + section_title).casefold()]
                # The root # title is only a wrapper.  Do not inject it merely
                # because a broad word matches the model name: require a
                # substantive section/body match so users see the rule that
                # actually informed the answer.
                if section_title == model_title and not body_hits:
                    continue
                score = len(body_hits) + 2 * len(title_hits)
                if score:
                    candidate_rows.append((score, model, section, section_title))
        receipt["candidate_count"] = len(candidate_rows)
        max_chars = min(max(900, int(plan.get("max_tokens") or 1200) * 2), 3600)
        selected: list[dict[str, Any]] = []
        used_chars = 0
        seen_models: set[str] = set()
        for score, model, section, section_title in sorted(candidate_rows, key=lambda row: row[0], reverse=True):
            model_id = str(model.get("id") or "")
            # A model is a coherent framework; inject its strongest section once
            # rather than duplicating overlapping chapters.
            if model_id in seen_models:
                continue
            source_revalidation={}
            if model.get('is_stale'):
                from lib.guidance_provenance import revalidate_model_section
                source_revalidation=revalidate_model_section(model,section,timeout=min(.75,max(.1,timeout)))
                if not source_revalidation['allowed']:
                    receipt['excluded_stale']+=1
                    receipt.setdefault('section_source_checks',[]).append({'model_id':model_id,'section':section_title,**source_revalidation})
                    continue
                receipt.setdefault('source_revalidated_sections',0);receipt['source_revalidated_sections']+=1
            compact_section=compact_guidance_section(section, max_chars=min(MENTAL_MODEL_SECTION_MAX_CHARS,560))
            text = f"【心智模型｜{model.get('name') or model_id}｜{section_title}】\n{compact_section}"
            if source_revalidation:
                text='【历史模型章节：已回读引用来源；新增证据覆盖未穷尽，当前事实与要求优先】\n'+text+'\n来源记录：'+','.join(source_revalidation['source_ids'])
            if selected and used_chars + len(text) > max_chars:
                continue
            metadata = {
                "source_class": "mental_model",
                "mental_model_id": model_id,
                "mental_model_section": section_title,
                "mental_model_section_gate": True,
                "is_stale": bool(model.get('is_stale')),
                "semantic_body": section,
                "guidance_compacted": len(compact_section)<len(section),
                "guidance_original_chars": len(section),
                "guidance_delivery_chars": len(compact_section),
                "source_revalidated_reference": bool(source_revalidation.get('allowed')),
                "source_revalidation": source_revalidation,
                "_ccy_admission": {
                    "decision": "candidate",
                    "reason": "稳定心智模型与本轮解释/决策信号匹配；仅取相关章节，仍需通过统一相关性和预算准入。",
                    "source_class": "mental_model",
                    "mental_model_section_gate": True,
                },
            }
            selected.append({
                "id": f"mental-model:{model_id}:{hashlib.sha256(section_title.encode()).hexdigest()[:12]}",
                "text": text, "type": "mental_model", "metadata": metadata,
                "tags": list(model.get("tags") or []), "scores": {"final": min(1.0, 0.45 + 0.08 * score)},
            })
            seen_models.add(model_id)
            used_chars += len(text)
        receipt["selected_count"] = len(selected)
        receipt["budget_chars"] = max_chars
        return selected, receipt

    def direct_policy_sidecar(self, query: str, plan: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Load auditable direct-user policies locally; no model/API call occurs."""
        receipt: dict[str, Any] = {"enabled": True, "status": "completed", "candidate_count": 0, "selected_count": 0,
                                   "reason": "尚无可读取的直接政策索引。", "store": str(DEFAULT_DIRECT_POLICY_INDEX)}
        try:
            index = load_json(DEFAULT_DIRECT_POLICY_INDEX)
            active = [dict(row) for row in (index.get("policies") or []) if row.get("status") == "active_provisional"]
            receipt["candidate_count"] = len(active)
            if not active:
                receipt["reason"] = "没有已审核的跨任务直接政策。"
                return [], receipt
            compact = re.sub(r"\s+", "", full_prompt_text(query)).casefold()
            terms = guidance_query_terms(query)
            candidates: list[tuple[int, dict[str, Any], list[str]]] = []
            generic = {"这个", "那个", "这些", "问题", "什么", "怎么", "如何", "为什么", "是否", "是不是", "现在", "需要", "一下", "我们", "相关"}
            for row in active:
                keywords = [str(x).casefold() for x in (row.get("keywords") or []) if len(str(x).strip()) >= 2]
                keyword_hits = sorted({x for x in keywords if x not in generic and x in compact})
                policy_text = str(row.get("policy") or row.get("original_text") or "")
                lexical_hits = [term for term in terms if len(term) >= 2 and term in re.sub(r"\s+", "", policy_text).casefold()]
                score = 4 * len(keyword_hits) + len(lexical_hits)
                # Keep all possible candidates in the trace; custom admission
                # below decides injection, not a hidden count cap.
                if score:
                    candidates.append((score, row, keyword_hits))
            items: list[dict[str, Any]] = []
            for score, row, keyword_hits in sorted(candidates, key=lambda x: x[0], reverse=True):
                policy = str(row.get("policy") or row.get("original_text") or "").strip()
                if not policy:
                    continue
                scope = str(row.get("scope") or "跨任务可复用范围待后续证据确认")
                boundary = str(row.get("boundary") or "不替代当前任务事实；不适用于无关场景。")
                text = f"【用户直接政策｜待后续验证】\n{policy[:DIRECT_POLICY_SECTION_MAX_CHARS]}\n适用范围：{scope}\n边界：{boundary}"
                items.append({
                    "id": f"direct-policy:{row.get('source_memory_id') or row.get('policy_id')}", "text": text,
                    "type": "direct_policy", "scores": {"final": min(1.0, 0.35 + 0.12 * score)},
                "metadata": {"source_class": "direct_policy", "direct_policy_gate": True,
                                 "policy_status": "active_provisional", "policy_id": row.get("policy_id"),
                                 "source_memory_id": row.get("source_memory_id"), "source_document_id": row.get("source_document_id"),
                                 "direct_policy_keywords": list(row.get("keywords") or []), "direct_policy_keyword_hits": keyword_hits,
                                 "direct_policy_scope": scope, "direct_policy_boundary": boundary,
                                 "evidence_status": row.get("evidence_status"),
                                 "_ccy_admission": {"decision": "candidate", "reason": "用户直接政策命中局部锚点；仍须统一准入。", "source_class": "direct_policy"}},
                })
            receipt.update({"selected_count": len(items), "reason": "仅把命中具体锚点的已审核用户直接政策交给统一准入；不设置条数上限。"})
            return items, receipt
        except Exception as error:
            receipt.update({"status": "failed", "error": repr(error), "reason": "读取直接政策索引失败；不影响常规 Hindsight 召回。"})
            return [], receipt

    def _recall_key(self, path: str, body: dict[str, Any], headers: dict[str, str], bank_id: str) -> str:
        runtime = runtime_context_from_headers(headers)
        payload = {
            # A background/deep result is valid only for the controller and
            # relevance policy that produced it.  Without these fields a
            # policy upgrade could keep serving yesterday's selected memories
            # for 24 hours, making a fixed false negative appear unchanged.
            "controller_version": VERSION,
            "relevance_policy": RELEVANCE_POLICY,
            "bank": urllib.parse.unquote(bank_id),
            "query": request_semantic_query(body),
            "types": list(body.get("types") or []),
            "budget": str(body.get("budget") or ""),
            "max_tokens": int(body.get("max_tokens") or 0),
            "profile": str(headers.get("X-Memory-Recall-Profile") or "").casefold(),
            "session_id": str(runtime.get("session_id") or ""),
            "execution_mode": str(runtime.get("execution_mode") or "production"),
            "evaluation_as_of": str(runtime.get("evaluation_as_of") or ""),
            "admission_revision": str(runtime.get("admission_revision") or ""),
            "path": RECALL_PATH.sub("/v1/default/banks/:bank/memories/recall", urllib.parse.urlparse(path).path),
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:32]

    def _foreground_reuse_key(self, path: str, body: dict[str, Any], headers: dict[str, str], bank_id: str) -> str:
        """Return a same-turn key independent of the adapter's recall profile.

        The ordinary continuation key intentionally includes profile and budget;
        that is correct for long-lived deep-result reuse.  The foreground key is
        narrower in time but broader across focused/deep adapters, so one user
        turn cannot be audited as two contradictory recalls.
        """
        runtime = runtime_context_from_headers(headers)
        scope = str(runtime.get("session_id") or runtime.get("project_key") or "")
        if not scope:
            # Do not coalesce unscoped requests across separate callers. Client
            # identity is only a conservative last resort for legacy Hooks.
            scope = "client:" + str(headers.get("X-Memory-Client") or headers.get("User-Agent") or "unknown")[:160]
        payload = {
            "controller_version": VERSION,
            "relevance_policy": RELEVANCE_POLICY,
            "bank": urllib.parse.unquote(bank_id),
            "query": request_semantic_query(body),
            "types": list(body.get("types") or []),
            "path": RECALL_PATH.sub("/v1/default/banks/:bank/memories/recall", urllib.parse.urlparse(path).path),
            "scope": scope,
            "execution_mode": str(runtime.get("execution_mode") or "production"),
            "evaluation_as_of": str(runtime.get("evaluation_as_of") or ""),
            "admission_revision": str(runtime.get("admission_revision") or ""),
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:32]

    def _foreground_reuse_get(self, key: str) -> dict[str, Any] | None:
        ttl = max(5, min(120, int(self.config.get("foregroundReuseWindowSeconds", 45))))
        now = datetime.now(timezone.utc)
        with self.foreground_reuse_lock:
            kept: dict[str, dict[str, Any]] = {}
            for candidate_key, value in self.foreground_reuse_cache.items():
                try:
                    age = (now - datetime.fromisoformat(str(value.get("completed_at") or "").replace("Z", "+00:00"))).total_seconds()
                except ValueError:
                    age = ttl + 1
                if age <= ttl:
                    kept[candidate_key] = value
            self.foreground_reuse_cache = kept
            item = dict(kept.get(key) or {})
        if not item or not item.get("response"):
            return None
        receipt = ((item.get("response") or {}).get("query_controller") or {})
        if str(receipt.get("version") or "") != VERSION:
            return None
        return item

    def _foreground_reuse_put(self, key: str, response: dict[str, Any]) -> None:
        receipt = dict((response.get("query_controller") or {}))
        with self.foreground_reuse_lock:
            self.foreground_reuse_cache[key] = {
                "completed_at": utc_now(),
                "response": json.loads(json.dumps(response, ensure_ascii=False)),
                "execution_id": str(receipt.get("execution_id") or receipt.get("query_id") or ""),
                "query_fingerprint": str(receipt.get("query_fingerprint") or ""),
            }
            # The cache survives only for its small time window, yet this hard
            # cap also bounds memory if a legacy client sends many unique turns.
            newest = sorted(self.foreground_reuse_cache.items(), key=lambda row: str(row[1].get("completed_at") or ""), reverse=True)[:80]
            self.foreground_reuse_cache = dict(newest)

    def _continuation_cache_get(self, key: str) -> dict[str, Any] | None:
        ttl = max(60, int(self.config.get("continuationCacheTtlSeconds", 86400)))
        with self.continuation_lock:
            item = dict((self.continuation_cache.get("items") or {}).get(key) or {})
        completed_at = str(item.get("completed_at") or "")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(completed_at.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            age = ttl + 1
        if not item or age > ttl or not item.get("response"):
            return None
        cached_receipt = ((item.get("response") or {}).get("query_controller") or {})
        if str(cached_receipt.get("version") or "") != VERSION:
            return None
        return item

    def _continuation_cache_put(self, key: str, response: dict[str, Any], meta: dict[str, Any]) -> None:
        # Keep exact selected evidence, but compact duplicate diagnostic
        # ledgers. Full immutable traces remain in the audit, not this cache.
        cached_response=dict(response)
        if isinstance(response.get("query_controller"),dict):
            cached_response["query_controller"]=status_trace_projection(response["query_controller"])
            cached_response["trace"]={"query_controller":cached_response["query_controller"]}
        entry={"completed_at":utc_now(),"response":cached_response,**meta}
        # Conservative serialized-size accounting includes nested indentation.
        encoded=json.dumps(entry,ensure_ascii=False,indent=2)
        entry["_cache_bytes"]=len(encoded.encode())+6*encoded.count('\n')+256
        max_bytes=max(1024,int(self.config.get("continuationCacheMaxBytes",16*1024*1024)))
        with self.continuation_lock:
            items = dict(self.continuation_cache.get("items") or {})
            items[key] = entry
            # Keep only a bounded local cache; this is an execution-result cache,
            # not another memory store or vector index.
            newest = sorted(items.items(), key=lambda row: str(row[1].get("completed_at") or ""), reverse=True)[:80]
            bounded={};used=512
            for cache_key,value in newest:
                # Legacy oversized entries have no size receipt and are
                # safely recomputable; never let them bypass the byte bound.
                size=int(value.get("_cache_bytes") or max_bytes)
                if used+size>max_bytes:continue
                bounded[cache_key]=value;used+=size
            self.continuation_cache={"schema":1,"items":bounded,"max_bytes":max_bytes}
            if not hasattr(self,"continuation_persist_lock"):
                self.continuation_persist_lock=threading.Lock()
        # Serialise writers, not readers. Snapshot after taking this lock so
        # a delayed older writer cannot replace a newer cache generation.
        with self.continuation_persist_lock:
            with self.continuation_lock:
                snapshot=dict(self.continuation_cache)
            atomic_json(self.continuation_cache_path, snapshot)

    def _schedule_continuation(self, key: str, path: str, body: dict[str, Any], headers: dict[str, str], bank_id: str, parent: dict[str, Any]) -> None:
        if not bool(self.config.get("backgroundContinuation", {}).get("enabled", True)):
            return
        if headers.get("X-Memory-Background-Continuation") == "1":
            return
        with self.inflight_lock:
            if key in self.inflight_requests and self.inflight_requests[key].get("continuation_scheduled"):
                return
            if key in self.inflight_requests:
                self.inflight_requests[key]["continuation_scheduled"] = True
        payload = dict(body)
        background_headers = dict(headers)
        background_headers["X-Memory-Background-Continuation"] = "1"
        background_headers["X-Memory-Recall-Profile"] = "deep"
        parent_execution = str(parent.get("execution_id") or "")

        def run() -> None:
            started_at = utc_now()
            try:
                result = self._execute_recall_uncached(path, payload, background_headers, bank_id)
                receipt = dict((result.get("query_controller") or {}))
                self._continuation_cache_put(key, result, {
                    "status": "completed" if receipt.get("execution_complete") else "partial",
                    "parent_execution_id": parent_execution,
                    "bank_id": urllib.parse.unquote(bank_id),
                    "coverage_complete": bool(receipt.get("coverage_complete")),
                    "started_at": started_at,
                })
                self._append_audit_event({
                    "at": utc_now(), "event": "background_continuation", "outcome": "completed" if receipt.get("execution_complete") else "partial",
                    "parent_execution_id": parent_execution, "cache_key": key, "bank_id": urllib.parse.unquote(bank_id),
                    "coverage_complete": bool(receipt.get("coverage_complete")), "execution_complete": bool(receipt.get("execution_complete")),
                    "result_count": int(receipt.get("result_count") or 0), "deadline_ms": int(receipt.get("deadline_ms") or 0),
                })
            except Exception as error:
                self._append_audit_event({
                    "at": utc_now(), "event": "background_continuation", "outcome": "failed",
                    "parent_execution_id": parent_execution, "cache_key": key, "bank_id": urllib.parse.unquote(bank_id), "error": repr(error),
                })

        self.continuation_executor.submit(run)

    def _record_reused_invocation(
        self,
        result: dict[str, Any],
        body: dict[str, Any],
        headers: dict[str, str],
        bank_id: str,
        *,
        origin: str,
        reuse_kind: str,
    ) -> dict[str, Any]:
        """Give every Hook delivery its own trace even when results are reused.

        Reuse saves a second Bank read, but it must never make a later user
        turn invisible in 9998 or attach that turn's injection receipt to the
        earlier execution.  The returned memories remain byte-for-byte the
        cached result; only the audit identity is new.
        """
        invocation_id = str(headers.get("X-Memory-Invocation-Id") or "").strip()
        if not invocation_id:
            return result
        receipt = result.setdefault("query_controller", {})
        reused_from = origin or str(receipt.get("execution_id") or "")
        body_full_prompt = full_prompt_text(str(body.get("full_prompt") or ""))
        query = body_full_prompt or latest_query(str(body.get("query") or ""))
        raw_user_prompt = full_prompt_text(str(body.get("raw_user_prompt") or ""))
        user_fp = str(headers.get("X-Memory-User-Prompt-Fingerprint") or "").strip()
        runtime_context = runtime_context_from_request(headers, body)
        receipt.update({
            "query_id": invocation_id,
            "execution_id": invocation_id,
            "cache_hit": True,
            "reused_from_execution_id": reused_from,
            "reuse_kind": reuse_kind,
            "execution_complete": True,
            "result_count": len(result.get("results") or []),
        })
        event = {
            "at": utc_now(), "event": "recall", "outcome": "reused",
            "query_id": invocation_id, "execution_id": invocation_id,
            "query_fingerprint": hashlib.sha256(query.encode()).hexdigest()[:16],
            "user_prompt_fingerprint": user_fp or hashlib.sha256(query.encode()).hexdigest()[:16],
            "session_id": runtime_context.get("session_id") or "",
            "turn_id": runtime_context.get("turn_id") or "",
            "execution_mode": runtime_context.get('execution_mode') or 'production',
            "prompt_origin": runtime_context.get('prompt_origin') or '',
            "hook_invocation_id": runtime_context.get("invocation_id") or invocation_id,
            "query_preview": raw_user_prompt[:TRACE_PREVIEW_CHARS] if raw_user_prompt else query[:TRACE_PREVIEW_CHARS],
            "raw_user_prompt": raw_user_prompt or query,
            "full_prompt": query,
            "full_prompt_source": runtime_context.get("full_prompt_source") or "",
            "input_query_tokens": query_token_count(raw_user_prompt or query),
            "query_compacted": False,
            "role": receipt.get("role") or infer_role(headers.get("User-Agent", ""), bank_id, headers.get("X-Memory-Role", "")),
            "client": str(headers.get("X-Memory-Client") or headers.get("User-Agent") or "unknown")[:120],
            "bank_id": urllib.parse.unquote(bank_id),
            "shape": receipt.get("primary_shape") or "point",
            "matched_shapes": receipt.get("matched_shapes") or [receipt.get("primary_shape") or "point"],
            "strategies": receipt.get("strategies") or [],
            "route_decisions": receipt.get("route_decisions") or [],
            "memory_action": receipt.get("memory_action") or "focused_recall",
            "working_set": receipt.get("working_set") or {"sufficient": False, "decision": "reused_recall", "reason": "复用同一问题的已完成召回，不重复读取 Bank。"},
            "planner": receipt.get("planner"),
            "value_of_information": receipt.get("value_of_information"),
            "coverage_dimensions": receipt.get("coverage_dimensions") or [],
            "queries_requested": 0,
            "queries_completed": 0,
            "result_count": len(result.get("results") or []),
            "fallback_used": False,
            "coverage_complete": bool(receipt.get("coverage_complete", True)),
            "execution_complete": True,
            "coverage_receipt": receipt.get("coverage_receipt") or {"required": [], "covered": [], "missing": [], "complete": True},
            # Reuse is a cache optimisation, not a different routing decision.
            # Preserve the original graph/closure evidence so the new user-turn
            # card shows the same complete chain rather than a Controller node
            # that appears to have skipped the graph lane.
            "relation_closure": receipt.get("relation_closure"),
            "graph_route": receipt.get("graph_route"),
            "entity_resolution": receipt.get("entity_resolution"),
            "facets": receipt.get("facets") or [],
            "project_state": receipt.get("project_state"),
            "source_guard": receipt.get("source_guard"),
            "selected_results": list(result.get("results") or []),
            "errors": [],
            "cache_hit": True, "reused_from_execution_id": reused_from,
            "reuse_kind": reuse_kind,
            "elapsed_ms": 0,
        }
        self._append_audit_event(event)
        result["trace"] = {"query_controller": dict(receipt)}
        return result

    def execute_recall(
        self,
        path: str,
        body: dict[str, Any],
        headers: dict[str, str],
        bank_id: str,
    ) -> dict[str, Any]:
        # Internal continuation never coalesces with the foreground request; it
        # deliberately obtains the remaining deep evidence after the Hook has
        # returned its fast, honest first answer.
        if headers.get("X-Memory-Background-Continuation") == "1":
            return self._execute_recall_uncached(path, body, headers, bank_id)
        key = self._recall_key(path, body, headers, bank_id)
        foreground_key = self._foreground_reuse_key(path, body, headers, bank_id)
        # A deliberate caller override remains available for a human-requested
        # second deep audit, but profile changes made by adapters do not count as
        # such an override.
        if headers.get("X-Memory-Force-Deep") != "1":
            foreground = self._foreground_reuse_get(foreground_key)
            if foreground:
                result = json.loads(json.dumps(foreground["response"], ensure_ascii=False))
                receipt = result.setdefault("query_controller", {})
                origin = str(foreground.get("execution_id") or receipt.get("execution_id") or "")
                receipt.update({
                    "cache_hit": True,
                    "coalesced": True,
                    "foreground_reused": True,
                    "reused_from_execution_id": origin,
                    "foreground_reuse_window_seconds": max(5, min(120, int(self.config.get("foregroundReuseWindowSeconds", 45)))),
                    "continuation_cache_key": key,
                })
                result = self._record_reused_invocation(result, body, headers, bank_id, origin=origin, reuse_kind="foreground")
                self.status.update(inc_foreground_reuses=1)
                return result
        cached = self._continuation_cache_get(key)
        if cached:
            result = json.loads(json.dumps(cached["response"], ensure_ascii=False))
            receipt = result.setdefault("query_controller", {})
            receipt.update({"cache_hit": True, "coalesced": False, "continuation_status": "ready", "continuation_cache_key": key})
            return self._record_reused_invocation(result, body, headers, bank_id, origin=str(cached.get("parent_execution_id") or receipt.get("execution_id") or ""), reuse_kind="continuation_cache")
        with self.inflight_lock:
            slot = self.inflight_requests.get(key)
            if slot is None:
                slot = {"event": threading.Event(), "result": None, "error": None, "continuation_scheduled": False}
                self.inflight_requests[key] = slot
                owner = True
            else:
                owner = False
        if owner:
            try:
                # Keep the first pass on the same request identity, but tell
                # the uncached executor that this owner is still alive long
                # enough to adopt a required coverage completion.  Before
                # this marker existed the executor scheduled a deep helper
                # after returning the focused result, so Hook built a Packet
                # from the smaller result and the helper could never affect
                # the actual user turn.
                foreground_headers = dict(headers)
                foreground_headers["X-Memory-Foreground-Completion"] = "1"
                result = self._execute_recall_uncached(path, body, foreground_headers, bank_id)
                receipt = dict(result.get("query_controller") or {})
                needs_foreground_completion = bool(
                    self.config.get("foregroundCompletionEnabled", True)
                    and not headers.get("X-Memory-Transport-Timeout-Ms")
                    and receipt.get("execution_complete")
                    and receipt.get("coverage_required")
                    and not receipt.get("coverage_complete")
                    # A fast foreground packet is already deliverable.  Do
                    # not synchronously run the 180–300s official/graph lane
                    # and make the real Hook appear hung; that lane is
                    # scheduled below and can update the continuation cache.
                    and not receipt.get("fast_recovered")
                )
                if needs_foreground_completion:
                    # The normal Hook contract is synchronous.  Complete the
                    # missing required facets while that contract is still
                    # open, and only fall back to the old background cache if
                    # this bounded deep attempt fails or remains incomplete.
                    deep_headers = dict(headers)
                    deep_headers["X-Memory-Background-Continuation"] = "1"
                    deep_headers["X-Memory-Foreground-Continuation"] = "1"
                    deep_headers["X-Memory-Recall-Profile"] = "deep"
                    parent_execution = str(receipt.get("execution_id") or receipt.get("query_id") or "")
                    try:
                        deep_result = self._execute_recall_uncached(path, body, deep_headers, bank_id)
                        deep_receipt = deep_result.setdefault("query_controller", {})
                        deep_complete = bool(
                            deep_receipt.get("execution_complete")
                            and deep_receipt.get("coverage_complete")
                        )
                        if deep_complete:
                            deep_receipt.update({
                                "foreground_continuation": True,
                                "foreground_parent_execution_id": parent_execution,
                                "continuation_status": "foreground_complete",
                                "continuation_cache_key": key,
                            })
                            deep_result["trace"] = {"query_controller": dict(deep_receipt)}
                            result = deep_result
                        else:
                            self._schedule_continuation(
                                key, path, body, headers, bank_id, receipt
                            )
                    except Exception:
                        # Preserve the truthful focused result and let the
                        # existing durable continuation path retry later.  A
                        # deep failure must never turn a valid first pass into
                        # a synthetic zero-result response.
                        self._schedule_continuation(
                            key, path, body, headers, bank_id, receipt
                        )
                elif bool(
                    (receipt.get("fast_recovered") or headers.get("X-Memory-Transport-Timeout-Ms"))
                    and receipt.get("execution_complete")
                    and receipt.get("coverage_required")
                    and not receipt.get("coverage_complete")
                ):
                    # Fast recovery deliberately returns before the official
                    # deep lane.  Preserve the honest partial receipt for the
                    # current turn, and let the durable continuation finish
                    # without holding the Hook response open.
                    self._schedule_continuation(
                        key, path, body, headers, bank_id, receipt
                    )
                    result.setdefault("query_controller", {}).update({
                        "continuation_status": "background_running",
                        "foreground_partial": True,
                    })
                self._foreground_reuse_put(foreground_key, result)
                slot["result"] = result
                return result
            except Exception as error:
                slot["error"] = error
                raise
            finally:
                slot["event"].set()
                # Keep slot long enough for a continuation scheduler to mark it,
                # then release it: future same requests use completed cache only.
                with self.inflight_lock:
                    self.inflight_requests.pop(key, None)
        wait_seconds = max(1.0, min(float(self.config.get("singleflightWaitSeconds", 22)), float(self.config.get("deepDeadlineMs", 180000))/1000.0))
        if not slot["event"].wait(wait_seconds):
            # Do not turn shared work into a false failure. Caller receives an
            # explicit retryable receipt while original request keeps running.
            return {"results": [], "entities": {}, "query_controller": {"version": VERSION, "coalesced": True, "execution_complete": False, "coverage_complete": False, "continuation_status": "in_flight", "continuation_cache_key": key, "scope_claim": "shared_request_still_running"}, "trace": {"query_controller": {"version": VERSION, "coalesced": True, "execution_complete": False, "coverage_complete": False, "continuation_status": "in_flight"}}}
        if slot.get("error"):
            raise slot["error"]
        result = json.loads(json.dumps(slot.get("result") or {}, ensure_ascii=False))
        receipt = result.setdefault("query_controller", {})
        receipt.update({"coalesced": True, "cache_hit": False, "continuation_cache_key": key})
        result["trace"] = {"query_controller": dict(receipt)}
        return result

    def _execute_recall_uncached(
        self,
        path: str,
        body: dict[str, Any],
        headers: dict[str, str],
        bank_id: str,
    ) -> dict[str, Any]:
        request_started = time.monotonic()
        query = str(body.get("query") or "")
        raw_user_prompt = str(body.get("raw_user_prompt") or "").strip()
        role = infer_role(headers.get("User-Agent", ""), bank_id, headers.get("X-Memory-Role", ""))
        client = headers.get("X-Memory-Client") or headers.get("User-Agent", "unknown")
        user_prompt_fingerprint = str(headers.get("X-Memory-User-Prompt-Fingerprint") or "").strip()
        recall_profile = headers.get("X-Memory-Recall-Profile", "")
        foreground_completion = headers.get("X-Memory-Foreground-Completion") == "1"
        foreground_continuation = headers.get("X-Memory-Foreground-Continuation") == "1"
        runtime_context = runtime_context_from_request(headers, body)
        if raw_user_prompt:
            # Keep this separate from ``query``: the latter is the resolved
            # Full Prompt used for retrieval and cache semantics.  The body
            # path is authoritative and is not clipped to a header-sized
            # value.
            runtime_context["raw_user_prompt"] = raw_user_prompt
        trace_identity = {
            "execution_mode": str(runtime_context.get('execution_mode') or 'production'),
            "session_id": str(runtime_context.get("session_id") or ""),
            "turn_id": str(runtime_context.get("turn_id") or ""),
            "hook_invocation_id": str(runtime_context.get("invocation_id") or ""),
        }
        agent_plan = body.get("agent_plan") if isinstance(body.get("agent_plan"), dict) else None
        plan = self.plan(query, role, client, bank_id, recall_profile=recall_profile, runtime_context=runtime_context, agent_plan=agent_plan)
        # The public plan remains a plain JSON response.  Retain only the
        # bounded request identity internally so merge_recall_responses can
        # bind admission decisions to this concrete Hook turn.
        plan["_runtime_context"] = runtime_context
        # From this point on, every Hindsight-facing lane must use the same
        # resolved Full Prompt as the primary facets.  ``query`` is still the
        # adapter's raw/expanded request and remains useful for legacy audit
        # previews, but using it for sidecars, graph seeds or fallback would
        # silently reintroduce the very raw-vs-full split fixed above.
        semantic_query = full_prompt_text(str(plan.get("full_prompt") or query))
        if plan.get('memory_action')=='guidance_only':
            semantic_query=str(plan.get('guidance_query') or semantic_query)
        # ``source_first`` also occurs for current-file/current-source guards.
        # Those valid paths do not carry Hook's optional authority-only header,
        # so the receipt must not read an uninitialised policy flag.
        policy_authority = False
        authority_only_override = None
        # The Hook sometimes owns a deterministic local authority (for example
        # the executable backup policy).  It still needs a Controller trace so
        # the owner page can show the complete route and the subsequent
        # injection receipt can attach to a real execution.  Record a
        # source-first execution without querying the Bank or letting old
        # semantic candidates compete with that live authority.
        authority_only_requested = headers.get("X-Memory-Authority-Only") == "1"
        if authority_only_requested and plan.get('memory_action')=='guidance_only':
            authority_only_override={'requested':True,'applied':'facts_only','reason':'当前事实由Hook权威源提供；指导层独立检查。'}
            authority_only_requested=False
        historical_bank_required = requires_historical_bank_recall(
            semantic_query or raw_user_prompt or query
        )
        if authority_only_requested and historical_bank_required:
            # A stale/over-eager Hook may have classified a mixed migration
            # question as “current authority only” because it contains words
            # such as 当前/状态.  The explicit durable-memory scope wins.  Do
            # not silently accept the header as a hard zero-query boundary;
            # retain an audit marker and continue through the normal Bank
            # facets so the user can actually recover the old task evidence.
            authority_only_override = {
                "requested": True,
                "applied": False,
                "reason": "explicit historical/long-term scope requires Bank recall; Hook authority-only hint was overridden",
                "historical_bank_required": True,
            }
            plan["authority_only_override"] = authority_only_override
        if authority_only_requested and not historical_bank_required:
            authority_kind = str(headers.get("X-Memory-Authority-Kind") or "hook_deterministic_authority")[:80]
            policy_authority = authority_kind.endswith("_memory_policy")
            plan.update({
                "memory_action": "source_first",
                "primary_shape": "current",
                "matched_shapes": ["current"],
                "strategies": ["hook_deterministic_authority"],
                "queries": [], "query_metrics": [],
                "escalation_queries": [], "escalation_query_metrics": [],
                "query_count": 0, "planned_query_count": 0,
                "coverage_dimensions": ["hook_authoritative_source"],
                "coverage_required": False,
                "scope_claim": "hook_authoritative_source",
                "source_guard": {"active": True, "kind": authority_kind, "reason": ("当前问题询问稳定记忆规则；只注入该规则，不读取历史 Bank。" if policy_authority else "Hook 已取得当前可执行权威来源；不从旧语义记忆补写该当前事实。")},
                "working_set": {"sufficient": True, "decision": ("hook_policy_authority_only" if policy_authority else "hook_authority_only"), "reason": ("当前问题由一条稳定规则直接回答；Controller 只记录链路，不读取历史 Bank。" if policy_authority else "当前事实由 Hook 的可执行权威来源提供；Controller 只记录链路，不读取历史 Bank。"), "signals": {}},
            })
        continuation_mode = headers.get("X-Memory-Background-Continuation") == "1"
        if continuation_mode:
            plan["deadline_ms"] = int(self.config.get("backgroundContinuation", {}).get("deadlineMs", 300000))
            plan["continuation_mode"] = True
        guidance_enabled = bool((plan.get("guidance_sidecar") or {}).get("enabled"))
        # A source-first receipt is a hard authority boundary.  Optional
        # guidance sidecars must not reopen Bank retrieval merely because they
        # are enabled; doing so made an attachment-first trace inject unrelated
        # historical memories while the UI correctly said "current source first".
        if plan.get("memory_action") == "noop_long_term" or plan.get("memory_action") == "source_first":
            source_first = plan.get("memory_action") == "source_first"
            authority_items = (
                source_first_authority_items(semantic_query, runtime_context)
                if source_first
                else []
            )
            authority_binding = admission_binding_context(
                runtime_context,
                execution_id=str(plan.get("execution_id") or ""),
                prompt_sha256=str(
                    user_prompt_fingerprint or plan.get("query_fingerprint") or ""
                ),
            )
            authority_final = finalize_admission(
                authority_binding,
                authority_items,
                admitted=authority_items,
                rejected=[],
                deferred=[],
            )
            authority_items = authority_final["items"]
            authority_covered = bool(authority_items)
            authority_admission = {
                "policy": RELEVANCE_POLICY,
                "candidate_count": len(authority_items),
                "qualified_count": len(authority_items),
                "rejected_count": 0,
                "deferred_count": 0,
                "admitted_items": authority_items,
                "rejected_items": [],
                "deferred_items": [],
                "reason": (
                    "当前问题命中本机可执行权威来源，返回同回合可验证的权威快照。"
                    if authority_covered
                    else "当前来源优先，但本轮没有生成可验证的本机权威快照。"
                ),
            }
            event = {
                "at": utc_now(),
                "event": "recall",
                "outcome": "completed" if authority_covered else ("deferred" if source_first else "skipped"),
                "query_id": plan["query_id"],
                "execution_id": plan["execution_id"],
                "query_fingerprint": plan["query_fingerprint"],
                "user_prompt_fingerprint": user_prompt_fingerprint or plan["query_fingerprint"],
                **trace_identity,
                "query_preview": latest_query(query)[:TRACE_PREVIEW_CHARS],
                # Preserve both forms for the status-page audit.  query_preview
                # is a legacy display field and may be the Full Prompt, so it
                # must not be used to reconstruct the raw user turn later.
                "raw_user_prompt": plan.get("raw_user_prompt", ""),
                "prompt_origin": plan.get("prompt_origin", ""),
                "full_prompt": plan.get("full_prompt", ""),
                "full_prompt_source": plan.get("full_prompt_source", ""),
                "input_query_tokens": plan["input_query_tokens"],
                "query_compacted": False,
                "role": role,
                "client": client[:120],
                "bank_id": urllib.parse.unquote(bank_id),
                "shape": plan["primary_shape"],
                "matched_shapes": plan["matched_shapes"],
                "strategies": plan["strategies"],
                "route_decisions": plan["route_decisions"],
                "memory_action": plan["memory_action"],
                "working_set": plan["working_set"],
                "planner": plan.get("planner"),
                "value_of_information": plan["value_of_information"],
                "coverage_dimensions": [],
                "queries_requested": 0,
                "queries_completed": 0,
                "result_count": len(authority_items),
                "fallback_used": False,
                "coverage_complete": not source_first or authority_covered,
                "execution_complete": True,
                "coverage_receipt": {
                    "required": ["authoritative_source"] if source_first else [],
                    "covered": ["authoritative_source"] if authority_covered else [],
                    "missing": [] if authority_covered or not source_first else ["authoritative_source"],
                    "complete": not source_first or authority_covered,
                },
                "project_state": plan.get("project_state"),
                "source_guard": plan.get("source_guard"),
                "elapsed_ms": 0,
                "facets": [],
                "selected_results": authority_items,
                "relevance_admission": authority_admission,
                "admission_decisions": authority_final["decisions"],
                "errors": [],
            }
            self._append_audit_event(event)
            self.status.update(
                inc_recalls=1,
                inc_noop_recalls=1 if not source_first else 0,
                inc_source_first_recalls=1 if source_first else 0,
                functional_status="healthy",
                last_error=None,
                last_error_at=None,
                last_recall={
                    "at": event["at"], "query_id": plan["query_id"],
                    "execution_id": plan["execution_id"], "query_fingerprint": plan["query_fingerprint"],
                    "user_prompt_fingerprint": user_prompt_fingerprint or plan["query_fingerprint"],
                    "role": role,
                    "bank_id": urllib.parse.unquote(bank_id), "shape": plan["primary_shape"],
                    "memory_action": plan["memory_action"], "queries_completed": 0,
                    "result_count": len(authority_items), "elapsed_ms": 0, "coverage_complete": not source_first or authority_covered,
                    "execution_complete": True, "errors": [],
                    "project_state": plan.get("project_state"),
                    "source_guard": plan.get("source_guard"),
                },
            )
            controller_receipt = {
                "version": VERSION,
                "query_id": plan["query_id"],
                "execution_id": plan["execution_id"],
                "query_fingerprint": plan["query_fingerprint"],
                "memory_action": plan["memory_action"],
                "working_set": plan["working_set"],
                "planner": plan.get("planner"),
                "value_of_information": plan["value_of_information"],
                "primary_shape": plan["primary_shape"],
                "strategies": plan["strategies"],
                "route_decisions": plan["route_decisions"],
                "query_count": 0,
                "queries_requested": 0,
                "queries_completed": 0,
                "coverage_dimensions": [],
                "coverage_complete": not source_first or authority_covered,
                "execution_complete": True,
                "scope_claim": ("stable_policy_authority_only" if policy_authority else "current_source_inspection_required") if source_first else "current_working_set_only",
                "result_count": len(authority_items),
                "project_state": plan.get("project_state"),
                "source_guard": plan.get("source_guard"),
                "relevance_admission": authority_admission,
                "admission_decisions": authority_final["decisions"],
            }
            return {
                "results": authority_items,
                "entities": {},
                "query_controller": controller_receipt,
                "trace": {"query_controller": dict(controller_receipt)},
            }
        timeout = max(1.0, float(plan["deadline_ms"]) / 1000.0)
        # Facets run concurrently, so dividing the deadline by facet count
        # caused otherwise healthy 2-4 second local recalls to time out.  Each
        # facet receives the shared wall-clock deadline; the executor still
        # bounds total concurrency.  Preserve a caller's larger established
        # token budget instead of silently shrinking existing integrations.
        # One wall-clock budget is shared by initial facets and escalation.
        # The old code granted a fresh deadline to both stages.
        deadline_at = time.monotonic() + timeout
        if not continuation_mode:
            try:
                transport_ms = int(headers.get("X-Memory-Transport-Timeout-Ms") or 0)
            except (TypeError, ValueError):
                transport_ms = 0
            if transport_ms > 0:
                # Includes planning and all foreground facets. Leave time for
                # final admission, JSON serialization and the Hook socket.
                reserve_ms = min(4000, max(250, transport_ms // 5))
                deadline_at = min(deadline_at, request_started + max(0.05, (transport_ms-reserve_ms)/1000.0))
                plan["transport_timeout_ms"] = transport_ms
                plan["foreground_receipt_reserve_ms"] = reserve_ms
        requested_tokens = int(body.get("max_tokens") or 0)
        per_query_tokens = min(
            max(int(plan["max_tokens"]), requested_tokens, 600),
            int(self.config.get("maxPerQueryTokens", 4096)),
        )
        requests: list[tuple[str, str, dict[str, Any]]] = []
        request_meta: dict[str, dict[str, Any]] = {}
        for index, expanded in enumerate(plan["queries"]):
            item = dict(body)
            item["query"] = expanded
            item["budget"] = plan["budget"]
            item["max_tokens"] = per_query_tokens
            item["types"] = plan["types"]
            item["prefer_observations"] = plan["prefer_observations"]
            temporal_anchor = plan.get("explicit_temporal_anchor")
            if temporal_anchor:
                item["query_timestamp"] = temporal_anchor["query_timestamp"]
                normalized_date = str(temporal_anchor["normalized"])
                if normalized_date not in item["query"]:
                    item["query"] = item["query"] + f"\n检索时间锚点：{normalized_date}。"
            label = f"facet:{index}"
            requests.append((label, path, item))
            metric = dict(plan.get("query_metrics", [])[index])
            request_meta[label] = {
                "label": label,
                "path": path,
                "query_preview": expanded[:TRACE_PREVIEW_CHARS],
                "query_tokens": metric.get("after_tokens", query_token_count(expanded)),
                "before_tokens": metric.get("before_tokens"),
                "compacted": bool(metric.get("compacted")),
            }

        # Stable observations are fetched on a separate official Recall call.
        # This lets current attachments remain fact-authoritative while durable
        # quality/personalization guidance is still available to the agent.
        if guidance_enabled and bool((plan.get("guidance_sidecar") or {}).get("observation")):
            sidecar_query, sidecar_metric = compact_query(
                semantic_query + "\n记忆检索维度：只找可改变本轮解释、质量标准、稳定偏好或决策边界的观察；不要返回项目细节。",
                int(self.config.get("maxQueryTokens", SAFE_QUERY_TOKEN_LIMIT)),
            )
            sidecar_body = dict(body)
            sidecar_body.update({
                "query": sidecar_query, "budget": "low", "max_tokens": min(900, max(480, int(plan.get("max_tokens") or 1200))),
                "types": ["observation"], "prefer_observations": True,
            })
            requests.append(("stable_guidance_observations", path, sidecar_body))
            request_meta["stable_guidance_observations"] = {
                "label": "stable_guidance_observations", "path": path,
                "query_preview": sidecar_query[:TRACE_PREVIEW_CHARS], "query_tokens": sidecar_metric["after_tokens"],
                "before_tokens": sidecar_metric["before_tokens"], "compacted": sidecar_metric["compacted"],
                "kind": "stable_guidance_sidecar",
            }

        # Every role that its access policy grants the raw level may read the
        # unified evidence bank for a provenance question.  The old Hermes-only
        # guard made Codex/Xiaodai source audits weaker despite their A-class
        # permissions.
        if (
            plan["requires_direct_evidence"]
            and "raw" in plan["levels"]
            and plan.get("evidence_bank")
            and urllib.parse.unquote(bank_id) != plan["evidence_bank"]
        ):
            evidence_path = (
                "/v1/default/banks/"
                + urllib.parse.quote(str(plan["evidence_bank"]), safe="")
                + "/memories/recall"
            )
            evidence_body = dict(body)
            evidence_query, evidence_metric = compact_query(
                semantic_query + "\n记忆检索维度：保留原话、来源和证据时间。",
                int(self.config.get("maxQueryTokens", SAFE_QUERY_TOKEN_LIMIT)),
            )
            evidence_body.update({
                "query": evidence_query,
                "budget": "low",
                "max_tokens": 700,
                "types": ["world"],
                "prefer_observations": False,
            })
            temporal_anchor = plan.get("explicit_temporal_anchor")
            if temporal_anchor:
                evidence_body["query_timestamp"] = temporal_anchor["query_timestamp"]
                normalized_date = str(temporal_anchor["normalized"])
                if normalized_date not in evidence_body["query"]:
                    evidence_body["query"] += f"\n检索时间锚点：{normalized_date}。"
            requests.append(("evidence", evidence_path, evidence_body))
            request_meta["evidence"] = {
                "label": "evidence",
                "path": evidence_path,
                "query_preview": evidence_query[:TRACE_PREVIEW_CHARS],
                "query_tokens": evidence_metric["after_tokens"],
                "before_tokens": evidence_metric["before_tokens"],
                "compacted": evidence_metric["compacted"],
            }

        responses: list[tuple[str, dict[str, Any]]] = []
        errors: list[str] = []
        facet_traces: list[dict[str, Any]] = []
        fast_items: list[dict[str, Any]] = []
        # ``foreground_completion`` is set by the owner request that still
        # has a live Hook socket.  A derived cache hit gives us a useful,
        # auditable packet immediately; it must not then wait for the slow
        # official vector/LLM lane merely because that lane is allowed to run
        # in the background.  Keep the original plan deadline in the receipt
        # and record this smaller *effective* foreground budget separately.
        fast_foreground_mode = False
        fast_admitted_items: list[dict[str, Any]] = []
        effective_deadline_ms = int(plan.get("deadline_ms") or 0)
        fast_receipt: dict[str, Any] = {
            "enabled": bool(getattr(self, "fast_recall_enabled", False)),
            "status": "disabled" if not getattr(self, "fast_recall_enabled", False) else "pending",
            "endpoint": "local_fast_bank_cache",
            "candidate_count": 0,
            "selected_count": 0,
        }
        max_workers = min(
            len(requests),
            int(self.config.get("readParallelism", 3)),
        )
        started = time.monotonic()

        # Start with the local derived cache for every ordinary memory read.
        # It is intentionally not used for source-first/no-op turns.  The
        # official facets still run below and, when they finish in time, their
        # results fuse with these rows; when they do not, the cache prevents a
        # false zero-injection turn without hiding the official timeout receipt.
        if getattr(self, "fast_recall_enabled", False) and plan.get("memory_action") not in {"noop_long_term", "source_first"}:
            fast_limit = int((self.config.get("fastRecall") or {}).get("maxCandidates", 96))
            # The derived lane must be able to recover the stable guidance
            # classes that the official sidecars would otherwise provide.
            # Keep the normal plan types first, then add only the sidecar
            # classes actually enabled for this turn; unified admission still
            # decides whether any of them can enter the Packet.
            fast_types = list(plan.get("types") or [])
            sidecar = plan.get("guidance_sidecar") or {}
            if sidecar.get("observation") and "observation" not in fast_types:
                fast_types.append("observation")
            if sidecar.get("mental_model") and "mental_model" not in fast_types:
                fast_types.append("mental_model")
            if sidecar.get("direct_policy") and "direct_policy" not in fast_types:
                fast_types.append("direct_policy")
            fast_items, fast_receipt = self.fast_bank.search(
                semantic_query,
                limit=max(8, min(160, fast_limit)),
                types=fast_types,
            )
            # Candidate presence alone must not shorten the official lane. A
            # long Full Prompt can make the lexical cache return generic rows
            # (for example ``研究方向`` or ``完整工作流``) that the same
            # admission boundary correctly rejects.  Treat the cache as a
            # foreground recovery source only when at least one candidate
            # already proves the current proposition; otherwise leave the
            # normal plan deadline intact so the official facets can finish.
            if fast_items:
                fast_admitted_items, _ = fast_recovery_admission(
                    semantic_query,
                    fast_items,
                    plan,
                )
            fast_receipt["admission_selected_count"] = len(fast_admitted_items)
            if fast_items:
                responses.append(("local_fast_bank_cache", {"results": fast_items, "entities": {}}))
            fast_receipt["status"] = "completed" if fast_items else str(fast_receipt.get("status") or "completed")
            fast_receipt["selected_ids"] = [str(item.get("id") or item.get("chunk_id") or "")[:100] for item in fast_items[:32]]
            fast_receipt["used_as_foreground_recovery"] = bool(fast_admitted_items)
            # A cache hit is safe to shorten an ordinary point read, but not a
            # coverage-bearing read.  Coverage prompts have escalation facets
            # whose counter-evidence/transition result can change the answer;
            # shortening their deadline to the 10–12s interactive cap caused
            # the official facets to time out and left a misleading
            # ``timed_out_recovered`` receipt even when the fast packet had
            # some relevant rows.  Let the plan's own complex/deep deadline
            # govern those reads; the cache remains a fusion source and can
            # still prevent a zero if the official lane is genuinely slow.
            coverage_bearing = bool(plan.get("coverage_required") or plan.get("explicit_deep_recall"))
            # A qualified fast result is already a valid semantic Packet even
            # for a coverage/deep query. Return the proven subset promptly and
            # leave remaining breadth to the continuation lane.
            if fast_admitted_items and foreground_completion and not continuation_mode:
                # The Hook/adapter has a much smaller practical interactive
                # window than the deep Bank deadline.  Let the fast derived
                # index satisfy that window, while ``execute_recall`` below
                # schedules the official deep/graph completion separately.
                fast_config = self.config.get("fastRecall") or {}
                configured_cap_ms = int(fast_config.get("foregroundDeadlineMs", 10000))
                max_cap_ms = int(fast_config.get("maxForegroundDeadlineMs", 12000))
                # Keep a small receipt/transport reserve inside the plan's
                # wall-clock budget.  The previous hard 3s (and later 8s)
                # ceiling routinely cancelled healthy 1.5–4s official facets,
                # turning a recoverable recall into a stale-cache-only result.
                receipt_reserve_ms = max(250, int(fast_config.get("foregroundReceiptReserveMs", 1200)))
                plan_budget_ms = max(1000, int(plan.get("deadline_ms") or effective_deadline_ms or 12000))
                available_ms = max(1000, plan_budget_ms - receipt_reserve_ms)
                cap_ms = max(1000, min(configured_cap_ms, max_cap_ms, available_ms))
                fast_foreground_mode = True
                deadline_at = min(deadline_at, time.monotonic() + cap_ms / 1000.0)
                effective_deadline_ms = min(effective_deadline_ms, cap_ms)
                fast_receipt.update({
                    "foreground_mode": True,
                    "foreground_deadline_ms": cap_ms,
                    "foreground_recovery_reason": "derived cache first; official lanes continue asynchronously",
                    "coverage_partial_allowed": coverage_bearing,
                })
            facet_traces.append({
                "label": "local_fast_bank_cache",
                "path": "derived://hindsight-memory-units",
                "query_preview": semantic_query[:TRACE_PREVIEW_CHARS],
                "query_tokens": query_token_count(semantic_query),
                "before_tokens": query_token_count(semantic_query),
                "compacted": False,
                "status": fast_receipt.get("status"),
                "elapsed_ms": fast_receipt.get("elapsed_ms", 0),
                "result_count": len(fast_items),
                "error": fast_receipt.get("error"),
                "derived_cache": True,
            })

        def run_request(label: str, req_path: str, value: dict[str, Any], request_timeout: float):
            request_started = time.monotonic()
            try:
                response = self.upstream_json(req_path, value, headers, request_timeout)
                return response, round((time.monotonic() - request_started) * 1000, 2), None
            except Exception as error:
                return None, round((time.monotonic() - request_started) * 1000, 2), repr(error)

        def execute_batch(
            batch: list[tuple[str, str, dict[str, Any]]],
            call_deadline_at: float | None = None,
        ) -> None:
            if not batch:
                return
            effective_deadline = min(deadline_at, call_deadline_at or deadline_at)
            if effective_deadline <= time.monotonic():
                for label, _, _ in batch:
                    errors.append(f"{label}:TimeoutError('global recall deadline exhausted')")
                    trace = dict(request_meta[label])
                    trace.update({"status": "skipped", "elapsed_ms": 0, "result_count": 0,
                                  "error": "global recall deadline exhausted"})
                    facet_traces.append(trace)
                return
            workers = min(len(batch), int(self.config.get("readParallelism", 3)))
            # Do not let an upstream request that ignored its client timeout
            # hold the Controller past its own deadline.  The Codex Hook has a
            # hard 25-second wall clock; the Controller must return its
            # completed facets, graph receipt and explicit skipped-facet trace
            # before that deadline instead of silently making the whole chain
            # disappear.  Running workers are allowed to finish in the
            # background, but their late result is intentionally not merged.
            pool = ThreadPoolExecutor(max_workers=max(1, workers))
            futures = {
                pool.submit(run_request, label, req_path, value, max(0.25, effective_deadline - time.monotonic())): label
                for label, req_path, value in batch
            }
            pending = set(futures)
            try:
                for future in as_completed(futures, timeout=max(0.01, effective_deadline - time.monotonic())):
                    pending.discard(future)
                    label = futures[future]
                    response, facet_elapsed_ms, error = future.result()
                    trace = dict(request_meta[label])
                    trace.update({
                        "status": "failed" if error else "completed",
                        "elapsed_ms": facet_elapsed_ms,
                        "result_count": len((response or {}).get("results") or []),
                        "error": error,
                    })
                    facet_traces.append(trace)
                    if error:
                        errors.append(f"{label}:{error}")
                    else:
                        responses.append((label, response))
            except FuturesTimeoutError:
                pass
            finally:
                for future in pending:
                    label = futures[future]
                    future.cancel()
                    errors.append(f"{label}:TimeoutError('facet deadline reserved for graph/receipt')")
                    trace = dict(request_meta[label])
                    trace.update({"status": "skipped", "elapsed_ms": 0, "result_count": 0,
                                  "error": "facet deadline reserved for graph/receipt"})
                    facet_traces.append(trace)
                pool.shutdown(wait=False, cancel_futures=True)

        # Reserve a bounded slice for the official graph and receipt build.
        # Without it, broad parallel semantic facets can occupy the whole Hook
        # window, so a relation question appears to have an interrupted chain
        # although the controller merely ran out of time before Output.
        graph_reserved_seconds = 0.0
        if bool((plan.get("graph_route") or {}).get("enabled")) and not fast_foreground_mode:
            graph_reserved_seconds = min(6.0, max(2.0, timeout * 0.30))
        initial_deadline = deadline_at - graph_reserved_seconds
        execute_batch(requests, initial_deadline if graph_reserved_seconds else None)

        # A clean-room replay may set a historical cutoff. Filter immediately
        # after every upstream response so later self-retained answers cannot
        # seed graph traversal, ranking, coverage, or prompt injection.
        evaluation_excluded_count = 0
        evaluation_as_of = str(plan.get("evaluation_as_of") or "")
        if evaluation_as_of:
            filtered_responses: list[tuple[str, dict[str, Any]]] = []
            for label, response in responses:
                value = dict(response or {})
                kept, excluded = filter_results_as_of(
                    list(value.get("results") or []), evaluation_as_of
                )
                value["results"] = kept
                evaluation_excluded_count += len(excluded)
                filtered_responses.append((label, value))
            responses = filtered_responses
        plan["evaluation_excluded_count"] = evaluation_excluded_count

        # Relationship closure is a first-class, bounded lane.  The official
        # Bank graph has historically powered the constellation UI but was not
        # consumed by the Controller.  Read it only for an explicit closure
        # request, then merge canonical memory records as ordinary candidates.
        graph_receipt: dict[str, Any] = {
            "enabled": False,
            "endpoint": "official_memory_graph",
            "status": "not_applicable",
            "node_count": 0,
            "edge_count": 0,
            "candidate_count": 0,
            "selected_count": 0,
            "max_hops_examined": 0,
            "reason": "本轮不是实体/关系/流程闭包问题。",
        }
        if bool((plan.get("graph_route") or {}).get("enabled")) and deadline_at > time.monotonic():
            graph_seed_memory_ids = [
                str(item.get("chunk_id") or "")
                for _, response in responses
                for item in list(response.get("results") or [])[:8]
                if str(item.get("chunk_id") or "")
            ][:4]
            graph_seed_entity_names = list(dict.fromkeys(
                str(name)
                for _, response in responses
                for item in list(response.get("results") or [])[:8]
                for name in _entity_names(item.get("entities") or [])
                if _usable_entity_form(str(name or ""))
            ))[:16]
            graph_items, graph_receipt = self.graph_closure_lookup(
                urllib.parse.unquote(bank_id), semantic_query, headers,
                max(0.25, deadline_at - time.monotonic()), graph_seed_memory_ids, graph_seed_entity_names,
            )
            if graph_items:
                responses.append(("official_graph_closure", {"results": graph_items, "entities": {}}))
            facet_traces.append({
                "label": "official_graph_closure", "path": "official_memory_graph",
                "query_preview": semantic_query[:TRACE_PREVIEW_CHARS], "query_tokens": 0,
                "before_tokens": 0, "compacted": False, "kind": "graph_closure",
                "status": str(graph_receipt.get("status") or "completed"),
                "elapsed_ms": graph_receipt.get("elapsed_ms", 0),
                "result_count": len(graph_items), "error": graph_receipt.get("error"),
            })
        plan["graph_route"] = {**dict(plan.get("graph_route") or {}), "receipt": graph_receipt}

        mental_model_receipt: dict[str, Any] = {"enabled": False, "reason": "本轮不需要稳定心智模型侧车。"}
        if guidance_enabled and bool((plan.get("guidance_sidecar") or {}).get("mental_model")) and deadline_at > time.monotonic():
            mental_items, mental_model_receipt = self.stable_mental_model_sidecar(
                urllib.parse.unquote(bank_id), semantic_query, plan, headers, max(0.25, deadline_at - time.monotonic())
            )
            if mental_items:
                responses.append(("stable_guidance_mental_models", {"results": mental_items, "entities": {}}))
            facet_traces.append({
                "label": "stable_guidance_mental_models", "path": "official_mental_models_list",
                "query_preview": semantic_query[:TRACE_PREVIEW_CHARS], "query_tokens": 0,
                "before_tokens": 0, "compacted": False, "kind": "stable_guidance_sidecar",
                "status": str(mental_model_receipt.get("status") or "completed"),
                "elapsed_ms": 0, "result_count": len(mental_items), "error": mental_model_receipt.get("error"),
            })
        direct_policy_receipt: dict[str, Any] = {"enabled": False, "reason": "本轮不需要直接政策侧车。"}
        if guidance_enabled:
            direct_policy_items, direct_policy_receipt = self.direct_policy_sidecar(semantic_query, plan)
            if direct_policy_items:
                responses.append(("stable_guidance_direct_policies", {"results": direct_policy_items, "entities": {}}))
            facet_traces.append({
                "label": "stable_guidance_direct_policies", "path": "local_direct_policy_index",
                "query_preview": semantic_query[:TRACE_PREVIEW_CHARS], "query_tokens": 0,
                "before_tokens": 0, "compacted": False, "kind": "stable_guidance_sidecar",
                "status": str(direct_policy_receipt.get("status") or "completed"), "elapsed_ms": 0,
                "result_count": len(direct_policy_items), "error": direct_policy_receipt.get("error"),
            })
            plan["guidance_sidecar"] = {**dict(plan.get("guidance_sidecar") or {}), "mental_models": mental_model_receipt, "direct_policies": direct_policy_receipt}

        # A precise source/time request benefits from a literal confirmation
        # path in addition to the normal semantic, graph, and temporal route.
        # This is deliberately bounded to the current bank and the authorized
        # unified evidence bank.  It does not run for ordinary questions.
        direct_evidence: dict[str, Any] = {
            "eligible": False,
            "reason": "本次不是要求核对原话、来源或证据时间的查询。",
            "anchor": None,
            "lookups": [],
            "matched_items": 0,
        }
        literal_anchor = plan.get("direct_evidence_anchor")
        if (
            plan.get("requires_direct_evidence")
            and "raw" in plan.get("levels", [])
            and literal_anchor
            and deadline_at > time.monotonic()
        ):
            direct_evidence.update({
                "eligible": True,
                "reason": (
                    "用户明确要求追溯原话/来源；以最新用户问题中的独特片段作词面核对。"
                    + str(plan.get("direct_evidence_bank_reason") or "")
                ),
                "anchor": str(literal_anchor)[:DIRECT_EVIDENCE_MAX_ANCHOR_CHARS],
            })
            direct_banks = [urllib.parse.unquote(bank_id)]
            for evidence_bank in list(plan.get("direct_evidence_banks") or []):
                evidence_bank = str(evidence_bank or "").strip()
                if evidence_bank and evidence_bank not in direct_banks:
                    direct_banks.append(evidence_bank)
            workers = min(len(direct_banks), int(self.config.get("readParallelism", 3)))
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {
                    pool.submit(
                        self.anchored_evidence_lookup,
                        target_bank,
                        str(literal_anchor),
                        headers,
                        max(0.25, deadline_at - time.monotonic()),
                    ): target_bank
                    for target_bank in direct_banks
                }
                for future in as_completed(futures):
                    target_bank = futures[future]
                    matches, receipt = future.result()
                    direct_evidence["lookups"].append(receipt)
                    facet_traces.append({
                        "label": f"direct_evidence:{target_bank}",
                        "path": "official_memory_list",
                        "query_preview": str(literal_anchor)[:TRACE_PREVIEW_CHARS],
                        "query_tokens": query_token_count(str(literal_anchor)),
                        "before_tokens": query_token_count(str(literal_anchor)),
                        "compacted": False,
                        "status": receipt["status"],
                        "elapsed_ms": receipt["elapsed_ms"],
                        "result_count": len(matches),
                        "error": receipt.get("error"),
                        "reason": "仅对明确追溯请求执行的词面锚点核对。",
                    })
                    if matches:
                        responses.append((f"direct_evidence:{target_bank}", {"results": matches, "entities": {}}))
                        direct_evidence["matched_items"] += len(matches)
            direct_evidence["lookups"].sort(key=lambda item: str(item.get("bank") or ""))
        elif plan.get("requires_direct_evidence") and not literal_anchor:
            direct_evidence["reason"] = "已走常规证据召回；原问题没有足够长且唯一的原话片段，因此未启用精确词面核对。"

        # Two-stage adaptive recall: only open the remaining facets when the
        # initial evidence does not cover the required dimensions. This keeps
        # ordinary questions cheap while making the reason for escalation
        # explicit and auditable.
        preliminary = merge_recall_responses(
            responses, plan, ranking_deadline_at=deadline_at,
        ) if responses else {"results": []}
        initial_receipt = evaluate_coverage(preliminary.get("results") or [], plan)
        retrieval_contract = dict(plan.get("retrieval_contract") or build_retrieval_contract(effective_semantic_query if 'effective_semantic_query' in locals() else query, plan, self.config))
        evidence_quality = evaluate_evidence_quality(
            str(plan.get("full_prompt") or query), preliminary.get("results") or [], retrieval_contract
        )
        escalated = False
        escalation_skipped_reason = None
        escalation_reason = []
        escalation_requests: list[tuple[str, str, dict[str, Any]]] = []
        if (
            (initial_receipt["missing"] or evidence_quality.get("needs_escalation"))
            and (plan.get("escalate_if_missing") or {}).get("enabled")
            and plan.get("escalation_queries")
            and bool(plan.get("foreground_escalation_allowed", True))
        ):
            escalation_reason = list(dict.fromkeys([*initial_receipt["missing"], *(evidence_quality.get("reasons") or [])]))
            remaining_ms = max(0, int((deadline_at - time.monotonic()) * 1000))
            min_remaining_ms = int(self.config.get("minEscalationRemainingMs", 3500))
            semantic_responses = [
                item for item in responses
                if not str(item[0]).startswith(("direct_evidence:", "stable_guidance_"))
            ]
            if not semantic_responses:
                escalation_skipped_reason = (
                    "首轮没有可用语义结果，保留剩余时间给直接回退，不再放大请求。"
                )
            elif remaining_ms < min_remaining_ms:
                escalation_skipped_reason = (
                    f"剩余 {remaining_ms}ms，低于可选扩展最低预算 {min_remaining_ms}ms；"
                    "保留已有结果，不把预算不足误报为召回失败。"
                )
            else:
                escalated = True
            escalation_budget = str((plan.get("escalate_if_missing") or {}).get("budget") or "high")
            escalation_tokens = min(
                int(self.config.get("maxPerQueryTokens", 4096)),
                max(per_query_tokens, int((plan.get("escalate_if_missing") or {}).get("max_tokens") or per_query_tokens)),
            )
            metrics = plan.get("escalation_query_metrics") or []
            for offset, expanded in enumerate(plan.get("escalation_queries") or [] if escalated else []):
                item = dict(body)
                item.update({
                    "query": expanded,
                    "budget": escalation_budget,
                    "max_tokens": escalation_tokens,
                    "types": plan["types"],
                    "prefer_observations": plan["prefer_observations"],
                })
                label = f"escalation:{offset}"
                metric = dict(metrics[offset]) if offset < len(metrics) else {}
                request_meta[label] = {
                    "label": label,
                    "path": path,
                    "query_preview": expanded[:TRACE_PREVIEW_CHARS],
                    "query_tokens": metric.get("after_tokens", query_token_count(expanded)),
                    "before_tokens": metric.get("before_tokens"),
                    "compacted": bool(metric.get("compacted")),
                    "reason": "补齐覆盖缺口：" + ",".join(escalation_reason),
                }
                escalation_requests.append((label, path, item))
            requests.extend(escalation_requests)
            execute_batch(escalation_requests)

        fallback_used = False
        if requires_direct_fallback(responses):
            fallback_used = True
            fallback_body = dict(body)
            fallback_query, fallback_metric = compact_query(
                semantic_query,
                int(self.config.get("maxQueryTokens", SAFE_QUERY_TOKEN_LIMIT)),
            )
            fallback_body["query"] = fallback_query
            fallback_response, fallback_elapsed, fallback_error = run_request(
                "direct_fallback", path, fallback_body, max(0.25, deadline_at - time.monotonic())
            )
            facet_traces.append({
                "label": "direct_fallback",
                "path": path,
                "query_preview": fallback_query[:TRACE_PREVIEW_CHARS],
                "query_tokens": fallback_metric["after_tokens"],
                "before_tokens": fallback_metric["before_tokens"],
                "compacted": fallback_metric["compacted"],
                "status": "failed" if fallback_error else "completed",
                "elapsed_ms": fallback_elapsed,
                "result_count": len((fallback_response or {}).get("results") or []),
                "error": fallback_error,
            })
            if fallback_error:
                errors.append(f"direct_fallback:{fallback_error}")
                elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                self._append_audit_event({
                    "at": utc_now(), "event": "recall_failed", "outcome": "failed",
                    "query_id": plan["query_id"],
                    "execution_id": plan["execution_id"],
                    "query_fingerprint": plan["query_fingerprint"],
                    "user_prompt_fingerprint": user_prompt_fingerprint or plan["query_fingerprint"],
                    **trace_identity,
                    "query_preview": semantic_query[:TRACE_PREVIEW_CHARS],
                    "raw_user_prompt": plan.get("raw_user_prompt", ""),
                    "prompt_origin": plan.get("prompt_origin", ""),
                    "full_prompt": plan.get("full_prompt", ""),
                    "full_prompt_source": plan.get("full_prompt_source", ""),
                    "input_query_tokens": plan["input_query_tokens"],
                    "query_compacted": plan["query_compacted"],
                    "role": role, "client": client[:120],
                    "bank_id": urllib.parse.unquote(bank_id),
                    "shape": plan["primary_shape"], "matched_shapes": plan["matched_shapes"],
                    "memory_action": plan["memory_action"],
                    "working_set": plan["working_set"],
                    "planner": plan.get("planner"),
                    "value_of_information": plan["value_of_information"],
                    "strategies": plan["strategies"],
                    "route_decisions": plan["route_decisions"],
                    "coverage_dimensions": plan["coverage_dimensions"],
                    "queries_requested": len(requests), "queries_completed": 0,
                    "result_count": 0, "fallback_used": True,
                    "coverage_complete": False, "elapsed_ms": elapsed_ms,
                    "facets": sorted(facet_traces, key=lambda item: item["label"]),
                    "errors": errors,
                })
                raise RuntimeError(errors[-1])
            responses.append(("direct_fallback", fallback_response))

        # Escalation, graph closure and literal evidence execute after the
        # initial cutoff. Apply it once more before ranking so a late lane
        # cannot reintroduce a post-cutoff self-retained answer.
        if evaluation_as_of:
            final_responses: list[tuple[str, dict[str, Any]]] = []
            for label, response in responses:
                value = dict(response or {})
                kept, excluded = filter_results_as_of(
                    list(value.get("results") or []), evaluation_as_of
                )
                value["results"] = kept
                evaluation_excluded_count += len(excluded)
                final_responses.append((label, value))
            responses = final_responses
            plan["evaluation_excluded_count"] = evaluation_excluded_count

        merged = merge_recall_responses(responses, plan, ranking_deadline_at=deadline_at)
        brand_kept, brand_rejected = filter_superseded_brand_constraints(
            str(plan.get("full_prompt") or query), list(merged.get("results") or [])
        )
        if brand_rejected:
            merged["results"] = brand_kept
            merged["query_controller"]["brand_supersession_filter"] = {
                "active": True,
                "rejected_memory_ids": [str(item.get("id") or item.get("chunk_id") or "") for item in brand_rejected],
                "reason": "当前 Evolving Profile 查询不采用已被当前架构取代的旧产品完整保留约束。",
            }
        # Use the resolved task wording for claim/coverage descriptions.  This
        # is a naming boundary only; admission itself has already happened
        # inside ``merge_recall_responses``.
        effective_semantic_query = str(plan.get("full_prompt") or query)
        # ``merge_recall_responses`` is the one and only admission boundary.
        # It already applies prompt-bound relevance, source/time governance
        # and the separate observation/mental-model/direct-policy gates.  Do
        # not re-run the generic admission here: a second pass has no new
        # evidence and previously turned qualified, graph-supported memories
        # into an empty current-turn Packet.  Keep its complete receipt for
        # 9998 rather than overwriting it with a second, less-informed gate.
        rejected_ids = self.project_states.rejected_ids(runtime_context)
        if rejected_ids:
            kept_results = []
            rejected_now = []
            for item in merged.get("results") or []:
                item_id = str(item.get("id") or item.get("chunk_id") or "")
                if item_id and item_id in rejected_ids:
                    rejected_now.append(item_id)
                else:
                    kept_results.append(item)
            if rejected_now:
                merged["results"] = kept_results
                merged["query_controller"]["feedback_filter"] = {
                    "active": True,
                    "rejected_memory_ids": rejected_now,
                    "reason": "同一项目中用户已纠正过的旧候选仅保留审计，不再注入当前任务。",
                }
                merged["query_controller"]["result_count"] = len(kept_results)
        # Literal provenance matches are additional read-only receipts, not
        # model-recall facets.  Counting them as required facets made a fully
        # successful audit look incomplete (and incorrectly degraded the
        # controller health) whenever direct evidence was found.
        semantic_response_count = sum(
            1 for label, _ in responses
            if not str(label).startswith(("direct_evidence:", "stable_guidance_"))
        )
        # Keep the official lane result separate from foreground delivery.
        # Hindsight's vector/LLM recall can legitimately outlive an interactive
        # Hook deadline.  The derived read-only cache is allowed to recover a
        # useful packet in that case, but we must expose the official timeout
        # instead of pretending that every planned facet completed.
        official_execution_complete = not errors
        fast_recovered = bool(fast_admitted_items and merged.get("results"))
        execution_complete = bool(official_execution_complete or fast_recovered)
        coverage_receipt = evaluate_coverage(merged.get("results") or [], plan)
        coverage_complete = bool(coverage_receipt["complete"])
        evidence_quality = evaluate_evidence_quality(
            str(plan.get("full_prompt") or query), merged.get("results") or [], retrieval_contract
        )
        claim_receipt = build_claim_receipt(effective_semantic_query, merged.get("results") or [], plan)
        closure = dict(plan.get("relation_closure") or {})
        closure["status"] = coverage_receipt.get("relation_closure_status", "not_applicable")
        closure["covered"] = [x for x in coverage_receipt.get("covered", []) if x in ("related_entities", "related_decisions", "scope")]
        closure["missing"] = [x for x in coverage_receipt.get("missing", []) if x in ("related_entities", "related_decisions", "scope")]
        closure["graph_evidence"] = graph_receipt
        merged["query_controller"].update({
            "queries_requested": len(requests),
            "queries_completed": len(responses),
            "coverage_complete": coverage_complete,
            "execution_complete": execution_complete,
            "official_execution_complete": official_execution_complete,
            "official_lane_status": (
                "completed" if official_execution_complete
                else ("timed_out_recovered" if fast_recovered else "failed")
            ),
            "fast_recall": fast_receipt,
            "fast_recovered": fast_recovered,
            "fast_foreground_mode": fast_foreground_mode,
            "coverage_budget_route": bool(plan.get("coverage_budget_route")),
            "effective_deadline_ms": effective_deadline_ms,
            "errors": list(errors),
            "coverage_receipt": coverage_receipt,
            "claim_receipt": claim_receipt,
            "query_contract": claim_receipt["query_contract"],
        "retrieval_contract": retrieval_contract,
            "evidence_quality": evidence_quality,
            "relation_closure": closure,
            "graph_route": plan.get("graph_route"),
            "initial_coverage_receipt": initial_receipt,
            "escalated": escalated,
            "escalation_reason": escalation_reason,
            "escalation_skipped_reason": escalation_skipped_reason,
            "dimensions": plan.get("dimensions"),
            "layers": plan.get("layers"),
            "disclosure_level": plan.get("disclosure_level"),
            "scope_claim": (
                plan["scope_claim"] if coverage_complete
                else "partial_indexed_scope_only"
            ),
            "memory_action": plan["memory_action"],
            "working_set": plan["working_set"],
            "memory_needs": plan.get('memory_needs'),
            "guidance_query": plan.get('guidance_query'),
            "guidance_sidecar": plan.get('guidance_sidecar'),
            "planner": plan.get("planner"),
            "value_of_information": plan["value_of_information"],
            "explicit_temporal_anchor": plan.get("explicit_temporal_anchor"),
            "direct_evidence": direct_evidence,
            "source_coverage": {
                "requested_bank": urllib.parse.unquote(bank_id),
                "semantic_banks": list(dict.fromkeys([urllib.parse.unquote(bank_id)] + list(plan.get("banks") or []))),
                "direct_evidence_banks": list(plan.get("direct_evidence_banks") or []),
                "direct_evidence_lookups": list(direct_evidence.get("lookups") or []),
                "selection_reason": plan.get("direct_evidence_bank_reason"),
            },
            "continuation_status": (
                "foreground_pending"
                if (foreground_completion and execution_complete and not coverage_complete and plan.get("coverage_required"))
                else ("background_running" if (continuation_mode is False and execution_complete and not coverage_complete and plan.get("coverage_required")) else "not_needed")
            ),
            "continuation_mode": continuation_mode,
            "foreground_completion": foreground_completion,
            "foreground_continuation": foreground_continuation,
            "coverage_required": bool(plan.get("coverage_required")),
            "foreground_parent_execution_id": str(headers.get("X-Memory-Foreground-Parent-Execution-Id") or ""),
            "project_state": plan.get("project_state"),
            "source_guard": plan.get("source_guard"),
            "authority_only_override": plan.get("authority_only_override"),
            "historical_bank_required": historical_bank_required,
        })
        # Refresh the compatibility trace after execution fields and coverage
        # receipts have been finalized; clients then see the exact live route.
        merged["trace"] = {"query_controller": dict(merged["query_controller"])}
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        self.status.update(
            inc_recalls=1,
            inc_multi_query_recalls=1 if len(plan["queries"]) > 1 else 0,
            inc_fallbacks=1 if fallback_used else 0,
            inc_errors=1 if errors else 0,
            # A fast packet is useful recovery, but it does not mean the
            # official Hindsight lane completed.  Reporting ``healthy`` here
            # hid the exact production failure behind a green status page
            # whenever the stale derived cache happened to return anything.
            functional_status="healthy" if official_execution_complete else "degraded",
            last_error=None if official_execution_complete else (errors[-1] if errors else "official recall incomplete; fast recovery may be active"),
            last_error_at=None if official_execution_complete else utc_now(),
            last_recall={
                "at": utc_now(),
                "query_id": plan["query_id"],
                "execution_id": plan["execution_id"],
                "query_fingerprint": plan["query_fingerprint"],
                "role": role,
                "bank_id": urllib.parse.unquote(bank_id),
                "shape": plan["primary_shape"],
                "recall_profile": plan.get("recall_profile"),
                "deadline_ms": plan.get("deadline_ms"),
                "memory_action": plan["memory_action"],
                "queries_completed": len(responses),
                "result_count": len(merged.get("results") or []),
                "elapsed_ms": elapsed_ms,
                "input_query_tokens": plan["input_query_tokens"],
                "query_compacted": plan["query_compacted"],
                "coverage_complete": coverage_complete,
                "execution_complete": execution_complete,
                "official_execution_complete": official_execution_complete,
                "official_lane_status": (
                    "completed" if official_execution_complete
                    else ("timed_out_recovered" if fast_recovered else "failed")
                ),
                "fast_recall": fast_receipt,
                "fast_recovered": fast_recovered,
                "coverage_budget_route": bool(plan.get("coverage_budget_route")),
                "coverage_missing": coverage_receipt["missing"],
                "project_state": plan.get("project_state"),
                "source_guard": plan.get("source_guard"),
                "planner": plan.get("planner"),
                "relation_closure": closure,
                "feedback_filter": (merged.get("query_controller") or {}).get("feedback_filter"),
            "escalated": escalated,
            "errors": errors,
            },
        )
        continuation_key = self._recall_key(path, body, headers, bank_id)
        self._append_audit_event({
            "at": utc_now(),
            "event": "recall",
            "outcome": "completed" if execution_complete else "partial",
            "query_id": plan["query_id"],
            "execution_id": plan["execution_id"],
            "query_fingerprint": plan["query_fingerprint"],
            "user_prompt_fingerprint": user_prompt_fingerprint or plan["query_fingerprint"],
            **trace_identity,
            "query_preview": latest_query(query)[:TRACE_PREVIEW_CHARS],
            "raw_user_prompt": plan.get("raw_user_prompt", ""),
            "prompt_origin": plan.get("prompt_origin", ""),
            "full_prompt": plan.get("full_prompt", ""),
            "full_prompt_source": plan.get("full_prompt_source", ""),
            "input_query_tokens": plan["input_query_tokens"],
            "query_compacted": plan["query_compacted"],
            "role": role,
            "client": client[:120],
            "bank_id": urllib.parse.unquote(bank_id),
            "shape": plan["primary_shape"],
            "recall_profile": plan.get("recall_profile"),
            "deadline_ms": plan.get("deadline_ms"),
            "memory_action": plan["memory_action"],
            "working_set": plan["working_set"],
            "planner": plan.get("planner"),
            "value_of_information": plan["value_of_information"],
            "matched_shapes": plan["matched_shapes"],
            "strategies": plan["strategies"],
            "route_decisions": plan["route_decisions"],
            "contextual_intent": plan.get("contextual_intent"),
            "coverage_dimensions": plan["coverage_dimensions"],
            "queries_requested": len(requests),
            "queries_completed": len(responses),
            "result_count": len(merged.get("results") or []),
            "fallback_used": fallback_used,
            "coverage_complete": coverage_complete,
            "execution_complete": execution_complete,
            "official_execution_complete": official_execution_complete,
            "official_lane_status": (
                "completed" if official_execution_complete
                else ("timed_out_recovered" if fast_recovered else "failed")
            ),
            "fast_recall": fast_receipt,
            "fast_recovered": fast_recovered,
            "coverage_budget_route": bool(plan.get("coverage_budget_route")),
            "coverage_receipt": coverage_receipt,
            "claim_receipt": claim_receipt,
            "query_contract": claim_receipt["query_contract"],
            "retrieval_contract": retrieval_contract,
            "evidence_quality": evidence_quality,
            "relation_closure": closure,
            "graph_route": plan.get("graph_route"),
            "initial_coverage_receipt": initial_receipt,
            "escalated": escalated,
            "escalation_reason": escalation_reason,
            "escalation_skipped_reason": escalation_skipped_reason,
            "dimensions": plan.get("dimensions"),
            "layers": plan.get("layers"),
            "disclosure_level": plan.get("disclosure_level"),
            "explicit_temporal_anchor": plan.get("explicit_temporal_anchor"),
            "direct_evidence": direct_evidence,
            "source_coverage": (merged.get("query_controller") or {}).get("source_coverage"),
            "continuation_status": (merged.get("query_controller") or {}).get("continuation_status"),
            "continuation_mode": continuation_mode,
            "foreground_completion": foreground_completion,
            "foreground_continuation": foreground_continuation,
            "coverage_required": bool(plan.get("coverage_required")),
            "foreground_parent_execution_id": str(headers.get("X-Memory-Foreground-Parent-Execution-Id") or ""),
            "project_state": plan.get("project_state"),
            "source_guard": plan.get("source_guard"),
            "authority_only_override": plan.get("authority_only_override"),
            "historical_bank_required": historical_bank_required,
            "feedback_filter": (merged.get("query_controller") or {}).get("feedback_filter"),
            "entity_resolution": (merged.get("query_controller") or {}).get("entity_resolution"),
            # Persist the complete admission ledger in the owner execution
            # event.  The status service must not reconstruct rejected rows or
            # raw candidate counts from the much smaller selected-results
            # projection.
            "relevance_admission": (merged.get("query_controller") or {}).get("relevance_admission"),
            "pipeline_stages": (merged.get("query_controller") or {}).get("pipeline_stages"),
            "elapsed_ms": elapsed_ms,
            "facets": sorted(facet_traces, key=lambda item: item["label"]),
            "selected_results": trace_result_summaries(merged.get("results") or []),
            "errors": errors,
        })
        merged.setdefault('query_controller',{}).update(memory_needs=plan.get('memory_needs'),guidance_query=plan.get('guidance_query'),guidance_sidecar=plan.get('guidance_sidecar'))
        if (
            not continuation_mode
            and not foreground_completion
            and execution_complete
            and not coverage_complete
            and bool(plan.get("coverage_required"))
        ):
            self._schedule_continuation(continuation_key, path, body, headers, bank_id, merged.get("query_controller") or {})
        return merged


def is_client_disconnect(error: BaseException) -> bool:
    """Return whether a response failed because the caller went away.

    A slow recall can outlive a browser/CLI timeout.  The recall itself may
    already have completed and written an audit receipt; a broken response
    socket must not turn the controller's functional health into ``degraded``
    or trigger a second 502 write to the same closed socket.
    """
    return isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))


class ControllerHandler(BaseHTTPRequestHandler):
    server_version = f"MemoryQueryController/{VERSION}"

    @property
    def app(self) -> MemoryQueryController:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def send_bytes(
        self,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        self.send_response(status)
        forwarded = headers or {}
        for key, value in forwarded.items():
            if key.casefold() in HOP_HEADERS or key.casefold() == "content-type":
                continue
            self.send_header(key, value)
        forwarded_type = next(
            (value for key, value in forwarded.items() if key.casefold() == "content-type"),
            "application/json",
        )
        self.send_header("Content-Type", content_type or forwarded_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The caller may time out while a bounded status/effectiveness
            # snapshot is still being assembled. This is a client disconnect,
            # not a Controller failure and must not degrade health.
            return

    def send_json(
        self,
        status: int,
        value: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_bytes(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            content_type="application/json; charset=utf-8",
        )

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith('/v2/memory-os/'):
            status, value = self.app.ham_api.handle('GET', self.path, b'', dict(self.headers.items()))
            self.send_json(status, value)
            return
        if parsed.path == "/health":
            try:
                code, _, body = self.app.upstream("GET", "/health", None, dict(self.headers.items()), 3)
                upstream = json.loads(body.decode("utf-8")) if body else {}
                healthy = code == 200 and upstream.get("status") == "healthy"
                self.send_json(200 if healthy else 503, {
                    "status": "healthy" if healthy else "degraded",
                    "controller": "memory-query-controller",
                    "version": VERSION,
                    "upstream": upstream,
                    "functional_status": self.app.status.snapshot().get("functional_status"),
                    "contract_schema": self.app.contract.get("schema"),
                })
            except Exception as error:
                if is_client_disconnect(error):
                    return
                self.app.status.update(
                    inc_errors=1, functional_status="degraded",
                    last_error=repr(error), last_error_at=utc_now(),
                )
                self.send_json(503, {"status": "degraded", "error": repr(error)})
            return
        if parsed.path == "/v1/status":
            self.send_json(200, self.app.status.snapshot())
            return
        if parsed.path == "/v1/current-project-state":
            context = runtime_context_from_headers(dict(self.headers.items()))
            self.send_json(200, {"schema": 1, "project_state": self.app.project_states.summary(context)})
            return
        if parsed.path == "/ccy/observation-timeline":
            query = urllib.parse.parse_qs(parsed.query)
            bank_id = (query.get("bank") or [""])[0].strip()
            headers = {"Access-Control-Allow-Origin": "http://127.0.0.1:9999", "Cache-Control": "no-store", "Vary": "Origin"}
            if not bank_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", bank_id):
                self.send_json(400, {"schema": 1, "error": "valid bank is required", "items": []}, headers=headers)
                return
            try:
                path = f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/list?type=observation&limit=500&offset=0"
                status, _, body = self.app.upstream("GET", path, None, dict(self.headers.items()), 20)
                if status != 200:
                    raise RuntimeError(f"upstream observation list HTTP {status}")
                payload = json.loads(body.decode("utf-8"))
                self.send_json(200, {"schema": 1, "bank": bank_id, "items": payload.get("items") or [], "total": payload.get("total", 0)}, headers=headers)
            except Exception as error:
                self.app.status.update(inc_errors=1, functional_status="degraded", last_error=repr(error), last_error_at=utc_now())
                self.send_json(502, {"schema": 1, "error": repr(error), "items": []}, headers=headers)
            return
        if parsed.path == "/ccy/token-savings":
            try:
                self.send_json(200, token_savings_payload(urllib.parse.parse_qs(parsed.query)), headers={
                    "Access-Control-Allow-Origin": "http://127.0.0.1:9999",
                    "Cache-Control": "no-store", "Vary": "Origin",
                })
            except Exception as error:
                self.send_json(500, {"schema": 1, "error": repr(error)}, headers={
                    "Access-Control-Allow-Origin": "http://127.0.0.1:9999", "Cache-Control": "no-store",
                })
            return
        if parsed.path == "/ccy/backup-status":
            try:
                self.send_json(200, backup_status_payload(), headers={
                    "Access-Control-Allow-Origin": "http://127.0.0.1:9999",
                    "Cache-Control": "no-store",
                    "Vary": "Origin",
                })
            except Exception as error:
                self.send_json(500, {
                    "schema": 1,
                    "overall_status": "error",
                    "error": repr(error),
                }, headers={
                    "Access-Control-Allow-Origin": "http://127.0.0.1:9999",
                    "Cache-Control": "no-store",
                })
            return
        if parsed.path == "/v1/traces":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = min(100, max(1, int((query.get("limit") or [40])[0])))
            except ValueError:
                limit = 40
            tail_rows = recent_jsonl(self.app.audit_path, limit=limit)
            # Broad recall rows can push an otherwise recent user execution
            # outside the 1 MB safe tail. Merge the bounded trace projection
            # before joining feedback so 9998 remains truthful after log
            # pressure and after a browser refresh.
            is_status_projection=str((query.get("projection") or [""])[0]).casefold()=="status"
            indexed_rows = trace_index_rows(self.app.trace_index_path, limit=240 if is_status_projection else limit)
            rows_by_execution: dict[str, dict[str, Any]] = {}
            for row in indexed_rows + tail_rows:
                execution_id = str(row.get("execution_id") or row.get("query_id") or "")
                if execution_id:
                    rows_by_execution[execution_id] = row
            rows = sorted(rows_by_execution.values(), key=lambda row: str(row.get("at") or ""), reverse=True)[:limit]
            if is_status_projection:
                rows=select_status_trace_rows(list(rows_by_execution.values()),limit)
            execution_ids = {str(row.get("execution_id") or row.get("query_id") or "") for row in rows}
            feedback = recent_jsonl_events(self.app.effectiveness_path, limit=max(200, limit * 12))
            # Keyed receipt projection restores the real Hook injection event
            # when the append-only ledger has grown beyond its safe tail read.
            feedback.extend(receipt_index_events(self.app.effectiveness_receipt_index_path, execution_ids))
            feedback = events_for_execution_ids(
                self.app.effectiveness_path,
                execution_ids,
                recent_events=feedback,
            )
            rows = join_effectiveness(rows, feedback)
            if str((query.get("projection") or [""])[0]).casefold() == "status":
                rows = [status_trace_projection(row) for row in rows]
            self.send_json(200, {"items": rows, "count": len(rows), "limit": limit})
            return
        if parsed.path == "/v1/effectiveness":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = min(500, max(1, int((query.get("limit") or [200])[0])))
            except ValueError:
                limit = 200
            events = recent_jsonl_events(self.app.effectiveness_path, limit=limit)
            traces = recent_jsonl(self.app.audit_path, limit=min(100, limit))
            events = events_for_execution_ids(
                self.app.effectiveness_path,
                {str(row.get("execution_id") or row.get("query_id") or "") for row in traces},
                recent_events=events,
            )
            joined = join_effectiveness(traces, events)
            metric_rows=[r for r in joined if r.get('execution_mode') not in {'replay','shadow_replay','cassette_replay','mixed_execution_origins'} and r.get('prompt_origin') not in {'test_probe','diagnostic','fixture','benchmark'}]
            totals = {
                key: sum(int((row.get("memory_effectiveness") or {}).get(key) or 0) for row in metric_rows)
                for key in (
                    "retrieved_count", "injected_count", "cited_count", "likely_used_count",
                    "unknown_count", "ignored_count", "corrected_count",
                )
            }
            fingerprints: dict[str, dict[str, Any]] = {}
            for row in metric_rows:
                fingerprint = str(row.get("query_fingerprint") or "legacy-unknown")
                bucket = fingerprints.setdefault(fingerprint, {
                    "query_fingerprint": fingerprint,
                    "executions": 0,
                    "retrieved_count": 0,
                    "injected_count": 0,
                    "cited_count": 0,
                    "likely_used_count": 0,
                    "unknown_count": 0,
                    "ignored_count": 0,
                    "corrected_count": 0,
                })
                bucket["executions"] += 1
                effectiveness = row.get("memory_effectiveness") or {}
                for key in (
                    "retrieved_count", "injected_count", "cited_count",
                    "likely_used_count", "unknown_count", "ignored_count",
                    "corrected_count",
                ):
                    bucket[key] += int(effectiveness.get(key) or 0)
            self.send_json(200, {
                "schema": 2,
                "event": "memory_effectiveness",
                "identity_semantics": {
                    "execution_id": "每次召回执行唯一，用于准确关联注入与回答反馈",
                    "query_fingerprint": "相同问题文本稳定，用于跨执行聚合分析",
                },
                "items": events,
                "traces": joined,
                "aggregate": totals,
                "aggregate_scope": "production_only; replay/conflicted origins excluded",
                "diagnostic_trace_count":len(joined)-len(metric_rows),
                "by_query_fingerprint": sorted(
                    fingerprints.values(), key=lambda item: item["executions"], reverse=True
                ),
                "count": len(events),
                "limit": limit,
                "semantics": "注入不等于使用；无法证明时保持 unknown。",
            })
            return
        if parsed.path == "/v1/contract":
            self.send_json(200, {
                "schema": self.app.contract.get("schema"),
                "queryController": self.app.config,
                "strategies": sorted(set(STRATEGY_BY_SHAPE.values())),
            })
            return
        self.proxy()

    def do_POST(self) -> None:
        body = self.read_body()
        if urllib.parse.urlparse(self.path).path.startswith('/v2/memory-os/'):
            status, value = self.app.ham_api.handle('POST', self.path, body, dict(self.headers.items()))
            self.send_json(status, value)
            return
        if urllib.parse.urlparse(self.path).path == "/v1/feedback":
            try:
                value = json.loads(body.decode("utf-8"))
                execution_mode = header_value(dict(self.headers.items()), "X-Memory-Execution-Mode").strip().casefold() or "production"
                query_id = str(value.get("execution_id") or value.get("query_id") or "").strip()
                stage = str(value.get("stage") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9._:-]{4,128}", query_id):
                    raise ValueError("valid query_id is required")
                if stage not in {"injection", "answer", "correction"}:
                    raise ValueError("stage must be injection, answer, or correction")
                event = dict(value)
                event.update({
                    "at": str(value.get("at") or utc_now()),
                    "event": "memory_effectiveness",
                    "query_id": query_id,
                    "execution_id": query_id,
                    "stage": stage,
                    "execution_mode": execution_mode,
                })
                event.setdefault(
                    "feedback_id",
                    hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24],
                )
                duplicate = any(
                    row.get("feedback_id") == event["feedback_id"]
                    for row in recent_jsonl_events(self.app.effectiveness_path, limit=500)
                )
                if not duplicate:
                    append_jsonl(self.app.effectiveness_path, event)
                    with self.app.effectiveness_receipt_index_lock:
                        upsert_receipt_index(self.app.effectiveness_receipt_index_path, event)
                    if stage == "correction" and execution_mode not in {"replay", "shadow_replay", "cassette_replay"}:
                        self.app.project_states.record_correction(event)
                    self.app.status.update(inc_effectiveness_events=1)
                self.send_json(200, {
                    "status": "duplicate" if duplicate else "accepted",
                    "feedback_id": event["feedback_id"],
                    "query_id": query_id,
                    "execution_id": query_id,
                    "stage": stage,
                })
            except Exception as error:
                if is_client_disconnect(error):
                    return
                self.send_json(400, {"status": "error", "error": repr(error)})
            return
        if self.path == "/v1/plan":
            try:
                value = json.loads(body.decode("utf-8"))
                role = str(value.get("role") or "default")
                header_runtime = runtime_context_from_headers(dict(self.headers.items()))
                body_runtime = value.get("runtime_context")
                # Keep transport flags from headers (especially
                # X-Memory-Plan-Only) when the Hook also supplies the bounded
                # contextual-intent envelope in its JSON body.
                if isinstance(body_runtime, dict):
                    runtime_context = dict(header_runtime)
                    runtime_context.update(body_runtime)
                else:
                    runtime_context = header_runtime
                plan = self.app.plan(
                    str(value.get("query") or ""),
                    role,
                    str(value.get("client") or "api"),
                    str(value.get("bank_id") or ""),
                    runtime_context=runtime_context,
                    agent_plan=(value.get("agent_plan") if isinstance(value.get("agent_plan"), dict) else None),
                )
                self.send_json(200, plan)
            except Exception as error:
                if is_client_disconnect(error):
                    return
                self.app.status.update(
                    inc_errors=1, functional_status="degraded",
                    last_error=repr(error), last_error_at=utc_now(),
                )
                self.send_json(400, {"status": "error", "error": repr(error)})
            return
        match = RECALL_PATH.match(urllib.parse.urlparse(self.path).path)
        if match:
            try:
                value = json.loads(body.decode("utf-8"))
                result = self.app.execute_recall(
                    self.path,
                    value,
                    dict(self.headers.items()),
                    match.group(1),
                )
                self.send_json(200, result)
            except Exception as error:
                if is_client_disconnect(error):
                    return
                self.app.status.update(
                    inc_errors=1, functional_status="degraded",
                    last_error=repr(error), last_error_at=utc_now(),
                )
                self.send_json(502, {"status": "error", "error": repr(error), "results": []})
            return
        self.proxy(body)

    def do_PUT(self) -> None:
        self.proxy(self.read_body())

    def do_PATCH(self) -> None:
        self.proxy(self.read_body())

    def do_DELETE(self) -> None:
        self.proxy(self.read_body())

    def do_OPTIONS(self) -> None:
        if urllib.parse.urlparse(self.path).path in {"/ccy/backup-status", "/ccy/token-savings", "/ccy/observation-timeline"}:
            self.send_bytes(204, b"", headers={
                "Access-Control-Allow-Origin": "http://127.0.0.1:9999",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
            })
            return
        self.proxy(self.read_body())

    def proxy(self, body: bytes | None = None) -> None:
        if body is None and self.command not in {"GET", "HEAD"}:
            body = self.read_body()
        try:
            status, headers, response = self.app.upstream(
                self.command, self.path, body, dict(self.headers.items()), 60
            )
            self.send_bytes(status, response, headers)
        except Exception as error:
            self.app.status.update(
                inc_errors=1, functional_status="degraded",
                last_error=repr(error), last_error_at=utc_now(),
            )
            self.send_json(502, {"status": "error", "error": repr(error)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--upstream", default="http://127.0.0.1:8888")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--effectiveness-audit", type=Path, default=DEFAULT_EFFECTIVENESS_AUDIT)
    args = parser.parse_args()
    app = MemoryQueryController(
        args.contract.expanduser(),
        args.policy.expanduser(),
        args.upstream,
        args.audit.expanduser(),
        args.status.expanduser(),
        args.effectiveness_audit.expanduser(),
    )
    server = ThreadingHTTPServer((args.host, args.port), ControllerHandler)
    server.app = app  # type: ignore[attr-defined]
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
