"""Hierarchical card routing, original-evidence verification and clique-safe proposals."""

import json
from itertools import combinations
from uuid import uuid4

from generation_budget import GenerationBudget, BudgetError
from outline_context import request_diagnostic, generation_error
from outline_group_plan import ComparisonPage, comparisons, make_plan, page_fits, page_messages, work_scopes
from outline_group_steps import claim_step, finish_step, fail_step, steps, require_attempt
from outline_group_store import get_run, approved_group
from outline_service import check_request, require_current
from outline_store import OutlineBusy, timestamp


def key(left, right):
    return tuple(sorted((left, right)))


def validate_page(result, records, pairs):
    from outline_grouping import validate_result
    result = ComparisonPage.model_validate(result.model_dump())
    validate_result(result, {'candidates': records})
    asked = {p['index']: p for p in pairs}
    checked, deferred = result.checked_indices, result.deferred_indices
    if (len(set(checked)) != len(checked) or len(set(deferred)) != len(deferred) or
            set(checked).intersection(deferred) or set(checked + deferred) != asked.keys()):
        raise ValueError('比较回执含未知、重复或遗漏的身份。')
    by_pair = {key(p['left'], p['right']): p['index'] for p in pairs}
    relations = {}
    for group in result.suggestions:
        for left, right in combinations(group.candidate_ids, 2):
            index = by_pair.get(key(left, right))
            if index is None:
                continue  # Extra relationships never expand the requested work scope.
            if index not in checked:
                raise ValueError('关系建议引用了未处理的比较。')
            relations.setdefault(str(index), []).append(group.model_dump())
    # A full suggestion slot can hide additional positives. Revisit non-reported
    # comparisons even if the model claimed to finish them.
    effective = checked if len(result.suggestions) < 4 else [i for i in checked if str(i) in relations]
    return {**result.model_dump(), 'effective_checked': effective, 'relations': relations}


def progress(saved, pairs):
    routed, verified, hypotheses, evidence = set(), set(), set(), {}
    for step in saved:
        if step['status'] != 'completed':
            continue
        result = step['result']
        accepted = set(result['effective_checked'])
        if step['kind'] == 'route':
            routed.update(accepted)
            hypotheses.update(i for i in accepted if result['relations'].get(str(i)))
        else:
            verified.update(accepted)
            for i in accepted:
                for group in result['relations'].get(str(i), []):
                    evidence.setdefault(i, []).append({'step_id': step['id'], 'proposal': group})
    return {'routed': routed, 'verified': verified, 'hypotheses': hypotheses, 'evidence': evidence,
            'pending_verification': hypotheses - verified,
            'handled': routed | verified, 'total': len(pairs)}


def select_page(pending, records, objective, language, budget, evidence, limit):
    selected = []
    for pair in pending:
        trial = selected + [pair]
        ids = {p[k] for p in trial for k in ('left', 'right')}
        own = [r for r in records if r['id'] in ids]
        if len(trial) > limit or not page_fits(budget, objective, own, trial, language, evidence):
            if not selected:
                messages = page_messages(objective, own, trial, language, evidence)
                check_request(budget, messages, ComparisonPage)
                raise BudgetError('最小比较单元的估计输出超过预留，尚未裁剪候选或证据；请核对服务预算后继续。')
            break
        selected = trial
    ids = {p[k] for p in selected for k in ('left', 'right')}
    return selected, [r for r in records if r['id'] in ids]


def update_plan(store, run, plan):
    with store.practice.transaction() as db:
        require_attempt(db, run['id'], run['attempt'])
        db.execute('UPDATE outline_group_runs SET plan_json=?,updated_at=? WHERE id=?',
                   (json.dumps(plan), timestamp(), run['id']))


def proposals_from_proofs(pairs, evidence, candidate_ids):
    """Only complete pair support can assemble a large group. Never union chains."""
    graph, uncertain = {}, set()
    for pair in pairs:
        k = key(pair['left'], pair['right'])
        proofs = evidence.get(pair['index'], [])
        kinds = {p['proposal']['kind'] for p in proofs}
        if 'needs_review' in kinds:
            uncertain.add(k)
        graph[k] = proofs
    result = []
    for kind in ('duplicate_targets', 'same_knowledge', 'needs_review'):
        available = {k for k, proofs in graph.items() if any(p['proposal']['kind'] == kind for p in proofs)
                     and (kind == 'needs_review' or k not in uncertain)}
        covered = set()
        for pair in pairs:
            seed = key(pair['left'], pair['right'])
            if seed not in available or seed in covered:
                continue
            members = [c for c in candidate_ids if c in seed]
            if kind != 'needs_review':
                for candidate in candidate_ids:
                    if candidate not in members and all(key(candidate, c) in available for c in members):
                        members.append(candidate)
            member_pairs = {key(a, b) for a, b in combinations(members, 2)}
            covered.update(member_pairs)
            proofs = [p for k in sorted(member_pairs) for p in graph[k] if p['proposal']['kind'] == kind]
            original = proofs[0]['proposal']
            proposal = {**original, 'candidate_ids': members,
                'reason': original['reason'][:450] + f'\n整合依据：组内 {len(member_pairs)} 对关系均有原文核验记录；需用户审核。'}
            if kind == 'duplicate_targets' and proposal['canonical_id'] not in members:
                proposal['canonical_id'] = members[0]
            result.append({'proposal': proposal, 'model_proposal': original,
                           'support': sorted({p['step_id'] for p in proofs})})
    return result


def publish(store, run, inputs, pairs, state):
    proposals = proposals_from_proofs(pairs, state['evidence'], [c['id'] for c in inputs['candidates']])
    with store.practice.transaction() as db:
        require_attempt(db, run['id'], run['attempt'])
        for candidate in inputs['candidates']:
            row = db.execute('SELECT revision,status FROM outline_candidates WHERE id=?', (candidate['id'],)).fetchone()
            if not row or row['revision'] != candidate['revision'] or row['status'] != 'proposed' or approved_group(db, candidate['id']):
                raise OutlineBusy('候选或审核版本已变化，未发布旧整合。')
        for result in proposals:
            p = result['proposal']
            members = {c['id']: c['revision'] for c in inputs['candidates'] if c['id'] in p['candidate_ids']}
            db.execute('''INSERT INTO outline_groups
                (id,run_id,outline_id,proposal_json,model_json,members_json,support_json) VALUES (?,?,?,?,?,?,?)''',
                (uuid4().hex, run['id'], run['outline_id'], json.dumps(p, ensure_ascii=False),
                 json.dumps(result['model_proposal'], ensure_ascii=False), json.dumps(members), json.dumps(result['support'])))
        db.execute("UPDATE outline_group_runs SET status='completed',attempt=NULL,error=NULL,note=?,updated_at=? WHERE id=?",
            ('计划内比较及被筛选关联的原文核验完成；模型回执不是语义完整性保证。', timestamp(), run['id']))


def execute_integration(store, run, inputs, current):
    from outline_grouping import group_messages, require_inputs, _request_structured
    budget = GenerationBudget.from_env()
    data = json.loads(group_messages(store, run)[1]['content'])
    records, objective = data['candidates'], data['objective']
    job = store.get(run['outline_id'])
    locations = {c['id']: next(b['section_id'] for b in job['batches'] if b['id'] == c['batch_id']) for c in job['candidates']}
    plan = json.loads(run['plan_json'])
    if 'mode' not in plan:
        plan = make_plan(records, locations, objective, run['language'], budget, run['allowance'])
        update_plan(store, run, plan)
    pairs = comparisons(plan['candidate_ids'])
    by_key = {key(p['left'], p['right']): p for p in pairs}
    while True:
        require_current(store.get(run['outline_id']), current)
        require_inputs(store, run)
        with store.practice.transaction() as db:
            require_attempt(db, run['id'], run['attempt'])
        saved = steps(store, run['id'])
        state = progress(saved, pairs)
        kind, scope, pending = None, None, None
        # Verify navigation hypotheses before taking on more upper-level work.
        if state['pending_verification']:
            kind, scope = 'verify', 'original-evidence'
            pending = [p for p in pairs if p['index'] in state['pending_verification']]
        else:
            for own_scope, own_kind, own_pairs in work_scopes(plan):
                remaining = [by_key[key(a, b)] for a, b in own_pairs if by_key[key(a, b)]['index'] not in state['handled']]
                if remaining:
                    kind, scope, pending = own_kind, own_scope, remaining
                    break
        if pending is None:
            publish(store, run, inputs, pairs, state)
            return
        page, own = select_page(pending, records, objective, run['language'], budget, kind != 'route', plan['page_limit'])
        messages = page_messages(objective, own, page, run['language'], kind != 'route')
        check_request(budget, messages, ComparisonPage)
        diagnostic = {**request_diagnostic(budget, messages, ComparisonPage),
            'evidence_present': kind != 'route', 'candidate_ids': [r['id'] for r in own],
            'visible': {r['id']: [{k: c[k] for k in ('source_id', 'line_start', 'line_end')} for c in r['original_evidence']]
                        for r in own} if kind != 'route' else {}}
        step = claim_step(store, run, kind, scope, page, diagnostic)
        if step is None:
            return
        try:
            generated = _request_structured(messages, ComparisonPage)
            result = validate_page(generated, own, page)
            require_current(store.get(run['outline_id']), current)
            require_inputs(store, run)
            if not result['effective_checked'] and len(page) == 1:
                raise ValueError('模型未完成最小比较单元，保留待处理工作。')
            finish_step(store, run, step, result)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            fail_step(store, step, generation_error(exc, '候选整合'))
            if isinstance(exc, RuntimeError) and ('was truncated' in str(exc) or type(exc.__cause__).__name__ == 'LengthFinishReasonError') and len(page) > 1:
                plan.update(mode='hierarchical', page_limit=max(1, len(page) // 2),
                            reason='明确输出截断，保留失败步骤并缩小比较工作后继续。')
                update_plan(store, run, plan)
                continue
            raise
        if result['deferred_indices'] or len(result['effective_checked']) < len(page):
            plan.update(mode='hierarchical', page_limit=max(1, len(page) // 2),
                        reason='有未完成比较或建议容量已满，自动转为有界续处理。')
            update_plan(store, run, plan)


def public_integration(store, run):
    plan = run.get('plan')
    saved = steps(store, run['id']) if plan else []
    pairs = comparisons(plan['candidate_ids']) if plan and 'mode' in plan else []
    state = progress(saved, pairs)
    handled_ids = {p[k] for p in pairs if p['index'] in state['handled'] for k in ('left', 'right')}
    return {'plan': plan, 'calls_used': run['calls_used'], 'allowance': run['allowance'],
        'progress': {'handled_candidates': len(handled_ids), 'total_candidates': len(run['inputs']['candidates']),
                     'handled_pairs': len(state['handled']), 'total_pairs': len(pairs),
                     'verified_pairs': len(state['verified']), 'pending_verification': len(state['pending_verification'])},
        'steps': [{k: s[k] for k in ('id', 'kind', 'scope', 'inputs', 'status', 'result', 'diagnostic', 'error')} for s in saved]}
