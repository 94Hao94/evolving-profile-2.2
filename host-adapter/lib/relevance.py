"""Source-aware relevance admission shared by the controller and Codex hook.

The vector store is a candidate generator, not permission to cross the prompt
boundary.  Admission combines the *current* user intent, the candidate source
class, independent lexical anchors, local semantic reranking, and temporal /
authority metadata.  It deliberately has no fixed item-count limit.
"""
from __future__ import annotations

import hashlib
import re
import threading
from difflib import SequenceMatcher
from typing import Any, Iterable

# Bump whenever admission semantics change.  Controller-side cached decisions
# from an older policy must never silently cross the Hook boundary.
POLICY = "v17_weak_background_subject_boundary"

WEAK_TERMS = {
    "这个", "那个", "这些", "问题", "什么", "怎么", "如何", "为什么", "是不是",
    "没有", "还有", "很多", "一些", "之前", "现在", "历史", "聊天", "记录", "对应",
    "实际", "当前", "到底", "因为", "看到", "一个", "信息", "我们", "相关", "测试",
    "证据", "方案", "项目", "完整", "功能", "关联", "所有", "内容", "回答", "读取",
    "召回", "注入", "检索", "记忆", "系统", "机制", "进行", "需要", "要求", "情况",
}

# Cross-domain words that are meaningful even when only two or three Chinese
# characters long.  This list is intentionally generic and extensible; unknown
# terms still work through Latin identifiers, exact common blocks, and the
# semantic reranker.
STRONG_TERMS = (
    "hindsight", "agentmemory", "codex", "openclaw", "hermes", "trainer", "小黛", "飞书", "wps",
    "心智模型", "世界事实", "时间线", "实体", "观察", "原始事实", "权威",
    "岗位", "角色", "职责", "课程", "预算", "报价", "费用", "表格", "图表", "ppt", "word", "excel",
    "医疗", "健康", "备份", "恢复", "数据库", "并行", "限流", "hook", "mcp", "controller",
    "视觉", "链路", "可视化", "布局", "溢出", "单词", "词义", "上限", "截断", "固定",
    "工作背景", "项目背景", "关联修改", "全文检查",
)

ENTITY_ONLY_TERMS = {
    "hindsight", "agentmemory", "codex", "openclaw", "hermes", "trainer", "小黛", "飞书", "wps",
    "hook", "mcp", "controller", "ppt", "word", "excel",
}

# Reusable paraphrase families.  These do not encode answers or project names;
# they only bridge common ways the same intent is expressed.
CONCEPT_GROUPS = {
    "召回相关性": ("召回相关", "注入相关", "无关记忆", "注入无关", "记忆污染", "漏掉相关", "相关性过滤", "召回准确", "证据兜底", "召回失败", "漏召回"),
    "工作版图": ("工作背景", "工作体系", "工作主线", "工作版图", "工作汇报", "客户类型", "高校政企", "高校/政企", "客户项目", "方案架构", "业务架构", "公司业务", "投标", "商务"),
    "关联修改": ("同步修改", "关联修改", "全文依赖", "下游页面", "相关页面", "所有相关", "全局一致", "牵一发动全身", "依赖扫描", "联动修改", "同步进入", "同步纳入", "联动更新", "覆盖检查", "依赖项"),
    "医疗决策": ("医疗", "健康", "治疗", "疗效", "副作用", "诊疗", "用药", "疾病"),
    "备份恢复": ("备份", "恢复", "云端副本", "保留策略", "恢复演练", "加密备份"),
    "当前路由": ("当前路由", "日常对话", "承接日常", "不再通过", "不再参与", "改为直连", "迁移至", "迁移完成", "独立服务", "常驻服务", "当前状态", "线上模型"),
    "来源核对": ("我说过", "我的原话", "原文", "出处", "哪次说", "什么时候说", "逐字"),
    "正式交付质量": (
        "正式对客", "客户成品", "成品材料", "外发前", "发出前", "交付验收",
        "内部痕迹", "可打开性", "可访问性", "最终成品",
    ),
    "链路可视化质量": (
        "视觉检查", "视觉验收", "链路图", "实际链路", "策略图", "可视化链路",
        "布局不合理", "文字溢出", "超框", "节点详情", "动态箭头",
    ),
    "界面导航": (
        "在哪查", "在哪里看", "怎么查看", "如何查看", "操作路径", "界面元素",
        "点击查看", "点开查看", "展开详情", "打开详情", "查看入口", "查看卡片",
    ),
    "记忆写入边界": (
        "单词是什么意思", "普通单词", "临时查资料", "一次性问题", "是否写成长期记忆",
        "是否进入长期记忆", "写入长期记忆", "长期复用", "筛选存入",
    ),
    "记忆层级证据": (
        "证据血统", "证据链", "单向派生", "反向证明", "倒着用", "上层证据",
        "底层事实", "高阶模型", "原始证据", "覆盖原始事实",
    ),
    "决策风险权重": (
        "主路径", "主要风险", "残余风险", "低概率风险", "风险路径", "风险权重",
        "总体风险等级", "条件化风险", "风险信号", "行动建议",
    ),
    # Reusable input-channel ontology for validating a visible real-dialogue
    # test.  It intentionally names the channel/evidence, not a project.
    "真实可见交互": (
        "真实对话", "真实交互", "真实回复", "真实输入", "实际输入", "可见输入",
        "对话框", "对话窗口", "窗口中", "窗口里", "用户发送", "用户输入",
        "输入内容", "对话记录", "跨对话", "另一个对话", "真实测试",
    ),
    # A conflict/timeline question is a reusable proposition family. Keep
    # aliases empty: a bare ``superseded`` or ``unresolved`` must not turn an
    # arbitrary candidate into a structural match. Contextual regexes below
    # require state plus temporal/scope evidence.
    "时序冲突裁决": (),
    # Version-lineage questions are a distinct, reusable proposition family:
    # they ask when different releases/failed rollbacks must remain separate
    # and when supersession/deduplication is safe.  Keep aliases concrete so
    # a generic mention of ``版本`` alone cannot widen the query.
    "版本沿革保留": (
        "不同时间版本", "不同版本", "旧版本", "失败版本", "回退版本", "回滚版本",
        "版本沿革", "版本演进", "版本历史", "简单去重", "版本去重",
    ),
}

# These families encode an answer relation, not merely a broad topic word. A
# match therefore remains useful even when a generic cross-encoder assigns a
# low absolute score (for example a graph-expanded company fact answering a
# “工作背景” question). Broad domains such as 医疗决策 are intentionally not
# listed: the word “医疗” alone must not turn an unrelated company description
# into a medical-preference memory.
STRUCTURAL_CONCEPTS = {
    "工作版图", "关联修改", "当前路由", "正式交付质量", "记忆层级证据", "链路可视化质量", "界面导航", "医疗决策", "记忆写入边界", "时序冲突裁决", "版本沿革保留",
}

# Phrase order and particles vary in Chinese, so exact alias containment alone
# is not enough (召回相关性 vs 召回的相关性).  These patterns identify answer
# families, not project-specific content.
CONCEPT_PATTERNS = {
    "召回相关性": (
        r"(?:召回|注入|检索).{0,8}(?:相关|无关|过滤|准确|失败|误召|漏)",
        r"(?:相关|无关|不相干|污染|漏掉).{0,8}(?:召回|注入|记忆|候选)",
    ),
    "工作版图": (
        r"工作.{0,8}(?:背景|体系|主线|版图|汇报)",
        r"(?:客户|高校|政企|投标|商务).{0,20}(?:方案|项目|业务|公司)",
        r"(?:方案|项目|业务|公司).{0,20}(?:客户|高校|政企|投标|商务)",
    ),
    "关联修改": (
        r"(?:同步|联动|全文|下游|所有相关|关联(?:修改|更新|页面|依赖|复核|检查)).{0,12}(?:修改|更新|检查|扫描|页面|内容|依赖|覆盖)",
        r"(?:修改|增加|新增|删除|替换).{0,16}(?:后面|下游|所有相关|全文|关联|联动|同步|覆盖)",
        r"(?:同步|联动|覆盖|纳入|进入|扩展|涉及).{0,32}(?:岗位|角色|模块|指标|课程|字段|节点|工位|验收|平台|工程|页面|内容)",
        r"(?:岗位|角色|模块|指标|课程|字段|节点|工位|验收|平台|工程|页面|内容).{0,32}(?:同步|联动|覆盖|纳入|进入|扩展|涉及)",
    ),
    "医疗决策": (r"(?:医疗|健康|治疗|用药|药物|疾病|诊疗|副作用)",),
    "备份恢复": (r"(?:备份|恢复|云端副本|恢复演练|保留策略)",),
    "当前路由": (
        r"(?:当前|现在|日常).{0,4}(?:路由|对话入口|运行模型|线上模型|服务|承接方)",
        r"(?:不再|改为|迁移|直连|独立).{0,12}(?:路由|对话|模型|服务|承接|通过|参与)",
        r"(?:当前|现在|实时).{0,20}(?:配置|运行|启用|停用|端口|enabled|plugin)",
        r"(?:配置|运行|启用|停用|端口|enabled|plugin).{0,20}(?:当前|现在|实时)",
    ),
    "来源核对": (r"(?:原话|原文|逐字|出处|来源|哪次说|什么时候说|是否说过)",),
    "正式交付质量": (
        r"(?:客户|对客|外发|发出|成品|交付).{0,18}(?:验收|真实|痕迹|打开|访问|渲染|链接)",
        r"(?:验收|真实|痕迹|打开|访问|渲染|链接).{0,18}(?:客户|对客|外发|发出|成品|交付)",
        r"(?:读入|修改|保存|结构扫描|渲染|打开|复核|验收).{0,54}(?:结构扫描|渲染|打开|复核|验收|可访问|链接|回滚)",
        r"(?:结构对比|结构扫描|页眉|页脚|分节符|公式|溢出|图片数量|表格数量|页面).{0,48}(?:检查|异常|回到|复核|渲染|打开|修改前|验收)",
    ),
    "链路可视化质量": (
        r"(?:视觉|可视化|链路图|流程图).{0,24}(?:检查|核对|验收|布局|节点|箭头|溢出|超框|详情)",
        r"(?:检查|核对|验收|布局|节点|箭头|溢出|超框|详情).{0,24}(?:视觉|可视化|链路图|流程图)",
    ),
    "界面导航": (
        r"(?:查看|查找|打开|展开|进入|点击|点开).{0,28}(?:页面|状态页|卡片|节点|详情|入口|界面|内容)",
        r"(?:页面|状态页|卡片|节点|详情|入口|界面|操作路径).{0,28}(?:查看|查找|打开|展开|进入|点击|点开)",
    ),
    "记忆写入边界": (
        r"(?:单词|临时查资料|一次性问题).{0,28}(?:长期记忆|写入|存入|保留|筛选)",
        r"(?:长期记忆|写入|存入|保留|筛选).{0,28}(?:单词|临时查资料|一次性问题)",
    ),
    "记忆层级证据": (
        r"(?:底层|上层|高阶|世界事实|经历|观察|心智模型|原始证据).{0,24}(?:证据链|派生|反向|倒着|覆盖|证明)",
        r"(?:证据链|派生|反向|倒着|覆盖|证明).{0,24}(?:底层|上层|高阶|世界事实|经历|观察|心智模型|原始证据)",
    ),
    "时序冲突裁决": (
        # Require a concrete governance proposition in addition to a status
        # word.  A database note such as "superseded 字段只是删除标记，
        # 不涉及时间线" must not become a timeline/conflict memory.
        r"(?:superseded|unresolved|状态替代|冲突断言|矛盾状态|旧结论|新结论|取代旧结论).{0,96}(?:同一主体|同一对象|属性|范围(?:相同|重叠)|有效时间|生效时间|证据谱系|证据范围|适用边界|版本链|历史经历|保留|追加|取代旧结论|更新或取代|当前或特定时期|后一次成功不删除|新证据|判断顺序|去重|并列保留|状态台账|生命周期状态机)",
        r"(?:同一主体|同一对象|属性|范围(?:相同|重叠)|有效时间|生效时间|证据谱系|证据范围|适用边界|版本链|历史经历|保留|追加|取代旧结论|更新或取代|当前或特定时期|后一次成功不删除|新证据|判断顺序|去重|并列保留|状态台账|生命周期状态机).{0,96}(?:superseded|unresolved|状态替代|冲突断言|矛盾状态|旧结论|新结论|取代旧结论)",
    ),
    "版本沿革保留": (
        r"(?:不同时间版本|不同版本|旧版本|失败版本|回退版本|回滚版本|版本沿革|版本演进|版本历史|版本去重|简单去重).{0,56}(?:升级|切换|替代|回退|回滚|保留|去重|历史|生效|失效|失败|版本)",
        r"(?:升级|切换|替代|回退|回滚|保留|去重|历史|生效|失效|失败).{0,56}(?:不同时间版本|不同版本|旧版本|失败版本|回退版本|回滚版本|版本沿革|版本演进|版本历史|版本去重|简单去重)",
        r"(?:观察|经历|事实|结论|记忆).{0,40}(?:版本|去重|保留|时间范围|scope).{0,40}(?:去重|保留|版本|时间)",
        r"(?:旧结论|旧规则|旧记忆|旧状态).{0,56}(?:替代|失效|保留|回退|回滚|删除|不删除|历史)",
        r"(?:去重|保留|时间感知排序|版本链).{0,56}(?:观察|经历|事实|结论|记忆|记录|版本|范围)",
    ),
    "决策风险权重": (
        r"(?:主路径|主要风险|残余风险|低概率风险|风险路径).{0,24}(?:权重|等权|等级|行动|条件|信号)",
        r"(?:权重|等权|等级|行动|条件|信号).{0,24}(?:主路径|主要风险|残余风险|低概率风险|风险路径)",
    ),
}

# Deterministic ontology bridges used only to widen candidate generation.  They
# contain stable vocabulary for an intent family, never a project-specific
# answer.  Admission still evaluates every returned item against the original
# current question, so a bridge may improve recall without granting injection.
QUERY_EXPANSION_HINTS = {
    "正式交付质量": "正式对客材料 外发前验收 真实性 内部痕迹 可打开性 可访问性 渲染 来源链接",
    "记忆层级证据": "世界事实 经历 观察 心智模型 原始证据 单向派生 不能反向",
    "决策风险权重": "可决策风险 主路径 低概率残余风险 条件化总体风险等级 可观察因果信号 行动建议",
    "链路可视化质量": "视觉检查 链路图 实际执行路径 策略全貌 节点详情 布局 箭头 溢出 超框",
    # Hindsight stores sentence-like propositions.  A bag of UI nouns was
    # dominated by generic Hook architecture memories; phrase the missing
    # relation as a natural question so the official semantic retriever can
    # recover click-path and card-detail evidence for any named UI target.
    "界面导航": "界面中如何查看目标的详细内容 应点击哪一个卡片或节点",
    "医疗决策": "医疗 健康 治疗 用药 高风险 时效性 副作用 因果链 替代方案 可决策",
    "记忆写入边界": "普通单词释义 临时查资料 一次性问题 不进入长期记忆 当前对话上下文 长期复用偏好状态规则经历规律",
    "真实可见交互": "真实可见对话 用户在窗口输入 用户发送 真实回复 实际交互 触发Hook 真实测试回放",
    "时序冲突裁决": "同一对象状态冲突 旧结论 新结论 superseded unresolved 生效时间 证据时间 来源 范围 版本 历史经历保留",
    "版本沿革保留": "不同时间版本 版本沿革 旧版本 失败版本 回退版本 保留历史 去重 生效时间 superseded unresolved",
}

RAW_SOURCE_MARKERS = (
    "direct_evidence", "user-evidence", "user_evidence", "qianwen", "doubao", "豆包", "千问",
    "feishu-trainer-work", "user-messages", "user_messages", "raw-evidence", "raw_evidence",
    "chat-history", "conversation-history", "archive-v1", "原话证据", "用户原话",
)
TRUSTED_SOURCE_MARKERS = (
    "deterministic", "authority", "governance", "semantic-dependency", "operational-timeline",
    "project-current-state", "live-authority", "stable-core",
)

_MODEL = None
_MODEL_ERROR = None
_MODEL_LOCK = threading.Lock()


def compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def filter_superseded_brand_constraints(
    query: str, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep an explicitly historical product rule out of a current EP answer.

    This is deliberately narrow. It does not erase Hindsight history, nor does
    it reject ordinary architectural evidence that mentions the old engine.
    It only blocks the earlier "must preserve the original product completely"
    constraint when the user has explicitly asked about the current Evolving
    Profile product.
    """
    query_text = compact(query)
    if "evolvingprofile" not in query_text:
        return items, []

    obsolete_markers = (
        "原版hindsight", "完整保留", "严禁删减", "轻量原型",
        "全局替换", "保留原名", "双路径迁移", "双路径", "尚未直接重命名", "旧链接",
        "底层根目录", "mcp工具名", "包名",
    )
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in items:
        text = compact(item.get("text") or item.get("content"))
        legacy_product_constraint = (
            "hindsight" in text
            and (
                sum(marker in text for marker in obsolete_markers[:4]) >= 2
                or sum(marker in text for marker in obsolete_markers[4:]) >= 2
            )
        )
        if legacy_product_constraint:
            metadata = dict(item.get("metadata") or {})
            admission = dict(metadata.get("_ccy_admission") or {})
            admission.update({
                "decision": "rejected_superseded_brand_constraint",
                "reason": "当前问题明确指向 Evolving Profile；旧产品的完整保留约束仅保留作历史审计，不作为当前架构指导。",
            })
            metadata["_ccy_admission"] = admission
            item["metadata"] = metadata
            rejected.append(item)
        else:
            kept.append(item)
    return kept, rejected


def intent_text(query: str) -> str:
    """Remove retrieval exclusions from topical matching, not from semantics.

    In “只检查召回，不要搜索豆包原话”, 豆包/原话 are forbidden
    sources, not requested subjects.  They must not become positive anchors.
    The narrow command patterns below deliberately do not remove proposition
    negation such as “trainer 现在不走 OpenClaw 吗”.
    """
    text = str(query or "")
    exclusion = re.compile(
        r"(?:不要|不用|无需|禁止)(?:再)?(?:搜索|查找|查|找|读取|调取)?"
        r"[^，。；;!?！？\n]{0,36}(?:原话|原文|出处|来源|证据|豆包|千问|飞书|微信)"
        r"[^，。；;!?！？\n]{0,24}"
    )
    return exclusion.sub(" ", text)


def _medical_domain_signal(value: str) -> bool:
    """Return whether ``健康`` is a personal/medical intent, not service health.

    ``健康`` is deliberately a reusable strong term, but phrases such as
    ``服务健康`` and ``系统健康`` occur in operational prompts.  Treating
    those as the medical concept family adds the medical synonym bridge to
    otherwise unrelated questions (for example a memory-quality audit),
    polluting candidate generation and latency.  Explicit medical vocabulary
    always wins; a bare health question remains supported.
    """
    text = compact(value)
    explicit = ("医疗", "治疗", "疗效", "副作用", "诊疗", "用药", "药物", "疾病", "医生", "症状", "临床", "就医", "患者")
    if any(marker in text for marker in explicit):
        return True
    if "健康" not in text:
        return False
    operational = (
        "服务健康", "系统健康", "接口健康", "运行健康", "进程健康",
        "数据库健康", "api健康", "健康检查", "健康状态", "健康端点",
        "连接健康", "服务的健康",
    )
    if any(marker in text for marker in operational):
        return False
    return True


def _concept_labels(value: str) -> set[str]:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", compact(value))
    labels = {
        label for label, aliases in CONCEPT_GROUPS.items()
        if (label != "医疗决策" or _medical_domain_signal(normalized))
        and any(compact(alias) in normalized for alias in aliases)
    }
    for label, patterns in CONCEPT_PATTERNS.items():
        if label == "医疗决策" and not _medical_domain_signal(normalized):
            continue
        if any(re.search(pattern, normalized, re.I | re.S) for pattern in patterns):
            labels.add(label)
    return labels


def semantic_query_expansions(query: str) -> list[str]:
    """Return bounded, reusable vocabulary bridges for candidate retrieval."""
    return [
        QUERY_EXPANSION_HINTS[label]
        for label in sorted(_concept_labels(intent_text(query)))
        if label in QUERY_EXPANSION_HINTS
    ]


def operational_audit_query(query: str) -> bool:
    """Detect a structured runtime/Bank comparison without topic allow-lists.

    A prompt can mention ``日志、脚本、链路、Bank、状态页`` because it asks
    us to reconcile candidate/admission/delivery evidence.  That is different
    from asking for the user's verbatim wording or the original archive.  The
    old provenance gate saw words such as ``来源``/``我说过`` and rejected the
    very structured chain records needed to explain the audit.  This helper is
    deliberately conjunctive: at least two operational surfaces plus an
    audit action are required, while explicit raw-wording markers keep their
    provenance behavior.
    """
    q = compact(query)
    surfaces = (
        "日志", "脚本", "链路", "状态页", "9998", "实际注入", "注入回执", "回执",
        "候选", "bank", "controller", "hook", "packet", "检索", "召回",
        "运行时", "实际流程", "对账",
    )
    actions = (
        "对比", "比较", "核对", "审计", "找全", "全面", "验证", "一致",
        "问题在哪", "问题是什么", "怎么回事", "为什么", "是否合理", "是否有问题",
    )
    raw_markers = (
        "原话", "原文", "逐字", "出处", "哪次说", "什么时候说",
        "说过的原话", "我的原文", "具体哪条原话",
    )
    surface_hits = sum(marker in q for marker in surfaces)
    # ``链路/候选/检索/脚本`` also describe perfectly ordinary work plans
    # (for example a literature-search workflow).  They are not proof that
    # the user is asking for a runtime injection audit.  Require at least one
    # runtime-specific surface before opening the stricter audit gate.  This
    # keeps the audit lane for prompts that actually mention a Hook,
    # Controller, Bank, Packet/receipt, status page, logs or a runtime
    # execution, while leaving domain workflows on their normal subject lane.
    runtime_surfaces = (
        "日志", "状态页", "9998", "实际注入", "注入回执", "投递回执", "回执",
        "controller", "控制器", "hook", "packet", "memorypacket",
        "executionid", "queryid", "hookinvocationid", "运行时", "实际流程",
        "对账", "bank", "hindsight检索", "官方hindsight", "准入",
    )
    runtime_surface_hits = sum(marker in q for marker in runtime_surfaces)
    action_hit = any(marker in q for marker in actions)
    # Natural user complaints often omit an explicit verb such as “核对” or
    # “审计”: “状态页近期注入都是 0”“候选很多但实际注入为 0”.  These are
    # still precise runtime-audit propositions.  Recognize the zero-injection
    # relation only when it is tied to a visible status/receipt/packet surface,
    # so an unrelated numerical question cannot open the audit lane.
    zero_injection_complaint = bool(
        any(marker in q for marker in ("注入都是0", "注入全是0", "注入为0", "注入是0", "注入都为0", "注入数量为0"))
        or ("候选" in q and "实际注入" in q and "0" in q)
        or ("注入" in q and any(marker in q for marker in ("零条", "没注入", "失败", "不准", "对不上", "不一样")))
    )
    explicit_raw = any(marker in q for marker in raw_markers)
    return (
        (surface_hits >= 2 or (zero_injection_complaint and any(marker in q for marker in ("状态页", "9998", "注入回执", "packet"))))
        and runtime_surface_hits >= 1
        and (action_hit or zero_injection_complaint)
        and not explicit_raw
    )


# These are *evidence surfaces*, not product/topic allow-lists.  They are
# deliberately grouped by role so a structured audit record must describe a
# connected retrieval/delivery path rather than merely repeat one word such as
# ``Hindsight`` or ``注入``.  The groups are also used by the Hook boundary;
# keeping the vocabulary here makes the Controller and Hook use the same
# proposition instead of silently drifting.
_OPERATIONAL_SURFACE_FAMILIES = {
    "prompt_context": (
        "fullprompt", "原始prompt", "用户prompt", "完整问题", "上下文", "前文",
        "工作集", "背景", "userpromptsubmit",
    ),
    "runtime_trace": (
        "日志", "脚本", "运行时", "真实链路", "executionid", "queryid",
        "hookinvocationid", "链路", "实际流程",
    ),
    "controller": ("controller", "控制器", "查询控制器"),
    "bank_recall": (
        "bank", "hindsight检索", "官方hindsight", "检索结果", "召回结果",
        "候选", "准入", "筛选",
    ),
    "delivery_packet": (
        "packet", "memorypacket", "claimbundle", "实际注入", "注入回执",
        "投递回执", "回执", "未投递", "未注入", "注入",
    ),
    "status_projection": (
        "状态页", "页面投影", "状态投影", "9998", "状态回读",
    ),
    # Method/lineage records may not repeat every runtime node, but they still
    # explain how the audit is performed (for example, separating the raw
    # Prompt from Full Prompt and checking source/time/evidence).  Keeping this
    # as a distinct role avoids rejecting those useful governance memories
    # while the multi-surface requirement still blocks generic project notes.
    "evidence_method": (
        "审计", "对比", "比较", "核对", "对齐", "来源", "时间", "证据",
        "分开保存", "补足上下文", "完整问题", "验收", "复测",
    ),
}
_OPERATIONAL_EVIDENCE_MARKERS = (
    "区分", "分别", "对齐", "贯穿", "投递", "实际", "回执", "状态",
    "链路", "核对", "验证", "证据", "完整", "分开", "来源", "时间",
    "执行", "保存", "补足", "重建", "覆盖", "候选", "准入", "检索",
)


def _is_legacy_candidate_wrapper(text: str) -> bool:
    """Return whether text is an embedded/unsaved conversation wrapper.

    The fast timeout lane can surface ``candidate:...`` rows copied from the
    old common-candidates queue.  Those rows are useful for an audit oracle,
    but a full user/assistant transcript is not a canonical Bank claim: its
    assistant half can repeat the current query and make an unrelated project
    appear relevant.  Keep this shape test separate from topic relevance so
    it remains generic and does not encode a project or prompt allow-list.
    """
    raw_text = str(text or "")
    # The fast fallback frequently stores only the assistant half of a pending
    # item (``[pending item …][role: assistant]``), with no serialized user
    # half.  Requiring a user tag here therefore lets exactly the dangerous
    # wrapper through.  A ``candidate:`` identity is already non-canonical;
    # either an explicit pending marker or a role-tagged transcript is enough
    # to classify it as an old conversation wrapper.  Concise canonical
    # candidate summaries have neither marker and continue through normal
    # relevance gates.
    return bool(
        (
            re.search(r"\[pending\s*item", raw_text, re.I)
            and re.search(r"\[role\s*:\s*(?:user|assistant)\]", raw_text, re.I)
        )
        or (
            re.search(r"\[role\s*:\s*user\]", raw_text, re.I)
            and re.search(r"\[role\s*:\s*assistant\]", raw_text, re.I)
        )
    )


def _is_diagnostic_artifact_candidate(item: dict[str, Any]) -> bool:
    """Reject log/script output masquerading as a semantic memory."""
    identity = str(item.get("id") or item.get("chunk_id") or "").casefold()
    if not identity.startswith("candidate:"):
        return False
    text = " ".join(str(item.get("text") or item.get("content") or "").split()).strip()
    if not text:
        return False
    prefixes = ("=== recent llm log", "=== recent log", "ok {", "warning:", "traceback (most recent call last)")
    if text.casefold().startswith(prefixes):
        return True
    if len(text) >= 80 and text.count("{") >= 1 and text.count("}") >= 1 and not any(mark in text for mark in ("。", "；", ". ")):
        return True
    return False


def operational_audit_alignment(query: str, text: str) -> dict[str, Any]:
    """Check whether a candidate carries a connected operational-audit claim.

    ``operational_audit_query`` only decides that the *question* is asking
    for a logs/Bank/Packet reconciliation.  It must not, by itself, make all
    Controller candidates relevant.  This second, candidate-side check asks
    for either (a) three or more connected chain surfaces plus a relation, or
    (b) three evidence surfaces plus two audit/evidence markers.  The latter
    keeps useful Full-Prompt/context methodology records while rejecting
    unrelated project or plugin memories that happen to mention Hindsight.
    No project name, memory id, or fixed item count is used.
    """
    required = operational_audit_query(query)
    # Hindsight's legacy fallback can wrap an old conversation as
    # ``[pending item] … [role: user] … [role: assistant] …``.  The assistant
    # half often contains the very words being audited (``注入``/``回执``), so
    # scoring the whole wrapper creates a self-referential false positive and
    # lets an unrelated old chat cross the audit boundary.  For this one
    # candidate shape, use only the original user side as evidence.  Normal
    # structured memory is left untouched, and a genuinely operational user
    # prompt still qualifies from its own chain words.
    raw_text = str(text or "")
    legacy_pending_wrapper = _is_legacy_candidate_wrapper(raw_text)
    if legacy_pending_wrapper:
        raw_text = re.split(r"\[role\s*:\s*assistant\]", raw_text, maxsplit=1, flags=re.I)[0]
    t = compact(raw_text)
    family_hits = {
        family: [marker for marker in markers if marker in t]
        for family, markers in _OPERATIONAL_SURFACE_FAMILIES.items()
    }
    surfaces = [family for family, hits in family_hits.items() if hits]
    # Count distinct chain roles, not aliases within one role.  A candidate
    # saying “注入、注入回执、实际注入” therefore cannot pass on delivery
    # repetition alone.
    chain_roles = [
        family for family in (
            "prompt_context", "runtime_trace", "controller", "bank_recall",
            "delivery_packet", "status_projection",
        ) if family in surfaces
    ]
    evidence_hits = [marker for marker in _OPERATIONAL_EVIDENCE_MARKERS if marker in t]
    relation = [marker for marker in ("区分", "分别", "对齐", "贯穿", "核对", "投递", "覆盖", "分开", "因果", "导致", "解释", "说明", "链路", "顺序", "位置") if marker in t]
    qualified = bool(
        required
        and not legacy_pending_wrapper
        and (
            (len(chain_roles) >= 3 and bool(relation))
            or (len(surfaces) >= 3 and len(evidence_hits) >= 2)
        )
    )
    return {
        "required": required,
        "surface_families": surfaces,
        "surface_hits": {family: hits[:8] for family, hits in family_hits.items() if hits},
        "chain_roles": chain_roles,
        "evidence_markers": evidence_hits[:16],
        "relation_markers": relation,
        "legacy_pending_wrapper": legacy_pending_wrapper,
        "qualified": qualified,
    }


def explicit_user_source_request(query: str) -> bool:
    """True only when the user asks for their own source/wording/provenance.

    A system audit mentioning "evidence", "recall", or "injection" is not a
    request to search raw Doubao/Qianwen/Feishu archives.
    """
    q = compact(query)
    negative = any(x in q for x in (
        "不要搜索豆包", "不要搜索千问", "不要搜索飞书", "不要查豆包", "不要查千问", "不要查飞书",
        "不查原话", "无需原话", "不要原话", "禁止搜索原话", "不要搜索原话", "不用找原话",
    ))
    if negative:
        return False
    personal = any(x in q for x in (
        "我说过", "我之前说", "我以前说", "我的原话", "我当时说", "是否说过", "哪次说",
        "什么时候说", "我提到过", "我问过", "跟豆包", "在豆包", "豆包里", "在千问", "千问里",
        "飞书里", "微信里", "培训导师说过", "原文在哪", "出处在哪",
    ))
    provenance = any(x in q for x in (
        "原话", "原文", "逐字", "出处", "来源", "哪次", "什么时候", "时间", "说过", "提到过",
    ))
    # “证据来源/适用边界” in a synthesis question describes the metadata
    # that should accompany an observation or mental model.  It is not a
    # request to recover the user's verbatim wording.  Treating every
    # occurrence of “来源” or “时间” as an archive request activates the
    # provenance proposition gate and rejects the very structured rows needed
    # to answer the synthesis.  Keep a separate, explicit wording signal for
    # opening raw-dialogue provenance mode.
    explicit_wording = any(x in q for x in (
        "原话", "原文", "逐字", "出处", "说过", "提到过", "哪次", "什么时候",
        "在豆包", "在千问", "在飞书", "在微信",
    ))
    named_archive = any(x in q for x in ("豆包", "千问", "飞书", "微信", "培训导师"))
    # “我以前关于 X 的原话/来源是什么” is also an explicit provenance
    # request.  Earlier code recognized only the more rigid “我以前说…”, so
    # a natural first-person wording silently skipped both raw evidence lookup
    # and the audit trace.  First-person history alone still does not open raw
    # archives: it must be paired with an explicit provenance term below.
    first_person_history = any(x in q for x in (
        "我以前", "我之前", "我过去", "我当时", "我曾经", "我历史上",
    ))
    system_only = any(x in q for x in (
        "召回相关性", "注入相关性", "记忆系统", "记忆机制", "querycontroller", "memoryquerycontroller",
        "召回链路", "注入链路", "检索策略", "准入策略",
    ))
    # "我之前……的原话/时间/来源" remains an explicit provenance request
    # even when its subject is the memory system itself.  The old system_only
    # exclusion accidentally treated that natural wording as a generic system
    # audit, so raw evidence was never opened and broad mental models leaked
    # into a question that explicitly asked for the user's own words.
    return bool(
        personal
        or (first_person_history and explicit_wording)
        or ((named_archive or "我的" in q) and explicit_wording and provenance and not system_only)
    )


def provenance_proposition(query: str) -> str:
    """Return the concrete claim in a user-provenance question.

    Source names and instructions such as ``找原话、时间和来源`` describe how
    to verify a claim; they are not part of the claim.  Keeping them in the
    relevance text caused any Feishu archive chunk to look relevant merely
    because both sides said ``飞书``.
    """
    value = intent_text(str(query or ""))
    wrappers = (
        r"我?(?:之前|以前|当时)?在?(?:飞书|千问|豆包|微信)(?:里|中)?",
        r"我?(?:之前|以前|当时)?(?:是否|有没有)?",
        r"(?:什么时候|哪次|是否|有没有)?说过",
        r"(?:请)?(?:给我)?(?:找|查|查找|核对|回顾)(?:一下)?",
        r"(?:原话|原文|逐字内容|证据|出处|来源|时间)(?:和|及|、|，|,)*",
    )
    for pattern in wrappers:
        value = re.sub(pattern, " ", value, flags=re.I)
    value = re.sub(r"[？?，,：:；;。.!！]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def provenance_structured_alignment(query: str, text: str) -> dict[str, Any]:
    """Decide whether a structured row is a useful *lead* for an original-word query.

    A provenance request must not turn every architecture memory into apparent
    evidence.  Non-raw rows may remain only when they preserve at least two
    concrete proposition facets from the requested claim; raw evidence still
    goes through the stricter local excerpt path below.
    """
    q = compact(provenance_proposition(query))
    t = compact(text)
    requested = explicit_user_source_request(query)
    families = {
        "injection": ("注入", "召回"),
        "fixed_count": ("固定", "条数", "上限", "限额"),
        "irrelevant": ("无关", "误注", "不必要", "噪声"),
        "coverage": ("漏注", "漏掉", "遗漏", "覆盖", "完整", "相关记忆"),
    }
    requested_families = [name for name, terms in families.items() if any(term in q for term in terms)]
    candidate_families = [name for name, terms in families.items() if any(term in t for term in terms)]
    # Query wording can describe all three quality concerns.  Candidate rows
    # with any two preserve a useful audit lead; generic architecture rows do
    # not.  For a narrower source request, require every explicitly named
    # family (but at least one) instead.
    required_hits = 2 if len(requested_families) >= 2 else 1
    shared = sorted(set(requested_families) & set(candidate_families))
    return {
        "requested": requested,
        "requested_families": requested_families,
        "candidate_families": candidate_families,
        "shared_families": shared,
        "required_hits": required_hits,
        "passes": (not requested) or len(shared) >= required_hits,
    }


def continuity_task_alignment(query: str, text: str) -> dict[str, Any]:
    """Require the specific task thread for a concrete cross-task handoff.

    ``当前状态`` plus an agent name is intentionally broad.  When a user asks
    to take over a *named migration / rollback / unfinished task*, that broad
    overlap must not admit every old Codex or agent configuration fact.  This
    gate is structural: it activates only when the question contains both a
    continuity request and at least two task-operation families.  A terse
    ``接管上个任务`` still retains normal semantic recall.
    """
    q = compact(intent_text(query))
    candidate = compact(text)
    # Do not confuse a generic methodology question with a concrete
    # cross-task handoff.  Words such as ``回退``/``未完成`` occur naturally
    # when someone asks how migration states should be classified; activating
    # the handoff gate for those words alone makes a broad question require an
    # exact file/path hit and drops otherwise relevant migration evidence.  A
    # hard continuity scope needs an explicit handoff cue, or an explicit
    # historical case cue (``之前/上次/那次``) tied to a task/migration.
    explicit_handoff = any(term in q for term in (
        "接管", "接手", "移交", "交接", "接续", "继续处理", "继续做", "上个任务", "之前任务", "跨任务", "跨会话",
        "还原", "恢复该任务", "恢复这个任务",
    ))
    # A document-revision prompt often says “之前已整理好的内容” and
    # “项目需求书/旧版文件”.  Those are temporal references, but they do
    # not mean that the user is handing over an earlier task.  Treat a
    # historical case as a handoff only when the temporal cue is attached to
    # an explicit lifecycle operation (migration/rollback) or an explicit
    # task/session continuity cue.  Generic project/document words and the
    # ordinary “恢复/旧版” wording stay in the document-change lane.
    historical_marker = any(term in q for term in (
        "之前", "此前", "上次", "那次", "曾经", "过去的", "历史", "历史任务",
    ))
    historical_lifecycle = any(term in q for term in (
        "迁移", "迁出", "迁入", "搬迁", "转移", "回退", "回滚", "rollback",
    ))
    historical_continuity = any(term in q for term in (
        "之前的任务", "此前的任务", "上次任务", "历史任务", "原任务", "跨任务", "跨会话",
    ))
    historical_case = historical_marker and (historical_lifecycle or historical_continuity)
    continuity = explicit_handoff or historical_case
    families = {
        "迁移": ("迁移", "迁出", "迁入", "搬迁", "转移"),
        "回退": ("回退", "回滚", "rollback", "恢复"),
        "任务状态": ("未完成", "完成项", "下一步", "待处理", "进度"),
        "工件": ("文件夹", "目录", "文件", "路径", "脚本", "配置", "config"),
    }
    asked = [name for name, aliases in families.items() if any(alias in q for alias in aliases)]
    # A named cross-task handoff must remain attached to the named system or
    # agent.  Without this check, any record that says “迁移 + 当前状态” can
    # slip through (for example a Hindsight diagnostic when the user asked to
    # take over a Codex/Xiaodai folder migration).  These are reusable domain
    # identifiers, not a project-specific allowlist; an unnamed handoff keeps
    # the ordinary semantic route.
    requested_entities = sorted({term for term in ENTITY_ONLY_TERMS if term in q})
    entity_hits = [term for term in requested_entities if term in candidate]
    matched = []
    for name, aliases in families.items():
        if name != "回退":
            if any(alias in candidate for alias in aliases):
                matched.append(name)
            continue
        # “回退/回滚” means reversible restoration of the same task, whereas
        # a service merely saying “已恢复正常” is a different incident class.
        # Treat “恢复” as a synonym only when the user actually asked for
        # recovery rather than rollback.
        asks_rollback = any(alias in q for alias in ("回退", "回滚", "rollback"))
        rollback_match = any(alias in candidate for alias in ("回退", "回滚", "rollback"))
        recovery_match = "恢复" in candidate and "恢复" in q
        if rollback_match or (not asks_rollback and recovery_match):
            matched.append(name)
    required = continuity and len(asked) >= 2
    # Lifecycle evidence is what ties an old record to the named handoff.  A
    # file/config overlap by itself is too weak because almost every technical
    # task has one; require a migration, rollback, or explicit task-state link.
    lifecycle = {"迁移", "回退", "任务状态"}
    lifecycle_match = bool(lifecycle.intersection(asked).intersection(matched))
    # If the handoff names a concrete artifact (folder, path, configuration or
    # rollback script), the record must carry that artifact family too.  A
    # generic retrospective that happens to say “Codex + migration” is not an
    # actionable handoff record unless it identifies what is being taken over.
    artifact_required = "工件" in asked
    artifact_match = "工件" in matched
    # The broad ``工件`` family is useful for an unnamed handoff, but it is
    # not enough when the Full Prompt names a concrete artifact. A record
    # about another migration, config rollout or account rollback must not
    # enter simply because it shares the generic words ``迁移``/``回退``.
    # Preserve the user's specific scope with an ontology-level check rather
    # than a project allowlist; equivalent folder/path/file wording remains
    # valid across repositories.
    artifact_scope_terms = {
        "文件夹": ("文件夹", "目录", "路径"),
        "目录": ("文件夹", "目录", "路径"),
        "路径": ("文件夹", "目录", "路径"),
        "文件": ("文件", "脚本", "路径", "目录", "文件夹"),
        "脚本": ("脚本", "文件", ".mjs", ".py", ".sh"),
        "配置": ("配置", "config", "设置"),
    }
    requested_artifact_terms = [term for term in artifact_scope_terms if term in q]
    # Chinese terms overlap (``文件`` is contained in ``文件夹``). Keep only
    # the most specific requested members so a folder handoff does not
    # accidentally inherit the looser file/script/config aliases.
    requested_artifact_terms = [
        term for term in requested_artifact_terms
        if not any(term != other and term in other for other in requested_artifact_terms)
    ]
    artifact_scope_aliases = sorted({
        alias for term in requested_artifact_terms for alias in artifact_scope_terms[term]
    })
    artifact_scope_hits = [alias for alias in artifact_scope_aliases if alias in candidate]
    artifact_scope_required = bool(requested_artifact_terms)
    # A concrete folder/path request is stronger than the generic artifact
    # family. Requiring one exact family member prevents monitoring, account
    # or unrelated service migrations from being admitted as the handoff
    # record while accepting paraphrases such as “目录迁出”.
    artifact_scope_match = (not artifact_scope_required) or bool(artifact_scope_hits)
    # A lexical mention of the requested folder/task is not evidence that the
    # candidate actually describes that handoff.  This matters for memories
    # produced by UI inspection or prompt quoting: they may repeat the user's
    # task title while reporting an unrelated screen.  Require at least one
    # lifecycle/result predicate when a concrete handoff is in scope.  The
    # vocabulary is ontology-level (actions, verification and rollback), not a
    # project or test-case allowlist, so a paraphrased migration record still
    # qualifies while a quoted/negative mention does not.
    lifecycle_evidence_terms = (
        "执行", "完成", "复制", "重绑", "更新", "清理", "生成", "校验", "验证", "核对",
        "收尾", "结果", "风险", "待验证", "未完成", "阻塞", "保留", "差异", "脚本", "备份",
        "可读", "可用", "回退", "回滚", "rollback", "恢复", "路径", "目录",
    )
    lifecycle_evidence_hits = [term for term in lifecycle_evidence_terms if term in candidate]
    lifecycle_evidence_required = bool(required and ("迁移" in asked or "回退" in asked or "任务状态" in asked))
    lifecycle_evidence_match = (not lifecycle_evidence_required) or bool(lifecycle_evidence_hits)
    # If the only overlap is a quoted UI/prompt fragment, a negation/meta
    # marker exposes that it is not a task result.  A record with independent
    # lifecycle evidence remains valid even when it discusses a failure or
    # correction using words such as “不是/未”。
    quoted_or_meta_markers = (
        "不以", "预览内容", "最后一条用户消息", "仅提到", "只是提到", "不是当前任务",
        "无关", "不相关",
    )
    quoted_or_meta = any(marker in candidate for marker in quoted_or_meta_markers)
    candidate_handoff_evidence = lifecycle_evidence_match and not (quoted_or_meta and len(lifecycle_evidence_hits) < 2)
    passes = (not required) or (
        lifecycle_match
        and (not requested_entities or bool(entity_hits))
        and (not artifact_required or artifact_match)
        and artifact_scope_match
        and candidate_handoff_evidence
    )
    return {
        "required": required,
        "asked_families": asked,
        "matched_families": matched,
        "requested_entities": requested_entities,
        "entity_hits": entity_hits,
        "lifecycle_match": lifecycle_match,
        "artifact_required": artifact_required,
        "artifact_match": artifact_match,
        "requested_artifact_terms": requested_artifact_terms,
        "artifact_scope_hits": artifact_scope_hits,
        "artifact_scope_required": artifact_scope_required,
        "artifact_scope_match": artifact_scope_match,
        "lifecycle_evidence_required": lifecycle_evidence_required,
        "lifecycle_evidence_hits": lifecycle_evidence_hits,
        "lifecycle_evidence_match": lifecycle_evidence_match,
        "quoted_or_meta": quoted_or_meta,
        "candidate_handoff_evidence": candidate_handoff_evidence,
        "passes": passes,
    }


def continuation_policy_alignment(query: str, text: str) -> dict[str, Any]:
    """Align long-context continuation guidance without requiring a project name.

    A policy question about ``继续/续办`` is a semantic family in its own
    right.  Its best evidence is often a durable method record (for example,
    ``Full Prompt 定范围、活动任务证据定状态、远距离上下文补意图``), which
    does not repeat a concrete file or product from the current task.  The
    ordinary one-anchor gate consequently discarded exactly the records that
    explain the requested method.  This gate remains conjunctive: the query
    must ask about continuation and context recovery, while the candidate must
    carry continuation, context/boundary, and method evidence.  It never grants
    admission to a row merely because it mentions ``上下文`` or ``Full Prompt``.
    """
    q = compact(query)
    candidate = compact(text)
    continuation_terms = (
        "长任务", "续办", "续写", "继续", "不要停", "接着", "短续问",
    )
    context_terms = (
        "上下文", "fullprompt", "活动任务", "任务证据", "任务锚点",
        "远距离", "最近三轮", "尾部窗口", "增量上下文",
    )
    boundary_terms = (
        "普通续写", "同一任务", "误判", "承接", "连续", "任务边界",
        "任务身份", "范围",
    )
    method_terms = (
        "结合", "分层", "读取", "检索", "判断", "避免", "统一",
        "并行", "索引", "规划", "重排", "驱动", "定范围", "定状态",
    )

    def hit_groups(value: str) -> dict[str, list[str]]:
        return {
            "continuation": [term for term in continuation_terms if term in value],
            "context": [term for term in context_terms if term in value],
            "boundary": [term for term in boundary_terms if term in value],
            "method": [term for term in method_terms if term in value],
        }

    q_hits = hit_groups(q)
    candidate_hits = hit_groups(candidate)
    requested = bool(
        len(q_hits["continuation"]) >= 2
        and q_hits["context"]
        and (q_hits["boundary"] or q_hits["method"])
    )
    # Context plus a method and a continuation/boundary signal is the minimum
    # useful conjunction.  Requiring three groups prevents a generic Full
    # Prompt architecture heading from crossing the Hook boundary.
    candidate_groups = [name for name, hits in candidate_hits.items() if hits]
    qualified = bool(
        requested
        and len(candidate_groups) >= 3
        and "context" in candidate_groups
        and "method" in candidate_groups
        and ("continuation" in candidate_groups or "boundary" in candidate_groups)
    )
    return {
        "requested": requested,
        "qualified": qualified,
        "query_hits": q_hits,
        "candidate_hits": candidate_hits,
        "candidate_groups": candidate_groups,
    }


def active_validation_alignment(query: str, text: str, memory_type: str = "") -> dict[str, Any]:
    """Keep reusable execution-chain evidence for an active validation task.

    A validation continuation often names the *task* (for example CASE-01)
    and its acceptance contract, but not every implementation node.  Requiring
    three nodes in the user sentence made the generic gate reject genuinely
    useful historical runbooks such as ``Hook/Controller/Hindsight`` or
    ``Hook/Bank/Packet/receipt``.  This bridge is intentionally narrower than a
    semantic-score rescue:

    * the query must contain at least two active-validation markers and two
      lifecycle/action markers;
    * the candidate must contain at least three delivery-chain nodes and two
      concrete verification/control markers; and
    * only procedural memory types are eligible.

    It is ontology-level evidence, not a CASE or prompt allowlist.  A generic
    runtime/configuration note that merely lists the same component names does
    not pass without evidence of testing, comparison, repair, rollback or an
    actual delivery result.
    """
    q = compact(intent_text(query))
    t = compact(text)
    active_markers = (
        "真实窗口", "真实交互", "验证任务", "执行契约", "台账", "userpromptsubmit",
        "四路核对", "bankoracle", "状态页视觉", "同题回放", "换新题", "不要停止",
        "继续执行", "不中断", "强制执行机制", "case-",
    )
    lifecycle_markers = (
        "执行", "测试", "验证", "核对", "回归", "复测", "根因", "修复",
        "修正", "回退", "回滚", "对比", "补充", "闭环", "实际注入", "回执",
        "未召回", "未投递", "筛除", "真实0", "失败", "异常",
    )
    chain_nodes = (
        "hook", "controller", "hindsight", "bank", "packet", "回执", "状态页",
        "实际链路", "userpromptsubmit", "memorypacket", "9998", "mcp",
    )
    evidence_markers = (
        "真实测试", "测试", "执行证据", "独立检索", "对比", "根因", "修复", "回放",
        "换新题", "复测", "验证", "回执", "闭环", "实际注入", "未召回",
        "被筛除", "未投递", "失败", "异常", "覆盖", "未覆盖", "诊断", "截图",
        "实际召回", "召回", "注入", "真实", "实际调用", "正确", "回退", "回滚", "验收", "链路",
    )
    strong_result_markers = {
        "真实测试", "测试", "执行证据", "独立检索", "对比", "根因", "修复", "回放",
        "复测", "回执", "闭环", "实际注入", "未召回", "未投递", "失败",
        "异常", "未覆盖", "诊断", "截图", "实际召回", "正确", "回退", "回滚", "验收",
    }
    q_active = [marker for marker in active_markers if marker in q]
    q_lifecycle = [marker for marker in lifecycle_markers if marker in q]
    candidate_nodes = [marker for marker in chain_nodes if marker in t]
    candidate_evidence = [marker for marker in evidence_markers if marker in t]
    candidate_strong_results = [marker for marker in candidate_evidence if marker in strong_result_markers]
    case_tokens = list(dict.fromkeys(re.findall(r"case[-_][a-z0-9]+", q)))
    case_subject_match = bool(case_tokens and any(token in t for token in case_tokens))
    case_evidence = bool(case_subject_match and len(candidate_evidence) >= 2)
    # A concise incident/visual-evidence record may name only one node (for
    # example “真实 Hook 未覆盖” or “9998 截图”).  Keep it when it carries a
    # concrete result, but do not let a generic component/configuration note
    # through.  This is deliberately independent of any project or CASE name.
    evidence_bridge = bool(
        candidate_nodes
        and len(candidate_evidence) >= 3
        and candidate_strong_results
    )
    kind = str(memory_type or "").casefold()
    eligible_type = kind in {"experience", "world", "observation", "mental_model"}
    requested = len(q_active) >= 2 and len(q_lifecycle) >= 2
    qualified = bool(
        requested
        and eligible_type
        and (
            (len(candidate_nodes) >= 3 and len(candidate_evidence) >= 2)
            or case_evidence
            or evidence_bridge
        )
    )
    return {
        "requested": requested,
        "eligible_type": eligible_type,
        "query_active_markers": q_active[:8],
        "query_lifecycle_markers": q_lifecycle[:8],
        "candidate_chain_nodes": candidate_nodes[:10],
        "candidate_evidence_markers": candidate_evidence[:10],
        "candidate_strong_result_markers": candidate_strong_results[:10],
        "query_case_tokens": case_tokens[:6],
        "case_subject_match": case_subject_match,
        "case_evidence": case_evidence,
        "evidence_bridge": evidence_bridge,
        "qualified": qualified,
    }


def document_change_scope_alignment(query: str, text: str) -> dict[str, Any]:
    """Keep a named document change inside its requested workstream.

    A large project can contain procurement, research, Word and PPT histories.
    For a request such as "add a third role to this PPT and update every
    dependent page", a shared project name is not enough: the memory must
    also prove the document/page scope and the changed element or its
    propagation relation.  This is deliberately based on document-operation
    structure, rather than a project-name allowlist.
    """
    q = compact(intent_text(query))
    candidate = compact(text)
    document_terms = (
        "ppt", "幻灯片", "页面", "页", "word", "excel", "文档", "文件",
        "表格", "图表", "方案", "需求书", "章节", "段落",
        # For media/file workflows the artifact itself is the scope anchor;
        # a generic “文件” token is not enough to distinguish V20–V25 from
        # another project.  Treat the named artifact family as document
        # scope while still requiring an element and operation relation.
        "文件夹", "参考图", "提示词", "片段", "镜头", "关键帧", "素材",
    )
    # Keep the element/action ontology broad enough for a real document
    # repair.  The previous gate only knew about abstract items such as
    # ``模块`` and ``字段``; a perfectly scoped table restoration (表格、参数、
    # 填入、保留、回转) consequently passed the *candidate* search but was
    # never admitted.  These are document-operation families, not project or
    # filename allowlists, so the same rule generalises to Word/PPT/Excel and
    # to paraphrased repair instructions.
    changed_elements = (
        "岗位", "角色", "模块", "指标", "课程", "字段", "节点", "表格",
        "内容", "参数", "标题", "结构", "格式", "颜色", "占位", "示例",
        # Media/project artifacts use the same scoped-change contract as
        # Word/PPT/Excel work.  Without these nouns, a request to trim a
        # segment's reference images and synchronise its prompt file was
        # reduced to the generic ``角色/节点`` terms and every exact history
        # row failed the document-change gate.  These are artifact anchors,
        # not project names or fixed IDs, so the rule generalises to other
        # media/file workflows.
        "参考图", "提示词", "文件夹", "片段", "文件名", "清单", "readme",
        "镜头", "关键帧", "视频", "音频", "素材",
    )
    operations = (
        "新增", "增加", "加入", "加上", "放到", "放入", "修改", "更新", "替换",
        "删除", "删除了", "删", "删掉", "清空", "修订", "恢复", "回转", "回写",
        "填入", "保留", "清除", "清理", "还原", "没了", "核对", "检查", "保持",
    )
    propagation = (
        "同步", "联动", "相关", "全篇", "全部", "覆盖", "依赖", "逐页",
        "对应", "跨页", "关联", "匹配",
    )
    asked_documents = [term for term in document_terms if term in q]
    asked_elements = [term for term in changed_elements if term in q]
    asked_operations = [term for term in operations if term in q]
    # ``传播/逐页`` is optional: a concrete local repair (e.g. restore a
    # table, put parameters back, retain existing rows) is still a document
    # change request even when no cross-page word is present.  Conversely,
    # short words such as ``删`` and ``没了`` are included above so colloquial
    # user prompts do not fall through the generic semantic gate.
    required = bool(asked_documents and asked_elements and asked_operations)
    document_hits = [term for term in document_terms if term in candidate]
    element_hits = [term for term in changed_elements if term in candidate]
    operation_hits = [term for term in operations if term in candidate]
    propagation_hits = [term for term in propagation if term in candidate]
    generic_elements = {"内容"}
    # These elements identify the actual work object.  ``标题/结构/格式`` are
    # useful but often occur in generic writing policies; when a prompt also
    # names a table, parameter, module, field, colour or placeholder, a
    # candidate must mention at least one of those concrete anchors.
    high_specific_elements = {
        "表格", "参数", "占位", "示例", "颜色", "模块", "岗位", "角色",
        "指标", "课程", "字段", "节点", "参考图", "提示词", "文件夹", "片段",
        "文件名", "清单", "readme", "镜头", "关键帧", "视频", "音频", "素材",
    }
    generic_documents = {"页", "文件", "文档", "方案"}
    query_specific_elements = set(asked_elements) - generic_elements
    candidate_specific_elements = set(element_hits) - generic_elements
    element_overlap = sorted(query_specific_elements & candidate_specific_elements)
    document_overlap = sorted(set(asked_documents) & set(document_hits))
    high_specific_query_elements = sorted(query_specific_elements & high_specific_elements)
    high_specific_element_overlap = sorted(
        set(high_specific_query_elements) & candidate_specific_elements
    )
    specific_document_overlap = sorted(set(document_overlap) - generic_documents)

    # Normalize paraphrased edit verbs into a small operation ontology.  A
    # prompt saying "删了" and a memory saying "恢复/保留" are related repair
    # evidence; unrelated generation or formatting records are not.
    operation_families = {
        "add": {"新增", "增加", "加入", "加上", "放到", "放入", "回写", "填入"},
        "edit": {"修改", "更新", "替换", "修订"},
        "delete": {"删除", "删除了", "删", "删掉", "清空", "清除", "清理", "没了"},
        "restore": {"恢复", "回转", "还原", "保留", "保持"},
        "verify": {"核对", "检查"},
    }
    query_operation_families = {
        family for family, terms in operation_families.items()
        if any(term in q for term in terms)
    }
    candidate_operation_families = {
        family for family, terms in operation_families.items()
        if any(term in candidate for term in terms)
    }
    operation_family_overlap = sorted(query_operation_families & candidate_operation_families)
    repair_bridge = bool(
        {"add", "delete", "edit"} & query_operation_families
        and {"restore", "add", "delete", "edit"} & candidate_operation_families
    )
    operation_relation = bool(operation_family_overlap or repair_bridge)
    propagation_requested = bool(any(term in q for term in propagation))
    # A candidate may repeat the requested nouns only to explicitly deny that
    # they were part of the work (for example, "网络拓扑已恢复，未涉及需求书
    # 表格").  Treat that as a scope-negative, not as document evidence.  The
    # window is intentionally local and tied to a document/element term; a
    # generic "没有问题" elsewhere must not veto an otherwise valid record.
    scope_terms = tuple(dict.fromkeys(document_terms + changed_elements))
    negative_scope_markers = [
        marker for marker in ("未涉及", "不涉及", "未包含", "不包含", "不含", "不属于", "无关")
        if re.search(
            rf"{re.escape(marker)}.{{0,24}}(?:{'|'.join(re.escape(term) for term in scope_terms)})",
            candidate,
        )
    ]
    # A summary may call a slide a "page" or "section".  Exact document-scope
    # overlap plus a specific element and operation relation prevents a generic
    # project rule (for example a global word-count policy) from entering a
    # table repair merely because it says "内容" and "保留".
    passes = (not required) or bool(
        (specific_document_overlap or (document_overlap and not set(asked_documents) - generic_documents))
        and (element_overlap or not query_specific_elements)
        and (not high_specific_query_elements or high_specific_element_overlap)
        and operation_relation
        and (not propagation_requested or bool(propagation_hits))
        and not negative_scope_markers
    )
    return {
        "required": required,
        "asked_documents": asked_documents,
        "asked_elements": asked_elements,
        "asked_operations": asked_operations,
        "document_hits": document_hits,
        "element_hits": element_hits,
        "operation_hits": operation_hits,
        "propagation_hits": propagation_hits,
        "document_overlap": document_overlap,
        "specific_document_overlap": specific_document_overlap,
        "query_specific_elements": sorted(query_specific_elements),
        "candidate_specific_elements": sorted(candidate_specific_elements),
        "element_overlap": element_overlap,
        "high_specific_query_elements": high_specific_query_elements,
        "high_specific_element_overlap": high_specific_element_overlap,
        "query_operation_families": sorted(query_operation_families),
        "candidate_operation_families": sorted(candidate_operation_families),
        "operation_family_overlap": operation_family_overlap,
        "repair_bridge": repair_bridge,
        "operation_relation": operation_relation,
        "propagation_requested": propagation_requested,
        "negative_scope_markers": negative_scope_markers,
        "passes": passes,
    }


_CONCRETE_ARTIFACT_EXTENSIONS = (
    "zip", "app", "dmg", "pkg", "tar", "gz", "tgz", "7z", "rar",
    "exe", "msi", "deb", "rpm", "whl", "docx", "doc", "pptx", "ppt",
    "xlsx", "xls", "pdf", "py", "sh", "mjs", "js", "json",
)
_GENERIC_ARTIFACT_STEMS = {
    "file", "files", "archive", "package", "pkg", "app", "download",
    "document", "doc", "copy", "backup", "source", "release", "latest",
}
_ARTIFACT_SCOPE_MARKERS = (
    "文件", "文件夹", "目录", "路径", "工件", "安装", "解压", "用途", "适用",
    "结构", "损坏", "源码", "应用", "安装包", "下载", "拖进", "打开", "版本",
)


def concrete_artifact_scope_alignment(query: str, text: str) -> dict[str, Any]:
    """Keep a query about one named package/file from admitting another one.

    Candidate generation quite reasonably treats ``zip``/``app`` as useful
    lexical hints.  At the Hook boundary those extensions are far too broad:
    an IINA source archive, a Windows OpenSSH archive and an old Hindsight
    backup are different artifacts.  This gate activates only when the query
    contains a non-generic ASCII filename with a package/document extension
    *and* an artifact-scope marker.  It then requires the candidate to repeat
    the same filename or a sufficiently distinctive stem.  The rule is
    ontology-level and deliberately has no project/file allow-list; ordinary
    file and document workflows without a concrete identifier remain on their
    existing semantic/document lanes.
    """
    q = compact(intent_text(query))
    candidate = compact(text)
    extension_pattern = r"(?:" + "|".join(_CONCRETE_ARTIFACT_EXTENSIONS) + r")"
    token_pattern = rf"(?<![a-z0-9])([a-z0-9][a-z0-9_.-]{{2,}}\.{extension_pattern})(?![a-z0-9])"

    def extract(value: str) -> tuple[list[str], list[str]]:
        identifiers: list[str] = []
        stems: list[str] = []
        for match in re.finditer(token_pattern, value, re.I):
            token = match.group(1).rstrip(".").casefold()
            stem = token.rsplit(".", 1)[0]
            # ``.dmg``/``.pkg`` and generic ``file.zip`` are format hints, not
            # a concrete scope anchor.  Retain names such as ``iina`` and
            # ``OpenSSH-Win64`` even when the query also mentions a generic
            # extension in another clause.
            if stem in _GENERIC_ARTIFACT_STEMS or len(re.sub(r"[^a-z0-9]", "", stem)) < 4:
                continue
            identifiers.append(token)
            stems.append(stem)
        return sorted(set(identifiers)), sorted(set(stems))

    query_identifiers, query_stems = extract(q)
    candidate_identifiers, candidate_stems = extract(candidate)
    candidate_normalized = re.sub(r"[^a-z0-9]", "", candidate)
    stem_hits: list[str] = []
    for stem in query_stems:
        normalized_stem = re.sub(r"[^a-z0-9]", "", stem)
        if len(normalized_stem) >= 4 and normalized_stem in candidate_normalized:
            stem_hits.append(stem)
    identifier_hits = [identifier for identifier in query_identifiers if identifier in candidate]
    requested = bool(query_identifiers and any(marker in q for marker in _ARTIFACT_SCOPE_MARKERS))
    qualified = bool(not requested or identifier_hits or stem_hits)
    return {
        "requested": requested,
        "query_identifiers": query_identifiers,
        "query_stems": query_stems,
        "candidate_identifiers": candidate_identifiers,
        "candidate_stems": candidate_stems,
        "identifier_hits": identifier_hits,
        "stem_hits": sorted(set(stem_hits)),
        "qualified": qualified,
    }


_PATH_SCOPE_GENERIC_COMPONENTS = {
    "users", "user", "apple", "home", "documents", "downloads", "desktop",
    "projects", "project", "codex", "tmp", "var", "private", "work", "ag",
    "outputs", "output", "files", "file", "data", "src", "dist", "main",
}
_PATH_SCOPE_OPERATION_MARKERS = (
    "执行", "处理", "剪辑", "合成", "拼接", "制作", "安装", "解压", "用途", "放这里",
    "放到", "恢复", "修改", "更新", "替换", "检查", "核对", "文件夹", "目录", "文件",
)
_PATH_SCOPE_FAMILIES = {
    "media": ("视频", "视频文件", "mp4", "mov", "mkv", "片段", "素材", "镜头"),
    "postprocess": ("剪辑", "合成", "拼接", "片头", "片尾", "字幕", "输出", "流水线", "制作"),
    "audio": ("背景音乐", "bgm", "音效", "配音", "音轨", "混音"),
    "sequence": ("v01", "v25", "25段", "顺序", "首尾帧", "按序"),
    "document": ("文档", "需求书", "方案", "模板", "表格", "ppt", "word", "章节"),
}


def _path_scope_components(value: str) -> list[str]:
    """Extract non-generic components from absolute/local paths."""
    raw = str(value or "")
    matches = re.findall(
        r"(?:/(?:Users|Volumes|tmp|var|private|opt|etc)/[^\s`'\"，。；;!?！？]+|~/[^\s`'\"，。；;!?！？]+)",
        raw,
        re.I,
    )
    components: list[str] = []
    for path in matches:
        path = path.rstrip(".,;:!?！？)]}>")
        for component in path.split("/"):
            component = component.strip("`'\"()[]{}")
            if not component:
                continue
            stem = component.rsplit(".", 1)[0] if "." in component else component
            normalized = stem.casefold()
            alnum = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", normalized)
            if normalized in _PATH_SCOPE_GENERIC_COMPONENTS or alnum in _PATH_SCOPE_GENERIC_COMPONENTS:
                continue
            if not alnum or alnum.isdigit() or len(alnum) < 4:
                continue
            # Hex ids and timestamps are execution metadata, not task scope.
            if re.fullmatch(r"[0-9a-f]{12,}", alnum) or re.fullmatch(r"20\d{6,}", alnum):
                continue
            components.append(normalized)
    return sorted(set(components))


def _path_scope_anchor_hit(anchor: str, candidate: str) -> bool:
    normalized_anchor = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", anchor.casefold())
    normalized_candidate = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", candidate.casefold())
    if not normalized_anchor or normalized_anchor not in normalized_candidate:
        # Chinese folder names are often shortened in a memory summary (for
        # example ``优优汽车队`` → ``优优车队``).  Two meaningful shared bigrams
        # preserve that scope without maintaining a project-specific alias.
        if not re.search(r"[\u4e00-\u9fff]", anchor) or len(normalized_anchor) < 4:
            return False
        bigrams = {
            normalized_anchor[index:index + 2]
            for index in range(len(normalized_anchor) - 1)
            if re.search(r"[\u4e00-\u9fff]", normalized_anchor[index:index + 2])
        }
        return len([part for part in bigrams if part in normalized_candidate]) >= 2
    return True


def path_artifact_scope_alignment(query: str, text: str) -> dict[str, Any]:
    """Keep absolute-path work attached to its named task/artifact scope.

    A path such as ``.../优优汽车队`` is more specific than the parent
    ``Codex`` directory.  Without this guard, broad semantic rescue admitted
    unrelated configuration, game and storage memories into a video-editing
    packet merely because all lived under Codex.  The fallback is intentionally
    family-based: if a concise candidate omits the path it must still prove
    several independent operation families from the same prompt (media,
    post-processing, audio or sequence).  It is not a folder/project allowlist.
    """
    q = compact(intent_text(query))
    candidate = compact(text)
    requested_components = _path_scope_components(query)
    if not requested_components:
        return {
            "requested": False,
            "query_components": [],
            "candidate_components": [],
            "component_hits": [],
            "query_scope_families": [],
            "candidate_scope_families": [],
            "shared_scope_families": [],
            "required_scope_families": 0,
            "qualified": True,
        }
    query_scope_families = [
        family for family, markers in _PATH_SCOPE_FAMILIES.items()
        if any(marker in q for marker in markers)
    ]
    candidate_scope_families = [
        family for family, markers in _PATH_SCOPE_FAMILIES.items()
        if any(marker in candidate for marker in markers)
    ]
    component_hits = [
        component for component in requested_components
        if _path_scope_anchor_hit(component, candidate)
    ]
    # A path on its own can be a citation or a source note.  Activate the
    # scope gate only for an actual artifact operation, keeping ordinary
    # architecture/provenance questions on their existing lanes.
    requested = bool(
        any(marker in q for marker in _PATH_SCOPE_OPERATION_MARKERS)
        and query_scope_families
    )
    required_scope_families = (
        max(2, min(3, len(query_scope_families))) if query_scope_families else 0
    )
    shared_scope_families = sorted(set(query_scope_families) & set(candidate_scope_families))
    qualified = bool(
        not requested
        or component_hits
        or len(shared_scope_families) >= required_scope_families
    )
    return {
        "requested": requested,
        "query_components": requested_components,
        "candidate_components": _path_scope_components(text),
        "component_hits": component_hits,
        "query_scope_families": query_scope_families,
        "candidate_scope_families": candidate_scope_families,
        "shared_scope_families": shared_scope_families,
        "required_scope_families": required_scope_families,
        "qualified": qualified,
    }


# A concrete UI/window operation can carry a large project background in its
# Full Prompt (for example, a browser-close correction made while a proposal
# project is active). Product names such as ``Chrome`` or ``Codex`` also occur
# in network/traffic observations. Those observations are not evidence for a
# window operation unless the query explicitly asks about network behaviour.
# Keep this lane ontology-based: it reasons over UI targets/actions and
# network-domain markers, never over one project, host or application name.
_UI_OPERATION_TARGET_MARKERS = (
    "浏览器", "窗口", "标签页", "页面", "右下角", "chrome", "safari", "edge", "tab",
)
_UI_OPERATION_ACTION_MARKERS = (
    "关闭", "打开", "点击", "切换", "移动", "拖动", "保留", "不要影响", "恢复",
)
_UI_OPERATION_ACTION_FAMILIES = {
    "close": {"关闭"},
    "open": {"打开"},
    "select": {"点击", "切换"},
    "move": {"移动", "拖动"},
    "preserve": {"保留", "不要影响"},
    "restore": {"恢复"},
}
_NETWORK_DOMAIN_MARKERS = (
    "vpn", "代理", "流量", "带宽", "网络", "上传下载", "下载上传", "机场",
    "海外访问", "远程桌面", "连接", "http_proxy", "https_proxy", "all_proxy",
    "高消耗", "耗流量", "网络分流", "网速", "代理配置",
)
_NETWORK_OPERATION_MARKERS = (
    "连接", "分流", "代理", "访问", "流量", "带宽", "上传", "下载", "监控", "消耗",
)


def current_operation_scope_alignment(query: str, text: str) -> dict[str, Any]:
    """Keep a concrete UI operation separate from incidental network context.

    A Full Prompt is intentionally richer than the last user sentence, so it
    may mention both a browser action and the active project's other systems.
    The normal semantic/prequalified rescue can then admit a network memory
    merely because it repeats ``Chrome`` or ``Codex``. Activate only for a
    concrete UI target plus an action, and reject a candidate that proves a
    network/traffic proposition but no UI action. If the query itself asks
    about network behaviour, this scope guard is inactive so the two domains
    can legitimately coexist.
    """
    q = compact(intent_text(query))
    candidate = compact(text)
    query_targets = sorted({marker for marker in _UI_OPERATION_TARGET_MARKERS if marker in q})
    query_actions = sorted({marker for marker in _UI_OPERATION_ACTION_MARKERS if marker in q})
    query_network = sorted({marker for marker in _NETWORK_DOMAIN_MARKERS if marker in q})
    candidate_targets = sorted({marker for marker in _UI_OPERATION_TARGET_MARKERS if marker in candidate})
    candidate_actions = sorted({marker for marker in _UI_OPERATION_ACTION_MARKERS if marker in candidate})
    candidate_network = sorted({marker for marker in _NETWORK_DOMAIN_MARKERS if marker in candidate})
    candidate_network_actions = sorted({marker for marker in _NETWORK_OPERATION_MARKERS if marker in candidate})
    query_action_families = sorted(
        family for family, markers in _UI_OPERATION_ACTION_FAMILIES.items()
        if any(marker in q for marker in markers)
    )
    candidate_action_families = sorted(
        family for family, markers in _UI_OPERATION_ACTION_FAMILIES.items()
        if any(marker in candidate for marker in markers)
    )
    action_overlap = sorted(set(query_action_families) & set(candidate_action_families))
    requested = bool(len(query_targets) >= 2 and query_actions and not query_network)
    candidate_network_proposition = bool(len(candidate_network) >= 2 and candidate_network_actions)
    # For a concrete operation, a candidate must prove the same UI target and
    # at least one action family (closing is not interchangeable with merely
    # opening a browser). This is intentionally stricter than “mentions
    # Chrome”: otherwise global writing/procurement memories in the Full Prompt
    # can be admitted by the generic semantic rescue branch.
    candidate_ui_action = bool(candidate_targets and candidate_actions and action_overlap)
    mismatch = bool(requested and not candidate_ui_action)
    return {
        "requested": requested,
        "query_ui_targets": query_targets,
        "query_ui_actions": query_actions,
        "query_ui_action_families": query_action_families,
        "query_network_markers": query_network,
        "candidate_ui_targets": candidate_targets,
        "candidate_ui_actions": candidate_actions,
        "candidate_ui_action_families": candidate_action_families,
        "ui_action_overlap": action_overlap,
        "candidate_network_markers": candidate_network,
        "candidate_network_actions": candidate_network_actions,
        "candidate_network_proposition": candidate_network_proposition,
        "candidate_ui_action": candidate_ui_action,
        "mismatch": mismatch,
        "qualified": not mismatch,
    }


# Procurement memories are often concise, durable selection rules. A generic
# lexical gate used to keep the exact rule out because it shared only common
# words such as “招标文件”. This lane requires independent source, selection,
# service, time/amount or quantity facets, so it widens recall for a real rule
# without admitting every procurement template or equipment purchase note.
_PROCUREMENT_FAMILIES = {
    "source_document": (
        "招标", "采购", "中标", "成交", "公告", "附件", "采购文件", "招标文件",
    ),
    "selection_rule": (
        "找", "查", "下载", "整理", "筛选", "排除", "限定", "记住", "规则", "必须",
        "仅限", "不要", "回溯", "而非", "不找",
    ),
    "service_scope": (
        "服务类", "服务标", "开发", "建设", "升级", "运维", "租赁", "货物",
    ),
    "time_scope": (
        "近一年", "近两年", "近三年", "最近", "年份", "时间", "日期",
    ),
    "amount_scope": (
        "金额", "万元", "万以下", "预算", "不超过", "以内", "上限",
    ),
    "quantity_scope": (
        "目标", "数量", "个", "份", "条", "批", "十个", "10个", "五个", "5个",
    ),
}
_PROCUREMENT_RESULT_MARKERS = (
    "规定", "规则", "必须", "仅限", "排除", "限定", "范围", "目标", "符合",
    "回溯", "附件", "金额", "万元", "近三年", "服务类", "开发", "建设", "运维", "租赁",
)


def procurement_scope_alignment(query: str, text: str) -> dict[str, Any]:
    """Admit a procurement selection rule when independent facets agree."""
    q = compact(intent_text(query))
    candidate = compact(text)

    def family_hits(value: str) -> dict[str, list[str]]:
        return {
            family: [marker for marker in markers if marker in value]
            for family, markers in _PROCUREMENT_FAMILIES.items()
        }

    query_hits = family_hits(q)
    candidate_hits = family_hits(candidate)
    query_families = sorted(family for family, hits in query_hits.items() if hits)
    candidate_families = sorted(family for family, hits in candidate_hits.items() if hits)
    shared_families = sorted(set(query_families) & set(candidate_families))
    query_topic = bool(query_hits["source_document"])
    # A topic plus an explicit lookup/selection operation and one additional
    # scope facet is enough to activate. “招标文件是什么” remains a normal
    # semantic question; “找/筛选近三年服务标” enters this dedicated lane.
    query_action = bool(query_hits["selection_rule"])
    requested = bool(query_topic and query_action and len(query_families) >= 2)
    candidate_result_hits = sorted({marker for marker in _PROCUREMENT_RESULT_MARKERS if marker in candidate})
    # Source + rule is the minimum durable proposition. A concrete service,
    # time, amount or quantity facet must also overlap when the query asks for
    # one; this prevents a generic “招标规则” record from entering a bounded
    # service/amount search.
    scope_families = {"service_scope", "time_scope", "amount_scope", "quantity_scope"}
    requested_scopes = scope_families & set(query_families)
    shared_scope_families = sorted(scope_families & set(shared_families))
    qualified = bool(
        not requested
        or (
            "source_document" in shared_families
            and "selection_rule" in shared_families
            and bool(candidate_result_hits)
            and (not requested_scopes or bool(requested_scopes & set(shared_scope_families)))
        )
    )
    return {
        "requested": requested,
        "query_family_hits": query_hits,
        "candidate_family_hits": candidate_hits,
        "query_families": query_families,
        "candidate_families": candidate_families,
        "shared_families": shared_families,
        "requested_scope_families": sorted(requested_scopes),
        "shared_scope_families": shared_scope_families,
        "candidate_result_markers": candidate_result_hits,
        "qualified": qualified,
    }


def _provenance_units(text: str, max_chars: int = 720) -> list[str]:
    """Split a raw archive into auditable, locally coherent evidence windows."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    base = []
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            continue
        # A single imported chat line can contain several long utterances.
        sentences = [x.strip() for x in re.split(r"(?<=[。！？!?；;])\s*", line) if x.strip()]
        base.extend(sentences or [line])
    units: list[str] = []
    for part in base or [normalized.strip()]:
        if len(part) <= max_chars:
            units.append(part)
            continue
        step = max_chars - 120
        units.extend(part[start:start + max_chars] for start in range(0, len(part), step))
    # Join adjacent short sentences so two clauses from the same utterance can
    # jointly prove a proposition without joining unrelated messages far apart.
    joined = list(units)
    for index in range(len(units) - 1):
        candidate = units[index] + " " + units[index + 1]
        if len(candidate) <= max_chars:
            joined.append(candidate)
    return joined[:2000]


def _literal_proposition_hits(proposition: str, text: str) -> list[str]:
    """Find non-overlapping literal pieces of the concrete proposition."""
    q = compact(proposition)
    t = compact(text)
    if not q or not t:
        return []
    excluded = set(WEAK_TERMS) | set(ENTITY_ONLY_TERMS) | {
        "飞书里", "千问里", "豆包里", "用户原话", "找原话", "时间来源", "原话时间",
    }
    matches: list[tuple[int, int, str]] = []
    for block in SequenceMatcher(None, q, t, autojunk=False).get_matching_blocks():
        if block.size < 4:
            continue
        value = q[block.a:block.a + block.size]
        if value in excluded or any(value == compact(x) for x in excluded):
            continue
        # Generic action scaffolding is not a proposition anchor.
        if all(token in value for token in ("不能", "替代")) and len(value) < 6:
            continue
        q_start, q_end = block.a, block.a + block.size
        if any(not (q_end <= a or q_start >= b) for a, b, _ in matches):
            continue
        matches.append((q_start, q_end, value[:48]))
    return [value for _, _, value in sorted(matches)[:12]]


def provenance_excerpt(query: str, text: str) -> tuple[str, list[str]]:
    """Return the best local evidence window and its concrete claim anchors."""
    proposition = provenance_proposition(query)
    best_text, best_hits = "", []
    for unit in _provenance_units(text):
        hits = _literal_proposition_hits(proposition, unit)
        rank = (len(hits), max((len(x) for x in hits), default=0), sum(map(len, hits)))
        best_rank = (len(best_hits), max((len(x) for x in best_hits), default=0), sum(map(len, best_hits)))
        if rank > best_rank:
            best_text, best_hits = unit, hits
    return best_text.strip(), best_hits


def source_class(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") or {}
    haystack = compact(" ".join(str(x or "") for x in (
        metadata.get("source"), metadata.get("bank_id"), metadata.get("source_bank"),
        item.get("document_id"), item.get("source"),
    )))
    direct = bool(metadata.get("_ccy_direct_evidence") or item.get("_ccy_direct_evidence"))
    # Versioned local authority/behavior contracts may be grounded in a direct
    # user rule, but they are not raw chat archives. Classify the explicit
    # trusted source first; otherwise `_ccy_direct_evidence=True` would make a
    # current rule disappear unless the user also asked for provenance.
    if any(compact(marker) in haystack for marker in TRUSTED_SOURCE_MARKERS):
        return "trusted_rule"
    if direct or any(compact(marker) in haystack for marker in RAW_SOURCE_MARKERS):
        return "raw_evidence"
    return "structured_memory"


def _independent_hits(query: str, text: str) -> tuple[list[str], list[str]]:
    q = compact(intent_text(query))
    t = compact(text)
    candidates: list[str] = []
    concept_literal_spans: set[str] = set()
    weak_hits = [term for term in WEAK_TERMS if term in q and term in t]

    for term in STRONG_TERMS:
        value = compact(term)
        if value and value in q and value in t:
            candidates.append(value)
    for label, aliases in CONCEPT_GROUPS.items():
        if label == "医疗决策" and not _medical_domain_signal(q):
            # Do not let an operational phrase such as ``服务健康`` activate
            # the medical paraphrase family merely because the candidate also
            # contains the generic word ``健康``.
            continue
        q_aliases = [compact(x) for x in aliases if compact(x) in q]
        t_aliases = [compact(x) for x in aliases if compact(x) in t]
        if q_aliases and t_aliases:
            # A paraphrase-family hit is one independent signal, not a bonus
            # vote on top of the same literal phrase.  For example 工作背景
            # must never become both 工作背景 and 概念:工作版图.
            shared_literal = set(q_aliases) & set(t_aliases)
            if shared_literal:
                concept_literal_spans.update(shared_literal)
                candidates = [x for x in candidates if x not in shared_literal]
            candidates.append("概念:" + label)
    for term in re.findall(r"[a-z][a-z0-9_.-]{2,}", q):
        # Protocol/format words are candidate-generation plumbing, not topic
        # identity.  A proposal that cites ``https://...`` must not admit a
        # proxy-env or generic test row just because both contain ``http`` or
        # ``https``; concrete domains and product identifiers remain eligible.
        if term not in WEAK_TERMS and term not in _GENERIC_SUBJECT_LATIN and term in t:
            candidates.append(term)

    # Longest exact common blocks provide a language-independent lexical
    # fallback without counting overlapping CJK trigrams as separate evidence.
    matcher = SequenceMatcher(None, q, t, autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size < 4:
            continue
        value = q[block.a:block.a + block.size]
        if (
            not value
            or value in WEAK_TERMS
            or value in _GENERIC_SUBJECT_LATIN
            or value in _GENERIC_SUBJECT_ANCHORS
            or all(x in WEAK_TERMS for x in (value,))
        ):
            continue
        if any(weak in value and len(value) <= len(weak) + 1 for weak in WEAK_TERMS):
            continue
        candidates.append(value[:24])

    if concept_literal_spans:
        candidates = [
            value for value in candidates
            if value.startswith("概念:") or value not in concept_literal_spans
        ]

    # Keep the longest non-overlapping spans in the candidate text.  Thus
    # 工作背景 is one anchor, never 工作背 + 作背景 + 工作背景 three votes.
    chosen: list[tuple[int, int, str]] = []
    for value in sorted(set(candidates), key=lambda x: (-len(x), x)):
        # Concept-family hits are already independent semantic anchors and do
        # not need a literal span in the candidate.
        if value.startswith("概念:"):
            chosen.append((-len(chosen)-1, -len(chosen), value))
            continue
        start = t.find(value)
        if start < 0:
            continue
        end = start + len(value)
        if any(not (end <= a or start >= b) for a, b, _ in chosen):
            continue
        chosen.append((start, end, value))
    chosen.sort()
    return [value for _, _, value in chosen[:12]], sorted(set(weak_hits))[:12]


def _semantic_score(item: dict[str, Any]) -> float | None:
    metadata = item.get("metadata") or {}
    admission = metadata.get("_ccy_admission") or {}
    values = (
        metadata.get("semantic_relevance_score"), admission.get("semantic_relevance_score"),
        item.get("semantic_relevance_score"),
    )
    for value in values:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _meaningful_current_anchor(value: str) -> bool:
    """Reject grammatical/generic overlap before it can authorize injection.

    A longest-common-substring fallback is useful for Chinese paraphrases, but
    it must never promote fragments such as ``的Bug`` or ``有问题`` into the
    same class as a named subject like ``报价``.  Concrete two-character domain
    terms remain valid because they are handled by ``STRONG_TERMS`` before
    this guard.
    """
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", compact(value))
    if not normalized:
        return False
    generic_fragment = re.fullmatch(
        r"(?:的|了|是|有|这|那|一个|当前|实际|还是|就是)*(?:bug|错误|问题|测试|修复|失败)?",
        normalized,
        re.I,
    )
    return not bool(generic_fragment)


def recurrence_prevention_alignment(query: str, text: str) -> dict[str, Any]:
    """Recognize a terse request to stop a *named* workflow failing again.

    Chinese task subjects are often only two characters (for example ``周报``).
    Treating every two-character overlap as an anchor would over-admit noise,
    while requiring a four-character common span loses the actual failure and
    repair history.  This is therefore a three-part proposition gate:

    1. the user explicitly asks to prevent a repeat failure;
    2. the candidate records a failure mode or a preventative control; and
    3. both name the same non-scaffolding subject span (two characters is OK).

    It is not an item-count exception and it does not accept a generic schedule
    or a different workflow merely because both mention "avoid" or "report".
    """
    q = compact(intent_text(query))
    t = compact(text)
    prevention_requested = bool(re.search(
        r"(?:预防|防止|避免|杜绝|防范).{0,8}(?:再|重复|再次|复发|出问题|故障|失败)"
        r"|(?:不再|别再).{0,8}(?:出问题|故障|失败|复发)"
        r"|再出问题",
        q,
    ))
    control_or_failure = bool(re.search(
        r"(?:故障|失效|异常|失败|挂起|重试|复发|根因|闸门|修复|修正|校验|验收|回退)",
        t,
    ))
    scaffold = {
        "想办法", "办法预", "预防再", "防再出", "再出问", "出问题", "问题啊",
        "怎么做", "如何做", "避免再", "防止再", "不再出", "再出现", "问题",
        # Failure/control words describe the relation, never the workflow
        # subject. Otherwise “周报防止再次失败” incorrectly admits a 晨报
        # repair merely because both strings contain “失败”.
        "失败", "故障", "异常", "修复", "重试", "校验", "验收", "回退", "防错",
        # These name the requested relation/control, not the business subject.
        # Otherwise “周报…复用故障预防机制” can match a different workflow
        # solely on “机制”.
        "机制", "系统", "流程", "历史", "原因", "结果", "措施", "方案", "方法", "复用",
        "故障原因", "验收机制", "修复机制", "防复发", "预防机制", "重试机制",
    }
    subjects: list[str] = []
    if prevention_requested and control_or_failure:
        matcher = SequenceMatcher(None, q, t, autojunk=False)
        for block in matcher.get_matching_blocks():
            if block.size < 2:
                continue
            value = q[block.a:block.a + block.size]
            if value in scaffold or any(part in value for part in ("预防", "办法", "问题", "再出", "避免", "防止")):
                continue
            if value not in subjects:
                subjects.append(value[:24])
    return {
        "requested": prevention_requested,
        "candidate_has_control_or_failure": control_or_failure,
        "subject_hits": subjects[:4],
        "qualified": bool(prevention_requested and control_or_failure and subjects),
    }


def proposition_alignment(query: str, text: str, query_concepts: set[str], candidate_concepts: set[str], strong_hits: list[str]) -> dict[str, Any]:
    """Explain whether a candidate proves the requested *relation*, not only a noun.

    A single project/entity match is deliberately insufficient.  For structural
    questions the candidate must also describe propagation, coverage or a
    dependent component.  This repairs the old v4 hard veto without turning a
    high semantic score into a universal admission pass.
    """
    normalized = compact(text)
    structural = sorted(query_concepts & {"关联修改", "正式交付质量", "当前路由", "记忆层级证据", "界面导航", "时序冲突裁决"})
    relation_hits: dict[str, list[str]] = {}
    for label in structural:
        hits = [pattern for pattern in CONCEPT_PATTERNS.get(label, ()) if re.search(pattern, normalized, re.I | re.S)]
        if hits:
            relation_hits[label] = hits
    subject_hits = [
        value for value in strong_hits
        if not value.startswith("概念:") and value not in ENTITY_ONLY_TERMS and len(value) >= 4
    ]
    shared_concepts = sorted(query_concepts & candidate_concepts)
    # Two shared scoped system/document terms (for example Word + PPT) are a
    # subject signal; one generic product name is not.  This keeps a broad
    # Office question from admitting unrelated memory-maintenance records.
    scope_hits = [value for value in ENTITY_ONLY_TERMS if value in compact(intent_text(query)) and value in normalized]
    relation_support = bool(relation_hits or shared_concepts)
    subject_support = bool(subject_hits) or len(scope_hits) >= 2
    # A reusable supersession/temporal-governance model often has no named
    # product or project: its subject is the state/claim relation itself.
    # Once the contextual relation regex has proved concrete conditions (for
    # example evidence time + validity scope, or historical retention + an
    # explicit replacement state), do not require an unrelated entity anchor.
    # The regex gate above keeps a bare database ``superseded`` field out.
    if "时序冲突裁决" in relation_hits:
        temporal_subject_markers = (
            "同一主体", "同一对象", "属性", "范围相同", "范围重叠", "有效时间",
            "生效时间", "证据谱系", "证据范围", "适用边界", "版本链", "历史经历",
            "保留", "追加", "取代旧结论", "更新或取代", "后一次成功不删除",
            "新证据", "判断顺序", "去重", "并列保留", "状态台账", "生命周期状态机",
        )
        if sum(marker in normalized for marker in temporal_subject_markers) >= 2:
            subject_support = True
    operation_support = bool(re.search(
        r"(?:新增|增加|更新|修改|替换|删除|修订|校核|核对|检查|验收|渲染|复核|对比).{0,18}(?:岗位|角色|模块|指标|课程|字段|节点|页面|内容|文档|幻灯片|ppt|word|excel|方案)"
        r"|(?:岗位|角色|模块|指标|课程|字段|节点|页面|内容|文档|幻灯片|ppt|word|excel|方案).{0,18}(?:新增|增加|更新|修改|替换|删除|修订|校核|核对|检查|验收|渲染|复核|对比)"
        r"|(?:同步|联动|纳入|进入|覆盖).{0,28}(?:岗位|角色|模块|指标|课程|字段|节点|页面|内容|文档|幻灯片|ppt|word|excel|方案)",
        normalized,
        re.I | re.S,
    ))
    # A non-structural question may be matched by its concept family alone;
    # structural questions must prove both subject and relation whenever a
    # distinctive subject is available.
    required_relation = bool(structural)
    return {
        "subject_support": subject_support,
        "subject_hits": subject_hits[:8],
        "scope_hits": sorted(scope_hits),
        "relation_support": relation_support,
        "operation_support": operation_support,
        "relation_labels": sorted(relation_hits),
        "matched_concepts": shared_concepts,
        "required_relation": required_relation,
        "coverage_facets": sorted(set(structural or shared_concepts)),
    }


_REAL_DIALOGUE_CHANNEL_TERMS = (
    "真实对话", "真实交互", "真实回复", "真实输入", "实际输入", "可见输入",
    "对话框", "对话窗口", "窗口中", "窗口里", "用户发送", "用户输入",
    "输入内容", "对话记录", "跨对话", "另一个对话", "用户问题", "语义查询",
    "userpromptsubmit", "真实hook", "真实触发路径", "交互",
)
_REAL_DIALOGUE_RESULT_TERMS = (
    "测试", "验证", "回放", "触发", "真实", "实际", "回复", "回答",
    "未覆盖", "不真实", "失败", "根因", "问题根源", "结果",
)


def recent_activity_alignment(query: str, text: str) -> dict[str, Any]:
    """Admit a concrete recent-entry inventory record.

    A request such as “查看最近十几条我主动正常的条目” is neither small
    talk nor an open-ended semantic question: it asks for a bounded inventory
    of recent user-visible entries.  The ordinary topic gate has no stable
    subject token for this wording and used to reject the exact Bank rows.
    Keep the lane structural and conservative: the query needs a recency cue,
    an entry/count cue and an inspection action; a candidate needs the same
    entry/recency/result evidence.  No date, project or fixed count is baked
    into the rule.
    """
    q = compact(intent_text(query))
    t = compact(text)
    recency_terms = ("最近", "近期", "今天", "昨日", "昨天", "本周", "本月", "when:")
    entry_terms = ("条目", "记录", "回合", "对话", "prompt", "问题")
    inspect_terms = ("查看", "查找", "检查", "审查", "看看", "有没有问题", "是否有问题", "正常")
    query_recency = [term for term in recency_terms if term in q]
    query_entry = [term for term in entry_terms if term in q]
    query_inspect = [term for term in inspect_terms if term in q]
    requested = bool(query_recency and query_entry and query_inspect)
    candidate_recency = [term for term in recency_terms if term in t]
    candidate_entry = [term for term in entry_terms if term in t]
    candidate_inspect = [term for term in inspect_terms if term in t]
    # Preserve qualifiers that make an inventory request concrete.  A broad
    # recent-status row (for example "用户反馈服务正常") is not evidence for
    # the user's request to inspect *their actively created normal entries*.
    # Match only qualifiers that the query actually asserted; this keeps the
    # lane general for other bounded inventories without baking in a project,
    # date, or fixed count.
    qualifier_pairs = (
        ("主动", "主动"),
        ("正常", "正常"),
        ("用户", "用户"),
        ("问题", "问题"),
    )
    qualifier_mismatch = [
        query_term for query_term, candidate_term in qualifier_pairs
        if query_term in q and candidate_term not in t
    ]
    candidate_negative = bool(re.search(
        r"(?:没有|未有|无|未涉及|不含|不包含).{0,10}(?:条目|记录|回合|对话|prompt|问题)",
        t,
    ))
    # Require all three evidence families.  A memory that merely says
    # “recently updated” or “check the problem” without describing an entry
    # remains outside this lane and can use the normal semantic gate.
    qualified = bool(
        requested
        and candidate_recency
        and candidate_entry
        and candidate_inspect
        and not qualifier_mismatch
        and not candidate_negative
    )
    return {
        "requested": requested,
        "query_recency_markers": query_recency,
        "query_entry_markers": query_entry,
        "query_inspection_markers": query_inspect,
        "candidate_recency_markers": candidate_recency,
        "candidate_entry_markers": candidate_entry,
        "candidate_inspection_markers": candidate_inspect,
        "candidate_qualifier_mismatch": qualifier_mismatch,
        "candidate_negative_scope": candidate_negative,
        "qualified": qualified,
    }


def real_dialogue_alignment(query: str, text: str) -> dict[str, Any]:
    """Recognize evidence from an actual visible conversation test.

    The production Hook may receive a long Full Prompt that also asks for a
    status/Packet audit.  The audit relation is not an exclusive topic gate:
    a memory explaining the *real user-visible transport* is independently
    useful.  Admission still requires a conjunction on both sides: the query
    must ask for a real dialogue/input test, and the candidate must preserve
    at least two channel markers plus two test/result markers.  Computer Use,
    a product name, or a bare ``Hook`` therefore cannot pass by itself.
    """
    q = compact(intent_text(query))
    t = compact(text)
    query_channel = [term for term in _REAL_DIALOGUE_CHANNEL_TERMS if compact(term) in q]
    query_result = [term for term in _REAL_DIALOGUE_RESULT_TERMS if compact(term) in q]
    explicit_real = any(term in q for term in ("真实", "实际", "可见", "窗口", "对话框", "交互"))
    requested = bool(query_channel and query_result and explicit_real)

    candidate_channel = [term for term in _REAL_DIALOGUE_CHANNEL_TERMS if compact(term) in t]
    candidate_result = [term for term in _REAL_DIALOGUE_RESULT_TERMS if compact(term) in t]
    # ``Hook``/``语义查询`` are transport markers only when the candidate also
    # says that the path was real or triggered.  This avoids admitting generic
    # adapter plumbing that happens to mention a Hook.
    transport_support = bool(
        any(term in t for term in ("真实hook", "真实触发路径", "userpromptsubmit"))
        and any(term in t for term in ("测试", "触发", "未覆盖", "不真实", "实际"))
    )
    if transport_support:
        candidate_channel = list(dict.fromkeys(candidate_channel + ["真实 Hook 传输"])).copy()
    channel_count = len(set(candidate_channel))
    result_count = len(set(candidate_result))
    # At least one query/candidate marker must be from each semantic family;
    # this is a category conjunction, not an item-count exception.
    qualified = bool(
        requested
        and channel_count >= 2
        and result_count >= 2
        and bool(candidate_channel)
        and bool(candidate_result)
    )
    return {
        "requested": requested,
        "query_channel_markers": query_channel,
        "query_result_markers": query_result,
        "candidate_channel_markers": list(dict.fromkeys(candidate_channel)),
        "candidate_result_markers": list(dict.fromkeys(candidate_result)),
        "qualified": qualified,
    }


_SUBJECT_INFRASTRUCTURE = {
    "memory", "system", "agent", "codex", "openclaw", "hermes", "trainer",
    "hindsight", "agentmemory", "hook", "adapter", "controller", "bank",
    "packet", "memorypacket", "full", "prompt", "状态页", "链路", "回执", "注入", "检索",
    "测试", "验证", "当前", "实际", "真实", "用户", "助手", "对话",
    "过程", "内容", "事实", "关键", "几个", "这些", "它们", "全部",
    "computeruse",
}
_SUBJECT_STOP = set(WEAK_TERMS) | _SUBJECT_INFRASTRUCTURE | {
    "先", "再", "并", "及", "以及", "包括", "是否", "进入", "生成", "形成",
    "处理", "核对", "可见", "正确", "现状", "区别", "差异", "官方", "历史",
    "开发", "流程", "数据", "接入", "说明", "需要", "进行", "方式", "这种",
    "让我", "告诉", "已经", "刚才", "现在", "之前", "后续", "其中", "一个",
    "架构", "状态", "可见", "通道", "回复", "输入", "方式", "验证", "检查",
    "回退", "复测", "复核", "异常", "最短", "完整", "方案", "规则", "任务",
    "开始", "进行", "执行", "说明", "给出", "要做", "真实", "输入", "验收",
    "窗口", "窗口中", "窗口里", "对话框", "交互", "用户问题", "语义查询",
    "对话记录", "另一个", "方法", "这种", "后续", "不用", "容来", "来测",
    "式测", "种方", "入内", "方架", "发流", "史数", "据接", "否进", "页可",
}

# Span splitting must not turn generic grammar into a named subject.  Before
# this boundary existed, ``人工智能实训平台`` produced ``人工`` and a URL
# produced ``http``/``https``; either could make an unrelated proxy, package
# or delivery record look like evidence for a platform proposal.  These are
# ontology-level generic nouns and protocol/format tokens, not project names.
_GENERIC_SUBJECT_ANCHORS = {
    "人工", "智能", "模型", "数据", "软件", "硬件", "平台", "项目", "方案", "系统",
    "设备", "视频", "音频", "字幕", "音乐", "文件", "文件夹", "目录", "路径", "素材",
    "内容", "资料", "材料", "通知", "附件", "申报", "实训", "课程", "学校", "学院",
    "公司", "应用", "网页", "网站", "链接", "页面", "图片", "参考", "结构", "版本",
    "配置", "环境", "服务", "端口", "进程", "日志", "测试", "检查", "结果", "过程",
    "方向", "研究", "论文", "方法", "工具", "功能", "操作", "任务", "工作", "主题",
    "上面", "下面", "里面", "后面", "前面", "其他", "一些", "东西", "人家", "这里",
    "现在", "刚才", "主要", "相关", "完整", "最终", "部分", "这个", "那个", "那些",
    "http", "https", "www", "com", "cn", "org", "net", "zip", "json", "toml", "md",
}
_GENERIC_SUBJECT_LATIN = {
    "http", "https", "www", "com", "cn", "org", "net", "zip", "json", "toml", "md",
    "doc", "docx", "ppt", "pptx", "xls", "xlsx", "file", "files", "config", "proxy",
    "default", "latest", "source", "release", "package", "archive",
}


def _subject_anchor_is_robust(anchor: str) -> bool:
    """Return whether an extracted anchor carries identity signal."""
    value = compact(anchor)
    if not value or value in _GENERIC_SUBJECT_ANCHORS or value in _GENERIC_SUBJECT_LATIN:
        return False
    if re.fullmatch(r"v?\d+(?:\.\d+)*", value):
        return False
    if re.fullmatch(r"[a-z]{1,3}\d*", value) and value not in {"icu", "wps", "vpn"}:
        return False
    if re.search(r"[\u4e00-\u9fff]", value):
        # Two-character aliases remain useful when they occur as complete
        # spans (小黛、豆包); the extractor below never emits arbitrary
        # two-character windows from a longer sentence.
        return len(value) >= 2
    return len(re.sub(r"[^a-z0-9]", "", value)) >= 4


def _explicit_subject_anchors(value: str) -> list[str]:
    """Extract reusable named anchors without hard-coding project names.

    Latin identifiers are stable entity aliases (PCB, Doubao, v2, ...).
    Chinese spans keep explicit short aliases and longer noun chunks, rather
    than every two-character window, so generic grammar cannot become an
    anchor.
    """
    raw = str(value or "")
    normalized = compact(intent_text(raw))
    anchors: list[str] = []
    for token in re.findall(r"[a-z][a-z0-9_.-]{2,}|v\d+", normalized):
        if (
            token not in _SUBJECT_INFRASTRUCTURE
            and token not in _SUBJECT_STOP
            and _subject_anchor_is_robust(token)
        ):
            anchors.append(token)
            # A version suffix is not a different entity for exact candidate
            # matching: “Hindsight v2” should still match a proposition that
            # says “Hindsight” and then supplies the version in its text.
            if token.startswith("hindsight"):
                anchors.append("hindsight")
    for span in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        # Keep an explicit two-character span (for example ``小黛`` or
        # ``豆包``), and keep longer noun spans intact after splitting on
        # grammatical characters.  Enumerating every two-character window
        # turns ``文件夹`` into the false anchor ``件夹`` and ``人工智能`` into
        # the generic anchor ``人工``.
        if len(span) == 2:
            if span not in _SUBJECT_STOP and _subject_anchor_is_robust(span):
                anchors.append(span)
            continue
        function_chars = "的了是有这那其和与及并在为将被把从到对等先再还它我他她中里要实际后续不用途来式测种方入内框话"
        parts = [part for part in re.split(f"[{re.escape(function_chars)}]+", span) if part]
        for part in parts:
            if part in _SUBJECT_STOP or not _subject_anchor_is_robust(part):
                continue
            anchors.append(part)
    # Deduplicate overlapping fragments while retaining short aliases.  The
    # query may say “豆包历史数据” whereas a stored proposition only says
    # “豆包”; dropping the two-character alias would create a false miss.
    unique = sorted(set(anchors), key=lambda x: (-len(x), x))
    chosen: list[str] = []
    for anchor in unique:
        if any(anchor in other or other in anchor for other in chosen):
            if len(anchor) != 2:
                continue
            # Keep a named two-character alias even when a longer phrase
            # subsumes it; generic two-character grammar has already been
            # removed by the stopword/function-character checks above.
            if anchor not in chosen:
                chosen.append(anchor)
            continue
        chosen.append(anchor)
    if any(anchor.startswith("hindsight") for anchor in anchors) and "hindsight" not in chosen:
        chosen.append("hindsight")
    return chosen[:24]


_SUBJECT_PROPOSITION_MARKERS = (
    "架构", "机制", "职责", "策略", "流程", "开发", "模型", "版本", "召回",
    "注入", "写入", "接入", "同步", "数据", "历史", "官方", "差异", "区别",
    "现状", "错误", "真实链路", "触发", "回执", "retain", "stop", "api",
)


def _subject_is_infrastructure(anchor: str) -> bool:
    value = compact(anchor)
    return value in _SUBJECT_INFRASTRUCTURE or value.startswith("hindsight") or value.startswith("agentmemory")


def subject_evidence_alignment(query: str, text: str) -> dict[str, Any]:
    """Admit explicit subject propositions inside a multi-intent audit.

    A Full Prompt may ask both ``what facts are relevant`` and ``whether the
    Packet receipt is complete``.  The delivery-audit predicate must not erase
    the first part.  This lane requires a named subject anchor plus at least
    two proposition markers in the candidate.  A pure chain audit with only
    Hindsight/Hook/Bank nouns is intentionally left to the stricter procedure
    gate.
    """
    q = compact(intent_text(query))
    t = compact(text)
    all_query = _explicit_subject_anchors(query)
    non_infra_query = [
        x for x in all_query
        if not _subject_is_infrastructure(x) and _subject_anchor_is_robust(x)
    ]
    shared = [
        anchor for anchor in all_query
        if _subject_anchor_is_robust(anchor) and anchor in t
    ]
    propositions = [marker for marker in _SUBJECT_PROPOSITION_MARKERS if marker in t]
    # A multi-intent Full Prompt can contain a broad subject clause and a
    # concrete cross-task handoff clause.  The subject lane must not bypass the
    # handoff scope guard: otherwise a generic policy that happens to mention
    # “文件” plus “机制/模型” can enter before the stricter continuity branch.
    handoff = continuity_task_alignment(query, text)
    requested = bool(
        all_query
        and any(marker in q for marker in ("现状", "区别", "差异", "历史", "流程", "接入", "关键事实", "包括"))
        and any(marker in q for marker in ("验证", "核对", "检查", "进入", "生成", "形成", "注入", "召回"))
    )
    # A query that asks for a named non-infrastructure subject can use that
    # subject's evidence even when its Full Prompt also carries a chain audit.
    # Hindsight-only chain prompts remain governed by procedure_evidence.
    qualified = bool(
        requested
        and shared
        and len(propositions) >= 2
        and (non_infra_query or "hindsight" not in shared)
        and (not handoff.get("required") or handoff.get("passes"))
    )
    return {
        "requested": requested,
        "query_subject_anchors": all_query,
        "non_infrastructure_subjects": non_infra_query,
        "candidate_subject_hits": shared,
        "candidate_proposition_markers": propositions,
        "continuity_task_alignment": handoff,
        "qualified": qualified,
    }


def is_association_closure_query(query: str) -> bool:
    """Detect a conceptual graph/constellation-closure question.

    ``Packet 注入`` and ``为什么`` also occur in concrete delivery audits,
    so they cannot be used as a proxy for this lane.  We require an explicit
    closure/lexical/generic-node signal, or two independent association
    families, plus a method question.  A question such as ``图谱为什么没有
    注入`` therefore remains an audit, while ``词面相似为什么不够、还需要
    哪些关联闭包`` enters the broader association lane.
    """
    q = compact(intent_text(query))
    method_markers = (
        "为什么", "为何", "不够", "还需要", "需要哪些", "哪些关联", "如何", "怎样",
        "避免", "检索", "召回", "组合", "过滤", "闭合", "运作",
    )
    explicit_markers = (
        "关联闭包", "关系闭包", "关联簇", "图谱和星座", "图谱星座",
        "词面相似", "泛节点", "泛化节点", "genericnode", "lexicalsimilarity",
        "associationclosure", "relationclosure", "claimbundle",
    )
    association_markers = (
        "图谱", "星座", "关联", "关系边", "关系", "实体", "别名",
        "上下游", "因果", "graph", "constellation", "association", "relation",
    )
    bridge_markers = (
        "claimbundle", "命题覆盖", "命题", "来源", "时间", "版本", "冲突",
        "证据", "可解释", "requiredslot", "packet", "上下游", "因果",
    )
    return bool(
        any(marker in q for marker in method_markers)
        and (
            any(marker in q for marker in explicit_markers)
            or (
                sum(marker in q for marker in association_markers) >= 2
                and any(marker in q for marker in bridge_markers)
            )
        )
    )


def association_closure_alignment(query: str, text: str) -> dict[str, Any]:
    """Align a candidate with graph/constellation closure semantics.

    The vector store can find one shared noun, but a closure answer needs at
    least two independent evidence families: a structural relation family and
    either coverage/evidence or anti-generic admission evidence.  This keeps
    high-frequency graph nodes and bare component lists out while allowing
    entity aliases, time/source, Claim Bundles, conflict edges and thresholds
    to arrive as separate complementary records.
    """
    q = compact(intent_text(query))
    t = compact(text)
    requested = is_association_closure_query(query)
    structural_markers = (
        "图谱", "星座", "关联闭包", "关系闭包", "关联簇", "关系边", "实体",
        "别名", "上下游", "因果", "归并", "graph", "constellation", "association",
        "relation",
    )
    evidence_markers = (
        "claimbundle", "命题", "覆盖", "来源", "时间", "版本", "冲突", "证据",
        "可解释", "requiredslot", "当前", "历史", "因果", "上下游", "路径",
    )
    anti_generic_markers = (
        "泛节点", "泛化节点", "高频", "度数", "词面", "相似", "锚点", "关系边",
        "阈值", "准入", "拒绝", "不直接", "不能因为", "停止", "top-k", "负空间",
    )
    method_markers = (
        "检索", "召回", "组合", "闭包", "关联", "过滤", "覆盖", "合并", "归并",
        "注入", "路径", "阈值", "准入", "解释",
    )
    structural = sorted({marker for marker in structural_markers if marker in t})
    evidence = sorted({marker for marker in evidence_markers if marker in t})
    anti_generic = sorted({marker for marker in anti_generic_markers if marker in t})
    methods = sorted({marker for marker in method_markers if marker in t})
    families = [
        name for name, hits in (
            ("structural_relation", structural),
            ("coverage_evidence", evidence),
            ("anti_generic_admission", anti_generic),
        ) if hits
    ]
    # Keep the concrete delivery-audit lane available for mixed questions that
    # explicitly ask why a real Packet was empty.  Pure conceptual questions
    # should not let that narrow gate veto association evidence.
    concrete_delivery_markers = (
        "实际注入", "注入为0", "注入是0", "候选很多但实际", "投递回执", "注入回执",
        "状态页", "未投递", "未注入", "对账", "正常吗", "链路断", "链路缺",
    )
    mixed_delivery_audit = any(marker in q for marker in concrete_delivery_markers)
    qualified = bool(
        requested
        and len(families) >= 2
        and structural
        and (evidence or anti_generic)
        and methods
    )
    return {
        "requested": requested,
        "structural_markers": structural,
        "coverage_evidence_markers": evidence,
        "anti_generic_markers": anti_generic,
        "method_markers": methods,
        "evidence_families": families,
        "mixed_delivery_audit": mixed_delivery_audit,
        "qualified": qualified,
    }


# A graph/constellation question can name a concrete entity and ask whether
# two aliases refer to the same work item.  The corresponding Bank facts are
# often deliberately concise (for example, one sentence recording a move,
# backup or rollback) and therefore do not repeat words such as ``图谱`` or
# ``命题覆盖``.  Requiring those structural words here made the graph route
# find the facts and then discard them at admission.  This detector keeps the
# widening generic: it needs a named anchor from the question, a concrete
# action/result relation, and an action or scope term shared with the request.
# It does not contain a project allow-list, and it never admits a bare graph
# node or a row that only repeats a product name.
_ENTITY_FACT_INFRASTRUCTURE = {
    "hindsight", "agentmemory", "agentmemoryos", "codex", "openclaw", "hermes",
    "trainer", "hook", "mcp", "controller", "bank", "packet", "memorypacket",
}
_ENTITY_FACT_ACTION_TERMS = (
    "迁移", "迁入", "迁出", "转移", "移动", "复制", "重绑", "备份", "回退", "回滚",
    "恢复", "切换", "配置", "路径", "目录", "文件夹", "项目", "客户", "主体", "任务",
    "状态", "版本", "来源", "范围", "边界", "别名", "简称", "全称", "归一化", "核验",
    "验证", "完成", "未完成", "阻塞", "保留", "差异", "可读", "可用", "失败", "风险",
)
_ENTITY_FACT_RESULT_TERMS = (
    "执行", "完成", "失败", "保留", "回退", "回滚", "恢复", "复制", "迁移", "重绑",
    "备份", "核验", "验证", "阻塞", "差异", "路径", "目录", "文件夹", "状态", "结果",
    "可读", "可用", "风险", "待处理", "未完成",
)

# A concise question such as “Trainer 怎么回事、刚才回复有问题” is neither
# a graph/constellation design question nor a named migration/alias question.
# It asks for the historical incident evidence of a named service.  The old
# gate had no lane for this shape, so Bank returned the right Trainer failure
# records but the Hook reduced them to a semantic near-miss and injected zero
# (or an unrelated legacy candidate).  Keep this lane deliberately
# proposition-based: a named endpoint plus an explicit incident predicate in
# the query, and the same endpoint plus both an incident and an outcome/state
# marker in the candidate.  It does not use a project allow-list or a fixed
# count, and legacy ``candidate:`` wrappers are still rejected earlier.
_NAMED_ISSUE_QUERY_MARKERS = (
    "怎么回事", "有问题", "出问题", "不回复", "没回复", "不响应", "无响应",
    "卡住", "卡顿", "出错", "故障", "异常", "失败", "不正常", "原因", "排查",
)
_NAMED_ISSUE_CANDIDATE_MARKERS = (
    "问题", "故障", "异常", "失败", "错误", "bug", "出错", "卡住", "卡顿",
    "不回复", "没回复", "不响应", "无响应", "未保存", "无法", "丢失", "警告",
    "session start", "failed", "回复", "回答", "复习", "归档",
)
_NAMED_ISSUE_OUTCOME_MARKERS = (
    "原因", "导致", "根因", "修复", "恢复", "解决", "状态", "结果", "当前",
    "不再", "仍", "无法", "未保存", "丢失", "失败", "错误", "卡住", "回复",
    "回答", "检查", "验证", "独立服务", "路由",
)


def named_service_issue_alignment(query: str, text: str) -> dict[str, Any]:
    """Admit historical incident evidence for a named service/entity.

    This is intentionally narrower than “entity name + semantic similarity”.
    It is activated only by an explicit incident predicate and requires the
    candidate to state both the incident and an outcome/state.  Thus a generic
    Trainer architecture row or a random project note cannot enter merely
    because it mentions ``trainer``; concise canonical failure/repair records
    can enter even when they omit graph vocabulary.
    """
    q = compact(intent_text(query))
    t = compact(text)
    query_entities = [entity for entity in ENTITY_ONLY_TERMS if entity in q]
    query_incident_markers = [marker for marker in _NAMED_ISSUE_QUERY_MARKERS if marker in q]
    # “为什么 + Trainer + 回复/异常” is an incident request even when the
    # colloquial phrase does not contain the exact “怎么回事” family.
    explicit_incident = bool(query_incident_markers) or (
        "为什么" in q and any(marker in q for marker in ("回复", "回答", "状态", "问题", "异常", "失败"))
    )
    shared_entities = [entity for entity in query_entities if entity in t]
    candidate_incident_markers = [marker for marker in _NAMED_ISSUE_CANDIDATE_MARKERS if marker in t]
    candidate_outcome_markers = [marker for marker in _NAMED_ISSUE_OUTCOME_MARKERS if marker in t]
    # Require at least one concrete incident marker and one independent
    # outcome/state marker.  A single token such as “回复” or “状态” alone is
    # not enough; overlapping markers count once per family.
    qualified = bool(
        shared_entities
        and explicit_incident
        and candidate_incident_markers
        and candidate_outcome_markers
        and len(set(candidate_incident_markers) & {"问题", "故障", "异常", "失败", "错误", "bug", "出错", "卡住", "卡顿", "不回复", "没回复", "不响应", "无响应", "未保存", "无法", "丢失", "警告", "session start", "failed"}) >= 1
    )
    return {
        "requested": bool(query_entities and explicit_incident),
        "query_entities": query_entities,
        "query_incident_markers": query_incident_markers,
        "shared_entities": shared_entities,
        "candidate_incident_markers": candidate_incident_markers,
        "candidate_outcome_markers": candidate_outcome_markers,
        "qualified": qualified,
    }


_EXPLICIT_PREFERENCE_MARKERS = (
    "不要", "不用", "不擅长", "不希望", "偏好", "倾向", "尽量避免", "不以",
    "优先", "更适合", "不想",
)
_PREFERENCE_ANCHORS = (
    "cnn", "论文", "半冷门", "冷门", "实现路径", "落地应用", "智能体", "模型",
    "方向", "算法", "工具", "方案", "硬件", "编程", "技术路线", "研究主线",
)


def explicit_preference_alignment(query: str, text: str, memory_type: str = "") -> dict[str, Any]:
    """Admit a durable preference that directly constrains the current task.

    Stable preferences are answer evidence when the user states a concrete
    choice/boundary (for example, not centering CNN and preferring less common
    research directions).  The generic semantic gate used to reject these
    rows because ``cnn`` was the only STRONG lexical anchor.  This lane is
    intentionally conjunctive: explicit preference wording in the query,
    an actionable research/selection context, the same non-generic anchor in
    the candidate, and a preference assertion in the candidate.  It does not
    grant a topic or project allow-list and does not admit a generic model
    description without the preference relation.
    """
    q = compact(intent_text(query))
    t = compact(text)
    query_markers = [marker for marker in _EXPLICIT_PREFERENCE_MARKERS if marker in q]
    candidate_markers = [marker for marker in _EXPLICIT_PREFERENCE_MARKERS if marker in t]
    q_anchors = [anchor for anchor in _PREFERENCE_ANCHORS if anchor in q]
    shared_anchors = [anchor for anchor in q_anchors if anchor in t]
    # Research/deployment prompts are multi-intent even when the user only
    # writes one explicit boundary (for example “不用特意写非CNN”): the same
    # ``智能体`` token appears in many unrelated writing, learning and
    # orchestration memories.  Require two independent preference anchors for
    # that family.  This is an ontology-level guard (topic + requested
    # direction/evidence), not a project-name allow-list, so an exact CNN/
    # paper preference and a last-mile/agent preference both survive while a
    # generic agent workflow does not.
    research_scoped = bool(
        any(marker in q for marker in ("cnn", "论文", "半冷门", "冷门", "研究", "方向", "落地", "智能体"))
        and any(marker in q for marker in ("找", "检索", "研究", "论文", "方向", "路径", "落地", "分析", "推荐"))
    )
    minimum_shared_anchors = 2 if research_scoped else 1
    action_requested = any(marker in q for marker in (
        "找", "检索", "研究", "方向", "论文", "路径", "落地", "分析", "推荐", "选择", "写",
    ))
    # A stored preference can be phrased as “要求…”, “应重点…” or “建议…”
    # without repeating the exact negative particle from the user's sentence.
    candidate_assertion = bool(candidate_markers or any(marker in t for marker in (
        "要求", "偏好", "应重点", "重点寻找", "优先", "建议", "不以", "避免",
    )))
    kind = str(memory_type or "").casefold()
    eligible_type = kind in {"world", "experience", "observation", "mental_model"}
    requested = bool(query_markers and q_anchors and action_requested)
    qualified = bool(
        requested
        and eligible_type
        and candidate_assertion
        and len(shared_anchors) >= minimum_shared_anchors
    )
    return {
        "requested": requested,
        "eligible_type": eligible_type,
        "query_preference_markers": query_markers[:8],
        "candidate_preference_markers": candidate_markers[:8],
        "query_anchors": q_anchors[:12],
        "shared_anchors": shared_anchors[:12],
        "research_scoped": research_scoped,
        "minimum_shared_anchors": minimum_shared_anchors,
        "candidate_assertion": candidate_assertion,
        "qualified": qualified,
    }


def named_entity_fact_alignment(query: str, text: str) -> dict[str, Any]:
    """Recognise concise facts for a named alias/entity association query.

    Association closure is still governed by the graph/constellation lane,
    but concrete facts should not be forced to carry graph vocabulary in every
    summary.  A fact qualifies only when the request itself is a closure
    question, at least one non-infrastructure named anchor is shared, and the
    candidate states both a requested action/scope and a concrete result.  The
    quoted-span extraction tolerates Chinese labels such as ``“Codex 文件夹迁移”``
    without baking those labels into the policy.
    """
    q = compact(intent_text(query))
    t = compact(text)
    requested = is_association_closure_query(query)
    if not requested:
        return {
            "requested": False,
            "query_anchors": [],
            "shared_anchors": [],
            "query_action_terms": [],
            "candidate_action_terms": [],
            "candidate_result_terms": [],
            "qualified": False,
        }

    anchors: list[str] = []
    for token in _named_relation_tokens(q):
        if token not in _ENTITY_FACT_INFRASTRUCTURE:
            anchors.append(token)
    # Keep complete quoted labels as aliases; unlike arbitrary CJK bigrams,
    # these are explicit user-provided names and are safe to use as anchors.
    for quoted in re.findall(r"[\"“「『](.+?)[\"”」』]", str(query or "")):
        value = compact(quoted)
        if len(value) >= 3:
            anchors.append(value)
            # Also retain stable Latin portions inside a mixed label.
            anchors.extend(
                token for token in re.findall(r"[a-z][a-z0-9_.-]{2,}", value)
                if token not in _ENTITY_FACT_INFRASTRUCTURE
            )
    anchors = list(dict.fromkeys(anchors))
    shared: list[str] = []
    for anchor in anchors:
        variants = [anchor]
        # ``WPS Cloud Files`` and concise ``WPS`` summaries are the same
        # lexical endpoint.  This suffix rule is ontology-level and applies to
        # any ``<name>cloudfiles`` token, not just WPS.
        if anchor.endswith("cloudfiles") and len(anchor) > len("cloudfiles"):
            variants.append(anchor[: -len("cloudfiles")])
        positive = False
        for variant in variants:
            if not variant:
                continue
            start = 0
            while True:
                index = t.find(variant, start)
                if index < 0:
                    break
                left = t[max(0, index - 10):index]
                right = t[index + len(variant):index + len(variant) + 10]
                # A sentence can mention the requested name only to exclude
                # it ("未涉及 Alpha", "与 Beta 无关").  Such a mention must
                # not turn an unrelated row into a direct fact.  If the same
                # anchor also appears in a positive clause, keep it.
                # Restrict the negation check to the current clause so a
                # previous sentence does not poison a later positive mention,
                # while still catching ``未涉及 Alpha 或 Beta`` where the
                # second alias is joined by a coordinator.
                left_clause = re.split(r"[，,。；;：:、]", left)[-1]
                negated = bool(re.search(
                    r"(?:未涉及|不涉及|无关|不相关|没有|并非|不是|非同一|不属于|不含)",
                    left_clause,
                ) or re.search(
                    r"^(?:无关|不相关|之外|以外)", right,
                ))
                if not negated:
                    positive = True
                    break
                start = index + max(1, len(variant))
            if positive:
                break
        if positive:
            shared.append(anchor)

    query_actions = [term for term in _ENTITY_FACT_ACTION_TERMS if term in q]
    candidate_actions = [term for term in _ENTITY_FACT_ACTION_TERMS if term in t]
    candidate_results = [term for term in _ENTITY_FACT_RESULT_TERMS if term in t]
    shared_actions = [term for term in query_actions if term in t]
    non_infra_shared = [
        anchor for anchor in shared
        if anchor not in _ENTITY_FACT_INFRASTRUCTURE and len(anchor) >= 3
    ]
    qualified = bool(
        requested
        and non_infra_shared
        and shared_actions
        and candidate_results
    )
    return {
        "requested": requested,
        "query_anchors": anchors[:24],
        "shared_anchors": shared[:24],
        "non_infrastructure_shared": non_infra_shared[:24],
        "query_action_terms": query_actions,
        "shared_action_terms": shared_actions,
        "candidate_action_terms": candidate_actions,
        "candidate_result_terms": candidate_results,
        "qualified": qualified,
    }


def injection_audit_alignment(query: str, text: str) -> dict[str, Any]:
    """Require a real delivery/audit proposition for a status-injection turn.

    Status questions often contain broad type words such as “观察” and a
    number (“0 条”).  Those are candidate-generation vocabulary, not enough to
    put unrelated observation-storage or API details into the answer context.
    The gate is intentionally narrow: it activates only when the *query*
    jointly asks about injection plus a status/Packet/Hook/Full-Prompt/chain
    audit.  It has no item limit and does not suppress relevant observations;
    an observation may still pass when it states the same delivery/audit
    proposition.
    """
    q = compact(intent_text(query))
    t = compact(text)
    query_terms = {
        "状态页": "状态页" in q,
        "注入": "注入" in q,
        "packet": "memorypacket" in q or "packet" in q,
        "hook": "hook" in q,
        "回执": "回执" in q,
        "full_prompt": "fullprompt" in q,
        "链路": "链路" in q,
        "候选": "候选" in q,
    }
    # Merely listing “实际注入治理/状态页” as two stages in a historical
    # timeline is not itself a delivery audit.  The former rule activated on
    # any three co-occurring nouns, so a perfectly valid evolution question
    # was reinterpreted as a status-page audit and its stage evidence was
    # rejected with the misleading “候选/投递/实际注入” reason.  Require a
    # delivery predicate (zero-count diagnosis, candidate-vs-packet
    # reconciliation, explicit receipt/transport comparison, etc.) before
    # enabling this narrow veto.  Topic lists continue through ordinary
    # evolution/subject admission and can still admit real injection-governance
    # records when their stage proposition is independently evidenced.
    delivery_predicates = (
        "实际注入为0", "实际注入是0", "注入为0", "注入是0", "注入零",
        "实际注入数量", "注入数量", "候选很多但实际", "候选数和实际注入",
        "检索候选和实际注入", "实际注入与", "注入回执", "投递回执",
        "未投递", "未注入", "为什么注入", "为何注入", "注入正常吗",
        "注入是否", "注入链路", "链路断", "链路缺", "对账",
    )
    explicit_delivery_predicate = any(marker in q for marker in delivery_predicates)
    audit_structure = any(marker in q for marker in (
        "审计", "核对", "对账", "回执", "投递", "为什么", "为何",
        "是否一致", "链路", "问题在哪", "不应该", "正常吗", "区分",
    ))
    association_request = is_association_closure_query(query)
    concrete_delivery_markers = (
        "实际注入", "注入为0", "注入是0", "候选很多但实际", "投递回执", "注入回执",
        "状态页", "未投递", "未注入", "对账", "正常吗", "链路断", "链路缺",
    )
    mixed_delivery_audit = any(marker in q for marker in concrete_delivery_markers)
    required = bool(
        query_terms["注入"]
        and sum(query_terms.values()) >= 3
        and (explicit_delivery_predicate or audit_structure and "实际注入治理" not in q)
        and not (association_request and not mixed_delivery_audit)
    )
    candidate_terms = [label for label, present in {
        "状态页": "状态页" in t,
        "注入": "注入" in t,
        "packet": "memorypacket" in t or "packet" in t,
        "hook": "hook" in t,
        "回执": "回执" in t,
        "full_prompt": "fullprompt" in t,
        "链路": "链路" in t,
        "候选": "候选" in t,
    }.items() if present]
    # “候选” alone is merely a queue/storage word (for example a model-profile
    # processing backlog).  A status-injection audit needs a delivery/audit
    # predicate, not a coincidental mention of a candidate count.
    delivery_terms = [term for term in candidate_terms if term != "候选"]
    relation_markers = [
        marker for marker in ("分开", "分别", "区分", "区别", "未投递", "未注入", "不能称", "不要把", "混在", "每一步", "阶段")
        if marker in t
    ]
    return {
        "required": required,
        "candidate_terms": candidate_terms,
        "passes": bool(delivery_terms) if required else True,
        "relation_markers": relation_markers,
        "qualified": bool(required and len(delivery_terms) >= 2 and relation_markers),
        "association_closure_request": association_request,
        "suppressed_by_association_closure": bool(association_request and not mixed_delivery_audit),
    }


def observation_framework_alignment(query: str, text: str, item_type: str) -> dict[str, Any]:
    """Admit reusable observations/models that explain a zero-injection diagnosis.

    Delivery-audit admission is intentionally strict for ordinary records, but
    it used to run before any lane for reusable guidance.  That made a model
    explaining *why* the admission gate rejects observations impossible to
    inject unless it repeated every transport surface.  This lane is narrow:
    the query must explicitly diagnose a zero/low-injection problem and ask
    about observation/model admission, while the candidate must state a
    mechanism relation (not merely contain ``Hindsight`` or ``观察``).
    """
    q = compact(intent_text(query))
    t = compact(text)
    kind = str(item_type or "").casefold()
    zero_markers = (
        "注入0", "注入为0", "注入是0", "候选很多但实际", "一个该召回的都没有",
        "该召回的都没有", "注入都是0", "注入全是0", "记忆注入为0",
    )
    requested = bool(
        kind in {"observation", "mental_model"}
        and any(marker in q for marker in zero_markers)
        and any(marker in q for marker in ("观察", "心智模型", "框架", "准入", "门禁", "召回"))
    )
    mechanism_terms = (
        "候选", "准入", "packet", "hook", "投递", "状态页", "注入", "上下文",
        "门禁", "相关性", "过滤", "边界", "拒绝", "证明", "命中",
    )
    relation_terms = (
        "原因", "区分", "解释", "应", "不能", "不应", "优先", "通过", "避免",
        "保留", "误拒", "机制", "关系",
    )
    mechanism_hits = [term for term in mechanism_terms if term in t]
    relation_hits = [term for term in relation_terms if term in t]
    qualified = bool(requested and len(set(mechanism_hits)) >= 2 and relation_hits)
    return {
        "requested": requested,
        "mechanism_hits": mechanism_hits,
        "relation_hits": relation_hits,
        "qualified": qualified,
    }


_RELATION_TOKEN_STOP = {
    "memory", "system", "agent", "controller", "hook", "bank", "mcp", "md",
    "full", "prompt", "current", "status", "api", "http", "https",
}


def _named_relation_tokens(value: str) -> list[str]:
    tokens = [
        token for token in re.findall(r"[a-z][a-z0-9_.-]{2,}", compact(value))
        if token not in _RELATION_TOKEN_STOP
    ]
    return list(dict.fromkeys(tokens))[:12]


def system_comparison_alignment(query: str, text: str) -> dict[str, Any]:
    """Recognize a named-system role/replacement comparison without a model.

    Two shared named endpoints plus an explicit comparison relation are
    required.  A health/config record about only one system therefore cannot
    pass merely because it shares a product name.
    """
    q = compact(intent_text(query)); t = compact(text)
    # File-format words are document scope, not independent systems.  If they
    # are counted as endpoints, a normal Word/DOCX table-edit request with the
    # operation word ``替换`` is misrouted into the system-comparison lane and
    # can admit an unrelated topology/configuration memory.  Keep this
    # exclusion ontology-level; actual named systems (Codex, Hindsight,
    # Hermes, etc.) remain eligible.
    file_format_tokens = {
        "doc", "docx", "word", "ppt", "pptx", "pdf", "xls", "xlsx", "csv",
        "md", "txt", "zip", "py", "js", "mjs", "sh",
    }
    query_tokens = [
        token for token in _named_relation_tokens(q)
        if token not in file_format_tokens
    ]
    shared = [token for token in query_tokens if token in t]
    explicit_comparison = any(marker in q for marker in (
        "比较", "对比", "各自", "区别", "差异", "排行榜", "分别",
        "指什么", "是什么", "别名", "简称", "全称", "上下游",
        "不能混为一谈", "边界", "组件", "职责", "替代", "取代",
    ))
    # ``替换`` is also ordinary document-edit language.  Treat it as a
    # system comparison only when the query still names two non-file-format
    # endpoints; otherwise the document-change gate owns the decision.
    replacement_comparison = "替换" in q and len(query_tokens) >= 2
    requested = bool(len(query_tokens) >= 2 and (explicit_comparison or replacement_comparison))
    relation_markers = [
        marker for marker in (
            "角色", "主记忆", "冷档案", "替换", "排行榜", "迁移", "更适合", "定位",
            "承担", "相比", "对比", "负责", "职责", "中间层", "底座", "内核",
            "编排", "编排层", "数据面", "接入层", "入口", "数据库", "不自行",
            "不替代", "不拥有", "不是", "仅", "只作为", "分层", "输入", "输出",
            "流程", "边界", "上下游", "组件",
        )
        if marker in t
    ]
    return {
        "requested": requested,
        "query_systems": query_tokens,
        "shared_systems": shared,
        "relation_markers": relation_markers,
        "qualified": bool(requested and len(shared) >= 2 and relation_markers),
    }


_SYSTEM_NAME_ALIASES = {
    "codex": ("codex",),
    "hindsight": ("hindsight",),
    "ham-os": ("ham-os", "ham os", "hamos"),
    "agentmemoryos": ("agentmemoryos", "agent memory os", "agentmemory"),
    "querycontroller": ("querycontroller", "query controller", "controller"),
}
_SYSTEM_ROLE_MARKERS = (
    "定义", "含义", "定位", "作用", "负责", "职责", "角色", "架构", "内核",
    "上下游", "入口", "接入层", "中间层", "编排", "编排层", "底座", "存储",
    "持久", "数据流", "调用", "输入", "输出", "分层", "主记忆", "冷档案",
    # Concise architecture rows often use a noun role rather than the
    # longer "负责/职责" phrasing (for example "Hindsight 为数据库、
    # Controller 为调度器").  Keep these as structural role evidence so
    # named-system taxonomy questions do not silently lose the short but
    # decisive definition rows.
    "数据库", "模块", "调度器", "服务", "作为", "提供", "保留",
)
_SYSTEM_ALIAS_MARKERS = ("别名", "简称", "全称", "统称", "即", "不等于", "不是同一")
_SYSTEM_FLOW_MARKERS = ("→", "路由", "传递", "生成", "注入", "接入", "影子", "治理层", "实现")
_SYSTEM_BOUNDARY_MARKERS = (
    "边界", "不是同一个", "不是同一", "不等于", "不替代", "不能混", "不能混淆",
    "只作为", "不拥有", "不自动", "不应", "非同一",
)

_SYSTEM_STRONG_BOUNDARY_MARKERS = (
    "边界", "不是同一个", "不是同一", "不等于", "不替代", "不能混", "不能混淆",
    "只作为", "不拥有", "不自动", "不应", "非同一",
)


def system_taxonomy_alignment(query: str, text: str) -> dict[str, Any]:
    """Align a named-system taxonomy request with role/boundary evidence.

    The ordinary subject gate is intentionally bypassed for this request type:
    a single shared word such as ``Hindsight`` is not enough, but a record that
    names one or more requested systems and explains their role/relationship is
    useful even when the summary does not repeat every compound identifier.
    """
    q = compact(intent_text(query)); t = compact(text)
    query_tokens = _named_relation_tokens(q)
    requested = is_system_taxonomy_query(query)
    shared: list[str] = []
    for token in query_tokens:
        variants = _SYSTEM_NAME_ALIASES.get(token, (token,))
        if any(compact(variant) in t for variant in variants):
            shared.append(token)
    role_markers = [marker for marker in _SYSTEM_ROLE_MARKERS if marker in t]
    alias_markers = [marker for marker in _SYSTEM_ALIAS_MARKERS if marker in t]
    flow_markers = [marker for marker in _SYSTEM_FLOW_MARKERS if marker in t]
    boundary_markers = [marker for marker in _SYSTEM_BOUNDARY_MARKERS if marker in t]
    # Health/latency telemetry can mention a database or connection without
    # defining any component.  Do not let that operational fact pass the
    # concise-definition lane merely because it shares one named system.
    health_only = bool(
        any(marker in t for marker in ("健康", "连接正常", "延迟", "响应正常"))
        and not any(marker in t for marker in (
            "为", "是", "作为", "负责", "职责", "入口", "调度器", "架构",
            "定义", "含义", "定位", "作用", "角色", "底座", "内核", "主记忆",
            "冷档案", "模块", "服务", "分层", "数据流", "上下游", "→",
            "接入", "注入", "影子", "不替代", "不拥有",
        ))
    )
    experiment_noise = bool(
        any(marker in t for marker in ("A/B", "a/b", "测试配置", "隔离测试", "独立 bank", "独立bank", "实验"))
        and not any(marker in t for marker in (
            "定义", "含义", "定位", "作用", "负责", "职责", "架构", "上下游",
            "入口", "中间层", "编排", "底座", "内核", "主记忆", "冷档案",
            "数据流", "→", "影子", "不替代", "不拥有", "不能混",
        ))
    )
    # The old rule required two role markers for every role-only row.  That
    # dropped concise, high-value definitions such as "Hindsight 为数据库、
    # Controller 为调度器" and current-state rows that express a complete
    # flow with one arrow.  Keep the anti-noise boundary, but admit one
    # explicit structural relation when it is anchored to at least two named
    # systems; a single-system definition still needs a concrete role noun.
    strong_boundary = any(marker in t for marker in _SYSTEM_STRONG_BOUNDARY_MARKERS)
    named_relation = bool(
        len(shared) >= 2
        and (role_markers or flow_markers or boundary_markers)
        and not experiment_noise
    )
    concise_definition = bool(
        len(shared) >= 1
        and role_markers
        and not health_only
        and not experiment_noise
        and any(marker in role_markers for marker in (
            "定义", "含义", "定位", "作用", "负责", "职责", "角色", "架构",
            "入口", "中间层", "编排", "底座", "存储", "数据库", "模块",
            "调度器", "服务", "内核", "主记忆", "冷档案",
        ))
    )
    structural = bool(
        named_relation
        or concise_definition
        or alias_markers
        or strong_boundary
    )
    qualified = bool(requested and shared and structural)
    return {
        "requested": requested,
        "query_systems": query_tokens,
        "shared_systems": list(dict.fromkeys(shared)),
        "role_markers": role_markers,
        "alias_markers": alias_markers,
        "flow_markers": flow_markers,
        "boundary_markers": boundary_markers,
        "qualified": qualified,
    }


_SYSTEM_TAXONOMY_MARKERS = (
    "分别指什么", "分别是什么", "分别指的是", "各自指什么", "各自是什么",
    "别名", "简称", "全称", "上下游", "不能混为一谈", "不是同一个",
    "组件关系", "不能混淆", "边界", "职责", "承担什么角色",
    # A causal-chain audit is the same bounded system map even when the user
    # does not say “分别是什么”. Without these predicates, a prompt that
    # names Hook/Controller/Bank/Packet and asks which component causes a
    # zero-injection result is misclassified as an injection-status audit;
    # the narrow audit gate then rejects the architecture records needed to
    # explain the causal chain. The detector remains structural: the caller
    # still needs at least two explicit system identifiers below.
    "因果链", "每个组件负责", "组件负责什么", "哪个组件", "组件出问题", "组件导致",
)
_SYSTEM_TAXONOMY_STOPWORDS = {
    "agent", "memory", "os", "query", "controller", "system", "bank", "hook",
    "adapter", "full", "prompt", "status", "page", "api", "the", "and", "with",
}


def is_transfer_event_query(query: str) -> bool:
    q=compact(intent_text(query))
    return bool(re.search(r'从.+?(?:迁移|迁出|搬迁|转移|移动|复制)(?:到|至|去)',q)
                or re.search(r'from.+?(?:migrat|mov|transfer).+?to',q))


def is_system_taxonomy_query(query: str) -> bool:
    """Detect an explicit named-system definition/relationship question.

    This is deliberately structural rather than a product allow-list.  The
    user must name at least two concrete ASCII identifiers and ask for a
    definition, alias, relation, or boundary.  Such questions need the
    comparison gate; otherwise a generic subject lane lets weak bridge or
    collaboration observations enter the Packet before role evidence is
    checked.
    """
    q = compact(intent_text(query))
    if is_transfer_event_query(query):
        return False
    if not any(marker in q for marker in _SYSTEM_TAXONOMY_MARKERS):
        return False
    tokens = {
        token for token in re.findall(r"[a-z][a-z0-9_.-]{2,}", q)
        if token not in _SYSTEM_TAXONOMY_STOPWORDS
    }
    if "agentmemoryos" in q:
        tokens.add("agentmemoryos")
    if "querycontroller" in q:
        tokens.add("querycontroller")
    return len(tokens) >= 2


def shared_memory_alignment(query: str, text: str) -> dict[str, Any]:
    """Recognize multi-agent shared-memory mechanism propositions.

    This is intentionally conjunctive: the question must name at least two
    agent endpoints and cross-session/project memory; a candidate must name
    those endpoints plus a concrete shared mechanism such as the same Bank,
    Controller, Hook or shared core.  A one-agent provider fact is insufficient.
    """
    q = compact(intent_text(query)); t = compact(text)
    query_tokens = _named_relation_tokens(q)
    infrastructure = {"hindsight", "agentmemory"}
    endpoints = [token for token in query_tokens if token not in infrastructure]
    shared_endpoints = [token for token in endpoints if token in t]
    requested = bool(
        len(endpoints) >= 2
        and any(marker in q for marker in ("跨会话", "跨项目", "长期记忆", "共同", "共享"))
        and any(marker in q for marker in ("记忆", "memory", "hindsight"))
    )
    mechanism_markers = [
        marker for marker in ("共享", "共同", "同一个", "同一", "统一", "核心库", "bank", "controller", "hook", "适配器", "权威源")
        if marker in t
    ]
    memory_anchor = any(marker in t for marker in ("hindsight", "记忆", "memory", "bank"))
    return {
        "requested": requested,
        "query_endpoints": endpoints,
        "shared_endpoints": shared_endpoints,
        "mechanism_markers": mechanism_markers,
        "qualified": bool(requested and len(shared_endpoints) >= 2 and mechanism_markers and memory_anchor),
    }


def _personal_health_domain_mismatch(query: str, text: str) -> bool:
    """Distinguish a person's health decision from a CRM healthcare segment."""
    q = compact(query)
    candidate = compact(text)
    health_query = bool(
        any(marker in q for marker in ("健康", "治疗", "用药", "副作用", "诊疗", "疾病", "医疗建议"))
        and any(marker in q for marker in ("我", "我的", "建议", "应当", "如何", "风险", "时效"))
    )
    if not health_query:
        return False
    personal = ("治疗", "用药", "药物", "副作用", "疗效", "检查结果", "疾病", "健康", "诊疗", "医生", "医疗回答", "医疗建议")
    commercial = ("crm", "客户", "甲方", "市场", "行业", "对象清单", "团队", "指标", "商机", "投标", "背书")
    # A generic decision/quality model does not become medical guidance merely
    # because it is semantically adjacent; an explicit healthcare word is
    # required. A market/CRM record is excluded even if it contains 医疗.
    return not any(marker in candidate for marker in personal) or (
        any(marker in candidate for marker in commercial) and not any(marker in candidate for marker in personal)
    )


def admission_decision(
    query: str,
    item: dict[str, Any],
    *,
    deep: bool = False,
    preserve_controller_decision: bool = False,
) -> dict[str, Any]:
    from .guidance_provenance import guidance_source_decision
    source_review=guidance_source_decision(item)
    if not source_review['allowed'] and not explicit_user_source_request(query):
        return {'decision':'rejected','policy':POLICY,'reason':'指导性记忆的来源或派生链未通过核对：'+str(source_review['reason']),
                'source_class':source_class(item),'guidance_source_review':source_review,'fixed_item_limit':False,
                'hits':[],'strong_hits':[],'weak_hits':[],'matched_concepts':[],'semantic_relevance_score':None}
    text = str(item.get("text") or item.get("content") or "")
    strong_hits, weak_hits = _independent_hits(query, text)
    query_concepts = _concept_labels(intent_text(query))
    candidate_concepts = _concept_labels(text)
    matched_concepts = sorted(query_concepts & candidate_concepts)
    cls = source_class(item)
    score = _semantic_score(item)
    metadata = item.get("metadata") or {}
    previous = metadata.get("_ccy_admission") or {}
    # A cached/controller-side admission is authoritative only when it was
    # produced by this exact policy version. Otherwise a policy upgrade could
    # preserve an old false positive all the way into the Hook.
    previous_decision = str(previous.get("decision") or "")
    prequalified = (
        preserve_controller_decision
        and previous.get("policy") == POLICY
        and (
            previous_decision in {"qualified", "deferred_for_token_budget"}
            # Controller-specific qualified relations (for example a named
            # system's evolution stage) have already passed a stricter
            # structural conjunction. Do not erase them in the Hook merely
            # because their wording differs from the final user sentence.
            or previous_decision.startswith("qualified_")
        )
    )
    explicit_raw = explicit_user_source_request(query)
    operational_audit = operational_audit_query(query)
    operational_alignment = operational_audit_alignment(query, text)
    candidate_identity = str(item.get("id") or item.get("chunk_id") or "")
    legacy_candidate_identity = candidate_identity.casefold().startswith("candidate:")
    legacy_candidate_wrapper = bool(
        legacy_candidate_identity and _is_legacy_candidate_wrapper(text)
    )
    diagnostic_artifact = _is_diagnostic_artifact_candidate(item)
    named_target_missing = (
        "agentmemory" in compact(query)
        and "agentmemory" not in compact(text)
    )
    direct = bool(metadata.get("_ccy_direct_evidence") or item.get("_ccy_direct_evidence"))
    general_threshold = 0.55
    semantic_only_threshold = 0.45 if deep else 0.72
    # One lexical anchor is only a candidate-generation hint.  It cannot admit
    # a memory unless it also carries the requested proposition relation.
    one_anchor_threshold = 0.42 if deep else 0.52
    topical_hits = [
        x for x in strong_hits
        if x not in ENTITY_ONLY_TERMS
        and x != "概念:来源核对"
        and (x.startswith("概念:") or _meaningful_current_anchor(x))
    ]
    entity_hits = [x for x in strong_hits if x in ENTITY_ONLY_TERMS]
    subject_concepts = query_concepts - {"来源核对"}
    alignment = proposition_alignment(query, text, query_concepts, candidate_concepts, strong_hits)
    real_dialogue = real_dialogue_alignment(query, text)
    recent_activity = recent_activity_alignment(query, text)
    subject_evidence = subject_evidence_alignment(query, text)
    association_alignment = association_closure_alignment(query, text)
    entity_fact_alignment = named_entity_fact_alignment(query, text)
    named_issue_alignment = named_service_issue_alignment(query, text)
    preference_alignment = explicit_preference_alignment(
        query, text, str(item.get("type") or item.get("memory_type") or "")
    )
    audit_alignment = injection_audit_alignment(query, text)
    observation_framework = observation_framework_alignment(
        query, text, str(item.get("type") or item.get("memory_type") or "")
    )
    comparison_alignment = system_comparison_alignment(query, text)
    system_taxonomy_request = is_system_taxonomy_query(query)
    taxonomy_alignment = system_taxonomy_alignment(query, text)
    shared_alignment = shared_memory_alignment(query, text)
    recurrence_alignment = recurrence_prevention_alignment(query, text)
    handoff_alignment = continuity_task_alignment(query, text)
    continuation_alignment = continuation_policy_alignment(query, text)
    document_change_alignment = document_change_scope_alignment(query, text)
    artifact_scope_alignment = concrete_artifact_scope_alignment(query, text)
    path_scope_alignment = path_artifact_scope_alignment(query, text)
    current_operation_alignment = current_operation_scope_alignment(query, text)
    procurement_alignment = procurement_scope_alignment(query, text)
    provenance_alignment = provenance_structured_alignment(query, text)
    # A synthesis prompt may mention Hindsight/Hook/Controller only to say
    # that the answer must go beyond a component list.  The high semantic
    # score of a generic architecture summary must not let it cross the Hook
    # boundary in that case.  Keep this predicate proposition-based: a row
    # that also carries evidence, scope, validity, or the observation/model
    # distinction remains eligible.
    component_exclusion_request = any(marker in compact(query) for marker in (
        "不要只罗列", "不要只列", "不能只列", "不要只讲组件", "不只是组件",
    ))
    component_terms = ("hindsight", "hook", "controller", "bank", "packet", "状态页", "组件")
    component_only_candidate = (
        component_exclusion_request
        and any(marker in compact(text).casefold() for marker in component_terms)
        and not any(marker in compact(text) for marker in (
            "观察", "心智模型", "证据", "标准", "偏好", "有效性", "实际注入", "回答使用",
            "验收", "来源", "时间", "范围", "边界", "原因", "规律", "因果", "反例",
        ))
    )
    active_validation = active_validation_alignment(
        query, text, str(item.get("type") or item.get("memory_type") or "")
    )
    # Stable preferences and mental models are intentionally broader than a
    # factual entity lookup.  When the user explicitly asks how a deliverable
    # should reflect long-formed working preferences, a model/observation that
    # states a concrete quality or evidence standard is useful even if it does
    # not repeat the temporary deliverable's exact noun.  This remains a
    # conjunctive rule: explicit preference intent + model/observation source
    # + a quality/verification proposition are all required.
    preference_request = bool(
        any(marker in compact(query) for marker in ("长期偏好", "长期形成", "工作偏好", "一贯偏好", "稳定偏好"))
        and any(marker in compact(query) for marker in ("报告", "呈现", "验证", "证据", "验收", "给我看"))
    )
    preference_source = str(item.get("type") or item.get("memory_type") or "").casefold() in {"mental_model", "observation"}
    preference_quality_hits = [marker for marker in ("证据", "验证", "验收", "真实", "审计", "质量", "可见", "回执", "端到端", "结论") if marker in compact(text)]
    # The Controller can select a heading-bounded mental-model section that
    # defines what *real completion* means.  The Hook rechecks every section
    # at its own boundary, but must preserve that same narrow acceptance rule:
    # a request rejecting unit-test-only evidence needs a model that explicitly
    # couples runtime evidence with root-cause repair/end-to-end regression.
    # Requiring this full conjunction avoids admitting generic quality prose.
    q_for_acceptance = compact(query)
    real_chain_request = (
        any(marker in q_for_acceptance for marker in ("真实链路", "端到端", "真实交互", "运行时"))
        and any(marker in q_for_acceptance for marker in ("单测", "测试", "验收", "回归", "证据"))
    )
    real_completion_guidance = (
        preference_source
        and any(marker in compact(text) for marker in ("真实完成", "端到端", "运行时行为"))
        and any(marker in compact(text) for marker in ("根因", "回归", "测试结果", "硬证据"))
    )
    evidence_standard_request = (
        any(marker in q_for_acceptance for marker in ("测试通过", "单测", "测试"))
        and any(marker in q_for_acceptance for marker in ("真实证据", "可核对", "证据", "验收", "汇报"))
        and any(marker in q_for_acceptance for marker in ("稳定观察", "多次", "后续", "影响"))
    )
    # Cross-task questions about which memory layer to trust are taxonomy
    # questions, not project-change questions.  The governing model is useful
    # only if both sides explicitly name at least three of the four layers and
    # the model states their update/append/boundary semantics.
    memory_layers = ("世界事实", "事实", "经历", "观察", "心智模型")
    taxonomy_request = (
        sum(marker in q_for_acceptance for marker in memory_layers) >= 3
        and any(marker in q_for_acceptance for marker in ("边界", "当前", "旧结论", "跨任务", "优先"))
    )
    taxonomy_guidance = (
        preference_source
        and sum(marker in compact(text) for marker in memory_layers) >= 3
        and any(marker in compact(text) for marker in ("更新", "追加", "不可改写", "版本化", "底层证据", "置信度"))
    )
    # A question about an old failure, a later repair and present usability is
    # often intentionally generic: it asks for the governing temporal rule,
    # not for one named incident.  Exact-entity matching therefore used to
    # discard the four-layer model that explains how historical experiences,
    # current facts and invalidation interact.  Keep this bridge narrow: it
    # needs both sides of the temporal conflict in the request and an abstract
    # multi-layer memory rule in the candidate.  It does not admit arbitrary
    # project incidents merely because they contain the word "修复".
    q_compact = compact(query)
    temporal_conflict_request = (
        any(marker in q_compact for marker in ("旧", "曾", "之前", "过去", "失败"))
        and any(marker in q_compact for marker in ("后来", "新", "修复", "当前", "现在", "能不能用"))
        and any(marker in q_compact for marker in ("冲突", "时间", "证据", "有效", "失效"))
    )
    temporal_conflict_source = str(item.get("type") or item.get("memory_type") or "").casefold() in {"mental_model", "world"}
    layer_markers = ("世界事实", "经历", "观察", "心智模型")
    temporal_conflict_guidance = (
        temporal_conflict_request
        and temporal_conflict_source
        and sum(marker in compact(text) for marker in layer_markers) >= 2
        and any(marker in compact(text) for marker in ("可更新", "失效", "当前状态", "冲突", "更新"))
    )
    # Procedure questions can name a chain of system nodes rather than a
    # single entity (for example Hook → Controller → Bank → Packet → status
    # page).  Such a request needs actual historical run evidence, not only a
    # standing policy.  Two nodes are too permissive: generic migration plans
    # often happen to mention Bank plus the status page, yet cannot prove the
    # requested execution chain.  Require at least three named nodes and a
    # real verification, failure or rollback step.
    procedure_nodes = ("hook", "controller", "bank", "packet", "状态页", "回执")
    procedure_request = (
        sum(marker in q_compact for marker in procedure_nodes) >= 3
        and any(marker in q_compact for marker in ("流程", "步骤", "核对", "验收", "复测", "回退", "异常"))
    )
    candidate_compact = compact(text)
    procedure_evidence = (
        procedure_request
        and str(item.get("type") or item.get("memory_type") or "").casefold() in {"experience", "world"}
        and sum(marker in candidate_compact for marker in procedure_nodes) >= 3
        and any(marker in candidate_compact for marker in ("验证", "回执", "复测", "回退", "失败", "异常", "实际注入", "端到端"))
    )
    # v5 keeps the mismatch diagnostic but no longer treats absent ontology
    # labels as a hard veto.  Relation evidence (for example “同步进入课程、
    # 工位和验收体系”) can prove the same proposition even when a candidate
    # was summarized with different wording.
    # ``来源``/``证据`` is a metadata facet in a conflict or timeline
    # question, not by itself a verbatim-provenance intent.  When the query
    # does not explicitly ask for the user's original wording, the
    # provenance concept label must not make every otherwise relevant record
    # look like a concept mismatch and fall through to rejection.
    semantic_query_concepts = (
        query_concepts - {"来源核对"}
        # In an operational audit, “来源/日志/我说过” describes the
        # evidence surfaces being reconciled, not a demand that every
        # structured memory row prove a verbatim excerpt.  Raw evidence (when
        # explicitly requested) still follows its own local-excerpt gate.
        if not explicit_raw or operational_audit else query_concepts
    )
    concept_mismatch = bool(
        semantic_query_concepts
        and not matched_concepts
        and not alignment["relation_support"]
    )

    qualified = False
    reason = ""
    raw_excerpt, proposition_hits = ("", [])
    proposition = ""
    if cls == "raw_evidence":
        proposition = provenance_proposition(query)
        raw_excerpt, proposition_hits = provenance_excerpt(query, text)

    # Full Prompt is multi-intent: a status/Packet audit is a delivery
    # predicate, not an exclusive topic filter.  Preserve explicit subject
    # propositions first; a pure Hindsight/Hook chain audit still requires the
    # stricter procedure evidence below.
    subject_lane = bool(
        subject_evidence["qualified"]
        and not system_taxonomy_request
        and not (
            procedure_request
            and not procedure_evidence
            and not subject_evidence["non_infrastructure_subjects"]
        )
    )
    # ``candidate:...`` rows with an embedded old conversation are timeout
    # fallbacks, not canonical Bank claims.  They must never reach the Packet
    # merely because the quoted assistant text repeats a document/system name
    # or carries a stale controller-side qualified marker.  Canonical Bank
    # rows and concise, non-wrapper candidate summaries continue through the
    # ordinary relevance lanes below.
    if named_target_missing and cls != "raw_evidence":
        reason = "当前问题明确询问 AgentMemory；候选未提及 AgentMemory 本身，不能用仅描述 Codex/Hindsight 的背景替代目标系统证据。"
    elif diagnostic_artifact and cls != "raw_evidence":
        reason = (
            "候选是日志、脚本输出或机器状态工件，不是语义记忆；仅保留在原始审计候选中，"
            "不能跨越 Memory Packet 注入边界。"
        )
    elif legacy_candidate_wrapper and cls != "raw_evidence":
        reason = (
            "候选是旧对话包装而非 canonical Bank 记忆；仅保留为超时对账候选，"
            "不能跨越实际注入边界。"
        )
    elif observation_framework["qualified"]:
        qualified = True
        reason = (
            "候选属于观察/心智模型，并明确解释零注入诊断中的准入、过滤或投递机制；"
            "通过专用框架准入，不要求复述临时实体。"
        )
    elif (
        artifact_scope_alignment["requested"]
        and not artifact_scope_alignment["qualified"]
        and not (document_change_alignment["required"] and document_change_alignment["passes"])
        and not (handoff_alignment["required"] and handoff_alignment["passes"])
    ):
        # A concrete package/document identifier is a scope boundary of its
        # own.  Do this before named-subject, semantic and prequalified rescue
        # lanes so a generic ``zip``/``app`` overlap cannot admit a different
        # artifact.  Document-change and handoff lanes are exempt only after
        # their own conjunctive scope checks have already proved the same work
        # object and operation.
        reason = (
            "当前问题指定了具体文件/应用工件；候选未命中同一文件名或足够独立的工件标识，"
            "不能仅凭 zip、app、安装包等泛扩展名注入。"
        )
    elif (
        path_scope_alignment["requested"]
        and not path_scope_alignment["qualified"]
        and not (document_change_alignment["required"] and document_change_alignment["passes"])
        and not (handoff_alignment["required"] and handoff_alignment["passes"])
    ):
        reason = (
            "当前问题指定了具体路径/任务工件；候选既未命中同一路径主体，"
            "也未覆盖足够的同类媒体/文档操作分面，不能仅凭父目录或泛主题注入。"
        )
    elif current_operation_alignment["requested"] and not current_operation_alignment["qualified"]:
        # A concrete UI action can contain unrelated project/network context
        # in its Full Prompt. Reject any candidate that does not prove the
        # same target and action family before semantic/prequalified rescue can
        # use Chrome/Codex as a spurious anchor.
        reason = (
            "当前问题是具体界面/窗口操作；候选没有证明同一界面目标和操作动作，"
            "不能仅凭 Chrome/Codex 等共现词或 Full Prompt 背景注入。"
        )
    elif current_operation_alignment["requested"] and current_operation_alignment["candidate_ui_action"]:
        qualified = True
        reason = (
            "候选命中当前 Full Prompt 的界面目标与操作动作；"
            "作为同一窗口操作的工作集事实注入。"
        )
    elif named_issue_alignment["qualified"]:
        qualified = True
        reason = (
            "候选命中本题点名服务/实体的历史故障或回复问题，并保留原因、结果或当前状态；"
            "作为命名服务事件证据注入。"
        )
    elif preference_alignment["qualified"]:
        qualified = True
        reason = (
            "候选记录了与当前任务直接相关的稳定偏好/选择边界，并同时命中具体主题锚点；"
            "作为跨任务决策约束注入。"
        )
    elif entity_fact_alignment["qualified"]:
        qualified = True
        reason = (
            "候选命中本题点名实体/别名及其具体动作、范围和结果；"
            "虽未重复图谱术语，仍作为命名实体事实纳入关联闭包。"
        )
    elif association_alignment["requested"] and association_alignment["qualified"]:
        qualified = True
        reason = (
            "候选命中图谱/星座或实体关系，并补充命题覆盖、来源时序或泛节点抑制证据；"
            "通过关联闭包准入，不按狭义实际注入审计裁剪。"
        )
    elif association_alignment["requested"] and not association_alignment["mixed_delivery_audit"]:
        reason = (
            "当前问题要求解释关联闭包；候选没有同时提供结构关系与覆盖/证据或泛节点抑制，"
            "不能只凭词面、向量近邻或单一组件名注入。"
        )
    elif system_taxonomy_request and taxonomy_alignment["qualified"]:
        qualified = True
        reason = "候选命中本题点名系统的定义/角色/上下游或边界证据；通过系统关系准入，不按泛主题注入。"
    elif system_taxonomy_request:
        reason = "本题要求点名系统的定义与关系；候选没有同时命中至少一个点名系统和两个独立结构关系，不能注入。"
    elif subject_lane:
        qualified = True
        reason = (
            "候选命中 Full Prompt 明确主题及其命题证据；状态页审计关系只约束链路解释记录，"
            "不阻断该主题事实。"
        )
    elif real_dialogue["qualified"]:
        qualified = True
        reason = "候选命中真实可见对话的输入通道与测试/回复证据；作为真实交互验证依据注入。"
    elif recent_activity["qualified"]:
        qualified = True
        reason = (
            "候选同时保留近期范围、具体条目/记录和检查结果；"
            "作为用户要求的近期活动盘点证据注入。"
        )
    elif operational_audit and legacy_candidate_identity and cls != "raw_evidence":
        # ``candidate:…`` is the upstream fallback's embedded/unsaved chat
        # shape, not a canonical Bank memory id.  It is useful as an audit
        # oracle candidate, but injecting it would reintroduce old chat or
        # command output merely because it contains the chain vocabulary.
        # Canonical memories and explicitly requested raw evidence continue
        # through the normal structural gates.
        reason = (
            "当前是注入审计；该结果是无 canonical Bank 身份的候选包装，"
            "只能作为对账候选，不能直接跨越实际注入边界。"
        )
    elif operational_audit and operational_alignment["qualified"]:
        # An operational audit is a multi-surface proposition.  Let a
        # candidate through only when it explains a connected path (or the
        # Full Prompt/context evidence method) rather than because a stale
        # Controller prequalified it on a generic word such as “bank”.
        qualified = True
        reason = (
            "候选覆盖多个检索/投递/回执证据面并保留关系；作为本次运行审计的结构化链路证据注入。"
        )
    elif operational_audit and cls != "raw_evidence":
        # This guard must run before the generic prequalified/semantic rescue
        # branches below.  Otherwise a Controller-side provisional decision
        # can re-admit an unrelated project/plugin memory after the Hook has
        # correctly recognized the audit as a structured reconciliation.
        reason = (
            "当前是注入审计：要求对齐日志、Bank 与实际投递；候选未覆盖足够的独立证据面或链路关系，"
            "不能仅凭产品名、主题词或旧控制器预判注入。"
        )
    elif audit_alignment["required"] and not audit_alignment["qualified"]:
        reason = (
            "当前问题是状态页实际注入审计；候选只共享观察、数字或泛词，"
            "没有同时解释候选、投递和实际注入之间的区分关系，不能注入。"
        )
    elif _personal_health_domain_mismatch(query, text):
        reason = "当前问题是个人健康决策；候选没有健康决策语义，或只把医疗作为CRM/市场行业词，不能注入。"
    elif is_transfer_event_query(query) and not any(marker in compact(text) for marker in (
        "迁移", "迁出", "迁入", "搬迁", "转移", "移动", "复制", "重绑", "改为", "旧目录", "新目录", "migrat", "moved", "transfer",
    )):
        reason = "本题追溯来源到目标的迁移事件；仅含旧路径或文件交付地址，不能证明迁移动机、过程或验收。"
    elif handoff_alignment["required"] and not handoff_alignment["passes"] and not procedure_evidence:
        reason = (
            "这是一个具体跨任务交接；候选只与工具/当前状态泛相关，"
            "没有命中该任务要求的迁移、回退或未完成状态链。"
        )
    elif handoff_alignment["required"] and handoff_alignment["passes"]:
        # A named handoff candidate that proves the requested lifecycle and
        # artifact is already a direct answer relation. Do not force it
        # through the generic two-anchor/semantic-score branch: concise
        # migration records often contain exactly the task name, state and
        # rollback boundary, while the user's Full Prompt carries the richer
        # disambiguating context. The structural gate above still prevents
        # generic Codex/Hindsight configuration rows from entering.
        qualified = True
        reason = "候选命中 Full Prompt 指定的跨任务主体、迁移/回退生命周期和工件范围；作为交接事实注入。"
    elif document_change_alignment["required"] and document_change_alignment["passes"]:
        # The document-scope predicate is itself an admission lane.  The old
        # implementation only emitted a rejection reason for the failing
        # branch; a passing row fell through to concept/semantic matching and
        # was rejected as if it were unrelated.  That turned concrete table
        # restores and linked page edits into false zero-injection packets.
        # Promote only after the conjunctive document + element + action/
        # propagation check above; no filename or project-specific exception
        # is introduced here.
        qualified = True
        reason = (
            "候选命中 Full Prompt 指定的文档范围、修改元素及操作/联动关系；"
            "作为文档变更证据注入。"
        )
    elif document_change_alignment["required"] and not document_change_alignment["passes"]:
        reason = (
            "当前问题是指定文档中某一元素的联动修改；候选只共享项目或领域，"
            "没有同时证明文档/页面范围、被修改元素与修改或联动关系。"
        )
    elif procurement_alignment["requested"] and procurement_alignment["qualified"]:
        qualified = True
        reason = (
            "候选同时命中采购来源/文件、筛选规则及当前要求的服务、时间、金额或数量分面；"
            "作为可复用的招标筛选规则注入。"
        )
    elif procurement_alignment["requested"] and not procurement_alignment["qualified"]:
        reason = (
            "当前问题要求一组具体采购筛选规则；候选没有同时证明来源/文件、筛选动作"
            "和至少一个相同范围分面，不能因只出现‘招标文件’而注入。"
        )
    elif component_only_candidate:
        reason = (
            "用户明确要求不要只罗列组件；该候选只描述 Hindsight/Hook/Controller 等组件，"
            "没有提供观察、心智模型、证据、范围或有效性命题，不能作为本题答案依据。"
        )
    elif (
        provenance_alignment["requested"]
        and cls != "raw_evidence"
        and not provenance_alignment["passes"]
        and not operational_audit
    ):
        reason = (
            "当前问题要求原话、时间或来源；该结构化记录没有保留足够的具体命题分面，"
            "不能把宽泛架构说明伪装成原话证据或定位线索。"
        )
    elif cls == "raw_evidence" and not explicit_raw:
        reason = "这是原始对话/原话证据，但当前问题没有要求核对用户原话、来源或出处。"
    elif cls == "raw_evidence":
        # Raw archives are evidence only when one local source window proves the
        # *concrete proposition*.  A bank/source name, a broad domain concept,
        # or a high normalized retrieval rank cannot substitute for this gate.
        longest = max((len(x) for x in proposition_hits), default=0)
        qualified = bool(
            not concept_mismatch
            and (
                len(proposition_hits) >= 2
                or longest >= 8
                or (longest >= 4 and score is not None and score >= 0.35)
            )
        )
        reason = (
            "当前问题明确要求用户原话/来源；同一局部证据窗口命中了具体命题。"
            if qualified else
            "虽然要求了原话/来源，但该局部原文没有命中要核对的具体命题；来源名或宽泛主题不算证据。"
        )
    elif recurrence_alignment["qualified"]:
        qualified = True
        reason = (
            "用户要求预防同一工作流再次出问题；候选同时命中该工作流主题和故障/修复/校验链，"
            "作为防复发依据注入。"
        )
    elif recurrence_alignment["requested"]:
        reason = (
            "当前问题要求防止某一明确工作流复发；候选没有同时命中该工作流主体和故障/修复/校验链，"
            "不能因泛称‘机制、验收或失败’跨工作流注入。"
        )
    elif audit_alignment["qualified"]:
        qualified = True
        reason = "候选同时命中多项真实交付阶段和分账/回执关系，可用于解释检索、注入与回答使用如何区分。"
    elif comparison_alignment["qualified"]:
        qualified = True
        reason = "候选同时命中用户点名的多个系统及其角色、替换或比较关系；不依赖单一产品名近邻。"
    elif shared_alignment["qualified"]:
        qualified = True
        reason = "候选同时命中多个 Agent 端点和共享 Bank/Controller/Hook 机制，可回答跨会话、跨项目共同记忆如何成立。"
    elif preference_request and preference_source and preference_quality_hits:
        qualified = True
        reason = "用户明确要求按长期工作偏好设计验证呈现；该稳定观察/心智模型独立给出质量、证据或验收标准，可作为通用决策边界注入。"
    elif real_chain_request and real_completion_guidance:
        qualified = True
        reason = "用户要求超越单测的真实链路验收；该心智模型同时给出运行时证据、根因修复与端到端回归标准，可作为验收框架注入。"
    elif evidence_standard_request and real_completion_guidance:
        qualified = True
        reason = "用户要求把反复验证的真实证据标准归纳为稳定观察；该心智模型给出硬证据、根因修复与回归验收的完整标准，可作为长期验收框架注入。"
    elif taxonomy_request and taxonomy_guidance:
        qualified = True
        reason = "用户在跨任务判断四层记忆的用途与时序边界；该心智模型明确区分事实更新、经历追加、观察归纳与模型版本化，可作为分类裁决框架注入。"
    elif temporal_conflict_guidance:
        qualified = True
        reason = "用户询问旧失败、后修复与当前可用性的时序冲突；该四层记忆规则说明历史经历、当前事实和失效更新如何共同裁决，可作为通用边界注入。"
    elif continuation_alignment["qualified"] and str(item.get("type") or item.get("memory_type") or "").casefold() in {"experience", "world", "observation", "mental_model"}:
        qualified = True
        reason = (
            "用户询问长任务续办的上下文恢复方法；候选同时覆盖续办信号、上下文/任务边界和具体方法，"
            "作为通用长上下文策略依据注入。"
        )
    elif continuation_alignment["requested"]:
        reason = (
            "当前问题要求长任务续办的上下文恢复；候选未同时提供续办、上下文/边界和方法三类证据，"
            "不能仅凭 Full Prompt 或上下文词面注入。"
        )
    elif procedure_evidence:
        qualified = True
        reason = "用户点名多个链路节点并要求验收、回退或复测；该历史经历独立覆盖链路节点及实际验证/异常处置，可作为流程证据注入。"
    elif active_validation["qualified"]:
        qualified = True
        if active_validation["case_evidence"]:
            reason = "当前是持续中的真实窗口验证任务；候选命中同一活动 CASE 并保留可复核测试/状态证据，作为案例依据注入。"
        elif active_validation["evidence_bridge"] and len(active_validation["candidate_chain_nodes"]) < 3:
            reason = "当前是持续中的真实窗口验证任务；候选虽只点名部分链路节点，但保留了明确测试结果、根因、修复、截图或回执证据，作为通用诊断依据注入。"
        else:
            reason = "当前是持续中的真实窗口验证任务；候选覆盖至少三个执行链路节点，并保留测试、对比、根因、修复或回执等可复核证据，作为通用流程依据注入。"
    elif prequalified and not (
        topical_hits
        or entity_hits
        or (alignment["relation_support"] and alignment["subject_support"])
        or bool(matched_concepts)
    ):
        # Do not fall through to the normal semantic-only rescue below.  This
        # exact situation is the stale-controller false-positive pattern: an
        # older controller marked an item qualified, but the Hook can no
        # longer establish one independent current-topic anchor for it.
        reason = "控制器预判未能在 Hook 边界复证独立主题锚点；不能仅凭语义分把候选注入当前任务。"
    elif prequalified and (
        topical_hits
        or entity_hits
        or (alignment["relation_support"] and alignment["subject_support"])
        or bool(matched_concepts)
    ):
        # A Controller result is only a provisional candidate at the Hook
        # boundary.  In particular, a high cross-encoder score must not carry
        # an item across that boundary by itself: the score can be inflated by
        # a generic shared phrase such as “的 Bug”.  Require an independently
        # meaningful lexical/entity/relation anchor again here.  Pure semantic
        # rescue remains available below for *freshly* evaluated candidates,
        # where it is recorded as such instead of being mislabelled as a
        # controller-proven fact.
        qualified = True
        reason = "控制器已依据当前问题完成来源、语义和独立锚点准入。"
    elif cls == "trusted_rule":
        qualified = bool(
            (alignment["relation_support"] and (alignment["subject_support"] or matched_concepts))
            or (not query_concepts and score is not None and score >= one_anchor_threshold)
        )
        reason = (
            "版本化规则同时命中当前问题的主题与规则关系。" if qualified
            else "规则本身可信，但没有证明它正回答当前问题的主题和关系。"
        )
    elif explicit_raw and "来源核对" in query_concepts:
        # A structured summary can be a useful *lead* for a provenance query,
        # but it is never represented as verbatim proof.  Direct raw evidence
        # remains governed by the stricter local-excerpt check above.
        qualified = bool(
            (alignment["relation_support"] and (alignment["subject_support"] or matched_concepts))
            and (score is None or score >= general_threshold)
        )
        reason = (
            "结构化记忆命中具体命题；作为来源定位线索注入，不能替代原话证据。" if qualified
            else "来源核对必须命中具体命题；只命中来源名、人物名或宽泛主题不够。"
        )
    elif concept_mismatch:
        # Summaries can lose the exact relation verb while retaining a very
        # specific subject/scope (for example the named 河北项目 plus Word/PPT).
        # Permit that only with a high local semantic score AND independent
        # subject/scope support; same-tool/entity overlap alone still fails.
        semantic_subject_rescue = bool(
            alignment["required_relation"]
            and alignment["subject_support"]
            and alignment["operation_support"]
            and score is not None and score >= 0.82
        )
        qualified = semantic_subject_rescue
        reason = (
            "候选保留了同一主体/范围且本地语义分很高；虽未保留固定关系标签，作为改写后的相关记忆注入。"
            if qualified else
            "当前问题已明确意图分面，但该记忆既未证明同一主题关系，也没有主体、操作动作与高语义分三者同时成立的证据。"
        )
    elif alignment["required_relation"] and alignment["relation_support"]:
        # Structural relation alone is insufficient: a candidate needs either
        # concrete subject/scope support, or an exceptionally strong local
        # semantic score for a paraphrased subject omitted by summarisation.
        semantic_subject_rescue = bool(score is not None and score >= 0.75)
        qualified = bool(alignment["subject_support"] or semantic_subject_rescue)
        reason = (
            "命中当前问题的主体/范围和关系；按命题准入。" if alignment["subject_support"] and qualified
            else "候选未保留完整主体词，但本地语义分极高且命中所需关系，作为改写后的相关记忆注入。" if qualified
            else "虽命中一个通用关系词，但没有同一主体/范围证据，且语义分不足以证明它回答当前问题。"
        )
    elif subject_concepts & candidate_concepts & STRUCTURAL_CONCEPTS:
        qualified = True
        reason = (
            "命中同一结构化意图分面；本地语义分只参与排序，不否决图关系、"
            "时间关系或已定义的答案关系。"
        )
    elif len(topical_hits) >= 2 and not alignment["required_relation"]:
        qualified = True
        reason = "命中至少两个互不重叠的主题锚点。"
    elif topical_hits and not alignment["required_relation"]:
        concept_only = all(x.startswith("概念:") for x in topical_hits)
        threshold = (0.30 if deep else 0.40) if concept_only else one_anchor_threshold
        qualified = bool(score is not None and score >= threshold)
        reason = (
            "命中一个独立主题锚点，且语义分满足准入阈值。" if qualified
            else "只命中宽泛概念族，缺少具体命题锚点，语义分也不足。"
        )
    elif entity_hits and score is not None and score >= 0.90:
        qualified = True
        reason = "只命中系统/工具实体，但本地语义分足够高，确认讨论的是同一命题而不只是同一产品名。"
    elif score is not None and score >= semantic_only_threshold:
        qualified = True
        reason = "没有可靠词面重合，但本地跨编码器确认了改写/隐含语义相关。"
    else:
        reason = "只有泛词/向量近邻，缺少独立主题锚点，且语义相关性不足。"

    if direct:
        reason += " 直接原话只证明来源真实性，不会绕过主题相关性检查。"
    # Recall-first policy: preserve a weakly related candidate when it has at
    # least one independent topic/relation signal (or a modest semantic score)
    # so missing a useful memory costs more than carrying background context.
    # This does not bypass hard source, scope, provenance, diagnostic, or
    # explicit-conflict gates below; completely unrelated candidates remain
    # rejected.  The strength is recorded for the UI/agent to treat as
    # background rather than as a fact.
    weak_related = False
    weak_signal = bool(
        # The subject alignment can contain grammatical common fragments
        # rejected by _meaningful_current_anchor. Scores rank a relation;
        # they cannot manufacture one after a structural boundary failed.
        topical_hits or matched_concepts or alignment["relation_support"]
    )
    # A named endpoint occurring only in an explicit exclusion clause is
    # evidence of non-membership, not a weaker positive membership claim.
    # Reuse the current named-system projection and ordinary clause scope;
    # no query/entity list is introduced into the fallback.
    boundary_subjects = set(comparison_alignment.get("shared_systems") or []) | set(taxonomy_alignment.get("shared_systems") or [])
    positive_scope_text = compact(" ".join(
        clause for clause in re.split(r"[，,。！？!?；;\n]+", text)
        if not re.search(r"未涉及|不涉及|不属于|未包含|不包含|无关|不相关", clause)
    ))
    excluded_named_subject = bool(
        boundary_subjects
        and (comparison_alignment["requested"] or taxonomy_alignment["requested"] or entity_fact_alignment["requested"])
        and not any(compact(subject) in positive_scope_text for subject in boundary_subjects)
    )
    preference_scope_missing = bool(
        preference_alignment["requested"] and preference_alignment["research_scoped"]
        and len(preference_alignment["shared_anchors"]) < preference_alignment["minimum_shared_anchors"]
        and not alignment["relation_support"]
    )
    if preference_scope_missing:
        qualified = False
        reason = "当前要求带选择边界的研究或实现路径；候选只重复单个模型/工具名，未建立方向、论文或实现关系，不能作为弱相关背景恢复。"
    denied_structural_boundary = bool(
        (operational_audit and cls != "raw_evidence" and not operational_alignment["qualified"])
        or (audit_alignment["required"] and not audit_alignment["qualified"])
        # Reuse the existing clause-aware positive/negated named anchors.
        # An excluded endpoint or parent-name substring is not ownership.
        or (entity_fact_alignment["requested"] and not entity_fact_alignment["shared_anchors"])
        or excluded_named_subject
        or preference_scope_missing
    )
    hard_block = bool(
        denied_structural_boundary
        or legacy_candidate_wrapper or diagnostic_artifact or named_target_missing or component_only_candidate
        or (document_change_alignment["required"] and not document_change_alignment["passes"])
        or (artifact_scope_alignment["requested"] and not artifact_scope_alignment["qualified"])
        or (path_scope_alignment["requested"] and not path_scope_alignment["qualified"])
        or (current_operation_alignment["requested"] and not current_operation_alignment["qualified"])
        or (procurement_alignment["requested"] and not procurement_alignment["qualified"])
        or (provenance_alignment["requested"] and cls != "raw_evidence" and not provenance_alignment["passes"] and not operational_audit)
    )
    if not qualified and weak_signal and not hard_block:
        qualified = True
        weak_related = True
        reason = "召回优先策略：存在部分主题、关系或语义信号，作为弱相关背景候选保留；不作为已核实事实。"
    decision = "qualified" if qualified else "rejected"
    if not qualified and document_change_alignment["required"] and not document_change_alignment["passes"]:
        decision = "rejected_document_change_scope_mismatch"
    if (
        not qualified
        and artifact_scope_alignment["requested"]
        and not artifact_scope_alignment["qualified"]
        and not (document_change_alignment["required"] and document_change_alignment["passes"])
        and not (handoff_alignment["required"] and handoff_alignment["passes"])
    ):
        decision = "rejected_artifact_scope_mismatch"
    if (
        not qualified
        and path_scope_alignment["requested"]
        and not path_scope_alignment["qualified"]
        and not (document_change_alignment["required"] and document_change_alignment["passes"])
        and not (handoff_alignment["required"] and handoff_alignment["passes"])
    ):
        decision = "rejected_path_scope_mismatch"
    if (
        not qualified
        and current_operation_alignment["requested"]
        and not current_operation_alignment["qualified"]
    ):
        decision = "rejected_current_operation_scope_mismatch"
    if (
        not qualified
        and procurement_alignment["requested"]
        and not procurement_alignment["qualified"]
    ):
        decision = "rejected_procurement_scope_mismatch"
    if (
        not qualified
        and provenance_alignment["requested"]
        and cls != "raw_evidence"
        and not provenance_alignment["passes"]
        and not operational_audit
    ):
        decision = "rejected_provenance_proposition_mismatch"
    return {
        "decision": decision,
        "reason": reason,
        "source_class": cls,
        "strong_hits": strong_hits,
        "weak_hits": weak_hits,
        "hits": strong_hits,
        "semantic_relevance_score": None if score is None else round(score, 6),
        "query_concepts": sorted(query_concepts),
        "candidate_concepts": sorted(candidate_concepts),
        "matched_concepts": matched_concepts,
        "relevance_strength": "weak" if weak_related else ("direct" if qualified else "none"),
        "recall_first_policy": True,
        "proposition_alignment": alignment,
        "real_dialogue_alignment": real_dialogue,
        "recent_activity_alignment": recent_activity,
        "subject_evidence_alignment": subject_evidence,
        "association_closure_alignment": association_alignment,
        "named_entity_fact_alignment": entity_fact_alignment,
        "named_service_issue_alignment": named_issue_alignment,
        "explicit_preference_alignment": preference_alignment,
        "injection_audit_alignment": audit_alignment,
        "operational_audit_alignment": operational_alignment,
        "legacy_candidate_identity": legacy_candidate_identity,
        "legacy_candidate_wrapper": legacy_candidate_wrapper,
        "system_comparison_alignment": comparison_alignment,
        "system_taxonomy_alignment": taxonomy_alignment,
        "shared_memory_alignment": shared_alignment,
        "recurrence_prevention_alignment": recurrence_alignment,
        "continuity_task_alignment": handoff_alignment,
        "continuation_policy_alignment": continuation_alignment,
        "document_change_scope_alignment": document_change_alignment,
        "concrete_artifact_scope_alignment": artifact_scope_alignment,
        "path_artifact_scope_alignment": path_scope_alignment,
        "current_operation_scope_alignment": current_operation_alignment,
        "procurement_scope_alignment": procurement_alignment,
        "provenance_structured_alignment": provenance_alignment,
        "active_validation_alignment": active_validation,
        "evidence_role": "raw_proof" if cls == "raw_evidence" else ("structured_lead" if "来源核对" in query_concepts and qualified else "memory"),
        "authority_verified": bool(direct or cls == "trusted_rule"),
        "explicit_user_source_request": explicit_raw,
        "operational_audit_query": operational_audit,
        "policy": POLICY,
        "fixed_item_limit": False,
        "provenance_proposition": proposition if cls == "raw_evidence" else "",
        "proposition_hits": proposition_hits,
        "matched_excerpt": raw_excerpt if qualified and cls == "raw_evidence" else "",
    }


def admit_items(
    query: str,
    items: Iterable[dict[str, Any]],
    *,
    deep: bool = False,
    preserve_controller_decision: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    admitted, rejected = [], []
    for raw in items:
        item = dict(raw)
        metadata = dict(item.get("metadata") or {})
        decision = admission_decision(
            query, item, deep=deep,
            preserve_controller_decision=preserve_controller_decision,
        )
        metadata["_ccy_admission"] = decision
        if decision.get("decision") == "qualified" and decision.get("matched_excerpt"):
            original_text = str(item.get("text") or item.get("content") or "")
            metadata["_ccy_original_text_sha256"] = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
            metadata["_ccy_evidence_excerpted"] = True
            if "text" in item or "content" not in item:
                item["text"] = decision["matched_excerpt"]
            else:
                item["content"] = decision["matched_excerpt"]
        item["metadata"] = metadata
        (admitted if decision["decision"] == "qualified" else rejected).append(item)
    return admitted, rejected


def cross_encoder_scores(
    query: str,
    texts: list[str],
    *,
    model_name: str = "BAAI/bge-reranker-base",
    batch_size: int = 32,
) -> tuple[list[float | None], str | None]:
    """Score locally; never download or call the network."""
    global _MODEL, _MODEL_ERROR
    if not texts:
        return [], None
    with _MODEL_LOCK:
        if _MODEL is None and _MODEL_ERROR is None:
            try:
                from sentence_transformers import CrossEncoder
                _MODEL = CrossEncoder(model_name, device="cpu", local_files_only=True)
            except Exception as exc:  # deterministic lexical gate remains active
                _MODEL_ERROR = f"{type(exc).__name__}: {exc}"
        model, error = _MODEL, _MODEL_ERROR
    if model is None:
        return [None] * len(texts), error
    try:
        values = model.predict(
            [(query, text) for text in texts], batch_size=max(1, int(batch_size)),
            show_progress_bar=False,
        )
        return [float(value) for value in values], None
    except Exception as exc:
        return [None] * len(texts), f"{type(exc).__name__}: {exc}"
