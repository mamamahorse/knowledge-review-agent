"""BEIR on immutable mixed-origin labels; failures remain in denominators."""

from importlib.metadata import version
from math import isfinite
from statistics import mean

from retrieval_benchmark_data import digest, validate_review, ReviewStore

K_VALUES = [1, 3, 5, 10, 20, 50]


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def score_run(manifest, frozen, run):
    # Metrics never consume current editable review rows.
    if frozen['benchmark_fingerprint'] != manifest['fingerprint'] or run['benchmark_fingerprint'] != manifest['fingerprint']:
        raise ValueError('资料、分块、模型或题目版本与冻结标注不一致，不能比较。')
    if frozen['fingerprint'] != digest({k: v for k, v in frozen.items() if k not in ('id', 'fingerprint')}):
        raise ValueError('冻结标注指纹不一致。')
    if run['system'] == 'C_rerank':
        from rerank_retrieval import validate_same_pool
        validate_same_pool(manifest, run['input_b_run'], run)
    expected = {q['id'] for q in manifest['suite']['queries']}
    if len(frozen['cases']) != len(expected) or {c['query_id'] for c in frozen['cases']} != expected:
        raise ValueError('冻结标注未覆盖完整查询集。')
    queries = {q['id']: q for q in manifest['suite']['queries']}
    if len(run['queries']) != len({r['query_id'] for r in run['queries']}) or any(r['query_id'] not in queries for r in run['queries']):
        raise ValueError('运行记录包含重复或未知查询。')
    rankings = {}
    for row in run['queries']:
        query = queries[row['query_id']]
        allowed = {c['id'] for c in manifest['chunks'] if c['file'] in query['scope']}
        hits = row['hits']
        ids = [h['chunk_id'] for h in hits]
        if len(ids) != len(set(ids)) or not set(ids) <= allowed or any(type(h['score']) not in (int, float) or not isfinite(h['score']) for h in hits):
            raise ValueError('检索记录包含重复、越权或非法得分。')
        if row['status'] not in ('ok', 'failed') or (row['status'] == 'failed' and hits):
            raise ValueError('失败查询不能保留成功排名。')
        rankings[row['query_id']] = row
    try:
        from beir.retrieval.evaluation import EvaluateRetrieval
    except ImportError as exc:
        raise RuntimeError('请先安装 requirements-eval.txt；禁止用模拟指标冒充 BEIR。') from exc
    details, excluded, negatives = [], [], []
    for case in frozen['cases']:
        query = queries[case['query_id']]
        validate_review(manifest, query, case)
        if case['status'] == 'auto_accepted':
            audit = frozen.get('agent_audits', {}).get(case['audit_id'])
            if not audit or digest(audit) != case['audit_id'] or audit['benchmark_fingerprint'] != manifest['fingerprint']:
                raise ValueError('自动采用的标注缺少同版本完整初审快照。')
            audit_case = next((c for c in audit['cases'] if c['query_id'] == case['query_id']), None)
            if not audit_case or audit_case['decision'] != 'confident' or any(case[field] != audit_case['review'][field] for field in ('answerable', 'judgments', 'groups')):
                raise ValueError('自动采用标注与明确的初审决定不一致。')
        if case['status'] == 'excluded':
            excluded.append({'query_id': query['id'], 'reason': case['note']})
            continue
        if case['status'] not in ('approved', 'auto_accepted'):
            raise ValueError('冻结版本存在待审核样例。')
        row = rankings.get(query['id'], {'status': 'missing', 'hits': []})
        ids = [h['chunk_id'] for h in row['hits']]
        common = {'query_id': query['id'], 'split': query['split'], 'category': query['category'],
                  'family': query['family'], 'status': row['status'], 'returned_candidates': len(ids),
                  'label_origin': case.get('origin', 'user'), 'label_status': case['status']}
        if not case['answerable']:
            negatives.append({**common, 'notice': '检索返回相似片段不代表可回答；本阶段未评测回答拒答能力。'})
            continue
        qrels = {query['id']: {j['chunk_id']: j['grade'] for j in case['judgments']}}
        # BEIR/trec resolves equal scores differently from production's stable sort.
        # Encode the already-recorded rank with unique decreasing scores for metrics;
        # raw cosine scores remain unchanged in trace. Never re-sort or invent hits.
        results = {query['id']: {identity: float(len(ids) - i) for i, identity in enumerate(ids)}}
        if ids:
            ndcg, _, recall, _ = EvaluateRetrieval.evaluate(qrels, results, K_VALUES, ignore_identical_ids=False)
            mrr = EvaluateRetrieval.evaluate_custom(qrels, results, [10], metric='mrr')
        else:
            ndcg = {f'NDCG@{k}': 0.0 for k in K_VALUES}
            recall = {f'Recall@{k}': 0.0 for k in K_VALUES}
            mrr = {'MRR@10': 0.0}
        metrics = {**{f'Recall@{k}': recall[f'Recall@{k}'] for k in (1, 3, 5, 10)},
                   **{f'nDCG@{k}': ndcg[f'NDCG@{k}'] for k in (5, 10)}, **mrr}
        positives = {j['chunk_id'] for j in case['judgments'] if j['grade'] > 0}
        for k in (1, 3, 5, 10):
            selected = set(ids[:k])
            metrics[f'Hit@{k}'] = float(bool(selected & positives))
            metrics[f'CompleteEvidenceHit@{k}'] = float(all(
                any(set(bundle) <= selected for bundle in group['bundles']) for group in case['groups']))
        candidate_ids = [h['chunk_id'] for h in row.get('input_pool',row['hits'])] if run['system']=='C_rerank' else ids
        candidate_recall = recall
        if run['system']=='C_rerank':
            candidate_results = {query['id']:{identity:float(len(candidate_ids)-i) for i,identity in enumerate(candidate_ids)}}
            if candidate_ids:
                _,_,candidate_recall,_ = EvaluateRetrieval.evaluate(qrels,candidate_results,K_VALUES,ignore_identical_ids=False)
            else:
                candidate_recall = {f'Recall@{k}':0.0 for k in K_VALUES}
        for m in (20, 50):
            # Candidate availability is measured before reranking, so C cannot
            # claim better retrieval by promoting rank-40 into its top-20.
            metrics[f'CandidateRecall@{m}'] = candidate_recall[f'Recall@{m}']
            metrics[f'CandidateHit@{m}'] = float(bool(set(candidate_ids[:m]) & positives))
            metrics[f'Unrescuable@{m}'] = 1.0 - metrics[f'CandidateHit@{m}']
        details.append({**common, 'metrics': metrics})
    def aggregate(rows):
        return {'query_count': len(rows), 'metrics': {key: mean(r['metrics'][key] for r in rows)
                for key in rows[0]['metrics']} if rows else {}}
    latency = {}
    for split in ('dev', 'test'):
        samples = [value for r in run['queries'] if queries[r['query_id']]['split'] == split and r['status'] == 'ok'
                   for value in r.get('warm_ms', [])]
        latency[split] = {'sample_count': len(samples), 'p50_ms': percentile(samples, .5), 'p95_ms': percentile(samples, .95),
                          'notice': '成功的暖查询时间；失败数另列，不以零延迟混入。'}
    diagnostics = None
    if run['system']=='C_rerank':
        events = []
        for detail in details:
            identity = detail['query_id']
            case = next(c for c in frozen['cases'] if c['query_id']==identity)
            positives = {j['chunk_id'] for j in case['judgments'] if j['grade']>0}
            row = rankings[identity]
            before = [h['chunk_id'] for h in row['input_pool']]
            after = [h['chunk_id'] for h in row['hits']]
            events.append({'query_id':identity,'split':detail['split'],'status':row['status'],
                           **rerank_diagnostics(before,after,before,positives)})
        def rates(selected):
            rescue_den = sum(e['rescue_eligible'] for e in selected)
            regression_den = sum(e['regression_eligible'] for e in selected)
            rescued = sum(e['rescued'] for e in selected)
            regressed = sum(e['regressed'] for e in selected)
            return {'query_count':len(selected),'rescued':rescued,'rescue_eligible':rescue_den,
                    'rescue_rate_at3':rescued/rescue_den if rescue_den else None,
                    'regressed':regressed,'regression_eligible':regression_den,
                    'regression_rate_at3':regressed/regression_den if regression_den else None,
                    'unrescuable':sum(e['unrescuable'] for e in selected),
                    'unrescuable_rate':sum(e['unrescuable'] for e in selected)/len(selected) if selected else None,
                    'failed_queries':sum(e['status']!='ok' for e in selected)}
        diagnostics = {'k':3,'events':events,'splits':{split:rates([e for e in events if e['split']==split]) for split in ('dev','test')},
                       'notice':'同B池排序，分母0为null；失败继续在分母，退化也可能包含执行失败。无答案/排除不参与。'}
    return {'schema': 1, 'system': run['system'], 'freeze_id': frozen['id'], 'freeze_fingerprint': frozen['fingerprint'],
            'label_provenance': {'agent_auto_accepted': sum(c['status'] == 'auto_accepted' for c in frozen['cases']),
                                 'user_confirmed': sum(c['status'] == 'approved' for c in frozen['cases']),
                                 'excluded': sum(c['status'] == 'excluded' for c in frozen['cases']),
                                 'notice': '自动采用的标签由 Agent 初审，不能描述为逐条人工审核 gold。'},
            'backend': {'beir': version('beir'), 'pytrec_eval': version('pytrec-eval-terrier'),
                        'gain': 'BEIR/trec linear relevance gains; rank-preserving tie normalization'},
            'splits': {split: aggregate([r for r in details if r['split'] == split]) for split in ('dev', 'test')},
            'by_category': {category: aggregate([r for r in details if r['split'] == 'test' and r['category'] == category])
                            for category in sorted({r['category'] for r in details if r['split'] == 'test'})},
            'per_query': details, 'negative_queries': negatives, 'excluded': excluded, 'latency': latency,
            'failure_count': sum(r['status'] != 'ok' for r in details + negatives),
            'rerank_diagnostics': diagnostics, 'notice': 'C按固定B候选池重排；A/B无重排收益指标。无答案不计算相关性均值，候选指标以重排前池计。'}


def rerank_diagnostics(before, after, candidate_ids, positives, k=3):
    """Per-query events; aggregate rates must divide by eligible-event counts."""
    pre, post = bool(set(before[:k]) & positives), bool(set(after[:k]) & positives)
    available = bool(set(candidate_ids) & positives)
    if not set(after) <= set(candidate_ids):
        raise ValueError('重排不能添加候选池之外的片段。')
    return {'rescue_eligible': available and not pre, 'rescued': available and not pre and post,
            'regression_eligible': pre, 'regressed': pre and not post, 'unrescuable': not available}
