"""Resumable collection proposals and atomic opt-in enrollment in the learning DB."""

import json
import os
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from learning_types import LearningDraft


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def initialize_outlines(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS learning_outlines (
            id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id),
            request_id TEXT NOT NULL, objective TEXT NOT NULL, response_language TEXT NOT NULL,
            scope_fingerprint TEXT NOT NULL, snapshots_json TEXT NOT NULL, sections_json TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(collection_id,request_id));
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_outline ON learning_outlines(collection_id)
            WHERE status IN ('queued','running');
        CREATE TABLE IF NOT EXISTS outline_batches (
            id TEXT PRIMARY KEY, outline_id TEXT NOT NULL REFERENCES learning_outlines(id),
            position INTEGER NOT NULL, section_id TEXT NOT NULL, source_id TEXT NOT NULL,
            line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, status TEXT NOT NULL,
            attempt TEXT, error TEXT, note TEXT NOT NULL DEFAULT '', UNIQUE(outline_id,position));
        CREATE TABLE IF NOT EXISTS outline_candidates (
            id TEXT PRIMARY KEY, outline_id TEXT NOT NULL REFERENCES learning_outlines(id),
            batch_id TEXT NOT NULL REFERENCES outline_batches(id), position INTEGER NOT NULL,
            proposal_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'proposed', attempt TEXT, error TEXT,
            item_id TEXT REFERENCES review_items(id), session_id TEXT REFERENCES practice_sessions(id),
            UNIQUE(batch_id,position));
        CREATE TABLE IF NOT EXISTS outline_context_steps (
            id TEXT PRIMARY KEY, outline_id TEXT NOT NULL REFERENCES learning_outlines(id),
            kind TEXT NOT NULL, scope_id TEXT NOT NULL, inputs_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempt TEXT, error TEXT,
            result_json TEXT, request_json TEXT, UNIQUE(outline_id,id));
    """)
    if 'plan_json' not in {r[1] for r in conn.execute('PRAGMA table_info(learning_outlines)')}:
        conn.execute("ALTER TABLE learning_outlines ADD COLUMN plan_json TEXT")
    for name in ('visible_json', 'request_json'):
        if name not in {r[1] for r in conn.execute('PRAGMA table_info(outline_batches)')}:
            conn.execute(f'ALTER TABLE outline_batches ADD COLUMN {name} TEXT')


class OutlineBusy(ValueError):
    pass


class OutlineStore:
    def __init__(self, practice):
        self.practice = practice
        with closing(practice._connect()) as db, db:
            initialize_outlines(db)
            from outline_group_store import initialize_groups
            initialize_groups(db)
            from outline_route_store import initialize_routes
            initialize_routes(db)

    @contextmanager
    def lock(self, identity):
        if len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            raise KeyError("清单记录不存在。")
        path = self.practice.path.with_name(f"{self.practice.path.stem}-outline-{identity}.lock")
        with path.open('a+b') as file:
            if file.tell() == 0:
                file.write(b'0')
                file.flush()
            file.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise OutlineBusy("这份清单或目标正在处理，请等待完成。") from exc
            try:
                yield
            finally:
                file.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file, fcntl.LOCK_UN)

    def create(self, collection_id, request_id, objective, language, scope, snapshots, sections, batches, plan=None):
        with self.practice.transaction() as db:
            old = db.execute("SELECT * FROM learning_outlines WHERE collection_id=? AND request_id=?",
                             (collection_id, request_id)).fetchone()
            if old:
                if (old['scope_fingerprint'], old['objective'], old['response_language']) != (scope, objective, language):
                    raise OutlineBusy("同一提交标识的资料或目标已变化，请新建一份清单。")
                return old['id']
            if db.execute("SELECT id FROM learning_outlines WHERE collection_id=? AND status IN ('queued','running')",
                          (collection_id,)).fetchone():
                raise OutlineBusy("这个集合已有清单正在生成，请先等待或暂停。")
            identity, now = uuid4().hex, timestamp()
            db.execute("""INSERT INTO learning_outlines
                (id,collection_id,request_id,objective,response_language,scope_fingerprint,snapshots_json,sections_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'queued',?,?)""",
                       (identity, collection_id, request_id, objective, language, scope,
                        json.dumps(snapshots, ensure_ascii=False), json.dumps(sections, ensure_ascii=False), now, now))
            if plan is not None:
                db.execute('UPDATE learning_outlines SET plan_json=? WHERE id=?', (json.dumps(plan, ensure_ascii=False), identity))
            for index, batch in enumerate(batches):
                db.execute("""INSERT INTO outline_batches
                    (id,outline_id,position,section_id,source_id,line_start,line_end,status,error,note)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (uuid4().hex, identity, index, batch['section_id'], batch['source_id'],
                    batch['line_start'], batch['line_end'], batch.get('status', 'pending'), batch.get('error'), batch.get('note', '')))
        return identity

    def get(self, identity):
        with closing(self.practice._connect()) as db:
            row = db.execute("SELECT * FROM learning_outlines WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("找不到这份学习清单。")
            batches = [dict(r) for r in db.execute("SELECT * FROM outline_batches WHERE outline_id=? ORDER BY position", (identity,))]
            candidates = [dict(r) for r in db.execute("""SELECT c.* FROM outline_candidates c JOIN outline_batches b ON b.id=c.batch_id
                WHERE c.outline_id=? ORDER BY b.position,c.position""", (identity,))]
            steps = [dict(r) for r in db.execute('SELECT * FROM outline_context_steps WHERE outline_id=? ORDER BY rowid', (identity,))]
        return {**dict(row), 'snapshots': json.loads(row['snapshots_json']), 'sections': json.loads(row['sections_json']),
                'plan': json.loads(row['plan_json']) if row['plan_json'] else None,
                'context_steps': [{**s, 'inputs': json.loads(s['inputs_json']),
                    'result': json.loads(s['result_json']) if s['result_json'] else None,
                    'request': json.loads(s['request_json']) if s['request_json'] else None} for s in steps],
                'batches': batches, 'candidates': [{**r, 'learning': json.loads(r['proposal_json'])} for r in candidates]}

    def list(self, collection_id):
        with closing(self.practice._connect()) as db:
            return [dict(row) for row in db.execute("""SELECT id,collection_id,objective,response_language,status,created_at,updated_at,scope_fingerprint
                FROM learning_outlines WHERE collection_id=? ORDER BY created_at DESC""", (collection_id,))]

    def candidate(self, identity):
        with closing(self.practice._connect()) as db:
            row = db.execute("SELECT * FROM outline_candidates WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("找不到这个待审核目标。")
        return {**dict(row), 'learning': json.loads(row['proposal_json'])}

    def recover(self, identity):
        # Caller owns the OS lock; a live worker is never reset by a page read.
        with self.practice.transaction() as db:
            db.execute("UPDATE outline_batches SET status='failed',attempt=NULL,error=? WHERE outline_id=? AND status='running'",
                       ('生成曾被中断；已有结果保留，可以继续。', identity))
            db.execute("UPDATE outline_context_steps SET status='failed',attempt=NULL,error=? WHERE outline_id=? AND status='running'",
                       ('背景整理曾被中断，可以继续。', identity))
            db.execute("UPDATE learning_outlines SET status='paused' WHERE id=? AND status IN ('queued','running')", (identity,))

    def resume(self, identity):
        with self.practice.transaction() as db:
            job = db.execute("SELECT collection_id FROM learning_outlines WHERE id=?", (identity,)).fetchone()
            if db.execute("SELECT id FROM learning_outlines WHERE collection_id=? AND id!=? AND status IN ('queued','running')",
                          (job['collection_id'], identity)).fetchone():
                raise OutlineBusy("这个集合已有另一份清单正在生成，请先暂停它。")
            db.execute("UPDATE outline_batches SET status='pending',attempt=NULL,error=NULL WHERE outline_id=? AND status='running'", (identity,))
            db.execute("UPDATE outline_batches SET status='pending',error=NULL WHERE outline_id=? AND status='failed'", (identity,))
            db.execute("UPDATE outline_context_steps SET status='pending',attempt=NULL,error=NULL WHERE outline_id=? AND status IN ('failed','running')", (identity,))
            db.execute("UPDATE learning_outlines SET status='queued',updated_at=? WHERE id=?", (timestamp(), identity))

    def start(self, identity):
        with self.practice.transaction() as db:
            return bool(db.execute("UPDATE learning_outlines SET status='running',updated_at=? WHERE id=? AND status='queued'",
                                   (timestamp(), identity)).rowcount)

    def pause(self, identity):
        self.get(identity)
        with self.practice.transaction() as db:
            db.execute("UPDATE learning_outlines SET status='paused',updated_at=? WHERE id=? AND status IN ('queued','running')", (timestamp(), identity))

            db.execute("UPDATE outline_batches SET status='failed',attempt=NULL,error=? WHERE outline_id=? AND status='running'",
                       ('本段曾被暂停，可继续。', identity))
            db.execute("UPDATE outline_context_steps SET status='failed',attempt=NULL,error=? WHERE outline_id=? AND status='running'",
                       ('背景步骤曾被暂停，可继续。', identity))

    def claim_batch(self, identity):
        with self.practice.transaction() as db:
            job = db.execute("SELECT status FROM learning_outlines WHERE id=?", (identity,)).fetchone()
            if job['status'] != 'running':
                return None
            row = db.execute("SELECT * FROM outline_batches WHERE outline_id=? AND status='pending' ORDER BY position LIMIT 1", (identity,)).fetchone()
            if row is None:
                return None
            attempt = uuid4().hex
            db.execute("UPDATE outline_batches SET status='running',attempt=? WHERE id=?", (attempt, row['id']))
            return {**dict(row), 'attempt': attempt}

    def finish_batch(self, batch, proposals, note, visible=None, diagnostic=None):
        with self.practice.transaction() as db:
            row = db.execute("SELECT status,attempt FROM outline_batches WHERE id=?", (batch['id'],)).fetchone()
            if row['status'] != 'running' or row['attempt'] != batch['attempt']:
                return False
            for position, proposal in enumerate(proposals):
                db.execute("INSERT INTO outline_candidates(id,outline_id,batch_id,position,proposal_json) VALUES (?,?,?,?,?)",
                           (uuid4().hex, batch['outline_id'], batch['id'], position, proposal.model_dump_json()))
            db.execute("UPDATE outline_batches SET status='completed',attempt=NULL,error=NULL,note=?,visible_json=?,request_json=? WHERE id=?",
                (note, json.dumps(visible) if visible is not None else None,
                 json.dumps(diagnostic) if diagnostic is not None else None, batch['id']))
            db.execute("UPDATE learning_outlines SET updated_at=? WHERE id=?", (timestamp(), batch['outline_id']))
        return True

    def fail_batch(self, batch, error):
        with self.practice.transaction() as db:
            db.execute("UPDATE outline_batches SET status='failed',attempt=NULL,error=? WHERE id=? AND attempt=?",
                       (error, batch['id'], batch['attempt']))

    def settle(self, identity):
        with self.practice.transaction() as db:
            remaining = db.execute("SELECT count(*) FROM outline_batches WHERE outline_id=? AND status != 'completed'", (identity,)).fetchone()[0]
            remaining += db.execute("SELECT count(*) FROM outline_context_steps WHERE outline_id=? AND status NOT IN ('completed','superseded')", (identity,)).fetchone()[0]
            db.execute("UPDATE learning_outlines SET status=?,updated_at=? WHERE id=? AND status='running'",
                       ('failed' if remaining else 'completed', timestamp(), identity))

    def context_step(self, identity, key, kind, scope_id, inputs):
        """Deterministic keys let resumed reductions reuse published dependencies."""
        with self.practice.transaction() as db:
            db.execute("""INSERT OR IGNORE INTO outline_context_steps
                (id,outline_id,kind,scope_id,inputs_json) VALUES (?,?,?,?,?)""",
                (key, identity, kind, scope_id, json.dumps(inputs, ensure_ascii=False)))
            db.execute("UPDATE outline_context_steps SET status='pending',error=NULL WHERE id=? AND status='superseded'", (key,))
        return next(s for s in self.get(identity)['context_steps'] if s['id'] == key)

    def claim_context(self, key):
        with self.practice.transaction() as db:
            row = db.execute('SELECT * FROM outline_context_steps WHERE id=?', (key,)).fetchone()
            status = db.execute('SELECT status FROM learning_outlines WHERE id=?', (row['outline_id'],)).fetchone()[0]
            if status != 'running' or row['status'] != 'pending':
                return None
            attempt = uuid4().hex
            db.execute("UPDATE outline_context_steps SET status='running',attempt=? WHERE id=?", (attempt, key))
            return {**dict(row), 'attempt': attempt}

    def finish_context(self, step, result, diagnostic):
        with self.practice.transaction() as db:
            # Dependency versions are immutable published result hashes.
            from hashlib import sha256
            for item in json.loads(step['inputs_json']):
                if 'step_id' not in item:
                    continue
                dependency = db.execute('SELECT status,result_json FROM outline_context_steps WHERE id=? AND outline_id=?',
                                        (item['step_id'], step['outline_id'])).fetchone()
                if not dependency or dependency['status'] != 'completed' or sha256(dependency['result_json'].encode()).hexdigest() != item['version']:
                    return False
            return bool(db.execute("""UPDATE outline_context_steps SET status='completed',attempt=NULL,error=NULL,
                result_json=?,request_json=? WHERE id=? AND status='running' AND attempt=?""",
                (result.model_dump_json(), json.dumps(diagnostic), step['id'], step['attempt'])).rowcount)

    def fail_context(self, step, message):
        with self.practice.transaction() as db:
            db.execute("UPDATE outline_context_steps SET status='failed',attempt=NULL,error=? WHERE id=? AND attempt=?",
                       (message, step['id'], step['attempt']))

    def retire_context_attempts(self, identity):
        # All required roots have completed under the job lock. Budget changes may
        # have produced a different merge tree; keep unused failures as audit records.
        with self.practice.transaction() as db:
            db.execute("UPDATE outline_context_steps SET status='superseded',attempt=NULL WHERE outline_id=? AND status IN ('pending','failed')", (identity,))

    def split_batch(self, batch, end):
        """Keep the complete tail pending when actual background requires a smaller window."""
        with self.practice.transaction() as db:
            row = db.execute('SELECT * FROM outline_batches WHERE id=?', (batch['id'],)).fetchone()
            if row['status'] != 'running' or row['attempt'] != batch['attempt']:
                raise OutlineBusy('分段状态已变化，请刷新。')
            if not row['line_start'] <= end < row['line_end']:
                return batch
            # Move backwards through unique positions so shifts cannot collide.
            for later in db.execute('SELECT id,position FROM outline_batches WHERE outline_id=? AND position>? ORDER BY position DESC',
                                    (row['outline_id'], row['position'])).fetchall():
                db.execute('UPDATE outline_batches SET position=? WHERE id=?', (later['position'] + 1, later['id']))
            db.execute("""INSERT INTO outline_batches
                (id,outline_id,position,section_id,source_id,line_start,line_end,status)
                VALUES (?,?,?,?,?,?,?,'pending')""", (uuid4().hex, row['outline_id'], row['position'] + 1,
                    row['section_id'], row['source_id'], end + 1, row['line_end']))
            db.execute('UPDATE outline_batches SET line_end=? WHERE id=?', (end, row['id']))
        return {**batch, 'line_end': end}

    def edit(self, identity, learning: LearningDraft, revision):
        with self.practice.transaction() as db:
            row = db.execute('SELECT proposal_json,revision,status FROM outline_candidates WHERE id=?', (identity,)).fetchone()
            if not row or row['revision'] != revision or row['status'] != 'proposed':
                raise OutlineBusy("目标已变化或已加入计划，请刷新后再修改。")
            if not db.execute("UPDATE outline_candidates SET proposal_json=?,revision=revision+1,error=NULL WHERE id=? AND revision=? AND status='proposed'",
                              (learning.model_dump_json(), identity, revision)).rowcount:
                raise OutlineBusy("目标已变化或已加入计划，请刷新后再修改。")
            from outline_group_store import invalidate_candidate
            invalidate_candidate(db, identity)
        return self.candidate(identity)

    def begin_enrollment(self, identity, revision):
        with self.practice.transaction() as db:
            row = db.execute("SELECT * FROM outline_candidates WHERE id=?", (identity,)).fetchone()
            if row['status'] == 'enrolled':
                return dict(row)
            if row['revision'] != revision:
                raise OutlineBusy("目标已修改，请刷新后审核。")
            from outline_group_store import enrollment_token
            group_token = enrollment_token(db, identity)
            attempt = uuid4().hex
            # The caller owns the candidate lock; an abandoned attempt is safe to replace.
            db.execute("UPDATE outline_candidates SET status='enrolling',attempt=?,error=NULL WHERE id=?", (attempt, identity))
        return {**dict(row), 'attempt': attempt, 'group_token': group_token}

    def enrollment_failed(self, identity, attempt, error):
        with self.practice.transaction() as db:
            db.execute("UPDATE outline_candidates SET status='proposed',attempt=NULL,error=? WHERE id=? AND attempt=?", (error, identity, attempt))

    def recover_candidate(self, identity):
        with self.practice.transaction() as db:
            db.execute("UPDATE outline_candidates SET status='proposed',attempt=NULL,error=? WHERE id=? AND status='enrolling'",
                       ('加入曾被中断，可以重试；未重复创建复习项。', identity))

    def commit_enrollment(self, candidate, learning, draft, snapshots, visible, collection_id, unit_id, language):
        with self.practice.transaction() as db:
            row = db.execute("SELECT * FROM outline_candidates WHERE id=?", (candidate['id'],)).fetchone()
            if row['status'] != 'enrolling' or row['attempt'] != candidate['attempt'] or row['revision'] != candidate['revision']:
                raise OutlineBusy("审核状态已变化，未保存这次生成结果。")
            from outline_group_store import resolve_unit
            unit_id = resolve_unit(db, candidate['id'], candidate.get('group_token'), unit_id, learning)
            focus = visible[0]
            session_id = self.practice.create(learning.target.title, focus.source_id, focus.line_start, focus.line_end,
                snapshots, [{'source_id': c.source_id, 'line_start': c.line_start, 'line_end': c.line_end} for c in visible],
                draft, collection_id=collection_id,
                response_language=language, _connection=db)
            question_id = db.execute("SELECT id FROM practice_questions WHERE session_id=?", (session_id,)).fetchone()[0]
            item_id = self.practice.enroll(session_id, question_id, learning, unit_id=unit_id,
                criterion_indices=draft.criterion_indices, collection_id=collection_id, _connection=db)
            db.execute("UPDATE outline_candidates SET status='enrolled',attempt=NULL,error=NULL,item_id=?,session_id=? WHERE id=?",
                       (item_id, session_id, candidate['id']))
        return {'item_id': item_id, 'session_id': session_id}
