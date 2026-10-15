"""Routes are versioned advisory records, independent of targets and FSRS."""
import json
from contextlib import closing
from uuid import uuid4
from outline_store import OutlineBusy, timestamp


def initialize_routes(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS outline_routes (
        id TEXT PRIMARY KEY, outline_id TEXT NOT NULL REFERENCES learning_outlines(id),
        request_id TEXT NOT NULL, language TEXT NOT NULL, inputs_json TEXT NOT NULL,
        status TEXT NOT NULL, attempt TEXT, error TEXT, cursor INTEGER NOT NULL DEFAULT 0,
        calls_used INTEGER NOT NULL DEFAULT 0, allowance INTEGER NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, order_json TEXT, skipped_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(outline_id,request_id));
      CREATE UNIQUE INDEX IF NOT EXISTS one_running_outline_route ON outline_routes(outline_id)
        WHERE status IN ('queued','running');
      CREATE TABLE IF NOT EXISTS outline_route_pages (
        id TEXT PRIMARY KEY, route_id TEXT NOT NULL REFERENCES outline_routes(id), position INTEGER NOT NULL,
        status TEXT NOT NULL, attempt TEXT, inputs_json TEXT NOT NULL, result_json TEXT,
        diagnostic_json TEXT NOT NULL, error TEXT, UNIQUE(route_id,position));
      CREATE TABLE IF NOT EXISTS outline_route_edges (
        id TEXT PRIMARY KEY, route_id TEXT NOT NULL REFERENCES outline_routes(id), pair_index INTEGER,
        model_json TEXT, decision_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed',
        revision INTEGER NOT NULL DEFAULT 1, UNIQUE(route_id,pair_index));
    ''')


def decode(row):
    r = dict(row)
    for k in ('inputs','order','skipped','result','diagnostic','model','decision'):
        if k + '_json' in r: r[k] = json.loads(r.pop(k + '_json') or 'null')
    return r


def route_record(store, identity):
    with closing(store.practice._connect()) as db:
        row = db.execute('SELECT * FROM outline_routes WHERE id=?', (identity,)).fetchone()
    if not row: raise KeyError('学习路线不存在。')
    return decode(row)


def route_pages(store, identity):
    with closing(store.practice._connect()) as db:
        return [decode(r) for r in db.execute('SELECT * FROM outline_route_pages WHERE route_id=? ORDER BY position', (identity,))]


def route_edges(db, identity):
    return [decode(r) for r in db.execute('SELECT * FROM outline_route_edges WHERE route_id=? ORDER BY rowid', (identity,))]


def require_attempt(db, route, attempt):
    r = db.execute('SELECT * FROM outline_routes WHERE id=?', (route['id'],)).fetchone()
    if not r or r['status'] != 'running' or r['attempt'] != attempt:
        raise OutlineBusy('路线已暂停或执行版本已变化，在途结果未发布。')
    from outline_routes import check_stamp
    check_stamp(db, route)
    return r


def claim_page(store, route, attempt, comparisons, diagnostic):
    with store.practice.transaction() as db:
        r = require_attempt(db, route, attempt)
        if r['calls_used'] >= r['allowance']:
            db.execute("UPDATE outline_routes SET status='paused',attempt=NULL,error=? WHERE id=?",
                ('路线请求额度已用完；可增加额度继续。', route['id']))
            return None
        old = db.execute('SELECT id,status FROM outline_route_pages WHERE route_id=? AND position=?', (route['id'], r['cursor'])).fetchone()
        if old and old['status'] == 'completed': raise OutlineBusy('完成的关系判断不能重复请求。')
        key, token = old['id'] if old else uuid4().hex, uuid4().hex
        db.execute("""INSERT INTO outline_route_pages (id,route_id,position,status,attempt,inputs_json,diagnostic_json)
            VALUES (?,?,?,'running',?,?,?) ON CONFLICT(route_id,position) DO UPDATE SET status='running',attempt=excluded.attempt,
            inputs_json=excluded.inputs_json,diagnostic_json=excluded.diagnostic_json,error=NULL""",
            (key, route['id'], r['cursor'], token, json.dumps(comparisons), json.dumps(diagnostic)))
        db.execute('UPDATE outline_routes SET calls_used=calls_used+1,updated_at=? WHERE id=?', (timestamp(), route['id']))
    return {'id':key,'attempt':token}


def publish_page(store, route, attempt, page, result, edges):
    with store.practice.transaction() as db:
        require_attempt(db, route, attempt)
        if not db.execute("UPDATE outline_route_pages SET status='completed',attempt=NULL,result_json=? WHERE id=? AND status='running' AND attempt=?",
            (result.model_dump_json(), page['id'], page['attempt'])).rowcount: raise OutlineBusy('关系判断已过期。')
        for index, edge in edges:
            text = json.dumps(edge, ensure_ascii=False)
            db.execute('INSERT INTO outline_route_edges (id,route_id,pair_index,model_json,decision_json) VALUES (?,?,?,?,?)',
                (uuid4().hex, route['id'], index, text, text))
        db.execute('UPDATE outline_routes SET cursor=cursor+?,updated_at=? WHERE id=?', (len(result.comparisons), timestamp(), route['id']))


def pause_route(store, identity):
    route_record(store, identity)
    with store.practice.transaction() as db:
        db.execute("UPDATE outline_routes SET status='paused',attempt=NULL,error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
            ('已暂停，完成判断保留，在途结果失效。', timestamp(), identity))
        db.execute("UPDATE outline_route_pages SET status='failed',attempt=NULL,error=? WHERE route_id=? AND status='running'",
            ('请求曾暂停或中断，可继续。', identity))


def recover_routes(store, outline_id):
    from datetime import datetime, timezone
    with closing(store.practice._connect()) as db:
        tasks = [dict(r) for r in db.execute("SELECT * FROM outline_routes WHERE outline_id=? AND status IN ('queued','running')", (outline_id,))]
    for r in tasks:
        try:
            with store.lock(r['id']):
                with closing(store.practice._connect()) as db:
                    r = dict(db.execute('SELECT * FROM outline_routes WHERE id=?', (r['id'],)).fetchone())
                if r['status'] not in ('queued', 'running'):
                    continue
                if r['status'] == 'running' or (datetime.now(timezone.utc)-datetime.fromisoformat(r['updated_at'])).total_seconds()>60:
                    with store.practice.transaction() as db:
                        db.execute("UPDATE outline_routes SET status='failed',attempt=NULL,error=? WHERE id=? AND status IN ('queued','running')", ('路线生成曾中断，可以继续。', r['id']))
                        db.execute("UPDATE outline_route_pages SET status='failed',attempt=NULL,error=? WHERE route_id=? AND status='running'", ('请求曾中断，可以继续。', r['id']))
        except OutlineBusy: pass
