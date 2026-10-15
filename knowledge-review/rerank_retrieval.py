"""Reorder precisely the recorded B pool; never retrieve new candidates."""
from copy import deepcopy
from math import isfinite
from time import perf_counter

from retrieval_benchmark_data import digest


def validate_hybrid_pool(manifest, run):
    if run['system'] != 'B_hybrid' or run['benchmark_fingerprint'] != manifest['fingerprint']:
        raise ValueError('C要求同一资料/模型/分块/问题版本的B候选池。')
    if run.get('protocol', {}).get('quality_labels_used_during_retrieval') is not False:
        raise ValueError('B必须明确记录召回未使用相关性标签。')
    queries = {q['id']:q for q in manifest['suite']['queries']}
    chunks = {c['id']:c for c in manifest['chunks']}
    rows = {}
    limit = run['protocol'].get('candidate_m')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('B候选上限必须是明确的1到50。')
    for row in run['queries']:
        identity = row['query_id']
        if identity in rows or identity not in queries or row['scope'] != queries[identity]['scope']:
            raise ValueError('B查询重复、未知或许可范围不一致。')
        if row['status'] not in ('ok','failed') or row['status']=='failed' and row['hits']:
            raise ValueError('B状态与候选结果不一致。')
        if len(row['hits']) > limit:
            raise ValueError('B候选池超过协议上限。')
        seen = set()
        for rank, hit in enumerate(row['hits'], 1):
            source = chunks.get(hit['chunk_id'])
            if not source or source['file'] not in row['scope'] or hit['chunk_id'] in seen:
                raise ValueError('B候选重复、未知或越权。')
            if any(hit.get(key) != source[key] for key in ('source_id','line_start','line_end','text')):
                raise ValueError('B候选原文/行号与不可变资料不一致。')
            if type(hit.get('score')) not in (int,float) or not isfinite(hit['score']) or hit.get('rank') != rank:
                raise ValueError('B候选得分或原始排序非法。')
            seen.add(hit['chunk_id'])
        rows[identity] = row
    return rows


def document_windows(query, document, ranker):
    """Full query in every pair, lossless consecutive original character spans.

    Binary search finds a safe fitting prefix; final token count is always checked.
    It need not maximize window length to guarantee coverage without truncation.
    """
    if not isinstance(query,str) or not query.strip():
        raise ValueError('重排当前问题不能为空。')
    limit = ranker.max_tokens
    if ranker.token_count(query,'') >= limit:
        raise ValueError('当前问题及协议已占满重排输入容量，不能截断问题。')
    if not document:
        return [{'char_start':0,'char_end':0,'text':'','tokens':ranker.token_count(query,'')}]
    spans, start = [], 0
    while start < len(document):
        if len(spans) >= ranker.config.max_windows:
            raise ValueError('单片段原文需要的窗口超过上限；保留失败，不截掉尾文。')
        low, high, end = start+1, len(document), start
        while low <= high:
            mid = (low+high)//2
            if ranker.token_count(query,document[start:mid]) <= limit:
                end, low = mid, mid+1
            else:
                high = mid-1
        if end == start:
            raise ValueError('重排无法容纳完整问题与至少一个原文字符。')
        text = document[start:end]
        count = ranker.token_count(query,text)
        if count > limit:
            raise ValueError('实际重排窗口超预算。')
        spans.append({'char_start':start,'char_end':end,'text':text,'tokens':count})
        start = end
    return spans


def rerank_pool(query, pool, ranker):
    pairs, spans_by_hit = [], []
    for hit in pool:
        spans = document_windows(query,hit['text'],ranker)
        if len(pairs)+len(spans) > ranker.config.max_pairs:
            raise ValueError('本题原文窗口超过重排总对数上限；不删候选或截原文。')
        for span in spans:
            prefix = hit['text'][:span['char_start']]
            span.update(line_start=hit['line_start']+prefix.count('\n'),
                        line_end=hit['line_start']+hit['text'][:max(span['char_start'],span['char_end']-1)].count('\n'))
            pairs.append((query,span['text']))
        spans_by_hit.append(spans)
    values = ranker.predict(pairs) if pairs else []
    if len(values) != len(pairs) or any(type(v) not in (float,int) or not isfinite(v) for v in values):
        raise ValueError('重排分数缺失、数量不符或不是有限数值。')
    scored, offset = [], 0
    for hit, spans in zip(pool,spans_by_hit):
        for span in spans:
            span['score'] = values[offset]
            offset += 1
        scored.append({**deepcopy(hit), 'b_rank':hit['rank'], 'b_rrf_score':hit['score'],
                       'score':max(span['score'] for span in spans), 'rerank_windows':spans})
    scored.sort(key=lambda hit:(-hit['score'],hit['b_rank']))
    for rank, hit in enumerate(scored,1):
        hit['rank'] = rank
    return scored


def run_rerank(manifest, hybrid, ranker, repeats=3, progress=None):
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError('重排暖重复次数须为1–10。')
    before = validate_hybrid_pool(manifest,hybrid)
    rows = []
    for query in manifest['suite']['queries']:
        identity = query['id']
        b = before.get(identity,{'status':'missing','hits':[]})
        row = {'query_id':identity,'scope':query['scope'],'status':'failed','hits':[],
               'input_pool':deepcopy(b['hits']), 'b_status':b['status'],
               'pool_fingerprint':digest(b['hits']), 'warm_ms':[], 'warm_rerank_ms':[],
               'warm_end_to_end_estimate_ms':[]}
        if b['status'] != 'ok':
            row['error']={'type':'InheritedRetrievalFailure','message':'B检索失败或缺失，C不新检索补池。'}
            rows.append(row)
            if progress:
                progress(row)
            continue
        try:
            start = perf_counter()
            hits = rerank_pool(query['text'],b['hits'],ranker)
            row['first_call']={'rerank_ms':(perf_counter()-start)*1000}
            for _ in range(repeats):
                start = perf_counter()
                hits = rerank_pool(query['text'],b['hits'],ranker)
                elapsed = (perf_counter()-start)*1000
                row['warm_ms'].append(elapsed)
                row['warm_rerank_ms'].append(elapsed)
            # Sum with the recorded B sample only as an explicitly named estimate.
            # This is not a measured fresh end-to-end C request or extra retrieval.
            if len(b.get('warm_ms',[])) == repeats:
                row['warm_end_to_end_estimate_ms'] = [a+c for a,c in zip(b['warm_ms'],row['warm_ms'])]
            row['hits'] = hits
            row['window_count'] = sum(len(h['rerank_windows']) for h in hits)
            row['split_candidate_count'] = sum(len(h['rerank_windows'])>1 for h in hits)
            row['status'] = 'ok'
        except Exception as exc:
            row['error']={'type':type(exc).__name__,'message':str(exc)}
            row['hits'] = []
            row['warm_end_to_end_estimate_ms'] = []
        rows.append(row)
        if progress:
            progress(row)
    return rows


def validate_same_pool(manifest, hybrid, reranked):
    before = validate_hybrid_pool(manifest,hybrid)
    if reranked['system'] != 'C_rerank' or reranked['benchmark_fingerprint'] != manifest['fingerprint']:
        raise ValueError('重排运行必须为同版本C。')
    if reranked.get('input_b_run_digest') != digest(hybrid):
        raise ValueError('C绑定的B运行发生变化，不能声称同池比较。')
    expected = {q['id'] for q in manifest['suite']['queries']}
    rows = reranked['queries']
    if len(rows) != len(expected) or {r['query_id'] for r in rows} != expected:
        raise ValueError('C必须保留每条查询，包括检索失败项。')
    for row in rows:
        b_row = before.get(row['query_id'],{'hits':[],'scope':next(q['scope'] for q in manifest['suite']['queries'] if q['id']==row['query_id'])})
        pool = b_row['hits']
        if row['scope'] != b_row['scope']:
            raise ValueError('C许可范围与B不一致。')
        if row['input_pool'] != pool or row['pool_fingerprint'] != digest(pool):
            raise ValueError('C候选池与绑定的B原文/顺序不一致。')
        if row['status']=='failed':
            if row['hits']:
                raise ValueError('失败的C不能保留成功排名。')
            continue
        if row['status'] != 'ok' or set(h['chunk_id'] for h in row['hits']) != {h['chunk_id'] for h in pool} or len(row['hits'])!=len(pool):
            raise ValueError('C只能重排B的全部候选，不能补回、丢弃或重复。')
        originals = {h['chunk_id']:h for h in pool}
        for rank,hit in enumerate(row['hits'],1):
            original = originals[hit['chunk_id']]
            if any(hit[key] != original[key] for key in ('source_id','line_start','line_end','text')):
                raise ValueError('重排结果伪造原文范围。')
            if hit.get('rank')!=rank or hit.get('b_rank')!=original['rank'] or hit.get('b_rrf_score')!=original['score']:
                raise ValueError('重排前后的排序身份不一致。')
            spans = hit.get('rerank_windows',[])
            end = 0
            for span in spans:
                if span['char_start'] != end or not end <= span['char_end'] <= len(original['text']) or span['text'] != original['text'][end:span['char_end']]:
                    raise ValueError('重排原文窗口不连续、被裁掉或与原文不符。')
                if type(span['score']) not in (int,float) or not isfinite(span['score']):
                    raise ValueError('重排窗口分数非法。')
                line_start = original['line_start']+original['text'][:span['char_start']].count('\n')
                line_end = original['line_start']+original['text'][:max(span['char_start'],span['char_end']-1)].count('\n')
                if span['line_start']!=line_start or span['line_end']!=line_end:
                    raise ValueError('重排窗口行范围与实际字符范围不一致。')
                if type(span['tokens']) is not int or not 0 < span['tokens'] <= reranked['reranker']['effective_max_tokens']:
                    raise ValueError('重排窗口实际token数超预算或非法。')
                end = span['char_end']
            if not spans or end!=len(original['text']) or hit['score']!=max(s['score'] for s in spans):
                raise ValueError('重排必须覆盖完整原文且使用已记录的聚合分数。')
