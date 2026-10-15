"""Evidence-grounded failure inventory after compare_runs validates frozen data."""


def failure_inventory(manifest, frozen, quality, runs):
    chunks = {c['id']:c for c in manifest['chunks']}
    labels = {c['query_id']:c for c in frozen['cases']}
    queries = {q['id']:q for q in manifest['suite']['queries']}
    tables = {name:{r['query_id']:r for r in run['queries']} for name,run in runs.items()}
    scores = {name:{r['query_id']:r['metrics'] for r in quality[name]['per_query']} for name in runs}
    cases, negatives = [], []
    for identity, query in queries.items():
        label = labels[identity]
        if label['status'] == 'excluded':
            continue
        rows = {name:table.get(identity, {'status':'missing','hits':[]}) for name,table in tables.items()}
        base = {'query_id':identity,'query':query['text'],'split':query['split'],
                'family':query['family'],'category':query['category'],'scope':query['scope']}
        if not label['answerable']:
            negatives.append({**base,'returned':{n:len(r['hits']) for n,r in rows.items()},
                              'notice':'无答案检索可返回相似资料；没有生成答案，不能据返回数量断言拒答错误。'})
            continue
        reasons = []
        for name,row in rows.items():
            if row['status'] != 'ok':
                reasons.append(name+': execution_'+row['status'])
            metric = scores[name][identity]
            if metric['CandidateRecall@50'] < 1:
                reasons.append(name+': candidate_evidence_incomplete_at50')
            if metric['Hit@3'] == 0:
                reasons.append(name+': no_relevant_top3')
            if metric['CompleteEvidenceHit@3'] == 0:
                reasons.append(name+': required_evidence_incomplete_top3')
        for after,before in (('B','A'),('C','B')):
            if after in runs and scores[after][identity]['nDCG@5'] < scores[before][identity]['nDCG@5']:
                reasons.append(after+'-'+before+': ndcg5_regression')
        if not reasons:
            continue
        grades = {j['chunk_id']:j['grade'] for j in label['judgments']}
        top3, missing = {}, {}
        for name,row in rows.items():
            identities = [h['chunk_id'] for h in row['hits'][:3]]
            top3[name] = [{'chunk_id':i,'grade':grades[i],
                          **{k:chunks[i][k] for k in ('file','line_start','line_end')}} for i in identities]
            missing[name] = [g['id'] for g in label['groups'] if not any(set(b)<=set(identities) for b in g['bundles'])]
        cases.append({**base,'reasons':reasons,'top3':top3,'missing_groups_at3':missing,
                      'metrics':{n:scores[n][identity] for n in runs},
                      'notice':'记录排名、标签和范围的差异，不推断模型内部原因；不得据测试失败反改标签或调参。'})
    return {'schema':1,'freeze_id':frozen['id'], 'benchmark_fingerprint':manifest['fingerprint'],
            'cases':cases,'negatives':negatives,
            'coverage':{split:{'queries':sum(q['split']==split for q in queries.values()),
                               'families':len({q['family'] for q in queries.values() if q['split']==split}),
                               'diagnostic_cases':sum(q['split']==split for q in cases)} for split in ('dev','test')},
            'notice':'失败类型可重叠，条数不是错误率；独立问题族少于查询数，合成自审不等于外部盲测。'}


def failure_markdown(inventory):
    lines = ['', '## 失败案例清单', '', inventory['notice'], '',
             '| 分区 | 查询 / 问题族 | 诊断样例 |', '| --- | ---: | ---: |']
    for split,row in inventory['coverage'].items():
        lines.append(f"| {split} | {row['queries']} / {row['families']} | {row['diagnostic_cases']} |")
    lines += ['', '| 查询 | 分区 | 观测到的失败或退化 |', '| --- | --- | --- |']
    for case in inventory['cases']:
        lines.append(f"| {case['query_id']} | {case['split']} | {'; '.join(case['reasons'])} |")
    lines += ['', '完整 JSON 保存每条问题、权限范围、各系统前三原文行号/等级及缺失证据组。', '',
              f"无答案样例另列 {len(inventory['negatives'])} 条，只记录检索返回，不据此计算拒答成功率。"]
    return lines
