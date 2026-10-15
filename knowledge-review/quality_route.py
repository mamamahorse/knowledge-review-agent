"""Fixed synthetic route samples; smoke tests wiring, live tests the configured model.

No private notes, answer history, global web app or enrollment is used. Expected
relations remain provisional reference annotations, never injected into prompts.
"""
import argparse
import json
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from learning_store import LearningStore
from learning_types import LearningDraft
from outline_store import OutlineStore
from outline_service import section_manifest, scope_fingerprint
from outline_routes import start_route,run_route,public_routes
from outline_route_store import route_record
from sources import Source
from practice_model import _request_structured


CASES={
    'chain': {
        'text':'# 字节与加法\n\nLEN 表示连续数据字节数；100 加 20 等于 120。\n'
            '## 累计确认\n\nACK 表示下一个期望字节序号；理解此定义的前提是识别字节计数和整数加法。\n'
            '## 计算 ACK\n\n连续数据段不含 SYN/FIN 时 ACK=SEQ+LEN。计算前应理解 ACK 是下一个期望字节，不能将其当成最后一个收到的字节。SEQ=100，LEN=30 时 ACK=130。',
        'titles':['识别字节计数与整数加法','解释 ACK 的下一个期望字节含义','计算不含 SYN/FIN 的连续数据段 ACK'],
        'capabilities':['recall','explain','apply'],
        'expected':{'required':[[0,1],[1,2]],'optional':[[0,2]],'note':'合成原文明确前提链；传递依赖可直接提出，也可只保留相邻边。'},
    },
    'independent': {
        'text':'# 日志\n\n日志级别 INFO 记录一般运行事件；不讨论缓存或摘要计算。\n'
            '## 缓存\n\nCache-X 默认 TTL 为 60 秒；此目标仅回忆配置默认值。\n'
            '## 文件摘要\n\nTool-X 的 hash 命令计算文件 SHA-256；本节不涉及日志或缓存。',
        'titles':['回忆 INFO 日志级别的用途','回忆 Cache-X 默认 TTL','回忆 Tool-X 文件摘要命令'],
        'capabilities':['recall']*3,
        'expected':{'required':[],'optional':[],'note':'章节先后不构成前置关系；固定目标范围没有给出相互依赖。'},
    },
}


def seed_outline(store, case, *, source=None, snapshots=None, collection='root'):
    source=source or Source('synthetic-route-source',Path('route-fixture.md'),case['text'])
    snapshots=snapshots or [{'document_id':'route-fixture','source_id':source.source_id,'path':str(source.path),'name':source.path.name,'text':source.text}]
    sections=section_manifest([source],snapshots)
    batches=[{**s,'section_id':s['id']} for s in sections]
    job=store.create(collection,uuid4().hex,'给审核后的固定学习目标提出有依据的前置关系与学习顺序。','zh',scope_fingerprint(snapshots),snapshots,sections,batches)
    store.start(job);i=0
    while (batch:=store.claim_batch(job)):
        citation={'source_id':source.source_id,'line_start':batch['line_start']+2,'line_end':batch['line_end']}
        statement='\n'.join(source.text.splitlines()[citation['line_start']-1:citation['line_end']])
        draft=LearningDraft.model_validate({'knowledge':{'name':case['titles'][i],'statement':statement,'content_tags':['synthetic'],'citations':[citation]},
            'target':{'title':case['titles'][i],'capability':case['capabilities'][i],'description':case['titles'][i],
                'scope':'本段所述定义或明确的连续数据条件；不扩大到其他版本、SYN/FIN 或未测试能力。',
                'criteria':[case['titles'][i]+'，保留原文所述条件。']}})
        store.finish_batch(batch,[draft],'固定合成候选；没有测试提取质量。',visible=[{'source_id':source.source_id,'line_start':batch['line_start'],'line_end':batch['line_end']}]);i+=1
    store.settle(job)
    return job,lambda _:snapshots


def controlled(messages,schema):
    data=json.loads(messages[1]['content']);ids=[c['id'] for c in data['candidate_navigation']]
    chain='识别字节计数' in data['candidate_navigation'][0]['title']
    comparisons=[]
    for pair in data['comparisons']:
        relationship=chain and (ids.index(pair['a']),ids.index(pair['b'])) in ((0,1),(1,2))
        cites=[{k:e[k] for k in ('source_id','line_start','line_end')} for e in data['original_excerpts']] if relationship else []
        comparisons.append({'pair_index':pair['index'],'relation':'a_before_b' if relationship else 'none',
            'basis':'source' if relationship else 'pedagogical','certainty':'tentative','reason':'受控接口回执；真实语义质量需另行评估。','citations':cites[:4]})
    return schema(comparisons=comparisons,missing_topics=[],note='受控样例，只验证接线与存档。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['smoke','live'],default='smoke')
    parser.add_argument('--case',action='append',choices=list(CASES))
    parser.add_argument('--max-calls',type=int,default=4)
    args=parser.parse_args()
    if not 1<=args.max_calls<=24:parser.error('max-calls must be 1–24')
    if args.mode=='live':
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent/'.env')
    folder=Path(__file__).parent/'.data'/'evaluations'/('route-'+args.mode+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    folder.mkdir(parents=True)
    store=OutlineStore(LearningStore(folder/'practice.sqlite3'));calls=[];results=[]
    def invoke(messages,schema):
        if len(calls)>=args.max_calls:raise RuntimeError('Evaluation logical call limit reached.')
        call={'index':len(calls)+1};calls.append(call)
        try:return (_request_structured if args.mode=='live' else controlled)(messages,schema)
        except (RuntimeError,ValueError,OSError) as exc:
            from quality_source_review import failure_category
            call['failure_category']=failure_category(exc);raise
    for name in args.case or CASES:
        if len(calls)>=args.max_calls:break
        job,current=seed_outline(store,CASES[name]);route=start_route(store,job,'route','zh',None,current,args.max_calls-len(calls))
        with patch('outline_routes._request_structured',side_effect=invoke):run_route(store,route,current)
        public=public_routes(store,job,current)['runs'][0]
        results.append({'case':name,'candidate_inputs':route_record(store,route)['inputs'],'route':public,
            'reference_draft':CASES[name]['expected'],'reference_review_status':'unreviewed','extraction_tested':False})
    with closing(store.practice._connect()) as db:
        counts={t:db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('review_items','practice_sessions','review_events')}
    files=['outline_route_model.py','outline_route_store.py','outline_routes.py','route_recommendation.py','quality_route.py']
    report={'mode':args.mode,'created_at':datetime.now(timezone.utc).isoformat(),'logical_calls':calls,
        'logical_call_limit':args.max_calls,'protocol_note':'Each logical call can include the existing provider compatibility fallback.',
        'semantic_quality_rate':None,'learning_counts':counts,'results':results,
        'code_hashes':{f:sha256((Path(__file__).parent/f).read_bytes()).hexdigest() for f in files}}
    path=folder/'report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(path),'mode':args.mode,'logical_calls':len(calls),'statuses':[x['route']['status'] for x in results],'learning_counts':counts},ensure_ascii=False))


if __name__=='__main__':main()
