"""Public retrieval experiments: unchanged production dense A and scoped hybrid B."""

import argparse
import json
import platform
import sys
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from embedding_config import select_embedding_config
from embeddings import LocalEmbedder
from knowledge_tools import KnowledgeTools
from hybrid_retrieval import BM25Index, HybridConfig, lexical_tokens, reciprocal_rank_fusion
from retrieval import rank_chunks
from retrieval_benchmark_data import ROOT, SUITE, DEFAULT_REVIEW_DB, ReviewStore, load_suite, make_manifest, now, digest
from retrieval_benchmark_metrics import score_run
from retrieval_comparison import compare_runs, comparison_markdown
from rerank_model import LocalReranker, RerankConfig, download_reranker
from rerank_retrieval import run_rerank, validate_hybrid_pool

OUTPUT = ROOT / '.data/evaluations'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each run/report gets a new identity; never overwrite prior evidence.
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


class TimedEmbedder:
    """Observe the production call without changing tokenizer or model semantics."""
    def __init__(self, embedder):
        self.inner = embedder
        self.config = embedder.config
        self.max_tokens = embedder.max_tokens
        self.last_query = None
        self.query_ms = 0
        self.document_ms = 0

    def cache_profile(self):
        return self.inner.cache_profile()

    def prepare_chunks(self, chunks):
        return self.inner.prepare_chunks(chunks)

    def embed_query(self, text):
        start = perf_counter()
        self.last_query = self.inner.embed_query(text)
        self.query_ms = (perf_counter() - start) * 1000
        return self.last_query

    def embed_chunks(self, chunks):
        start = perf_counter()
        result = self.inner.embed_chunks(chunks)
        self.document_ms = (perf_counter() - start) * 1000
        return result


def run_baseline(manifest, sources, embedder, index_path, repeats=3):
    if not 1 <= repeats <= 10:
        raise ValueError('暖查询重复次数须为 1–10。')
    observed, tools_by_scope, records = TimedEmbedder(embedder), {}, []
    for query in manifest['suite']['queries']:
        row = {'query_id': query['id'], 'status': 'failed', 'scope': query['scope'],
               'hits': [], 'warm_ms': [], 'warm_query_embedding_ms': [], 'warm_cosine_and_overhead_ms': []}
        try:
            key = tuple(query['scope'])
            tool = tools_by_scope.get(key)
            if tool is None:
                tool = KnowledgeTools([s for s in sources if s.path.name in key], observed.config,
                                      embedder=observed, index_path=index_path)
                tools_by_scope[key] = tool
            observed.document_ms = 0
            start = perf_counter()
            first = tool.search_notes(query['text'], top_k=10)
            row['first_call'] = {'total_ms': (perf_counter() - start) * 1000,
                                 'query_embedding_ms': observed.query_ms, 'document_embedding_ms': observed.document_ms,
                                 'computed_vectors': first.computed, 'reused_vectors': first.reused,
                                 'memory_cache_hit': first.reused_in_memory}
            expected = [c['id'] for c in manifest['chunks'] if c['file'] in key]
            if [c.chunk_id for c in tool.chunks] != expected:
                raise ValueError('实际检索片段与待审核标注不一致。')
            for _ in range(repeats):
                start = perf_counter()
                result = tool.search_notes(query['text'], top_k=10)
                elapsed = (perf_counter() - start) * 1000
                row['warm_ms'].append(elapsed)
                row['warm_query_embedding_ms'].append(observed.query_ms)
                row['warm_cosine_and_overhead_ms'].append(max(0.0, elapsed - observed.query_ms))
            start = perf_counter()
            # Shadow candidate diagnostics use precisely the same query/doc vectors
            # and ranking implementation. The production tool retains its 10 cap.
            candidates = rank_chunks(tool.chunks, tool._vectors, observed.last_query, top_k=50)
            row['candidate_ranking_ms'] = (perf_counter() - start) * 1000
            if [h.chunk.chunk_id for h in candidates[:10]] != [h.chunk.chunk_id for h in result.hits]:
                raise ValueError('基线候选前十与正式工具结果不一致。')
            row['hits'] = [{'chunk_id': h.chunk.chunk_id, 'score': h.score, 'rank': i + 1,
                            'source_id': h.chunk.source_id, 'line_start': h.chunk.line_start,
                            'line_end': h.chunk.line_end, 'text': h.chunk.text} for i, h in enumerate(candidates)]
            row['status'] = 'ok'
        except Exception as exc:
            # Public synthetic data only. Keep unsuccessful IDs and timings rather
            # than dropping hard questions or publishing partial rankings.
            row['error'] = {'type': type(exc).__name__, 'message': str(exc)}
            row['hits'] = []
        records.append(row)
    return records


def hit_records(hits):
    return [{'chunk_id': h.chunk.chunk_id, 'score': h.score, 'rank': i + 1,
             'source_id': h.chunk.source_id, 'line_start': h.chunk.line_start,
             'line_end': h.chunk.line_end, 'text': h.chunk.text}
            for i, h in enumerate(hits)]


def run_hybrid(manifest, sources, embedder, index_path, repeats=3, config=HybridConfig()):
    """B never reads review labels; both branches share exactly the scoped chunks.

    Bootstrap through the existing tool to reuse its actual preprocessing/cache.
    Warm requests encode the query once and rank each branch to the recorded cap.
    """
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError('暖查询重复次数须为1–10。')
    observed, scopes, records = TimedEmbedder(embedder), {}, []
    for query in manifest['suite']['queries']:
        row = {'query_id': query['id'], 'status': 'failed', 'scope': query['scope'],
               'hits': [], 'dense_hits': [], 'bm25_hits': [], 'warm_ms': [],
               'warm_query_embedding_ms': [], 'warm_dense_ms': [], 'warm_bm25_ms': [], 'warm_rrf_ms': []}
        try:
            key = tuple(query['scope'])
            if key not in scopes:
                scopes[key] = (KnowledgeTools([s for s in sources if s.path.name in key], observed.config,
                                             embedder=observed, index_path=index_path), None)
            tool, lexical = scopes[key]
            observed.document_ms = 0
            start = perf_counter()
            first = tool.search_notes(query['text'], top_k=10)
            bootstrap_ms = (perf_counter() - start) * 1000
            expected = [c for c in manifest['chunks'] if c['file'] in key]
            if [(c.chunk_id, c.source_id, c.line_start, c.line_end, c.text) for c in tool.chunks] != [
                    (c['id'], c['source_id'], c['line_start'], c['line_end'], c['text']) for c in expected]:
                raise ValueError('Hybrid 原文范围与待审核实际片段不一致。')
            build_start = perf_counter()
            if lexical is None:
                lexical = BM25Index(tool.chunks, config)
                scopes[key] = (tool, lexical)
                build_ms = (perf_counter() - build_start) * 1000
                built = True
            else:
                build_ms, built = 0.0, False
            def search(vector):
                dense_start = perf_counter()
                dense = rank_chunks(tool.chunks, tool._vectors, vector, top_k=config.branch_m)
                dense_ms = (perf_counter() - dense_start) * 1000
                lexical_start = perf_counter()
                sparse = lexical.search(query['text'], top_k=config.branch_m)
                lexical_ms = (perf_counter() - lexical_start) * 1000
                fusion_start = perf_counter()
                fused, contributions = reciprocal_rank_fusion(tool.chunks, dense, sparse, config)
                fusion_ms = (perf_counter() - fusion_start) * 1000
                return dense, sparse, fused, contributions, dense_ms, lexical_ms, fusion_ms
            dense, sparse, fused, contributions, dense_ms, sparse_ms, fusion_ms = search(observed.last_query)
            if [h.chunk.chunk_id for h in dense[:10]] != [h.chunk.chunk_id for h in first.hits]:
                raise ValueError('Hybrid 的向量分支前十与现有工具不一致。')
            row['first_call'] = {'total_ms': (perf_counter() - start) * 1000,
                                 'bootstrap_dense_top10_ms': bootstrap_ms,
                                 'query_embedding_ms': observed.query_ms, 'document_embedding_ms': observed.document_ms,
                                 'computed_vectors': first.computed, 'reused_vectors': first.reused,
                                 'memory_cache_hit': first.reused_in_memory, 'bm25_built': built, 'bm25_build_ms': build_ms,
                                 'dense_candidate_ms': dense_ms, 'bm25_search_ms': sparse_ms, 'rrf_ms': fusion_ms,
                                 'notice': '首次观测包含原工具Top10引导调用及额外候选排序；不是纯冷启动横向延迟。'}
            for _ in range(repeats):
                warm_start = perf_counter()
                vector = observed.embed_query(query['text'])
                dense, sparse, fused, contributions, dense_ms, sparse_ms, fusion_ms = search(vector)
                row['warm_ms'].append((perf_counter() - warm_start) * 1000)
                row['warm_query_embedding_ms'].append(observed.query_ms)
                row['warm_dense_ms'].append(dense_ms)
                row['warm_bm25_ms'].append(sparse_ms)
                row['warm_rrf_ms'].append(fusion_ms)
            row['hits'] = [{**hit, 'branches': contributions[hit['chunk_id']]} for hit in hit_records(fused)]
            row['dense_hits'], row['bm25_hits'] = hit_records(dense), hit_records(sparse)
            row['rrf_union'] = contributions  # Includes candidates outside final cap, for future pool auditing.
            row['candidate_union_count'] = len(contributions)
            row['query_tokens'] = list(dict.fromkeys(lexical_tokens(query['text'])))
            row['status'] = 'ok'
        except Exception as exc:
            row['error'] = {'type': type(exc).__name__, 'message': str(exc)}
            row['hits'], row['dense_hits'], row['bm25_hits'] = [], [], []
        records.append(row)
    return records


def code_fingerprint():
    names = ('knowledge_tools.py', 'retrieval.py', 'embeddings.py', 'chunking.py', 'vector_index.py',
             'quality_retrieval.py', 'retrieval_benchmark_metrics.py', 'hybrid_retrieval.py', 'retrieval_comparison.py',
             'rerank_model.py', 'rerank_retrieval.py')
    names += ('retrieval_fixture_audit.py', 'retrieval_failure_report.py')
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地检索A/B/C与固定标签评分；不调用生成模型。')
    parser.add_argument('--review-db', type=Path, default=DEFAULT_REVIEW_DB)
    sub = parser.add_subparsers(dest='command', required=True)
    validator = sub.add_parser('validate')
    preparer = sub.add_parser('prepare')
    baseline = sub.add_parser('baseline')
    baseline.add_argument('--benchmark', required=True)
    baseline.add_argument('--freeze')
    baseline.add_argument('--repeats', type=int, default=3)
    hybrid = sub.add_parser('hybrid')
    hybrid.add_argument('--benchmark', required=True)
    hybrid.add_argument('--freeze')
    hybrid.add_argument('--repeats', type=int, default=3)
    hybrid.add_argument('--bm25-k1', type=float, default=1.2)
    hybrid.add_argument('--bm25-b', type=float, default=.75)
    hybrid.add_argument('--rrf-k', type=float, default=60.0)
    for command in (validator, preparer, baseline, hybrid):
        command.add_argument('--suite', type=Path, help='显式选择样例版本；默认保留原20条。资料及初审位于同目录。')
    scorer = sub.add_parser('score')
    scorer.add_argument('--run', type=Path, required=True)
    scorer.add_argument('--freeze', required=True)
    exporter = sub.add_parser('export')
    exporter.add_argument('--freeze', required=True)
    adopter = sub.add_parser('adopt-audit')
    adopter.add_argument('--benchmark', required=True)
    adopter.add_argument('--audit', type=Path, required=True)
    adopter.add_argument('--expected-agent-revisions', type=Path, help='显式追加确定错误的Agent修正；CAS保护用户决定/待处理项。')
    adopter.add_argument('--resolve-agent-disputes', action='store_true', help='按用户授权，Agent追加判定自己的未决项；不覆盖用户修订。')
    freezer = sub.add_parser('freeze')
    freezer.add_argument('--benchmark', required=True)
    freezer.add_argument('--reviewer', default='Agent本机评审')
    freezer.add_argument('--note', required=True)
    binder = sub.add_parser('bind-section-audit')
    binder.add_argument('--benchmark', required=True)
    binder.add_argument('--sections', type=Path, required=True)
    binder.add_argument('--output', type=Path, required=True)
    binder.add_argument('--corrections', type=Path, help='可选的显式语义等级修正台账；不从排名推断。')
    comparison = sub.add_parser('compare')
    comparison.add_argument('--a-run', type=Path, required=True)
    comparison.add_argument('--b-run', type=Path, required=True)
    comparison.add_argument('--freeze')
    comparison.add_argument('--c-run', type=Path)
    sub.add_parser('download-reranker')
    rerank = sub.add_parser('rerank')
    rerank.add_argument('--b-run', type=Path, required=True)
    rerank.add_argument('--freeze')
    rerank.add_argument('--repeats', type=int, default=3)
    rerank.add_argument('--max-tokens', type=int, default=512)
    rerank.add_argument('--batch-size', type=int, default=8)
    rerank.add_argument('--max-windows', type=int, default=16)
    rerank.add_argument('--max-pairs', type=int, default=1024)
    args = parser.parse_args(argv)
    hybrid_config = HybridConfig(k1=args.bm25_k1, b=args.bm25_b, rrf_k=args.rrf_k) if args.command == 'hybrid' else None
    if args.command == 'download-reranker':
        print(json.dumps({'model_dir':str(download_reranker()),'status':'downloaded'},ensure_ascii=False))
        return
    rerank_config = RerankConfig(max_tokens=args.max_tokens,batch_size=args.batch_size,
                                  max_windows=args.max_windows,max_pairs=args.max_pairs) if args.command == 'rerank' else None
    if args.command == 'validate':
        suite, sources = load_suite(args.suite) if args.suite else load_suite()
        print(json.dumps({'sources': len(sources), 'queries': len(suite['queries']), 'status': 'draft_not_gold'}, ensure_ascii=False))
        return
    store = ReviewStore(args.review_db)
    if args.command == 'freeze':
        manifest = store.manifest(args.benchmark)
        revisions = {q['id']:store.review(args.benchmark,q['id'])['revision'] for q in manifest['suite']['queries']}
        frozen = store.freeze(args.benchmark,args.reviewer,args.note,revisions)
        print(json.dumps({'freeze':frozen['id'],'fingerprint':frozen['fingerprint']},ensure_ascii=False))
        return
    if args.command == 'bind-section-audit':
        from retrieval_fixture_audit import bind_section_audit
        corrections = json.loads(args.corrections.read_text(encoding='utf-8')) if args.corrections else None
        audit = bind_section_audit(store.manifest(args.benchmark), json.loads(args.sections.read_text(encoding='utf-8')), corrections)
        write_json(args.output, audit)
        print(json.dumps({'audit':str(args.output),'status':'bound_not_adopted'},ensure_ascii=False))
        return
    if args.command == 'rerank':
        hybrid = json.loads(args.b_run.read_text(encoding='utf-8'))
        manifest = store.manifest(hybrid['benchmark_id'])
        validate_hybrid_pool(manifest,hybrid)
        frozen = store.frozen(args.freeze) if args.freeze else None
        if frozen and frozen['benchmark_fingerprint'] != manifest['fingerprint']:
            raise ValueError('冻结标签与B候选版本不一致。')
        start = perf_counter()
        ranker = LocalReranker(rerank_config)
        load_ms = (perf_counter()-start)*1000
        start = perf_counter()
        profile = ranker.profile()
        profile_ms = (perf_counter()-start)*1000
        records = run_rerank(manifest,hybrid,ranker,args.repeats,
                             progress=lambda row:print(f"rerank {row['query_id']}: {row['status']}",file=sys.stderr,flush=True))
        import torch
        run = {'schema':1,'id':uuid4().hex,'created_at':now(),'system':'C_rerank',
               'benchmark_id':hybrid['benchmark_id'],'benchmark_fingerprint':manifest['fingerprint'],
               'input_b_run_digest':digest(hybrid),
               'input_b_run_id':hybrid['id'],'input_b_run_path':str(args.b_run.resolve()),'input_b_run':hybrid,
               'reranker':profile,'model_load_ms':load_ms,'model_fingerprint_ms':profile_ms,'code':code_fingerprint(),
               'environment':{'platform':platform.platform(),'processor':platform.processor(),
                              'python':platform.python_version(),'device':'cpu','torch_threads':torch.get_num_threads()},
               'protocol':{'repeats':args.repeats,'production_k':10,'candidate_m':hybrid['protocol']['candidate_m'],
                           'quality_labels_used_during_retrieval':False,'input_pool':'exact recorded B topM, immutable by digest',
                           'latency_work':'rerank only on saved B pool; end-to-end sums explicitly estimated, not measured'},
               'queries':records,'quality_status':'frozen' if frozen else 'pending_label_freeze','quality':None}
        if frozen:
            run['quality'] = score_run(manifest,frozen,run)
        target = OUTPUT/f'retrieval-rerank-{run["id"]}.json'
        write_json(target,run)
        print(json.dumps({'run':str(target),'query_count':len(records),
                          'failure_count':sum(r['status']!='ok' for r in records),'quality_status':run['quality_status']},ensure_ascii=False))
        return
    if args.command == 'compare':
        dense = json.loads(args.a_run.read_text(encoding='utf-8'))
        hybrid = json.loads(args.b_run.read_text(encoding='utf-8'))
        manifest = store.manifest(dense['benchmark_id'])
        frozen = store.frozen(args.freeze) if args.freeze else None
        reranked = json.loads(args.c_run.read_text(encoding='utf-8')) if args.c_run else None
        report = compare_runs(manifest, dense, hybrid, frozen, reranked)
        report.update(created_at=now(), code=code_fingerprint())
        write_json(target := OUTPUT / f'retrieval-comparison-{uuid4().hex}.json', report)
        with target.with_suffix('.md').open('x', encoding='utf-8') as stream:
            stream.write(comparison_markdown(report))
        print(str(target))
        return
    if args.command == 'adopt-audit':
        audit = json.loads(args.audit.read_text(encoding='utf-8'))
        revisions = json.loads(args.expected_agent_revisions.read_text(encoding='utf-8')) if args.expected_agent_revisions else None
        print(json.dumps(store.adopt_agent_audit(args.benchmark, audit, revisions,
                          resolve_agent_disputes=args.resolve_agent_disputes), ensure_ascii=False))
        return
    if args.command == 'export':
        frozen = store.frozen(args.freeze)
        manifest = store.manifest(frozen['benchmark_id'])
        target = OUTPUT / f'retrieval-frozen-{args.freeze}-{uuid4().hex[:8]}'
        target.mkdir(parents=True, exist_ok=False)
        write_json(target / 'manifest.json', manifest)
        write_json(target / 'labels.json', frozen)
        with (target / 'corpus.jsonl').open('x', encoding='utf-8') as stream:
            for chunk in manifest['chunks']:
                stream.write(json.dumps({'_id': chunk['id'], 'title': chunk['file'], 'text': chunk['text'],
                                         'metadata': {'source_id': chunk['source_id'], 'line_start': chunk['line_start'],
                                                      'line_end': chunk['line_end']}}, ensure_ascii=False) + '\n')
        cases = {c['query_id']: c for c in frozen['cases']}
        with (target / 'queries.jsonl').open('x', encoding='utf-8') as stream:
            for query in manifest['suite']['queries']:
                case = cases[query['id']]
                if case['status'] in ('approved', 'auto_accepted') and case['answerable']:
                    stream.write(json.dumps({'_id': query['id'], 'text': query['text'],
                                             'metadata': {'scope': query['scope'], 'family': query['family']}}, ensure_ascii=False) + '\n')
        (target / 'qrels').mkdir()
        for split in ('dev', 'test'):
            with (target / 'qrels' / f'{split}.tsv').open('x', encoding='utf-8') as stream:
                stream.write('query-id\tcorpus-id\tscore\n')
                for query in manifest['suite']['queries']:
                    case = cases[query['id']]
                    if query['split'] == split and case['status'] in ('approved', 'auto_accepted') and case['answerable']:
                        for judgment in case['judgments']:
                            if judgment['grade'] > 0:
                                stream.write(f'{query["id"]}\t{judgment["chunk_id"]}\t{judgment["grade"]}\n')
        print(str(target))
        return
    if args.command == 'score':
        run = json.loads(args.run.read_text(encoding='utf-8'))
        frozen = store.frozen(args.freeze)
        report = score_run(store.manifest(frozen['benchmark_id']), frozen, run)
        report.update(created_at=now(), code=code_fingerprint())
        target = args.run.parent / f'scored-{args.freeze}-{uuid4().hex[:8]}.json'
        write_json(target, report)
        print(str(target))
        return
    suite, sources = load_suite(args.suite) if args.suite else load_suite()
    start = perf_counter()
    embedder = LocalEmbedder(select_embedding_config(), allow_download=False)
    load_ms = (perf_counter() - start) * 1000
    start = perf_counter()
    manifest = make_manifest(suite, sources, embedder)
    manifest_ms = (perf_counter() - start) * 1000
    if args.command == 'prepare':
        identity = store.create(manifest)
        # Materialize/check every initial mapping before giving user a review link.
        for query in suite['queries']:
            store.review(identity, query['id'])
        audit_path = args.suite.resolve().parent / 'agent-review.json' if args.suite else ROOT / 'evals/retrieval/agent-review.json'
        if audit_path.is_file():
            audit = json.loads(audit_path.read_text(encoding='utf-8'))
            if audit['benchmark_fingerprint'] == manifest['fingerprint']:
                store.adopt_agent_audit(identity, audit)
        reviews = [store.review(identity, q['id'])['payload'] for q in suite['queries']]
        print(json.dumps({'benchmark': identity, 'chunks': len(manifest['chunks']), 'queries': len(suite['queries']),
                          'review_url': f'/retrieval-review?benchmark={identity}',
                          'needs_user': sum(r['status']=='pending' for r in reviews),
                          'auto_accepted': sum(r['status']=='auto_accepted' for r in reviews),
                          'status': 'exceptions_only' if any(r.get('audit_id') for r in reviews) else 'needs_full_agent_audit'}, ensure_ascii=False))
        return
    if store.manifest(args.benchmark)['fingerprint'] != manifest['fingerprint']:
        raise ValueError('当前资料/分块/模型指纹已变化，请创建新的标注集，不能套旧标签。')
    frozen = store.frozen(args.freeze) if args.freeze else None
    if frozen and frozen['benchmark_id'] != args.benchmark:
        raise ValueError('冻结版本属于其他标注集。')
    system = 'B_hybrid' if hybrid_config else 'A_dense'
    if hybrid_config:
        records = run_hybrid(manifest, sources, embedder, args.review_db.parent / 'vectors.sqlite3', args.repeats, hybrid_config)
    else:
        records = run_baseline(manifest, sources, embedder, args.review_db.parent / 'vectors.sqlite3', args.repeats)
    import torch
    run = {'schema': 1, 'id': uuid4().hex, 'created_at': now(), 'system': system,
           'benchmark_id': args.benchmark, 'benchmark_fingerprint': manifest['fingerprint'],
           'code': code_fingerprint(), 'environment': {'platform': platform.platform(), 'processor': platform.processor(),
                       'python': platform.python_version(), 'device': 'cpu', 'torch_threads': torch.get_num_threads()},
           'protocol': {'repeats': args.repeats, 'production_k': 10, 'candidate_m': 50,
                        'candidate_notice': '每路最多50，去重后按RRF取最多50；向量前十与原工具一致，正式工具保持原上限。' if hybrid_config else '候选排名另测；生产工具前十逐查询验证；不改变正式搜索上限。',
                        'latency_work': 'query embedding + dense50 + BM25<=50 + RRF top50; Top10 is prefix' if hybrid_config else 'production query embedding + dense Top10; diagnostic dense50 time separate',
                        'hybrid': hybrid_config.protocol() if hybrid_config else None,
                        'quality_labels_used_during_retrieval': False},
           'model_load_ms': load_ms, 'manifest_and_model_fingerprint_ms': manifest_ms,
           'queries': records, 'quality_status': 'frozen' if frozen else 'pending_label_freeze',
           'quality': score_run(manifest, frozen, {'system': system, 'benchmark_fingerprint': manifest['fingerprint'], 'queries': records}) if frozen else None}
    prefix = 'hybrid' if hybrid_config else 'baseline'
    target = OUTPUT / f'retrieval-{prefix}-{run["id"]}.json'
    write_json(target, run)
    print(json.dumps({'run': str(target), 'query_count': len(records), 'failure_count': sum(r['status'] != 'ok' for r in records),
                      'quality_status': run['quality_status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
