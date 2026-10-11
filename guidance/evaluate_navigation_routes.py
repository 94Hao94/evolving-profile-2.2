"""Small disclosed navigation regression set; not a blind answer benchmark."""
import json
from pathlib import Path
from bank_hierarchy import model_json
from observation_rebuild import load_env,atomic


CASES=[
    ('family','以前提到过的家人关系和个人背景，应该去哪里找？',['家庭','身份','个人']),
    ('health','之前咨询嗓音不舒服、药品和食品的记录在哪？',['医疗','健康','药品']),
    ('hardware','回顾本地部署大模型时，GPU和显存如何选型？',['硬件','算力','部署']),
    ('school','之前天津高校智慧教学项目的招投标和交付记录在哪里？',['高校','招投标','教育']),
    ('learning','我的因果理解、复习方法和学习模型有过哪些讨论？',['学习','认知','复习']),
    ('documents','查一下以前做PPT和Word文档交付时的排版、质量检查规范。',['文档','PPT','工作流']),
]


def main():
    snapshot=json.loads((Path.home()/'.evolving-profile/catalog/corpus-navigation.json').read_text())
    topics=snapshot['topics'];roots=[r for r in topics if r['level']=='L0'];by_id={r['topic_id']:r for r in topics}
    brief=[{'id':r['topic_id'],'title':r['title'],'summary':r['navigation_summary']} for r in roots]
    response,cost=model_json(load_env(),'''只根据给定L0地图给每个问题选择最多2个有用入口，不回答问题本身。没有足够线索时输出unknown，不臆造ID。返回JSON {"routes":[{"case":"问题id","domains":["入口id"]}]}。''',
                            {'map':brief,'questions':[{'id':i,'question':q} for i,q,_ in CASES]},1800)
    decisions={r['case']:r.get('domains',[]) for r in response['routes']}
    stage2=[]
    for identity,query,_ in CASES:
        ids={c for r in decisions.get(identity,[]) if r in by_id for c in by_id[r].get('children',[])}
        stage2.append({'case':identity,'question':query,'L1':[{'id':c,'title':by_id[c]['title'],'summary':by_id[c]['navigation_summary']} for c in sorted(ids) if c in by_id]})
    second,cost2=model_json(load_env(),'''每个case从给定L1中选择最多2个最适合查询的主题。只返回JSON {"routes":[{"case":"输入case","topics":["给定L1 id"]}]}。不回答事实。''',stage2,2000)
    selected={r['case']:r.get('topics',[]) for r in second['routes']}
    results=[]
    for identity,query,hints in CASES:
        leaves=[by_id[v] for v in selected.get(identity,[]) if v in by_id and by_id[v]['level']=='L1']
        valid_targets={v['id'] for case in stage2 if case['case']==identity for v in case['L1']}
        route_valid=bool(leaves) and all(v['topic_id'] in valid_targets for v in leaves)
        relevant=any(any(h in (v['title']+v['navigation_summary']) for h in hints) for v in leaves)
        results.append({'case':identity,'query':query,'l0':[by_id[v]['title'] for v in decisions.get(identity,[]) if v in by_id],
                        'l1':[v['title'] for v in leaves],'source_refs':sum(len(v['source_locators']) for v in leaves),
                        'valid_route':route_valid,'disclosed_topic_check':relevant,'sources':[r for v in leaves for r in v['source_locators'][:1]]})
    report={'scope':'six_disclosed_navigation_cases; model-selected_routes; not fact-answer_accuracy_or_blind_test',
            'cases':results,'valid':sum(r['valid_route'] and r['disclosed_topic_check'] and r['source_refs']>0 for r in results),'total':len(results),'model_usage':[cost,cost2]}
    atomic(Path('/Users/apple/Documents/Codex/2026-09-09/hind/outputs/L0-L1-L2-navigation-route-eval-20260920.json'),report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if report['valid']!=report['total']:raise SystemExit(1)


if __name__=='__main__':main()
