"""Paired A/B reports from recorded rankings, without loading any model."""

from statistics import mean
from math import isfinite

from retrieval_benchmark_metrics import score_run, percentile
from retrieval_failure_report import failure_inventory, failure_markdown


def compare_runs(manifest, dense, hybrid, frozen=None, reranked=None):
    if dense['system'] != 'A_dense' or hybrid['system'] != 'B_hybrid':
        raise ValueError('当前对比入口需要A向量和B混合两份运行。')
    if reranked is not None:
        from rerank_retrieval import validate_same_pool
        validate_same_pool(manifest,hybrid,reranked)
    queries = {q['id']: q for q in manifest['suite']['queries']}
    chunks = {c['id']: c for c in manifest['chunks']}
    tables = []
    runs = (dense,hybrid,reranked) if reranked is not None else (dense,hybrid)
    names = ('A','B','C') if reranked is not None else ('A','B')
    for run in runs:
        if run['benchmark_fingerprint'] != manifest['fingerprint']:
            raise ValueError('对比资料、实际分块、模型或题目版本不一致。')
        if run.get('protocol', {}).get('quality_labels_used_during_retrieval') is not False:
            raise ValueError('对比运行必须明确记录召回未使用相关性标签。')
        table = {}
        for row in run['queries']:
            identity = row['query_id']
            if identity in table or identity not in queries or row['scope'] != queries[identity]['scope']:
                raise ValueError('对比查询重复、未知或许可范围变化。')
            if row['status'] not in ('ok', 'failed') or row['status'] == 'failed' and row['hits']:
                raise ValueError('运行状态与实际结果不一致。')
            seen = set()
            for hit in row['hits']:
                c = chunks.get(hit['chunk_id'])
                if not c or c['file'] not in row['scope'] or hit['chunk_id'] in seen:
                    raise ValueError('对比候选重复或越权。')
                if any(hit.get(field) != c[field] for field in ('source_id', 'line_start', 'line_end', 'text')):
                    raise ValueError('对比Trace原文/行号与固定资料不一致。')
                if type(hit.get('score')) not in (int, float) or not isfinite(hit['score']):
                    raise ValueError('对比Trace得分须是有限数值。')
                seen.add(hit['chunk_id'])
            table[identity] = row
        tables.append(table)
    rows = []
    for identity, query in queries.items():
        a, b = [table.get(identity, {'status': 'missing', 'hits': []}) for table in tables[:2]]
        a_ids, b_ids = [[h['chunk_id'] for h in row['hits']] for row in (a, b)]
        rows.append({'query_id': identity, 'split': query['split'], 'category': query['category'],
                     'a_status': a['status'], 'b_status': b['status'],
                     'a_top10': a_ids[:10], 'b_top10': b_ids[:10],
                     'top10_changed': a_ids[:10] != b_ids[:10],
                     'b_only_top10': [i for i in b_ids[:10] if i not in a_ids[:10]],
                     'a_only_top10': [i for i in a_ids[:10] if i not in b_ids[:10]]})
        if reranked is not None:
            c=tables[2][identity]
            c_ids=[h['chunk_id'] for h in c['hits']]
            rows[-1].update(c_status=c['status'],c_top10=c_ids[:10],b_c_top10_changed=b_ids[:10]!=c_ids[:10])
    environment_equal = bool(dense.get('environment')) and dense.get('environment') == hybrid.get('environment')
    repeats_equal = dense.get('protocol', {}).get('repeats') == hybrid.get('protocol', {}).get('repeats')
    latency = {}
    for name, table in zip(names, tables):
        latency[name] = {}
        for split in ('dev', 'test'):
            selected = [table.get(i, {'status': 'missing'}) for i, q in queries.items() if q['split'] == split]
            successful = [r for r in selected if r['status'] == 'ok']
            samples = [t for r in successful for t in r.get('warm_ms', [])]
            if any(type(t) not in (int, float) or not isfinite(t) or t < 0 for t in samples):
                raise ValueError('暖查询时间必须是有限非负数。')
            latency[name][split] = {'sample_count': len(samples),
                                   'successful_queries': len(successful),
                                   'failed_or_missing_queries': len(selected)-len(successful),
                                   'p50_ms': percentile(samples, .5), 'p95_ms': percentile(samples, .95)}
    report = {'schema': 1, 'systems': ['A_dense', 'B_hybrid'], 'benchmark_fingerprint': manifest['fingerprint'],
              'run_ids': {'A': dense.get('id'), 'B': hybrid.get('id')},
              'quality_status': 'frozen' if frozen else 'pending_label_freeze',
              'quality': None, 'per_query': rows, 'latency': latency,
              'ranking_changes': {split: sum(r['top10_changed'] for r in rows if r['split'] == split)
                                  for split in ('dev', 'test')},
              'latency_conditions': {'environment_equal': environment_equal, 'repeats_equal': repeats_equal,
                                    'a_work': dense.get('protocol', {}).get('latency_work', 'production Top10; diagnostic Top50 separate'),
                                    'b_work': hybrid['protocol'].get('latency_work'),
                                    'notice': '时间反映各方案实际工作；A候选诊断另测，B含两路候选和融合。首次引导/建索引不与暖查询混用；设备或重复次数不符时不作延迟差归因。'},
              'notice': '未冻结仅比较排名变化，变化不等于质量改善；本入口尚无配对置信区间或重排收益。'}
    if reranked is not None:
        report['systems'].append('C_rerank')
        report['run_ids']['C']=reranked['id']
        report['latency_conditions'].update(c_environment_equal=hybrid.get('environment')==reranked.get('environment'),
                                            c_work=reranked['protocol'].get('latency_work'))
        report['rerank_ranking_changes']={split:sum(r['b_c_top10_changed'] for r in rows if r['split']==split) for split in ('dev','test')}
        report['latency_estimated_total']={}
        for split in ('dev','test'):
            samples=[t for r in reranked['queries'] if queries[r['query_id']]['split']==split and r['status']=='ok'
                     for t in r.get('warm_end_to_end_estimate_ms',[])]
            report['latency_estimated_total'][split]={'sample_count':len(samples),'p50_ms':percentile(samples,.5),'p95_ms':percentile(samples,.95)}
        report['notice']='C复用绑定的B池；未冻结不出质量分数，C暖时间为纯重排，B+C合计仅估计；暂无配对区间。'
    if frozen is None:
        return report
    a_report, b_report = [score_run(manifest, frozen, run) for run in (dense, hybrid)]
    a_quality = {r['query_id']: r for r in a_report['per_query']}
    b_quality = {r['query_id']: r for r in b_report['per_query']}
    # Both scores use this same immutable freeze; absent/failed queries stay zero.
    delta_rows = [{'query_id': identity, 'split': a_quality[identity]['split'],
                   'family': a_quality[identity]['family'],
                   'delta': {key: b_quality[identity]['metrics'][key] - value
                             for key, value in a_quality[identity]['metrics'].items()}}
                  for identity in a_quality]
    deltas = {}
    for split in ('dev', 'test'):
        selected = [r for r in delta_rows if r['split'] == split]
        deltas[split] = {'query_count': len(selected),
                         'delta_B_minus_A': {key: mean(r['delta'][key] for r in selected)
                                            for key in selected[0]['delta']} if selected else {}}
    report['quality'] = {'A': a_report, 'B': b_report, 'paired_per_query': delta_rows, 'splits': deltas,
                         'notice': '同一冻结标签下的配对点估计；比例差按百分点解读，不是统计显著性结论。'}
    if reranked is not None:
        c_report=score_run(manifest,frozen,reranked)
        report['quality']['C']=c_report
        c_quality={r['query_id']:r for r in c_report['per_query']}
        for row in delta_rows:
            identity=row['query_id']
            row['delta_C_minus_B']={key:c_quality[identity]['metrics'][key]-value for key,value in b_quality[identity]['metrics'].items()}
            row['delta_C_minus_A']={key:c_quality[identity]['metrics'][key]-value for key,value in a_quality[identity]['metrics'].items()}
        for split in ('dev','test'):
            selected=[r for r in delta_rows if r['split']==split]
            for key in ('delta_C_minus_B','delta_C_minus_A'):
                deltas[split][key]={metric:mean(r[key][metric] for r in selected) for metric in selected[0][key]} if selected else {}
    report['failure_inventory'] = failure_inventory(manifest, frozen, report['quality'], dict(zip(names,runs)))
    return report


def comparison_markdown(report):
    """Readable companion to the complete JSON; never fills unapproved metrics."""
    has_c='C_rerank' in report['systems']
    lines = ['# 文档检索 A/B/C 运行对照' if has_c else '# 文档检索 A/B 运行对照', '',
             'A：原向量检索；B：向量 + BM25 + RRF；C：同B候选池Cross-Encoder重排。' if has_c else 'A：原向量检索；B：向量 + BM25 + RRF。本报告未包含C。', '', report['notice'], '',
             '| 分区 | 查询数 | Top10 排名变化数 |', '| --- | ---: | ---: |']
    for split, label in (('dev','开发集'),('test','测试集')):
        count = sum(r['split'] == split for r in report['per_query'])
        lines.append(f"| {label} | {count} | {report['ranking_changes'][split]} |")
    lines += ['', '排名变化数不是改善数；未冻结时不计算相关性质量。', '', '## 延迟解释', '',
              report['latency_conditions']['notice'], '',
              f"设备环境一致：{report['latency_conditions']['environment_equal']}；重复次数一致：{report['latency_conditions']['repeats_equal']}。", '',
              f"A 工作：{report['latency_conditions']['a_work']}", '',
              f"B 工作：{report['latency_conditions']['b_work']}", '',
              '| 系统 / 分区 | 成功 / 失败或缺失查询 | 暖样本数 | P50 ms | P95 ms |',
              '| --- | ---: | ---: | ---: | ---: |']
    for name in (('A','B','C') if has_c else ('A','B')):
        for split, label in (('dev','开发集'),('test','测试集')):
            row = report['latency'][name][split]
            p50 = f"{row['p50_ms']:.2f}" if row['p50_ms'] is not None else 'null'
            p95 = f"{row['p95_ms']:.2f}" if row['p95_ms'] is not None else 'null'
            lines.append(f"| {name} / {label} | {row['successful_queries']} / {row['failed_or_missing_queries']} | {row['sample_count']} | {p50} | {p95} |")
    lines += ['', '时间是本机此次观测，不是稳定性能收益；失败不以零延迟混入。']
    if has_c:
        lines+=['',f"C工作：{report['latency_conditions']['c_work']}",'',
                'C表中仅为实测重排时间。以下是已保存B暖时间加C暖时间的估计，不是一次真实端到端请求：','']
        for split in ('dev','test'):
            row=report['latency_estimated_total'][split]
            lines.append(f"- {split}：P50={row['p50_ms']}ms，P95={row['p95_ms']}ms，估计样本={row['sample_count']}。")
    quality = report['quality']
    if not quality:
        lines += ['## 质量结果', '', '本报告尚未绑定冻结标签，质量结果为 null。指定冻结身份后，重评已保存的 Trace 即可；无需重新编码。']
    else:
        lines += ['## 同一冻结标签的质量结果', '', quality['notice'], '',
                  '标签来源：Agent 初审自动采用与用户确认分别保留，不是全部人工 gold。']
        for split, label in (('dev','开发集'),('test','测试集')):
            lines += ['', f'### {label}', '', '| 指标 | A | B | B−A |', '| --- | ---: | ---: | ---: |']
            if has_c:
                lines[-2:]=['| 指标 | A | B | C | B−A | C−B | C−A |','| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
            a = quality['A']['splits'][split]['metrics']
            b = quality['B']['splits'][split]['metrics']
            for key in a:
                delta = quality['splits'][split]['delta_B_minus_A'][key]
                if has_c:
                    c=quality['C']['splits'][split]['metrics'][key]
                    lines.append(f'| {key} | {a[key]:.5f} | {b[key]:.5f} | {c:.5f} | {delta:+.5f} | {c-b[key]:+.5f} | {c-a[key]:+.5f} |')
                else:
                    lines.append(f'| {key} | {a[key]:.5f} | {b[key]:.5f} | {delta:+.5f} |')
        if has_c:
            lines+=['','## 同池重排诊断@3','',quality['C']['rerank_diagnostics']['notice'],'']
            for split,row in quality['C']['rerank_diagnostics']['splits'].items():
                rescue = '不适用（分母为0）' if row['rescue_rate_at3'] is None else f"{row['rescue_rate_at3']:.2%}"
                regression = '不适用（分母为0）' if row['regression_rate_at3'] is None else f"{row['regression_rate_at3']:.2%}"
                lines.append(f"- {split}：救回 {row['rescued']}/{row['rescue_eligible']}（{rescue}），退化 {row['regressed']}/{row['regression_eligible']}（{regression}），无从救回 {row['unrescuable']}/{row['query_count']}，失败={row['failed_queries']}。")
    if report.get('failure_inventory'):
        lines += failure_markdown(report['failure_inventory'])
    lines += ['', '本阶段未计算配对置信区间或最终回答正确率。', '',
              '## 运行身份', '', f"A：{report['run_ids']['A']}", '', f"B：{report['run_ids']['B']}", '',
              f"资料/分块/模型/问题指纹：{report['benchmark_fingerprint']}", '']
    if has_c:
        lines+=['',f"C：{report['run_ids']['C']}"]
    return '\n'.join(lines)
