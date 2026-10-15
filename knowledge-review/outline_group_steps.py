"""Short transactions for resumable comparison attempts; network stays outside."""

import json
from contextlib import closing
from hashlib import sha256
from uuid import uuid4

from outline_store import OutlineBusy, timestamp


def initialize_steps(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(outline_group_runs)')}
    for name, definition in (('plan_json', 'TEXT'), ('calls_used', 'INTEGER NOT NULL DEFAULT 0'),
                             ('allowance', 'INTEGER NOT NULL DEFAULT 24')):
        if name not in columns:
            db.execute(f'ALTER TABLE outline_group_runs ADD COLUMN {name} {definition}')
    db.execute('''CREATE TABLE IF NOT EXISTS outline_group_steps (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES outline_group_runs(id),
        kind TEXT NOT NULL, scope TEXT NOT NULL, inputs_json TEXT NOT NULL,
        status TEXT NOT NULL, attempt TEXT, result_json TEXT, diagnostic_json TEXT,
        error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def steps(store, run_id):
    with closing(store.practice._connect()) as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM outline_group_steps WHERE run_id=? ORDER BY rowid', (run_id,))]
    return [{**r, 'inputs': json.loads(r['inputs_json']),
             'result': json.loads(r['result_json']) if r['result_json'] else None,
             'diagnostic': json.loads(r['diagnostic_json']) if r['diagnostic_json'] else None} for r in rows]


def require_attempt(db, run_id, attempt):
    row = db.execute('SELECT * FROM outline_group_runs WHERE id=?', (run_id,)).fetchone()
    if not row or row['status'] != 'running' or row['attempt'] != attempt:
        raise OutlineBusy('整合已暂停或任务版本已变化，未发布在途结果。')
    return row


def claim_step(store, run, kind, scope, pairs, diagnostic):
    encoded = json.dumps(pairs, sort_keys=True)
    identity = sha256(f'{run["id"]}:{kind}:{scope}:{encoded}'.encode()).hexdigest()
    token, now = uuid4().hex, timestamp()
    with store.practice.transaction() as db:
        current = require_attempt(db, run['id'], run['attempt'])
        if current['calls_used'] >= current['allowance']:
            db.execute("UPDATE outline_group_runs SET status='paused',attempt=NULL,note=?,updated_at=? WHERE id=?",
                       ('本次模型请求额度已用完；已完成步骤保留，可继续。', now, run['id']))
            return None
        old = db.execute('SELECT * FROM outline_group_steps WHERE id=?', (identity,)).fetchone()
        if old and old['status'] == 'completed':
            raise OutlineBusy('已完成步骤不应重复请求。')
        db.execute('''INSERT INTO outline_group_steps
            (id,run_id,kind,scope,inputs_json,status,attempt,diagnostic_json,created_at,updated_at)
            VALUES (?,?,?,?,?,'running',?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            status='running',attempt=excluded.attempt,error=NULL,diagnostic_json=excluded.diagnostic_json,updated_at=excluded.updated_at''',
            (identity, run['id'], kind, scope, encoded, token, json.dumps(diagnostic), now, now))
        db.execute('UPDATE outline_group_runs SET calls_used=calls_used+1,updated_at=? WHERE id=?', (now, run['id']))
    return {'id': identity, 'attempt': token}


def finish_step(store, run, step, result):
    with store.practice.transaction() as db:
        require_attempt(db, run['id'], run['attempt'])
        changed = db.execute("UPDATE outline_group_steps SET status='completed',attempt=NULL,result_json=?,error=NULL,updated_at=? WHERE id=? AND status='running' AND attempt=?",
            (json.dumps(result, ensure_ascii=False), timestamp(), step['id'], step['attempt'])).rowcount
        if not changed:
            raise OutlineBusy('步骤已过期，未发布旧结果。')


def fail_step(store, step, message):
    with store.practice.transaction() as db:
        db.execute("UPDATE outline_group_steps SET status='failed',attempt=NULL,error=?,updated_at=? WHERE id=? AND attempt=?",
                   (message, timestamp(), step['id'], step['attempt']))


def pause_grouping(store, identity):
    with store.practice.transaction() as db:
        db.execute("UPDATE outline_group_runs SET status='paused',attempt=NULL,note=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                   ('已暂停，完成步骤保留；在途旧结果不发布。', timestamp(), identity))
        db.execute("UPDATE outline_group_steps SET status='failed',attempt=NULL,error=? WHERE run_id=? AND status='running'",
                   ('步骤曾被暂停或中断，可继续。', identity))
