"""Persistent directed investigations, sharing their parent comparison's allowance."""
import json
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from outline_store import OutlineBusy, timestamp
from outline_group_store import approved_group


def initialize_reviews(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS outline_reviews (
            id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES outline_groups(id),
            run_id TEXT NOT NULL REFERENCES outline_group_runs(id), request_id TEXT NOT NULL,
            group_revision INTEGER NOT NULL, language TEXT NOT NULL, inputs_json TEXT NOT NULL,
            status TEXT NOT NULL, attempt TEXT, error TEXT, result_json TEXT,
            applied_group_id TEXT REFERENCES outline_groups(id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(group_id,request_id));
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_outline_review ON outline_reviews(group_id)
            WHERE status IN ('queued','running','paused');
        CREATE TABLE IF NOT EXISTS outline_review_steps (
            id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES outline_reviews(id),
            position INTEGER NOT NULL, status TEXT NOT NULL, attempt TEXT,
            action_json TEXT, observation_json TEXT, diagnostic_json TEXT, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(review_id,position));
    ''')


def get_review(store, identity):
    with closing(store.practice._connect()) as db:
        row = db.execute('SELECT * FROM outline_reviews WHERE id=?', (identity,)).fetchone()
    if not row:
        raise KeyError('复查任务不存在。')
    r = dict(row)
    r['inputs'] = json.loads(r['inputs_json'])
    r['result'] = json.loads(r['result_json']) if r['result_json'] else None
    return r


def review_steps(store, identity):
    with closing(store.practice._connect()) as db:
        records = [dict(r) for r in db.execute('SELECT * FROM outline_review_steps WHERE review_id=? ORDER BY position', (identity,))]
    for r in records:
        for field in ('action', 'observation', 'diagnostic'):
            r[field] = json.loads(r[field + '_json']) if r[field + '_json'] else None
    return records


def check_members(db, group, inputs):
    if not group:
        raise KeyError('归组建议不存在。')
    if group['status'] != 'proposed' or group['revision'] != inputs['group_revision']:
        raise OutlineBusy('归组建议已经修改或审核，旧复查结果不再适用。')
    for c in inputs['candidates']:
        row = db.execute('SELECT revision,status FROM outline_candidates WHERE id=?', (c['id'],)).fetchone()
        if not row or row['revision'] != c['revision'] or row['status'] != 'proposed' or approved_group(db, c['id']):
            raise OutlineBusy('候选版本或加入状态已变化，旧复查结果不再适用。')


def require_review(db, review, attempt):
    row = db.execute('SELECT status,attempt FROM outline_reviews WHERE id=?', (review['id'],)).fetchone()
    if not row or row['status'] != 'running' or row['attempt'] != attempt:
        raise OutlineBusy('复查已暂停或执行版本已变化，在途结果未发布。')
    group = db.execute('SELECT * FROM outline_groups WHERE id=?', (review['group_id'],)).fetchone()
    if not group:
        raise KeyError('归组建议不存在。')
    check_members(db, group, review['inputs'])


def claim_review_step(store, review, attempt, position, diagnostic):
    token, now = uuid4().hex, timestamp()
    with store.practice.transaction() as db:
        require_review(db, review, attempt)
        parent = db.execute('SELECT calls_used,allowance FROM outline_group_runs WHERE id=?', (review['run_id'],)).fetchone()
        if parent['calls_used'] >= parent['allowance']:
            db.execute("UPDATE outline_reviews SET status='paused',attempt=NULL,error=?,updated_at=? WHERE id=?",
                       ('整合与复查的共同请求额度已用完；可增加额度继续。', now, review['id']))
            return None
        old = db.execute('SELECT * FROM outline_review_steps WHERE review_id=? AND position=?', (review['id'], position)).fetchone()
        if old and old['status'] == 'completed':
            raise OutlineBusy('完成的复查步骤不可重复请求。')
        identity = old['id'] if old else uuid4().hex
        db.execute('''INSERT INTO outline_review_steps
            (id,review_id,position,status,attempt,diagnostic_json,created_at,updated_at)
            VALUES (?,?,?,'running',?,?,?,?) ON CONFLICT(review_id,position) DO UPDATE SET
            status='running',attempt=excluded.attempt,error=NULL,diagnostic_json=excluded.diagnostic_json,updated_at=excluded.updated_at''',
            (identity, review['id'], position, token, json.dumps(diagnostic), now, now))
        db.execute('UPDATE outline_group_runs SET calls_used=calls_used+1,updated_at=? WHERE id=?', (now, review['run_id']))
    return {'id': identity, 'attempt': token}


def finish_review_step(store, review, attempt, step, action, observation, diagnostic):
    with store.practice.transaction() as db:
        require_review(db, review, attempt)
        if not db.execute("UPDATE outline_review_steps SET status='completed',attempt=NULL,action_json=?,observation_json=?,diagnostic_json=?,error=NULL,updated_at=? WHERE id=? AND status='running' AND attempt=?",
            (json.dumps(action, ensure_ascii=False), json.dumps(observation, ensure_ascii=False), json.dumps(diagnostic), timestamp(), step['id'], step['attempt'])).rowcount:
            raise OutlineBusy('复查步骤已过期。')


def finish_review(store, review, attempt, result):
    with store.practice.transaction() as db:
        require_review(db, review, attempt)
        db.execute("UPDATE outline_reviews SET status='completed',attempt=NULL,error=NULL,result_json=?,updated_at=? WHERE id=?",
                   (json.dumps(result, ensure_ascii=False), timestamp(), review['id']))


def pause_review(store, identity):
    get_review(store, identity)
    with store.practice.transaction() as db:
        db.execute("UPDATE outline_reviews SET status='paused',attempt=NULL,error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                   ('已暂停；有效补读保留，在途结果失效。', timestamp(), identity))
        db.execute("UPDATE outline_review_steps SET status='failed',attempt=NULL,error=? WHERE review_id=? AND status='running'",
                   ('补读判断曾被暂停或中断，可重试。', identity))


def recover_reviews(store, outline_id):
    # Invalidate active work before acquiring the worker lock: edits and pause
    # can revoke publication while a model request is still in flight.
    with store.practice.transaction() as db:
        active = db.execute("SELECT r.* FROM outline_reviews r JOIN outline_groups g ON g.id=r.group_id WHERE g.outline_id=? AND r.status IN ('queued','running','paused')", (outline_id,)).fetchall()
        for r in active:
            try:
                check_members(db, db.execute('SELECT * FROM outline_groups WHERE id=?', (r['group_id'],)).fetchone(), json.loads(r['inputs_json']))
            except (ValueError, KeyError):
                db.execute("UPDATE outline_reviews SET status='stale',attempt=NULL,error=?,updated_at=? WHERE id=?",
                    ('候选或建议已变化，旧复查仅供查看；请按当前版本重新复查。', timestamp(), r['id']))
                db.execute("UPDATE outline_review_steps SET status='failed',attempt=NULL,error=? WHERE review_id=? AND status='running'",
                    ('候选或建议已变化，在途结果失效。', r['id']))
    with closing(store.practice._connect()) as db:
        records = [dict(r) for r in db.execute("SELECT r.* FROM outline_reviews r JOIN outline_groups g ON g.id=r.group_id WHERE g.outline_id=? AND r.status IN ('queued','running')", (outline_id,))]
    for r in records:
        try:
            with store.lock(r['run_id']):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(r['updated_at'])).total_seconds()
                if r['status'] == 'running' or age > 60:
                    with store.practice.transaction() as db:
                        db.execute("UPDATE outline_reviews SET status='failed',attempt=NULL,error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                                   ('复查任务曾中断，可继续；完成的补读保留。', timestamp(), r['id']))
                        db.execute("UPDATE outline_review_steps SET status='failed',attempt=NULL,error=? WHERE review_id=? AND status='running'",
                                   ('步骤曾中断，可重试。', r['id']))
        except OutlineBusy:
            pass
