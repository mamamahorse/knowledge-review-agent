"""Isolated, fixed synthetic candidate investigations. Live requires an explicit flag.

This is NOT an extraction test or an independently reviewed semantic quality score.
Reference outcomes are saved only after the review, never sent to the model.
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
from sources import Source
from outline_store import OutlineStore
from outline_service import section_manifest, scope_fingerprint
from outline_grouping import start_grouping
from outline_review import start_source_review, run_source_review, public_source_reviews
from outline_review_store import get_review, review_steps
from outline_review_model import ReviewAction
from practice_model import _request_structured


def failure_category(exc):
    known = {'the model response was truncated.': 'output_truncated',
        'the model did not submit one structured result.': 'missing_result_submission',
        'the model returned no structured result.': 'missing_structured_result',
        'the model returned no result choices.': 'missing_choices'}
    return known.get(str(exc), 'provider_or_structure_failure')


CASES = {
    'definition': {
        'texts': ['# RESET\n\nRESET 终止当前连接。\n\n术语适用 TCP-X v1；RST 的含义见术语文件。',
                  '# RST\n\nRST 终止当前连接。\n\n术语适用 TCP-X v1；缩写定义见术语文件。',
                  '# 术语\n\nRST 是 RESET 的缩写；二者在 TCP-X v1 中均指立即终止当前连接。'],
        'statements': ['RESET 终止当前连接。', 'RST 终止当前连接。'],
        'titles': ['解释 RESET 的作用', '解释 RST 的作用'],
        'scope': 'TCP-X v1 中的连接终止，不涉及成功接收字节。',
        'expected': 'duplicate_targets', 'read_source': 2, 'read_start': 3, 'read_end': 3,
    },
    'versions': {
        'texts': ['# 缓存\n\n默认 TTL 为 60 秒。\n\n本章仅适用于 Cache-X v1。',
                  '# 缓存\n\n默认 TTL 为 90 秒。\n\n本章仅适用于 Cache-X v2；v1 仍为 60 秒。'],
        'statements': ['默认 TTL 为 60 秒。', '默认 TTL 为 90 秒。'],
        'titles': ['解释本章的默认 TTL', '解释本章的默认 TTL'],
        'scope': '各自章节对应的 Cache-X 版本；仅解释默认值。',
        'expected': 'distinct', 'read_source': 1, 'read_start': 3, 'read_end': 5,
    },
    'conflict': {
        'texts': ['# 规范 A\n\n默认 TTL 为 60 秒。\n\n适用 Cache-X v1；日期为 2026-01-01，无优先级约定。',
                  '# 规范 B\n\n默认 TTL 为 90 秒。\n\n适用 Cache-X v1；日期为 2026-01-01，无优先级约定。'],
        'statements': ['默认 TTL 为 60 秒。', '默认 TTL 为 90 秒。'],
        'titles': ['解释默认 TTL', '解释默认 TTL'],
        'scope': 'Cache-X v1；仅解释默认值。',
        'expected': 'unresolved', 'read_source': 1, 'read_start': 3, 'read_end': 5,
    },
    'clarification': {
        'texts': ['# ACK\n\nACK = SEQ + LEN。\n\n此式仅适用于不含 SYN/FIN 的连续数据段；SYN 和 FIN 各占一个序号。',
                  '# 数据段\n\n不含 SYN/FIN 时 ACK = SEQ + LEN。'],
        'statements': ['ACK = SEQ + LEN。', '不含 SYN/FIN 时 ACK = SEQ + LEN。'],
        'titles': ['用公式计算所有 TCP 段的 ACK', '用公式计算连续数据段的 ACK'],
        'scope': 'TCP 连续段；需保留各目标原有范围，不能默默修复。',
        'expected': 'revise_candidates', 'read_source': 0, 'read_start': 3, 'read_end': 5,
    },
}


def seed_review(store, case, call_limit, *, sources=None, snapshots=None, collection='root'):
    sources = sources or [Source(f'synthetic-source-{i}', Path(f'fixture-{i}.md'), text) for i, text in enumerate(case['texts'])]
    snapshots = snapshots or [{'document_id': f'fixture-{i}', 'source_id': s.source_id, 'path': str(s.path), 'name': s.path.name, 'text': s.text} for i, s in enumerate(sources)]
    sections = section_manifest(sources, snapshots)
    batches = [{**s, 'section_id': s['id']} for s in sections]
    outline = store.create(collection, uuid4().hex, '比较候选知识和目标，核对缺失的条件与定义。', 'zh', scope_fingerprint(snapshots), snapshots, sections, batches)
    store.start(outline)
    i = 0
    while (batch := store.claim_batch(outline)):
        if i >= len(case['statements']):
            store.finish_batch(batch, [], '定义来源只用于后续复查，不预先提取候选。', visible=[])
            i += 1
            continue
        draft = LearningDraft.model_validate({'knowledge': {'name': case['titles'][i], 'statement': case['statements'][i],
            'content_tags': ['synthetic'], 'citations': [{'source_id': sources[i].source_id, 'line_start': 3, 'line_end': 3}]},
            'target': {'title': case['titles'][i], 'capability': 'explain' if i == 0 or case['expected'] != 'revise_candidates' else 'apply',
                'description': case['titles'][i], 'scope': case['scope'], 'criteria': [case['titles'][i]]}})
        store.finish_batch(batch, [draft], '人工固定合成候选；未测试真实提取。', visible=[{'source_id': sources[i].source_id, 'line_start': 1, 'line_end': len(sources[i].text.splitlines())}])
        i += 1
    store.settle(outline)
    current = lambda _: snapshots
    run = start_grouping(store, outline, 'fixed-pending-group', 'zh', None, current, call_limit)
    candidates = store.get(outline)['candidates']
    proposal = {'kind': 'needs_review', 'candidate_ids': [c['id'] for c in candidates], 'name': '待核对的候选',
        'reason': '原候选引用可能缺少定义、条件或版本信息，需要核对原文。', 'canonical_id': None}
    group = uuid4().hex
    with store.practice.transaction() as db:
        db.execute("UPDATE outline_group_runs SET status='completed',calls_used=0 WHERE id=?", (run,))
        db.execute('INSERT INTO outline_groups (id,run_id,outline_id,proposal_json,model_json,members_json) VALUES (?,?,?,?,?,?)',
            (group, run, outline, json.dumps(proposal, ensure_ascii=False), json.dumps(proposal, ensure_ascii=False), json.dumps({c['id']: c['revision'] for c in candidates})))
    review = start_source_review(store, group, 'first-review', 'zh', current)
    return outline, review, current


def controlled(case, messages, schema):
    data = json.loads(messages[1]['content'])
    sid = data['sources'][case['read_source']]['source_id']
    if not data['lookup_observations']:
        return schema(action='read', issue='受控样例：核对引用外的定义或条件。',
            requests=[{'kind': 'read', 'source_id': s['source_id'], 'line_start': 3, 'line_end': s['line_count'], 'query': None} for s in
                ([data['sources'][2]] if case['expected'] == 'duplicate_targets' else data['sources'][:2])],
            outcome=None, name='', reason='', canonical_id=None, citations=[], clarifications=[])
    outcome = case['expected']
    return schema(action='conclude', issue='受控样例：核对定义、版本和目标条件。', requests=[], outcome=outcome,
        name='受控结果', reason='受控响应只验证流程，不测语义质量。',
        canonical_id=data['candidates'][0]['id'] if outcome == 'duplicate_targets' else None,
        citations=[{'source_id': sid, 'line_start': case['read_start'], 'line_end': case['read_end']}],
        clarifications=[{'candidate_id': data['candidates'][0]['id'], 'suggestion': '将范围明确限制为不含 SYN/FIN 的连续数据段。'}] if outcome == 'revise_candidates' else [])


def evaluate(mode='smoke', cases=None, call_limit=3):
    base = Path(__file__).resolve().parent
    root = base / '.data' / 'evaluations' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + f'-source-review-{mode}-' + uuid4().hex[:8])
    root.mkdir(parents=True)
    store = OutlineStore(LearningStore(root / 'practice.sqlite3'))
    report = {'mode': mode, 'fixture_kind': 'manual fixed candidates and pending grouping; synthetic only',
        'semantic_review': 'not independently reviewed; no quality rate', 'cases': [], 'calls': []}
    for key in cases or CASES:
        case = CASES[key]
        outline, review, current = seed_review(store, case, call_limit)
        def request(messages, schema):
            entry = {'case': key, 'schema': schema.__name__, 'messages': messages}
            report['calls'].append(entry)
            try:
                result = _request_structured(messages, schema) if mode == 'live' else controlled(case, messages, schema)
                entry['result'] = result.model_dump()
                return result
            except Exception as exc:
                entry['error_class'] = type(exc).__name__
                entry['cause_class'] = type(exc.__cause__).__name__ if exc.__cause__ else None
                entry['failure_category'] = failure_category(exc)
                raise
        with patch('outline_review._request_structured', side_effect=request):
            run_source_review(store, review, current)
        r = get_review(store, review)
        report['cases'].append({'id': key, 'reference_draft': case['expected'], 'status': r['status'], 'result': r['result'],
            'review': public_source_reviews(store, outline)[0], 'inputs': current('root')})
        (root / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with closing(store.practice._connect()) as db:
        report['preservation'] = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('practice_questions', 'review_items', 'review_events')}
        report['integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
        report['foreign_key_errors'] = len(db.execute('PRAGMA foreign_key_check').fetchall())
    report['code_hashes'] = {p.name: sha256(p.read_bytes()).hexdigest() for p in [base / name for name in
        ('outline_review.py', 'outline_review_model.py', 'outline_review_store.py', 'generation_budget.py', 'practice_model.py', 'structured_output.py')]}
    (root / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(root / 'results.json'), 'mode': mode, 'logical_calls': len(report['calls']),
        'results': [{'case': c['id'], 'status': c['status'], 'outcome': (c['result'] or {}).get('outcome')} for c in report['cases']]}, ensure_ascii=False))
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('smoke', 'live'), default='smoke')
    parser.add_argument('--case', choices=tuple(CASES), action='append')
    parser.add_argument('--call-limit', type=int, choices=range(1, 4), default=3)
    args = parser.parse_args()
    evaluate(args.mode, args.case, args.call_limit)
