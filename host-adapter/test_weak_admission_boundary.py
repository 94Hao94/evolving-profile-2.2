"""Weak background keeps real relations but never creates subject ownership."""
import pytest
from lib.relevance import admission_decision


def test_weak_fallback_cannot_use_grammatical_bug_fragment_as_subject():
    query='修复多情景切换导致报价丢失的Bug，核对报价保存状态。'
    unrelated={'text':'修复了聊天卡片在消息接口失败时一直显示加载的Bug。'}
    decision=admission_decision(query,unrelated,preserve_controller_decision=True)
    assert decision['decision'].startswith('rejected')


@pytest.mark.parametrize('first,second,other',[('Alpha','Beta','Gamma'),('Raven','Lynx','Otter')])
def test_negated_named_subject_does_not_become_positive_weak_ownership(first,second,other):
    query=f'对于“{first} 文件夹迁移”和“{second} Cloud Files”，如何确认别名是否同一项目，避免串线？'
    item={'text':f'{other} Cloud Files 普通目录整理已完成，未涉及 {first} 或 {second} 项目。','metadata':{'semantic_relevance_score':0.99}}
    decision=admission_decision(query,item,deep=True)
    assert decision['decision'].startswith('rejected')


@pytest.mark.parametrize('score',[0.38,0.94,0.99])
def test_failed_actual_delivery_audit_gate_cannot_be_reopened_by_score(score):
    query='检查状态页中 Full Prompt、实际注入和真实链路一致性；实际注入为0条是否正常，不能混淆候选、注入和回答使用。'
    item={'text':'expandIds 参数必须使用 sessionId|obsId 格式定位观察记录，纯 obsId 返回0条。','metadata':{'semantic_relevance_score':score}}
    decision=admission_decision(query,item,deep=True)
    assert decision['injection_audit_alignment']['required'] is True
    assert decision['injection_audit_alignment']['qualified'] is False
    assert decision['decision'].startswith('rejected')


def test_real_same_subject_background_stays_weak_without_claiming_mechanism_proof():
    decision=admission_decision('“Example Router”引入阶段解决了什么问题，验证边界是什么？',{'text':'Example Router 是一个组件名称。'},deep=True)
    assert decision['decision']=='qualified'
    assert decision['relevance_strength']=='weak'
    assert decision['authority_verified'] is False


def test_complete_version_method_evidence_survives_without_semantic_score():
    query='如果同一对象在不同时间、来源和范围出现矛盾，如何决定旧结论 superseded、历史经历保留？说明证据不足时 unresolved。'
    item={'text':'旧状态标记为 superseded 需同一主体与属性、范围相同或重叠、有效时间冲突、新证据更权威，记录替代关系；冲突未决并列保留 unresolved。'}
    decision=admission_decision(query,item,deep=True)
    assert decision['decision']=='qualified'
    assert decision['proposition_alignment']['relation_support'] is True


def test_version_field_background_does_not_become_verified_conflict_method():
    query='如果同一对象不同时间、范围出现矛盾，如何决定旧结论 superseded、哪些历史经历保留？'
    decision=admission_decision(query,{'text':'数据库的 superseded 字段只是删除标记，不涉及时间线、证据或冲突裁决。'},deep=True)
    assert decision['decision']=='qualified'
    assert decision['relevance_strength']=='weak'
    assert decision['proposition_alignment']['relation_support'] is False


@pytest.mark.parametrize('model,score',[('CNN',None),('GNN',0.99)])
def test_excluded_model_definition_does_not_answer_actionable_research_preference(model,score):
    query=f'不要{model}为主的AI，我不擅长；请继续找半冷门论文和可落地实现路径。'
    item={'type':'world','text':f'{model}是一种常用的深度学习模型。'}
    if score is not None:item['metadata']={'semantic_relevance_score':score}
    decision=admission_decision(query,item,deep=True)
    assert decision['decision'].startswith('rejected')


def test_independent_research_background_without_personal_preference_claim_still_allowed():
    query='不要CNN为主的AI，我不擅长；请继续找半冷门论文和可落地实现路径。'
    item={'type':'world','text':'半冷门论文中的实现路径可以先通过原作者代码做小规模实验。'}
    decision=admission_decision(query,item,deep=True)
    assert decision['decision']=='qualified'
    assert decision['explicit_preference_alignment']['candidate_assertion'] is False


def test_current_relevance_settings_can_exclude_weak_background_in_strong_mode():
    from lib.recall_relevance import apply_relevance_policy,resolve_min_relevance
    query='“Example Router”引入阶段解决了什么问题，验证边界是什么？'
    records=[{'id':'background','text':'Example Router 是一个组件名称。'}]
    weak=resolve_min_relevance({'recall_policy':{'default_min_relevance':'weak'}},'user_memory')
    strong=resolve_min_relevance({'recall_policy':{'default_min_relevance':'strong'}},'user_memory')
    assert [row['id'] for row in apply_relevance_policy(query,records,weak)[0]]==['background']
    assert apply_relevance_policy(query,records,strong)[0]==[]
