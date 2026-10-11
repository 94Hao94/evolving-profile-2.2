"""Meaningful recall relations, independent policies, and literal intent."""
import importlib.util

import pytest


def engine():
    assert importlib.util.find_spec("lib.recall_relevance") is not None, "shared relevance engine missing"
    from lib import recall_relevance
    return recall_relevance


def test_policy_inherits_per_plane_and_rag_stays_independent():
    api = engine()
    settings = {"recall_policy": {"default_min_relevance": "medium", "agent_memory": "strong"}, "rag": {"minimum_relevance": "weak"}}
    assert api.normalize_recall_policy(settings["recall_policy"])["user_memory"] == "inherit"
    assert api.resolve_min_relevance(settings, "user_memory")["effective_level"] == "medium"
    assert api.resolve_min_relevance(settings, "agent_memory")["effective_level"] == "strong"
    assert api.resolve_min_relevance(settings, "external_rag")["effective_level"] == "weak"
    decision = api.resolve_min_relevance(settings, "agent_memory", requested="weak")
    assert decision["effective_level"] == "strong"
    assert not decision["requested_applied"]
    assert "request_would_loosen_policy" in decision["reasons"]
    with pytest.raises(ValueError):
        api.normalize_recall_policy({"default_min_relevance": "none"})


def test_default_weak_has_explanation_but_excludes_unrelated_and_metadata_hits():
    api = engine()
    records = [{"id": "weak", "text": "Presentation preview caught clipping before delivery."},
               {"id": "unrelated", "text": "Vegetable soup tastes better with onions."},
               {"id": "metadata", "text": "Gardening instructions", "metadata": {"query": "presentation overflow"}}]
    kept, audit = api.apply_relevance_policy("presentation overflow rendering", records)
    assert [r["id"] for r in kept] == ["weak"]
    assert kept[0]["relevance_reasons"]
    assert audit["excluded_count"] == 2
    assert audit["effective_level"] == "weak"
    assert records[0].get("relevance_level") is None


@pytest.mark.parametrize("identifier", ["ABC_731", "MTR_908", "zxq_R17"])
def test_explicit_code_query_cannot_match_generic_test_mechanism(identifier):
    api = engine()
    query = f"Find the exact test code `{identifier}`"
    assert api.classify_candidate(query, {"text": "Test verification tool mechanism checks passing assertions."})["level"] == "none"
    assert api.classify_candidate(query, {"text": f"Receipt for `{identifier}` passed."})["level"] == "strong"
    assert api.classify_candidate(query, {"text": identifier.lower()})["level"] == ("strong" if identifier == identifier.lower() else "none")


def test_code_like_word_without_literal_intent_does_not_block_transferable_procedure():
    api = engine()
    result = api.classify_candidate("Fix invoice_parser pagination filtering before offset", {"task_archetype": ["document_office"], "text": "For report pagination, filter relevant rows before offset calculation."})
    assert result["level"] in {"medium", "strong"}


def test_cjk_transferable_mechanism_is_not_same_task_family_requirement():
    api = engine()
    result = api.classify_candidate("检索时先过滤再分页，避免总数错误", {"task_archetype": ["document_office"], "text": "导出报表时先过滤再分页，确保总数与筛选后的记录一致。"})
    assert result["level"] in {"medium", "strong"}
    assert result["reasons"]


def test_main_query_prevents_generic_facet_boilerplate_admission():
    api = engine()
    kept, audit = api.apply_relevance_policy("tools tests mechanism", [{"id": "bad", "text": "Test tool verification mechanism"}], main_query="Find exact identifier `RVK_442`")
    assert kept == []
    assert audit["level_counts"]["none"] == 1


def test_policy_filters_before_pagination_without_fixed_candidate_limit():
    api = engine()
    records = [{"id": str(i), "text": "filter relevant records before pagination offset calculation"} for i in range(75)]
    kept, audit = api.apply_relevance_policy("filter relevant records before pagination offset calculation", records, "strong")
    assert len(kept) == 75
    assert audit["kept_count"] == 75


def test_unclassifiable_content_is_unknown_and_explained():
    api = engine()
    result = api.classify_candidate("repair pagination", {"id": "only-a-locator", "score": 0.98})
    assert result["level"] == "unknown"
    assert result["reasons"]


def test_single_meaningful_subject_can_be_directly_relevant():
    api = engine()
    assert api.classify_candidate("photosynthesis", {"text": "Photosynthesis converts light into chemical energy."})["level"] == "strong"


def test_cjk_boilerplate_does_not_invent_meaningful_cross_word_tokens():
    api = engine()
    assert api.classify_candidate("测试工具机制验证", {"text": "测试工具机制验证"})["level"] == "unknown"


def test_requested_literal_without_quotes_does_not_accept_document_neighbor():
    api = engine()
    result = api.classify_candidate("精确查找 TJBD-2026-A-168 招标预算条款", {"text": "TJBD-2026-A-169 招标预算条款"})
    assert result["level"] == "none"


def test_policy_levels_preserve_explained_partial_relation_and_original_vector_score():
    api = engine()
    records = [{"id": "strong", "text": "presentation overflow rendering", "score": 0.12},
               {"id": "medium", "text": "presentation overflow", "score": 0.95},
               {"id": "weak", "text": "presentation guidance", "score": 0.99}]
    assert [r["id"] for r in api.apply_relevance_policy("presentation overflow rendering", records, "strong")[0]] == ["strong"]
    assert [r["id"] for r in api.apply_relevance_policy("presentation overflow rendering", records, "medium")[0]] == ["strong", "medium"]
    kept, _ = api.apply_relevance_policy("presentation overflow rendering", records, "weak")
    assert [r["id"] for r in kept] == ["strong", "medium", "weak"]
    assert [r["score"] for r in kept] == [0.12, 0.95, 0.99]


def test_unquoted_identifier_adjacent_to_cjk_keeps_literal_intent():
    api = engine()
    query = "精确查找测试代码QKM_552的回执"
    assert api.classify_candidate(query, {"text": "测试代码QKM_553的回执"})["level"] == "none"
    assert api.classify_candidate(query, {"text": "测试代码QKM_552的回执"})["level"] == "strong"


def test_generic_recovery_vocabulary_cannot_relate_different_targets():
    api = engine()
    result = api.classify_candidate("浏览器里的选框无法拖动，修复失败后总结经验和根因", {"text": "安装 Git 失败后修复环境，agent 总结经验并定位根因。"})
    assert result["level"] == "none"


def test_environment_blocks_paths_and_tool_routes_cannot_supply_subject_matches():
    api = engine()
    query = "安装 Git 时代理超时导致失败 <environment_context><cwd>/Users/example/node/browser</cwd></environment_context>"
    text = "修复浏览器节点 <environment_context><cwd>/Users/example/git/proxy</cwd></environment_context> external_codex_apps_open_page"
    assert api.classify_candidate(query, {"text": text})["level"] == "none"


def test_quoted_test_prompt_is_not_a_reusable_procedure_for_its_subject():
    api = engine()
    text = '查询工具的离线验证报告。测试 Prompt：“网页里选框无法拖动、手柄无法缩放”。返回了 3 条结果，随后修复检索逻辑。'
    assert api.classify_candidate("网页里选框无法拖动、手柄无法缩放", {"text": text})["level"] == "none"


def test_general_main_question_keeps_substantive_facet_subject():
    api = engine()
    result = api.classify_candidate("选框拖动与缩放手柄", {"text": "选框拖动依赖指针坐标转换，缩放手柄需单独处理事件。"}, main_query="帮我继续修复失败问题，总结经验")
    assert result["level"] in {"medium", "strong"}


def test_cross_domain_actual_procedure_still_admitted():
    api = engine()
    pairs = [("网页选框缩放时指针坐标转换错误", "CAD 编辑器缩放图元时，先把指针坐标转换为画布坐标再计算位移。"),
             ("筛选检索结果后分页偏移错误", "报表导出先筛选记录，再基于筛选后记录计算分页偏移与总数。")]
    for query, text in pairs:
        result = api.classify_candidate(query, {"text": text})
        assert result["level"] in {"medium", "strong"}
        assert result["reasons"]


def test_atom_cjk_overlap_is_not_mathematical_subject_evidence():
    api = engine()
    query = "测试失败后定位求和遍历遗漏末元素的根因，最小修复并增加空集合、单元素、负数、生成器边界复测的Agent过程经验"
    for text in ["用户要求和历史项目背景应先读情景摘要。", "新增了三组路由单元测试，覆盖用户历史。", "页面显示外部证据已送达，用户记忆不增加PDF内容。"]:
        assert api.classify_candidate(query, {"text": text})["level"] == "none"
    assert api.classify_candidate(query, {"text": "遍历遗漏末元素会导致求和不正确。保留生成器输入，检查空集合、单元素和负数，再复测。"})["level"] in {"medium", "strong"}


def test_fenced_imperative_prompt_example_does_not_assert_subject_procedure():
    api = engine()
    text = "重启工具后再发这些测试。例如：\n```text\n请搜索以前处理类似网页拖动、缩放和视觉验收问题的智能体过程经验。\n```\n应看到工具名称出现在时间线。"
    assert api.classify_candidate("网页拖动、缩放失败的原因", {"text": text})["level"] == "none"


def test_concrete_procedure_answering_one_verbose_subproblem_is_medium():
    api = engine()
    query = "报表仪表盘出现计数串位、拖拽缩放异常、弹窗详情为空，请分别核对以前的根因、修复动作、独立验证、会话阶段，保留原始事实并说明适用条件。"
    record = {"text": "报表仪表盘计数串位的原因是父层重复累计子层。按唯一标识去重，再聚合子层计数，移除重复累加。"}
    result = api.classify_candidate(query, record)
    assert result["level"] == "medium"
    assert result["match_signals"]["answered_problem_concepts"]


def test_direct_single_mechanism_with_scope_is_strong_despite_audit_prose():
    api = engine()
    query = "报表仪表盘计数串位。请研究过去的根因和修复路径，逐条保留当时阶段、验证证据、反例与适用范围，不能把旧完成声明当作现行验收。"
    record = {"text": "报表仪表盘计数串位：先按唯一标识去重，然后聚合子层计数，移除父层重复累加。"}
    assert api.classify_candidate(query, record)["level"] == "strong"


def test_subject_only_background_stays_weak_for_verbose_problem_request():
    api = engine()
    query = "报表仪表盘计数串位、弹窗详情为空，请核对当时的修复和验证证据。"
    assert api.classify_candidate(query, {"text": "报表仪表盘是领导经常使用的管理入口。"})["level"] == "weak"


def test_english_subproblem_procedure_is_medium_across_domains():
    api = engine()
    query = "Fix the reporting dashboard with duplicate counts, dragging errors, popup details missing and pagination offsets. Compare historical root causes, evidence, conditions and independent verification across tasks."
    result = api.classify_candidate(query, {"text": "In a document reporting pipeline, deduplicate by stable IDs before aggregation to calculate counts only once."})
    assert result["level"] == "medium"


def test_truncated_fence_cannot_promote_a_later_quoted_prompt():
    api = engine()
    text = "工具时间线\n```\n重新加载工具目录，读取配置即可。\n```text\n请搜索以前处理类似网页拖动、缩放问题的智能体过程经验。\n```\n应看到工具名称。"
    assert api.classify_candidate("网页拖动、缩放失败的原因", {"text": text})["level"] == "none"


def test_projection_of_unrelated_fields_is_background_for_detail_projection():
    api = engine()
    query = "仪表盘弹窗详情为空，需要修复详情投影。"
    result = api.classify_candidate(query, {"text": "仪表盘计数投影按父子分支聚合，移除重复累计，只展示真实数量。"})
    assert result["level"] == "weak"


def test_problem_heading_connects_adjacent_bullet_repair_actions():
    api = engine()
    query = "仪表盘计数串位、拖拽缩放异常、弹窗详情为空，请比较根因及验证证据。"
    text = "仪表盘重复计数\n\n父层再次包含了已包含的子层。\n\n已修复：\n\n- 按唯一标识去重；\n- 移除父层重复累加。"
    assert api.classify_candidate(query, {"text": text})["level"] == "medium"


def test_direct_coupled_mechanism_and_subject_are_strong_with_verbose_prose():
    api = engine()
    query = "仪表盘重复计数。请逐条保留当时阶段、验证证据、反例与适用范围，不能把旧完成声明当作现行验收。"
    assert api.classify_candidate(query, {"text": "仪表盘重复计数：按稳定标识去重，再聚合子层计数，移除重复累加。"})["level"] == "strong"


def test_resolved_policy_explains_configuration_inheritance_and_override():
    api = engine()
    settings = {"recall_policy": {"default_min_relevance": "medium", "agent_memory": "strong"}, "rag": {"minimum_relevance": "weak"}}
    inherited = api.resolve_min_relevance(settings, "user_memory", requested="strong")
    assert inherited["configuration_source"] == "global_default"
    assert inherited["global_level"] == "medium"
    assert inherited["plane_setting"] == "inherit"
    assert inherited["configured_level"] == "medium"
    assert inherited["effective_level"] == "strong"
    override = api.resolve_min_relevance(settings, "agent_memory")
    assert override["configuration_source"] == "plane_override"
    assert override["plane_setting"] == "strong"
    external = api.resolve_min_relevance(settings, "external_rag")
    assert external["configuration_source"] == "external_rag"
    assert external["global_level"] == "medium"
    assert external["plane_setting"] == "weak"


@pytest.mark.parametrize("query,text", [
    ("请回顾小王过去一段时间做过哪些客户工作", "小王负责学校客户的项目方案。"),
    ("我儿子优优的学习进度怎么样", "优优正在学习英语口语和数学。"),
    ("小王过去的经历有哪些", "小王曾参与学校信息化建设。"),
])
def test_meaningful_short_cjk_subjects_remain_available_as_weak_background(query, text):
    api = engine()
    assert api.classify_candidate(query, {"text": text})["level"] in {"weak", "medium", "strong"}


def test_short_cjk_subject_recall_preserves_different_person_scope():
    api = engine()
    assert api.classify_candidate("小王过去的经历有哪些", {"text": "小李曾参与学校信息化建设。"})["level"] == "none"
    assert api.classify_candidate("求和函数的系统节点故障修复经验", {"text": "修复其他系统节点故障，工具测试已通过。"})["level"] in {"none", "unknown"}
    assert api.classify_candidate("小王过去一段时间做过哪些客户工作", {"text": "小李负责学校客户工作。"})["level"] == "none"


@pytest.mark.parametrize("query", ["请把我全部的历史事实和经历都找出来", "Find all my historical facts and experiences", "List all my memories"])
def test_explicit_unfiltered_history_browse_admits_content_with_explained_scope(query):
    api = engine()
    result = api.classify_candidate(query, {"text": "用户曾在天津做高校信息化项目。"})
    assert result["level"] in {"weak", "medium", "strong"}
    assert result["match_signals"]["unfiltered_browse_intent"]


@pytest.mark.parametrize("query", ["把学校A全部的历史事实都找出来", "请把全部预算历史事实找出来", "Find all budget history", "List all memories about School A"])
def test_all_history_with_subject_qualifiers_remains_scoped(query):
    api = engine()
    assert api.classify_candidate(query, {"text": "家庭宠物喜欢散步。"})["level"] == "none"


def test_possessive_task_noun_does_not_become_an_invented_person_scope():
    api = engine()
    result = api.classify_candidate("报表的分页过滤偏移错误", {"text": "检索之前先过滤，再基于过滤后的记录分页计算偏移。"})
    assert result["level"] in {"medium", "strong"}


def test_exclusion_reasons_count_only_two_excluded_candidates_at_medium_floor():
    api = engine()
    records = [{"id": "strong", "text": "presentation overflow rendering"},
               {"id": "medium", "text": "presentation overflow"},
               {"id": "weak", "text": "presentation guidance"},
               {"id": "none", "text": "Potato soup"}]
    kept, audit = api.apply_relevance_policy("presentation overflow rendering", records, "medium")
    assert [record["id"] for record in kept] == ["strong", "medium"]
    assert audit["kept_count"] == 2
    assert audit["excluded_count"] == 2
    assert audit["exclusion_reasons"] == {"below_minimum_relevance": 1, "no_meaningful_content_relation": 1}
    assert sum(audit["exclusion_reasons"].values()) == audit["excluded_count"]
    assert all(decision["exclusion_reason"] is None for decision in audit["decisions"] if decision["kept"])
    _, weak_audit = api.apply_relevance_policy("presentation overflow rendering", records, "weak")
    assert "below_minimum_relevance" not in weak_audit["exclusion_reasons"]


def test_unknown_and_none_exclusions_keep_actual_classification_reasons():
    api = engine()
    _, audit = api.apply_relevance_policy("pagination filtering", [{"id": "unknown", "metadata": {"query": "pagination filtering"}}, {"id": "none", "text": "Potato soup"}])
    assert audit["exclusion_reasons"] == {"no_readable_candidate_content": 1, "no_meaningful_content_relation": 1}
    assert sum(audit["exclusion_reasons"].values()) == 2
    assert "configured_minimum_relevance" not in audit["exclusion_reasons"]
