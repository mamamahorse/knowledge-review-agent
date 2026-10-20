"""Finite source investigation after grouping; conclusions remain opt-in decisions."""
from agent_work import job_boundary
import json
from contextlib import closing
from uuid import uuid4

from generation_budget import GenerationBudget
from learning_types import LearningDraft
from outline_context import request_diagnostic, generation_error
from outline_group_store import rows
from outline_review_store import (get_review, review_steps, check_members,
    claim_review_step, finish_review_step, finish_review, pause_review, recover_reviews)
from outline_review_model import action_schema, validate_action, prepare_review_request, execute_lookup
from outline_service import require_current, candidate_context, candidate_visible, validate_learning, excerpt
from outline_store import OutlineBusy, timestamp
from practice_store import archived_sources
from practice_model import _request_structured
from response_preferences import validate_language


def review_inputs(store, group):
    job = store.get(group['outline_id'])
    if group['status'] != 'proposed' or group['proposal']['kind'] != 'needs_review':
        raise OutlineBusy('仅待审核、条件或依据待核对的建议可启动定向复查。')
    candidates = []
    for identity, revision in group['members'].items():
        candidate, _, source, chunk = candidate_context(store, identity)
        validate_learning(LearningDraft.model_validate(candidate['learning']), candidate_visible(candidate, job, source, chunk))
        if candidate['status'] != 'proposed' or candidate['revision'] != revision:
            raise OutlineBusy('候选已经变化，请按当前候选重新归组。')
        candidates.append({k: candidate[k] for k in ('id', 'revision', 'learning')})
    return {'group_revision': group['revision'], 'scope_fingerprint': job['scope_fingerprint'],
            'proposal': group['proposal'], 'candidates': candidates}


def start_source_review(store, group_id, request_id, language, current):
    validate_language(language)
    with store.lock(group_id):
        with closing(store.practice._connect()) as db:
            old = db.execute('SELECT id,language FROM outline_reviews WHERE group_id=? AND request_id=?', (group_id, request_id)).fetchone()
            raw = db.execute('SELECT outline_id FROM outline_groups WHERE id=?', (group_id,)).fetchone()
        if not raw:
            raise KeyError('归组建议不存在。')
        if old:
            if old['language'] != language:
                raise OutlineBusy('这个复查请求标识已用于另一种语言。')
            return old['id']
        require_current(store.get(raw['outline_id']), current)
        group = next(g for g in rows(store, raw['outline_id'])[1] if g['id'] == group_id)
        recover_reviews(store, raw['outline_id'])
        inputs = review_inputs(store, group)
        with store.practice.transaction() as db:
            check_members(db, db.execute('SELECT * FROM outline_groups WHERE id=?', (group_id,)).fetchone(), inputs)
            active = db.execute("SELECT id,language FROM outline_reviews WHERE group_id=? AND status IN ('queued','running','paused')", (group_id,)).fetchone()
            if active:
                if active['language'] != language:
                    raise OutlineBusy('此建议已有另一语言的复查任务，请先完成或暂停后继续原任务。')
                return active['id']
            identity, now = uuid4().hex, timestamp()
            db.execute("INSERT INTO outline_reviews (id,group_id,run_id,request_id,group_revision,language,inputs_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?)",
                (identity, group_id, group['run_id'], request_id, group['revision'], language, json.dumps(inputs, ensure_ascii=False), now, now))
        return identity


def check_current_review(store, review, current):
    job = store.get(store.candidate(review['inputs']['candidates'][0]['id'])['outline_id'])
    require_current(job, current)
    with closing(store.practice._connect()) as db:
        group = db.execute('SELECT * FROM outline_groups WHERE id=?', (review['group_id'],)).fetchone()
        check_members(db, group, review['inputs'])
    return job


def original_evidence(review, by_id):
    ranges = {(c['source_id'], c['line_start'], c['line_end']) for r in review['inputs']['candidates'] for c in r['learning']['knowledge']['citations']}
    return [excerpt(by_id[sid], start, end) for sid, start, end in sorted(ranges)]


def evidence_and_observations(review, by_id, saved):
    evidence = original_evidence(review, by_id)
    observations = []
    for step in saved:
        if step['status'] != 'completed' or step['action']['action'] != 'read':
            continue
        observations.append({'action': 'read', 'issue': step['action']['issue'], **step['observation']})
        for lookup in step['observation']['lookups']:
            r = lookup.get('actual_range')
            if r:
                evidence.append(excerpt(by_id[r['source_id']], r['line_start'], r['line_end']))
    # Do not send the same excerpt twice on resume or repeated reads.
    evidence = list({(c.source_id, c.line_start, c.line_end): c for c in evidence}.values())
    return evidence, observations


@job_boundary("review")
def run_source_review(store, identity, current):
    attempt = None
    step = None
    claimed_lock = False
    try:
        review = get_review(store, identity)
        with store.lock(review['run_id']):
            claimed_lock = True
            if review['status'] != 'queued':
                return
            job = check_current_review(store, review, current)
            attempt = uuid4().hex
            with store.practice.transaction() as db:
                if not db.execute("UPDATE outline_reviews SET status='running',attempt=?,error=NULL,updated_at=? WHERE id=? AND status='queued'",
                                  (attempt, timestamp(), identity)).rowcount:
                    return
            budget = GenerationBudget.from_env()
            by_id = {s.source_id: s for s in archived_sources(job['snapshots'])}
            while True:
                job = check_current_review(store, review, current)
                saved = review_steps(store, identity)
                completed = [s for s in saved if s['status'] == 'completed']
                if completed and completed[-1]['action']['action'] == 'conclude':
                    finish_review(store, review, attempt, completed[-1]['action'])
                    return
                evidence, observations = evidence_and_observations(review, by_id, saved)
                messages = prepare_review_request(budget, review, job, evidence, observations)
                schema = action_schema(observations)
                diagnostic = request_diagnostic(budget, messages, schema)
                diagnostic['visible'] = [{'source_id': c.source_id, 'line_start': c.line_start, 'line_end': c.line_end} for c in evidence]
                step = claim_review_step(store, review, attempt, len(completed), diagnostic)
                if step is None:
                    return
                result = _request_structured(messages, schema)
                validate_action(result, review['inputs']['candidates'], evidence, len(observations))
                observation = {'lookups': []}
                if result.action == 'read':
                    prior_requests = [x['request'] for o in observations for x in o['lookups']]
                    for request in result.requests:
                        if request.model_dump() in prior_requests:
                            observation['lookups'].append({'request': request.model_dump(), 'note': '相同查询已完成，保留前次结果，没有扩大引用权限。'})
                            continue
                        def accepts(chunks, proposed):
                            try:
                                prepare_review_request(budget, review, job, chunks, observations + [{'action': 'read', 'issue': result.issue, 'lookups': observation['lookups'] + [proposed]}])
                                return True
                            except ValueError:
                                return False
                        found, chunks = execute_lookup(request, by_id, evidence, accepts)
                        observation['lookups'].append(found)
                        evidence.extend(chunks)
                        prior_requests.append(request.model_dump())
                    # Check the following request including actual lookup metadata before publishing the read.
                    prepare_review_request(budget, review, job, evidence,
                        observations + [{'action': 'read', 'issue': result.issue, **observation}])
                check_current_review(store, review, current)
                finish_review_step(store, review, attempt, step, result.model_dump(), observation, diagnostic)
                step = None
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        if attempt:
            error = generation_error(exc, '定向复查')
            with store.practice.transaction() as db:
                if step:
                    db.execute("UPDATE outline_review_steps SET status='failed',attempt=NULL,error=?,updated_at=? WHERE id=? AND attempt=?",
                               (error, timestamp(), step['id'], step['attempt']))
                db.execute("UPDATE outline_reviews SET status='failed',attempt=NULL,error=?,updated_at=? WHERE id=? AND attempt=?",
                           (error, timestamp(), identity, attempt))
        elif claimed_lock:
            with store.practice.transaction() as db:
                db.execute("UPDATE outline_reviews SET status='failed',error=?,updated_at=? WHERE id=? AND status='queued' AND updated_at=?",
                    (generation_error(exc, '定向复查'), timestamp(), identity, review['updated_at']))


def continue_source_review(store, identity, current, call_limit=24):
    if type(call_limit) is not int or not 1 <= call_limit <= 200:
        raise ValueError('模型请求额度必须为 1–200 的整数。')
    review = get_review(store, identity)
    with store.lock(review['run_id']):
        review = get_review(store, identity)
        check_current_review(store, review, current)
        if review['status'] in ('completed', 'queued'):
            return
        with store.practice.transaction() as db:
            db.execute('UPDATE outline_group_runs SET allowance=calls_used+?,updated_at=? WHERE id=?', (call_limit, timestamp(), review['run_id']))
            db.execute("UPDATE outline_reviews SET status='queued',attempt=NULL,error=NULL,updated_at=? WHERE id=?", (timestamp(), identity))
            db.execute("UPDATE outline_review_steps SET status='failed',attempt=NULL WHERE review_id=? AND status='running'", (identity,))


def run_grouping_with_reviews(store, identity, current):
    from outline_grouping import run_grouping
    from outline_group_store import get_run
    run_grouping(store, identity, current)
    run = get_run(store, identity)
    if run['status'] != 'completed':
        return
    for group in rows(store, run['outline_id'])[1]:
        if group['run_id'] != identity or group['status'] != 'proposed' or group['proposal']['kind'] != 'needs_review':
            continue
        try:
            review_id = start_source_review(store, group['id'], 'auto-' + str(group['revision']), run['language'], current)
            run_source_review(store, review_id, current)
        except (OSError, RuntimeError, ValueError, KeyError):
            # A concurrent user edit must not erase the successful grouping.
            continue


def apply_source_review(store, identity, group_revision, current, canonical_id=None, unit_id=None):
    review = get_review(store, identity)
    result = review['result']
    if review['status'] != 'completed' or not result or result['outcome'] not in ('same_knowledge', 'duplicate_targets', 'distinct'):
        raise ValueError('这份复查仍需人工核对或修订候选，不能采用为归组决定。')
    if review['applied_group_id']:
        return review['applied_group_id']
    check_current_review(store, review, current)
    if review['group_revision'] != group_revision:
        raise OutlineBusy('复查对应的审核版本已变化。')
    with store.practice.transaction() as db:
        saved = db.execute('SELECT applied_group_id FROM outline_reviews WHERE id=?', (identity,)).fetchone()
        if saved['applied_group_id']:
            return saved['applied_group_id']
        old = db.execute('SELECT * FROM outline_groups WHERE id=?', (review['group_id'],)).fetchone()
        check_members(db, old, review['inputs'])
        if unit_id and not db.execute('SELECT 1 FROM knowledge_units WHERE id=?', (unit_id,)).fetchone():
            raise KeyError('所选知识点不存在。')
        if result['outcome'] == 'duplicate_targets':
            canonical_id = canonical_id or result['canonical_id']
            if canonical_id not in json.loads(old['members_json']):
                raise ValueError('代表目标必须属于复查的候选。')
        elif canonical_id is not None:
            raise ValueError('非重复关系不能选择代表目标。')
        # Preserve old model/user suggestion. An explicit user choice supersedes it.
        db.execute("UPDATE outline_groups SET status='rejected',revision=revision+1 WHERE id=?", (old['id'],))
        replacement = old['id']
        if result['outcome'] != 'distinct':
            proposal = {'kind': result['outcome'], 'candidate_ids': list(json.loads(old['members_json'])),
                        'name': result['name'], 'reason': result['reason'], 'canonical_id': canonical_id}
            model_proposal = {**proposal, 'canonical_id': result['canonical_id']}
            replacement = uuid4().hex
            db.execute("INSERT INTO outline_groups (id,run_id,outline_id,proposal_json,model_json,members_json,status,unit_id) VALUES (?,?,?,?,?,?,'accepted',?)",
                (replacement, old['run_id'], old['outline_id'], json.dumps(proposal, ensure_ascii=False),
                 json.dumps(model_proposal, ensure_ascii=False), old['members_json'], unit_id))
        db.execute('UPDATE outline_reviews SET applied_group_id=?,updated_at=? WHERE id=?', (replacement, timestamp(), identity))
    return replacement


def public_source_reviews(store, outline_id):
    recover_reviews(store, outline_id)
    job = store.get(outline_id)
    snapshots = {s['source_id']: s for s in job['snapshots']}
    with closing(store.practice._connect()) as db:
        identities = [r[0] for r in db.execute('SELECT r.id FROM outline_reviews r JOIN outline_groups g ON g.id=r.group_id WHERE g.outline_id=? ORDER BY r.rowid', (outline_id,))]
    public = []
    for identity in identities:
        r = get_review(store, identity)
        saved = review_steps(store, identity)
        with closing(store.practice._connect()) as db:
            group = db.execute('SELECT * FROM outline_groups WHERE id=?', (r['group_id'],)).fetchone()
            try:
                check_members(db, group, r['inputs'])
                stale = job['scope_fingerprint'] != r['inputs']['scope_fingerprint']
            except (ValueError, KeyError):
                stale = True
        def citation(c):
            s = snapshots[c['source_id']]
            return {**c, 'document_id': s['document_id'], 'name': s['name'],
                    'excerpt': '\n'.join(s['text'].splitlines()[c['line_start'] - 1:c['line_end']])}
        for step in saved:
            step['citations'] = [citation(l['actual_range']) for l in (step['observation'] or {}).get('lookups', []) if 'actual_range' in l]
        public.append({k: r[k] for k in ('id','group_id','group_revision','status','language','error','result','applied_group_id')} |
            {'stale': stale, 'rounds': sum(s['status'] == 'completed' and s['action']['action'] == 'read' for s in saved),
             'steps': [{k: s[k] for k in ('id','status','action','observation','diagnostic','error','citations')} for s in saved],
             'citations': [citation(c) for c in (r['result'] or {}).get('citations', [])]})
    return public
