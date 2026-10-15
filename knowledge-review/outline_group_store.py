"""Review decisions are separate from immutable candidate evidence and FSRS items."""

import json
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from outline_store import OutlineBusy, timestamp


def initialize_groups(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS outline_group_runs (
            id TEXT PRIMARY KEY, outline_id TEXT NOT NULL REFERENCES learning_outlines(id),
            request_id TEXT NOT NULL, language TEXT NOT NULL, inputs_json TEXT NOT NULL,
            status TEXT NOT NULL, attempt TEXT, error TEXT, note TEXT, diagnostic_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(outline_id,request_id));
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_group_run ON outline_group_runs(outline_id)
            WHERE status IN ('queued','running');
        CREATE TABLE IF NOT EXISTS outline_groups (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES outline_group_runs(id),
            outline_id TEXT NOT NULL REFERENCES learning_outlines(id),
            proposal_json TEXT NOT NULL, members_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed', revision INTEGER NOT NULL DEFAULT 1,
            unit_id TEXT REFERENCES knowledge_units(id));
    """)
    # Preserve the generated suggestion separately from the latest user edits.
    if 'model_json' not in {r[1] for r in db.execute('PRAGMA table_info(outline_groups)')}:
        db.execute('ALTER TABLE outline_groups ADD COLUMN model_json TEXT')
        db.execute('UPDATE outline_groups SET model_json=proposal_json WHERE model_json IS NULL')
    if 'support_json' not in {r[1] for r in db.execute('PRAGMA table_info(outline_groups)')}:
        db.execute('ALTER TABLE outline_groups ADD COLUMN support_json TEXT')
    from outline_group_steps import initialize_steps
    initialize_steps(db)
    from outline_review_store import initialize_reviews
    initialize_reviews(db)


def rows(store, outline_id):
    with closing(store.practice._connect()) as db:
        runs = [dict(r) for r in db.execute('SELECT * FROM outline_group_runs WHERE outline_id=? ORDER BY created_at', (outline_id,))]
        groups = [dict(r) for r in db.execute('SELECT * FROM outline_groups WHERE outline_id=? ORDER BY rowid', (outline_id,))]
    for r in runs:
        r['inputs'] = json.loads(r.pop('inputs_json'))
        r['diagnostic'] = json.loads(r.pop('diagnostic_json') or 'null')
        r['plan'] = json.loads(r.pop('plan_json') or 'null')
    for r in groups:
        r['proposal'] = json.loads(r.pop('proposal_json'))
        r['model_proposal'] = json.loads(r.pop('model_json'))
        r['members'] = json.loads(r.pop('members_json'))
        r['support'] = json.loads(r.pop('support_json') or '[]')
    return runs, groups


def invalidate_candidate(db, identity):
    for row in db.execute("SELECT id,members_json FROM outline_groups WHERE status IN ('proposed','accepted')").fetchall():
        if identity in json.loads(row['members_json']):
            db.execute("UPDATE outline_groups SET status='stale',revision=revision+1 WHERE id=?", (row['id'],))


def approved_group(db, identity):
    row = db.execute('SELECT outline_id FROM outline_candidates WHERE id=?', (identity,)).fetchone()
    if not row:
        raise KeyError('目标不存在。')
    for group in db.execute("SELECT * FROM outline_groups WHERE outline_id=? AND status='accepted'", (row['outline_id'],)):
        if identity in json.loads(group['members_json']):
            return dict(group)
    return None


def enrollment_token(db, identity):
    group = approved_group(db, identity)
    if not group:
        return None
    proposal = json.loads(group['proposal_json'])
    if proposal['kind'] == 'duplicate_targets' and proposal['canonical_id'] != identity:
        raise OutlineBusy('这个重复目标已保留为原始记录，请选择归组中的代表目标加入计划。')
    return (group['id'], group['revision'])


def resolve_unit(db, identity, token, requested_unit, learning):
    if enrollment_token(db, identity) != token:
        raise OutlineBusy('归组审核已变化，未保存这次首题；请刷新后重试。')
    if token is None:
        return requested_unit
    group = approved_group(db, identity)
    if requested_unit and requested_unit != group['unit_id']:
        raise OutlineBusy('已确认归组的目标使用统一知识点，请在归组审核中选择知识点。')
    unit = group['unit_id']
    if not unit:
        proposal = json.loads(group['proposal_json'])
        unit = uuid4().hex
        knowledge = learning.knowledge.model_copy(update={'name': proposal['name']})
        db.execute('INSERT INTO knowledge_units VALUES (?,?,?,?)',
                   (unit, proposal['name'], knowledge.model_dump_json(), timestamp()))
        # Binding happens in the same transaction as the first item; review revision stays stable.
        db.execute('UPDATE outline_groups SET unit_id=? WHERE id=?', (unit, group['id']))
    return unit


def create_run(store, outline_id, request_id, language, inputs, call_limit=24):
    encoded = json.dumps(inputs, ensure_ascii=False, sort_keys=True)
    with store.practice.transaction() as db:
        existing = db.execute('SELECT * FROM outline_group_runs WHERE outline_id=? AND request_id=?', (outline_id, request_id)).fetchone()
        if existing:
            if existing['inputs_json'] != encoded or existing['language'] != language:
                raise OutlineBusy('这个请求标识对应的候选或语言已变化，请重新生成。')
            return existing['id']
        if db.execute("SELECT 1 FROM outline_group_runs WHERE outline_id=? AND status IN ('queued','running')", (outline_id,)).fetchone():
            raise OutlineBusy('这份清单已有归组任务，请等待或重试原任务。')
        identity, now = uuid4().hex, timestamp()
        db.execute("""INSERT INTO outline_group_runs
            (id,outline_id,request_id,language,inputs_json,status,created_at,updated_at,plan_json,allowance)
            VALUES (?,?,?,?,?,'queued',?,?,?,?)""", (identity, outline_id, request_id, language, encoded, now, now,
                json.dumps({'version': 2}), call_limit))
    return identity


def get_run(store, identity):
    with closing(store.practice._connect()) as db:
        row = db.execute('SELECT * FROM outline_group_runs WHERE id=?', (identity,)).fetchone()
        if not row:
            raise KeyError('归组任务不存在。')
        return dict(row)


def recover_runs(store, outline_id):
    runs, _ = rows(store, outline_id)
    for run in runs:
        if run['status'] not in ('queued', 'running'):
            continue
        try:
            with store.lock(run['id']):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(run['updated_at'])).total_seconds()
                if run['status'] == 'running' or age > 60:
                    with store.practice.transaction() as db:
                        db.execute("UPDATE outline_group_runs SET status='failed',attempt=NULL,error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                                   ('归组任务曾被中断，可以重试；原始候选完整保留。', timestamp(), run['id']))
                        db.execute("UPDATE outline_group_steps SET status='failed',attempt=NULL,error=? WHERE run_id=? AND status='running'",
                                   ('步骤曾被中断，可继续。', run['id']))
        except OutlineBusy:
            pass


def review_group(store, identity, revision, decision, name, reason, canonical_id, unit_id):
    """Only user approval changes membership; never migrates existing review items."""
    with store.practice.transaction() as db:
        row = db.execute('SELECT * FROM outline_groups WHERE id=?', (identity,)).fetchone()
        if not row:
            raise KeyError('归组建议不存在。')
        old = json.loads(row['proposal_json'])
        proposal = {**old, 'name': name.strip(), 'reason': reason.strip(), 'canonical_id': canonical_id}
        if not proposal['name'] or not proposal['reason']:
            raise ValueError('归组名称和说明不能为空。')
        members = json.loads(row['members_json'])
        if old['kind'] == 'duplicate_targets' and canonical_id not in members:
            raise ValueError('代表目标必须属于这组候选。')
        if old['kind'] != 'duplicate_targets' and canonical_id is not None:
            raise ValueError('只有重复目标需要选择代表目标。')
        if decision == 'accepted' and old['kind'] == 'needs_review':
            raise ValueError('依据或条件尚不明确；请保留独立目标，后续重新整理。')
        # A repeated response lost in transit must not create another decision or unit.
        if row['status'] == decision and proposal == old and (unit_id is None or unit_id == row['unit_id']):
            return row['revision']
        if row['revision'] != revision or row['status'] not in ('proposed', 'accepted'):
            raise OutlineBusy('归组建议已变化，请刷新后重新审核。')
        if row['status'] == 'accepted' and decision != 'rejected':
            raise OutlineBusy('已接受的归组不能直接改写；可撤销后重新生成，已有计划保持原状。')
        if decision != 'rejected':
            for candidate_id, version in members.items():
                candidate = db.execute('SELECT revision,status FROM outline_candidates WHERE id=?', (candidate_id,)).fetchone()
                if not candidate or candidate['revision'] != version or candidate['status'] != 'proposed':
                    raise OutlineBusy('候选已修改或正在加入计划，请刷新后重新整理。')
                other = approved_group(db, candidate_id)
                if other and other['id'] != identity:
                    raise OutlineBusy('候选已属于另一组已确认建议，请先撤销冲突归组。')
            if unit_id and not db.execute('SELECT 1 FROM knowledge_units WHERE id=?', (unit_id,)).fetchone():
                raise KeyError('所选知识点不存在。')
        db.execute('UPDATE outline_groups SET proposal_json=?,status=?,revision=revision+1,unit_id=? WHERE id=?',
                   (json.dumps(proposal, ensure_ascii=False), decision, row['unit_id'] if decision == 'rejected' else unit_id, identity))
    return revision + 1
