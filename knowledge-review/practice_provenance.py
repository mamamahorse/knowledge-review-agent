"""Revocable discussion provenance; attached SQLite transactions guard publication."""

import json


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS practice_basis (
            session_id TEXT PRIMARY KEY REFERENCES practice_sessions(id),
            dependencies_json TEXT NOT NULL, references_json TEXT NOT NULL,
            kind TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS chat_practice_jobs (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
            request_json TEXT NOT NULL, status TEXT NOT NULL, attempt TEXT,
            dependencies_json TEXT NOT NULL DEFAULT '[]', session_id TEXT, error TEXT,
            UNIQUE(conversation_id,request_id));
    ''')
    if 'diagnostics_json' not in {r[1] for r in db.execute('PRAGMA table_info(chat_practice_jobs)')}:
        db.execute('ALTER TABLE chat_practice_jobs ADD COLUMN diagnostics_json TEXT')


def check_dependencies(db, dependencies, schema='memory'):
    for dependency in dependencies:
        row = db.execute(f'SELECT memory_revision FROM {schema}.conversations WHERE id=?',
                         (dependency['conversation_id'],)).fetchone()
        if row is None or row[0] != dependency['revision']:
            raise ValueError('聊天出题来源已删除或失效；旧题只保留学习记录，请根据当前资料重新出题。')


def check_session(db, session_id):
    row = db.execute('SELECT * FROM practice_basis WHERE session_id=?', (session_id,)).fetchone()
    if row:
        if row['revoked']:
            raise ValueError('这道题的聊天来源已撤销，不能继续评分、复习或加入计划。真实作答和复习日期保留。')
        check_dependencies(db, json.loads(row['dependencies_json']))


def revoke(db, identities, schema='learning'):
    """Called inside the same transaction that removes the source conversation."""
    for identity in identities:
        db.execute(f'''UPDATE {schema}.practice_basis SET revoked=1,references_json='[]'
            WHERE EXISTS (SELECT 1 FROM json_each(dependencies_json)
                          WHERE json_extract(value,'$.conversation_id')=?)''', (identity,))
        db.execute(f'''UPDATE {schema}.chat_practice_jobs SET status='revoked',attempt=NULL,
            diagnostics_json=NULL,
            error='聊天来源已撤销，请从当前资料重新出题。'
            WHERE conversation_id=? OR EXISTS (SELECT 1 FROM json_each(dependencies_json)
                          WHERE json_extract(value,'$.conversation_id')=?)''', (identity, identity))


def basis_record(db, session_id):
    row = db.execute('SELECT * FROM practice_basis WHERE session_id=?', (session_id,)).fetchone()
    if not row:
        return None
    result = {**dict(row), 'dependencies': json.loads(row['dependencies_json']),
              'references': json.loads(row['references_json'])}
    if not result['revoked']:
        try:
            check_dependencies(db, result['dependencies'])
        except ValueError:
            result.update(revoked=1, references=[])
    return result
