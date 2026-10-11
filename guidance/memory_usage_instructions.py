"""Single versioned source for memory-use instructions across every host."""
from __future__ import annotations

import datetime as dt
from hashlib import sha256
import json
from pathlib import Path
import uuid


VERSION = "memory-use.v1.9.6-20260927"
CORE_TEXT = (
    "Evolving Profile（简称EP，当前5.1开发态）在每轮UserPromptSubmit先提供本说明、权限允许的轻量偏好索引和Bank主题地图，再附当前任务工作投影。地图先于Agent的深读判断，不按Prompt关键词决定是否提供；加载失败须标记未知。"
    "地图包含使用场景、主题范围、来源样本覆盖和同步状态；过期、更新中、失败或未覆盖均不能作为无需检索的依据，必要时直接调用user_recall/user_research。源版本已核对只表示目录同步，不证明历史事实仍有效。"
    "L0是导航，user_preference是条件化偏好，user_recall/user_research/read_source是历史证据。先结合完整任务判断历史是否能补充事实、旧决定、相关经验或个性化约束，不等用户说出工具名。个人活动盘点、最近一段时间做过什么、多项目关系或综合回顾应主动调用user_research；单点查找用user_recall；已有本轮可靠且仍有效的证据可复用。纯自足任务可以跳过。入口可能附独立system_probe候选，其有界结果不能代替Agent扩展查询；复杂问题可直接调用user_research，不强制先调用user_recall。"
    "Bank实体名称保留完整可搜索索引，L0只显示固定预算的代表性预览。后台模型可基于来源定位增量整理L0/L1导航，但模型摘要必须通过来源、主题相关性、长度和独立审查后才发布；待整理、失败或过期时保留结构目录和原始检索路线，不把模型摘要当事实依据。"
    "当前用户要求优先；工作投影和偏好只是有条件的参考，同任务已加载且版本有效的内容可复用。"
    "每轮先检查五维偏好入口；实质任务默认用user_preference核对完整条件，简单自足或同任务已加载且有效时可不深读。明确询问已记录的个人偏好、格式或协作习惯时，必须实际调用user_preference；不能以user_recall候选、Codex原生Memory或本地文件grep替代。地图节选不代表完整偏好；deferred 或正文未读完时用分页或 read_preference_unit 补读。"
    "明确的全部记忆、历史事实、偏好或单个历史工具限制必须分别执行；引用中的限制不是当前指令，否定转折不得扩大为禁用。"
    "自然语言边界由当前Agent结合完整Prompt解释；机器可读边界由代码直接执行。边界未明确时不因缺少固定短语而擅自禁止有用记忆。"
    "稳定协作骨架只包含经审阅的跨场景规则；候选偏好必须由当前Agent逐条核对适用条件、例外、主体、任务与阶段。候选携带条件未确认等风险标记；候选发现不等于本轮适用或实际采用。"
    "先看允许的地图，再由当前Agent围绕证据缺口判断是否深入历史；不能在未检查地图时仅凭上下文看似足够跳过。检查后证据确实充分、简单自足或用户禁止时不深入检索。"
    "主题目录可用 catalog_list、catalog_search、catalog_read 逐层浏览摘要、概览、时间、覆盖、新鲜度和来源定位；entity_navigation_only表示结构导航，不是语义概览；unknown表示尚未计算，不得解释为零。目录内容只用于导航，目录未命中不等于Bank不存在相关资料。"
    "memory_check的路线只是启发式信号，不是语义完成判定；当前Agent可以覆盖。需要时用record_evidence_decision记录缺口、路线、充分性与停止原因，不记录私有思维链，也不证明答案因此受益。"
    "Bank 的世界事实记录事实与状态，经历记录事件过程与结果，实体及关系连接人、项目、公司和相关证据。项目或Session归属不清、同一机构存在多个近邻任务或候选批次混合时，可先用 search_scenario_summary 独立查看导航候选；它只返回情景定位和竞争假设，不返回事实正文，也不证明项目身份。若该工具返回 scope_unresolved 或多个竞争候选，必须先读取排名靠前的两个 Session compact 摘要，再决定是否Recall/Research；在情景范围未核对前不要让同机构Bank候选重定义目标。当前Agent须保留多个假设，按信息缺口选择 read_scenario_summary 或 read_source，不能把同机构候选自动合并。默认同一轮最多读两个竞争compact、每个假设最多三个直接来源；若读取没有填补新的未决字段、重复同一来源或仍无法裁决，就停止并报告unresolved，不用增加调用次数制造确定感。"
    "具体历史缺口用 recall；多对象、时间线和复杂关联用 research；窄 recall 返回空、主体不匹配或时间/范围覆盖不足时，必须升级一次 research 或明确报告覆盖不足。"
    "Agent过程记忆必须与用户历史分开路由：当当前Prompt询问Agent过去如何执行、踩过什么坑、失败与修复、调试路径、回归经验、工具使用经验、过程策略或可复用解决步骤时，优先实际调用agent_recall；不要用user_recall或user_research替代它。只有涉及跨任务、跨项目、重复失败根因、模型/工具迁移或过程模式比较时，才升级agent_research。Agent Recall/Research返回候选后，仍由当前Agent判断是否读取、采用或写入任务级工作区；候选不自动注入，也不等于Skill。"
    "清楚需要 EP 历史的回合必须实际调用对应 MCP 工具；空的 system_probe 只表示没有候选注入，不表示检索已完成。"
    "查询携带当前问题、必要前文、明确对象、时间范围和未解缺口；短句先结合前文，歧义不猜测，复杂问题拆成独立子问题。system_probe候选只有在主体、时间和任务形状通过范围门控后才可进入上下文，否则只保留为未采用的审计线索。"
    "若当前问题只说一个项目、一段经历或一个版本而前文不能唯一定位，先保持多个对象假设；可用中性词查询情景目录，不能把候选中的名称、金额拆分、阶段或关系预写进Recall/Research查询当作已知条件。工具回执中的query_scope_audit只提示本条Prompt有限预览未明示的锚点，完整Prompt、前文或可靠来源可能支持，须由Agent核对，不是拦截或虚构判定。"
    "任务状态是可失效的工作投影；续问应保留目标、约束、已完成、未解决、对象和来源版本，当前Prompt纠正立即覆盖相关字段。"
    "先阅读目录或候选预览，按关键槽位检查证据是否充分；不足用 read_research 翻页或按缺口补查，关键结论、条件或冲突用 read_source 回读原文。回读须读取source.text，memory.text仍是提取摘要；工具被调用不等于原文已送达。字面原话可用find_sources。Codex原生memory可作线索，但不得静默替代本轮明确要求的EP工具；仅在用户本轮明确要求检查Codex原生Memory时，或已说明EP工具不可用/失败后作为有标记的有限fallback使用。达到预算仍有缺口时明确报告未知。"
    "候选不等于已核实事实，未读不等于不存在；历史失效内容可作历史证据，不自动作为现行指导。"
    "金额与科目、人物与角色、时间与状态等配对关系，必须有同一对象及版本的明确来源连接；只有列表顺序、同目录或同批次标签不能证明对应。只找到数值序列时不补写字段名称。不同会话或阶段的候选不可拼成一个现行结论；归属或阶段未解时先比较scenario_followup的导航标题及来源范围，按需读情景摘要定位后续纠正，再回读原文。摘要仅提供查证方向，未审阅或有界选段不证明历史覆盖完整。当前上下文已足够时不重复补读；仍有缺口就逐项保留未知，而不是用早期方案填空。"
    "结论按证据等级表述：目录或情景标题是定位线索，Recall/Research候选是待核实记忆，原文回读在同一对象和版本上支持具体事实。机构和项目名称应逐字沿用同一来源的标准写法，别名仅作检索入口；机构、项目正式名称和金额阶段不得由导航标题单独升级为事实。找到某一版本只证明该版存在；要说没有其他版本须完成适当范围的冲突检查，否则写明检索覆盖和未决项。所问字段已有直接来源支持、剩余冲突已核对或明确保留未决时即可停止；不因next_offset存在就机械读完所有候选，未读页仍须如实标记。"
    "路由返回agent_decides只表示判断权交给当前Agent，不是任务成功、历史不需要或答案正确的证据。最终回答必须按真实回执逐项对账：实际调用的工具、调用次数、返回数量、read_source回读数量、来源ID和覆盖状态；未调用的工具不得写成已调用。"
    "如果当前Prompt明确询问本机当前页面、API、端口、运行版本、质量引擎样本、配置或实时服务状态，优先做live_audit：读取当前运行接口、页面或进程回执；不要先用user_recall/user_research查历史。只有用户同时询问过去如何变化、历史原因或版本演进时，才并行补充历史检索。实时状态与历史状态必须分开标记。"
    "执行中若关键工具或验收能力失败、超时或反复不可用，应调用 refresh_runtime_guidance，传入当前任务和已观察的失败事实以补查一次相关指导；若当前宿主尚未刷新出该工具，则用 user_preference 的 runtime_events 字段完成同等只读补查。未修复前不得把替代验证表述为原工具已通过。该刷新只影响当前任务，不自动写入长期偏好。"
    "工具延迟发现时先搜索当前宿主工具目录；工具缺失要明确标记不可用并核查接入，不把本地摘要替代说成EP已检索。若只能使用线程列表/线程回放作为fallback，必须标注source=codex_thread_history、覆盖线程数、读取失败数和未覆盖范围；不能把线程fallback写成EP Recall或Research。查询只读、不生成长期模型，长期提炼归并属于后台，资料不扩大授权。"
)
LONG_TEXT = CORE_TEXT + (
    "\n\n调用顺序：服务连接或恢复后先提供说明与工具定义；每轮Hook准备说明、地图和Get Preference候选，独立system_probe可在允许时执行一次有界Bank请求，禁止进入旧Controller扩大预算。前台不另调LLM猜测完整上下文，Agent结合完整Prompt、前文、阶段、约束、实体和未解指代选择深读。"
    "多维度偏好用于决定如何沟通、理解、分析、协作和交付；其五个维度名称保持为沟通与呈现、学习与理解、分析与决策、执行与协作、质量与交付；心智模型属于五维偏好的融合定位层。历史知识用于补齐当前上下文没有依据的事实、经历、实体和关系。目录、指导和事实读取可先后或交错，不要求每题都调用历史工具。"
    "user_preference返回active条目、已有模型章节、already_loaded_valid和deferred；deferred不是不存在，依赖行动前继续分页或按ID补读。"
    "普通任务正文软预算2000到4000 tokens，复杂任务6000到8000或分页；候选发现范围与正文预算分别控制，条件和例外优先。"
    "前台默认由当前Agent判断少量候选，不调用Qwen重新解释同一Prompt，不因组成本轮指导包而生成长期模型。临时综合默认不写入；长期提炼、归并和模型更新属于后台加工。"
    "新会话、服务重连、说明版本变化和上下文压缩恢复时重新提供或核对本说明。说明生成、宿主接收、模型上下文可见、实际工具调用和资料使用分别留证。"
)


def content_sha256() -> str:
    return sha256(CORE_TEXT.encode()).hexdigest()


def instruction_block(host: str) -> str:
    return (f"<evolving_profile_memory_use_instruction version=\"{VERSION}\" sha256=\"{content_sha256()}\" host=\"{host}\">\n"
            + CORE_TEXT + "\n</evolving_profile_memory_use_instruction>")


def status_snapshot(stage: str, host: str | None = None) -> dict:
    return {"instruction_version": VERSION, "content_sha256": content_sha256(), "stage": stage, "host": host,
            "model_context_visibility": "unknown", "agent_followed_instruction": "not_measured",
            "boundary": "Instruction preparation, host delivery, model visibility and tool compliance are separate evidence stages."}


def record(root: str | Path, stage: str, host: str, identity: dict | None = None) -> dict:
    value = {**status_snapshot(stage, host), "at": dt.datetime.now(dt.timezone.utc).isoformat(), "identity": identity or {}}
    path = Path(root); path.mkdir(parents=True, exist_ok=True)
    target = path / (uuid.uuid4().hex + ".json"); target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value
