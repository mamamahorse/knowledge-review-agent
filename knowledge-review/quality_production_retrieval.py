"""Reproduce saved B through the production tool; optional current-model QA smoke.

Only public synthetic fixtures, isolated vector cache, no learning/chat writes.
Parity checks ranking equivalence, not a second relevance benchmark.
"""
import argparse
import json
from math import isclose
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from hashlib import sha256

from agent_loop import run_agent
from embedding_config import select_embedding_config
from embeddings import LocalEmbedder
from knowledge_tools import KnowledgeTools
from quality_retrieval import OUTPUT, code_fingerprint, write_json
from retrieval_benchmark_data import DEFAULT_REVIEW_DB, ROOT, ReviewStore, load_suite, make_manifest, now
from rerank_retrieval import validate_hybrid_pool
from sources import Source


def main(argv=None):
    parser=argparse.ArgumentParser(description='正式工具与已保存B逐查询核对；--live-agent显式调用当前生成服务。')
    parser.add_argument('--b-run',type=Path,required=True)
    parser.add_argument('--suite',type=Path,default=ROOT/'evals/retrieval/expanded-v1/suite.json')
    parser.add_argument('--review-db',type=Path,default=DEFAULT_REVIEW_DB)
    parser.add_argument('--live-agent',action='store_true')
    parser.add_argument('--agent-only',action='store_true',help='仅复查当前模型样例，不重复排名核对；须同时--live-agent。')
    parser.add_argument('--cases',nargs='+',choices=('greeting','supported','missing'),help='只复测选定样例，仍保留每次独立报告。')
    args=parser.parse_args(argv)
    if args.agent_only and not args.live_agent:parser.error('--agent-only requires --live-agent')
    import torch
    torch.set_num_threads(8)
    saved=json.loads(args.b_run.read_text(encoding='utf-8'))
    manifest=ReviewStore(args.review_db).manifest(saved['benchmark_id'])
    validate_hybrid_pool(manifest,saved)
    suite,sources=load_suite(args.suite)
    embedder=LocalEmbedder(select_embedding_config(),allow_download=False)
    if make_manifest(suite,sources,embedder)['fingerprint']!=manifest['fingerprint']:
        raise ValueError('公开资料、模型或实际分块版本与保存B不同，不能核对。')
    rows,tools_by_scope=[],{}
    expected={r['query_id']:r for r in saved['queries']}
    for q in ([] if args.agent_only else suite['queries']):
        row={'query_id':q['id'],'status':'failed'}
        try:
            key=tuple(q['scope'])
            if key not in tools_by_scope:
                tools_by_scope[key]=KnowledgeTools([s for s in sources if s.path.name in key],embedder.config,
                    embedder=embedder,index_path=args.review_db.parent/'vectors.sqlite3',retrieval_mode='hybrid')
            tool=tools_by_scope[key]
            result=tool.search_notes(q['text'],10)
            target=expected[q['id']]
            if target['status']!='ok' or [h.chunk.chunk_id for h in result.hits]!=[h['chunk_id'] for h in target['hits'][:10]]:
                raise ValueError('正式前十与保存B不同。')
            if any(not isclose(h.score,t['score'],rel_tol=1e-12,abs_tol=1e-12) for h,t in zip(result.hits,target['hits'])):
                raise ValueError('RRF得分与保存B不同。')
            row.update(status='matched',trace=tool.retrieval_trace[-1])
        except Exception as exc:
            row['error_type']=type(exc).__name__
        rows.append(row)
    report={'id':uuid4().hex,'created_at':now(),'kind':'production_B_parity','benchmark_fingerprint':manifest['fingerprint'],
            'b_run_id':saved['id'],'code':code_fingerprint(),
            'production_code':{n:sha256((ROOT/n).read_bytes()).hexdigest() for n in
                               ('quality_production_retrieval.py','web_app.py','agent_loop.py','model_client.py','generation_budget.py','conversation_store.py')},
            'rows':rows,'agent_samples':[],'parity_rerun':not args.agent_only,
            'notice':'真实本地工具排名一致性检查，沿用冻结实验而非新的质量提升；不写正式学习/聊天。'}
    if args.live_agent:
        # The selection is deliberate smoke input, not production middleware.
        source=Source('public-hybrid-smoke-v1',ROOT/'evals/retrieval/expanded-v1/public-smoke.md',
                      '# 幂等协议\n\n相同job_key与payload_hash的重试返回原job_id，不再插入第二份作业。\n\n'
                      '# 冲突正文\n\n同一job_key但payload_hash不同则返回KEY_PAYLOAD_CONFLICT，原正文保持。\n\n'
                      '# 删除墓碑\n\n删除后的旧job_key再次提交返回JOB_DELETED。本文没有规定墓碑自动过期天数。')
        cases=[('greeting','Hello!','en',False),
               ('supported','请用search_notes查找job_key与payload_hash的重试和冲突规则，然后分别解释结果。','zh',True),
               ('missing','请查找资料是否规定删除墓碑的自动过期天数，只依据原文说明。','zh',True)]
        for identity,question,language,strict in cases:
            if args.cases and identity not in args.cases:continue
            tool=KnowledgeTools([source],embedder.config,embedder=embedder,retrieval_mode='hybrid',
                                index_path=args.review_db.parent/'vectors.sqlite3',min_chars=0)
            sample={'id':identity,'question':question,'language':language,'status':'failed'}
            start=perf_counter()
            try:
                run=run_agent([source],question,tool,strict=strict,response_language=language)
                sample.update(status='ok',answer=run.answer.model_dump(),
                              steps=[{'name':s.name,'arguments':s.arguments,'error':s.error} for s in run.steps],
                              context=run.context_snapshot,searches=tool.retrieval_trace)
            except Exception as exc:
                sample['error_type']=type(exc).__name__
                sample['searches']=tool.retrieval_trace
                chain=[];current=exc
                while current is not None:
                    chain.append(type(current).__name__);current=current.__cause__
                sample['error_chain']=chain
                message=str(exc)
                sample['error_category']=next((category for fragment,category in (
                    ('outside','citation_range'),('coverage','coverage_contract'),('citations','citation_contract'),
                    ('required','output_contract'),('truncated','output_truncated'),('model request failed','model_transport'))
                    if fragment in message),'unclassified')
            sample['agent_and_final_ms']=(perf_counter()-start)*1000
            report['agent_samples'].append(sample)
    target=OUTPUT/f'production-hybrid-{report["id"]}.json'
    write_json(target,report)
    print(json.dumps({'report':str(target),'queries':len(rows),'matched':sum(r['status']=='matched' for r in rows),
                      'agent_samples':[{k:s[k] for k in ('id','status')} for s in report['agent_samples']]},ensure_ascii=False))


if __name__=='__main__':main()
