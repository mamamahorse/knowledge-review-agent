"""Append-only review versions over original outline candidates; no learning records."""

from contextlib import closing
from hashlib import sha256
import json
from uuid import uuid4

from outline_store import OutlineBusy, timestamp


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS outline_final_reviews (
            id TEXT PRIMARY KEY, outline_id TEXT NOT NULL REFERENCES learning_outlines(id),
            fingerprint TEXT NOT NULL, inputs_json TEXT NOT NULL, status TEXT NOT NULL,
            attempt TEXT, plan_json TEXT, steps_json TEXT NOT NULL DEFAULT '[]',
            result_json TEXT, error TEXT, calls_used INTEGER NOT NULL DEFAULT 0,
            allowance INTEGER NOT NULL DEFAULT 24, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(outline_id,fingerprint));
    """)
    if 'origin_final_review_id' not in {r[1] for r in db.execute('PRAGMA table_info(outline_candidates)')}:
        db.execute('ALTER TABLE outline_candidates ADD COLUMN origin_final_review_id TEXT')


def inputs(job, exclude_review=None):
    return dict(scope_fingerprint=job['scope_fingerprint'], objective=job['objective'],language=job['response_language'],
        candidates=[{k:c[k] for k in ('id','revision','learning')} for c in job['candidates']
                    if exclude_review is None or c.get('origin_final_review_id')!=exclude_review])


def fingerprint(value):
    return sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def record(store, identity):
    with closing(store.practice._connect()) as db:
        row=db.execute('SELECT * FROM outline_final_reviews WHERE id=?',(identity,)).fetchone()
    if row is None: raise KeyError('整体复查不存在。')
    result=dict(row)
    for key in ('inputs','plan','steps','result'):
        result[key]=json.loads(result.pop(key+'_json') or 'null')
    return result


def latest(store, outline_id):
    with closing(store.practice._connect()) as db:
        row=db.execute('SELECT id FROM outline_final_reviews WHERE outline_id=? ORDER BY rowid DESC LIMIT 1',(outline_id,)).fetchone()
    return record(store,row['id']) if row else None


def start(store, job):
    if job['status']!='completed': raise OutlineBusy('原文提取完成后才能整体复查。')
    previous=latest(store,job['id'])
    if previous and not is_stale(previous,job): return previous['id']
    value=inputs(job); digest=fingerprint(value)
    with store.practice.transaction() as db:
        row=db.execute('SELECT id FROM outline_final_reviews WHERE outline_id=? AND fingerprint=?',(job['id'],digest)).fetchone()
        if row: return row['id']
        identity=uuid4().hex; now=timestamp()
        db.execute("INSERT INTO outline_final_reviews(id,outline_id,fingerprint,inputs_json,status,created_at,updated_at) VALUES (?,?,?,?,'queued',?,?)",
                   (identity,job['id'],digest,json.dumps(value,ensure_ascii=False),now,now))
    return identity


def require_inputs(store, review, current):
    from outline_service import require_current
    job=store.get(review['outline_id']); require_current(job,current)
    if fingerprint(inputs(job,review['id']))!=review['fingerprint']:
        raise OutlineBusy('整体复查期间原始候选已变化，旧结果未发布；请按当前候选重新复查。')
    return job


def active(db, identity, attempt):
    row=db.execute('SELECT * FROM outline_final_reviews WHERE id=?',(identity,)).fetchone()
    if not row or row['status']!='running' or row['attempt']!=attempt:
        raise OutlineBusy('整体复查已暂停或执行版本过期，未发布迟到结果。')
    return row


def pause(store, identity):
    with store.practice.transaction() as db:
        db.execute("UPDATE outline_final_reviews SET status='paused',attempt=NULL,updated_at=? WHERE id=? AND status IN ('queued','running')",(timestamp(),identity))


def resume(store, identity, current, allowance=24):
    if type(allowance) is not int or not 1<=allowance<=200: raise ValueError('复查请求额度须为1–200。')
    with store.lock(identity):
        review=record(store,identity); require_inputs(store,review,current)
        if review['status'] in ('completed','queued'): return
        with store.practice.transaction() as db:
            db.execute("UPDATE outline_final_reviews SET status='queued',attempt=NULL,error=NULL,allowance=calls_used+?,updated_at=? WHERE id=?",(allowance,timestamp(),identity))


def public(store, job):
    review=latest(store,job['id'])
    if not review: return None
    if review['status'] in ('queued','running'):
        from datetime import datetime, timezone
        try:
            with store.lock(review['id']):
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(review['updated_at'])).total_seconds()
                if review['status']=='running' or age>60:
                    pause(store,review['id']); review=record(store,review['id'])
        except OutlineBusy: pass
    stale=is_stale(review,job)
    return {k:review[k] for k in ('id','status','error','calls_used','allowance','result','steps')} | dict(stale=stale,
        windows_total=len((review['plan'] or {}).get('windows',[])),
        windows_completed=sum(s['kind']=='coverage' and s.get('status')=='completed' for s in review['steps']))


def is_stale(review,job):
    stale=fingerprint(inputs(job,review['id']))!=review['fingerprint']
    if review['result']:
        revisions={c['id']:c['revision'] for c in job['candidates']}
        stale=stale or any(revisions.get(k)!=v for k,v in review['result']['candidate_revisions'].items())
    return stale
