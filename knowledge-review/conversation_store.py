"""Immutable visible messages, idempotent turns and compare-and-swap summaries."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


class ConversationBusy(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(messages):
    return sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    seq INTEGER NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL,
                    request_json TEXT NOT NULL, attempt TEXT, error TEXT,
                    context_json TEXT, created_at TEXT NOT NULL, completed_at TEXT,
                    UNIQUE(conversation_id, seq), UNIQUE(conversation_id, request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_turn ON turns(conversation_id)
                    WHERE status IN ('running', 'failed');
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
                    result_json TEXT, created_at TEXT NOT NULL, UNIQUE(turn_id, role));
                CREATE TABLE IF NOT EXISTS summaries (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id), version INTEGER NOT NULL,
                    covers_through_seq INTEGER NOT NULL, source_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
            if "title_state" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN title_state TEXT NOT NULL DEFAULT 'manual'")
                db.execute("ALTER TABLE conversations ADD COLUMN title_attempt TEXT")
                db.execute("ALTER TABLE conversations ADD COLUMN title_started_at TEXT")
                db.execute("UPDATE conversations SET title_state='pending' WHERE title='新聊天'")
            if "archived" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE conversations ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 1")
            turn_columns = {row["name"] for row in db.execute("PRAGMA table_info(turns)")}
            if "memory_valid" not in turn_columns:
                db.execute("ALTER TABLE turns ADD COLUMN memory_valid INTEGER NOT NULL DEFAULT 1")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_dependencies (
                    turn_id TEXT NOT NULL REFERENCES turns(id), source_conversation_id TEXT NOT NULL,
                    PRIMARY KEY(turn_id, source_conversation_id));
                CREATE INDEX IF NOT EXISTS memory_dependency_source ON memory_dependencies(source_conversation_id);
                CREATE TABLE IF NOT EXISTS conversation_tombstones (id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memory_vectors (
                    message_id TEXT NOT NULL REFERENCES messages(id), start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL, profile TEXT NOT NULL, content_hash TEXT NOT NULL,
                    vector_json TEXT NOT NULL, PRIMARY KEY(message_id,start_offset,end_offset,profile));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if getattr(self, 'practice_path', None):
            db.execute('ATTACH DATABASE ? AS learning', (str(self.practice_path),))
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def generation_lock(self, conversation_id):
        self.get_conversation(conversation_id)
        # Only validated UUIDs reach a filename. OS releases the lock on crash;
        # model/summary calls hold this lock, never a SQLite write transaction.
        path = self.path.with_name(f"{self.path.stem}-{conversation_id}.lock")
        with path.open("a+b") as file:
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ConversationBusy("这个聊天正在生成，请等待完成后再提交或重试。") from exc
            try:
                yield
            finally:
                file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file, fcntl.LOCK_UN)

    def create(self, title="新聊天"):
        identity, timestamp = uuid4().hex, now()
        with self.connection() as db:
            db.execute("INSERT INTO conversations(id,title,created_at,updated_at,title_state) VALUES (?,?,?,?,?)",
                       (identity, title, timestamp, timestamp, "pending" if title == "新聊天" else "manual"))
        return self.get_conversation(identity)

    def get_conversation(self, identity):
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise KeyError("聊天不存在。")
        with self.connection() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("聊天不存在。")
        return dict(row)

    def list(self, *, archived=False):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM conversations WHERE archived=? ORDER BY updated_at DESC,id", (int(archived),))]

    def archive(self, identity, archived):
        self.get_conversation(identity)
        with self.connection() as db:
            db.execute("UPDATE conversations SET archived=? WHERE id=?", (int(archived), identity))
        return self.get_conversation(identity)

    @staticmethod
    def check_dependencies(db, dependencies):
        for source in dependencies:
            row = db.execute("SELECT memory_revision FROM conversations WHERE id=?", (source["conversation_id"],)).fetchone()
            if row is None or row[0] != source["revision"]:
                raise ConversationBusy("使用的聊天来源已删除或失效，旧结果不能发布；输入保留，可以重试。")

    def assert_dependencies(self, dependencies):
        with self.connection() as db:
            self.check_dependencies(db, dependencies)

    def delete(self, identity):
        """Revoke atomically without waiting for a network/generation lock.

        Visible replies in other chats remain records, but derived replies cannot
        re-enter memory, summaries or vectors. Propagate through the dependency
        graph so deletion cannot be bypassed by recalling a copied AI explanation.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (identity,)).fetchone():
                if db.execute("SELECT 1 FROM conversation_tombstones WHERE id=?", (identity,)).fetchone():
                    return  # Idempotent deletion.
                raise KeyError("聊天不存在。")
            queue, visited, invalid = [identity], {identity}, set()
            while queue:
                source = queue.pop()
                for row in db.execute("SELECT t.id,t.conversation_id FROM memory_dependencies d JOIN turns t ON t.id=d.turn_id WHERE d.source_conversation_id=?", (source,)):
                    invalid.add(row["id"])
                    if row["conversation_id"] not in visited:
                        visited.add(row["conversation_id"])
                        queue.append(row["conversation_id"])
            for turn_id in invalid:
                db.execute("UPDATE turns SET memory_valid=0,context_json=NULL WHERE id=?", (turn_id,))
                # Drop duplicated history excerpts from saved reference payloads.
                rows = db.execute("SELECT id,result_json FROM messages WHERE turn_id=? AND role='assistant'", (turn_id,)).fetchall()
                for row in rows:
                    result = json.loads(row["result_json"] or "{}")
                    result.pop("history_references", None)
                    db.execute("UPDATE messages SET result_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), row["id"]))
                db.execute("DELETE FROM memory_vectors WHERE message_id IN (SELECT id FROM messages WHERE turn_id=? AND role='assistant')", (turn_id,))
            for affected in visited:
                db.execute("DELETE FROM summaries WHERE conversation_id=?", (affected,))
                db.execute("UPDATE conversations SET memory_revision=memory_revision+1,title_attempt=NULL,"
                           "title_state=CASE WHEN title_state='manual' THEN 'manual' ELSE 'pending' END,"
                           "title=CASE WHEN title_state='manual' THEN title ELSE '新聊天' END WHERE id=?", (affected,))
            if getattr(self, 'practice_path', None):
                from practice_provenance import revoke
                revoke(db, visited)
            db.execute("DELETE FROM memory_vectors WHERE message_id IN (SELECT m.id FROM messages m JOIN turns t ON t.id=m.turn_id WHERE t.conversation_id=?)", (identity,))
            db.execute("DELETE FROM memory_dependencies WHERE turn_id IN (SELECT id FROM turns WHERE conversation_id=?) OR source_conversation_id=?", (identity, identity))
            db.execute("DELETE FROM messages WHERE turn_id IN (SELECT id FROM turns WHERE conversation_id=?)", (identity,))
            db.execute("DELETE FROM turns WHERE conversation_id=?", (identity,))
            db.execute("DELETE FROM conversations WHERE id=?", (identity,))
            db.execute("INSERT INTO conversation_tombstones VALUES (?,?)", (identity, now()))

    def rename(self, identity, title):
        self.get_conversation(identity)
        with self.connection() as db:
            db.execute("UPDATE conversations SET title=?,updated_at=?,title_state='manual',title_attempt=NULL WHERE id=?",
                       (title, now(), identity))
        return self.get_conversation(identity)

    def claim_title(self, identity):
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        attempt = uuid4().hex
        with self.connection() as db:
            changed = db.execute("""UPDATE conversations SET title_state='generating',title_attempt=?,title_started_at=?
                WHERE id=? AND (title_state IN ('pending','failed') OR
                    (title_state='generating' AND title_started_at<?))""", (attempt, now(), identity, cutoff)).rowcount
        return attempt if changed else None

    def opening_messages(self, identity):
        with self.connection() as db:
            return [dict(row) for row in db.execute("""SELECT m.role,m.content FROM messages m JOIN turns t ON t.id=m.turn_id
                WHERE t.conversation_id=? AND t.status='completed' AND t.memory_valid=1
                ORDER BY t.seq,CASE m.role WHEN 'user' THEN 0 ELSE 1 END LIMIT 2""", (identity,))]

    def publish_title(self, identity, attempt, title=None):
        with self.connection() as db:
            if title:
                db.execute("UPDATE conversations SET title=?,title_state='generated',title_attempt=NULL "
                           "WHERE id=? AND title_state='generating' AND title_attempt=?", (title, identity, attempt))
            else:
                db.execute("UPDATE conversations SET title_state='failed',title_attempt=NULL "
                           "WHERE id=? AND title_attempt=?", (identity, attempt))

    def recover(self, identity):
        # Caller owns generation_lock. An abandoned running row cannot still publish.
        with self.connection() as db:
            db.execute("UPDATE turns SET status='failed',attempt=NULL,error=? "
                       "WHERE conversation_id=? AND status='running'",
                       ("上次生成被中断，输入已保存，可以重试。", identity))

    def begin(self, identity, request_id, request):
        self.get_conversation(identity)
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (identity,)).fetchone():
                raise KeyError("聊天已删除。")
            old = db.execute("SELECT * FROM turns WHERE conversation_id=? AND request_id=?",
                             (identity, request_id)).fetchone()
            if old:
                previous = json.loads(old['request_json'])
                # Legacy requests had no retrieval field and used dense search.
                # Compare semantic defaults without rewriting archived raw inputs.
                previous.setdefault('retrieval_mode', 'dense')
                comparable = {**request, 'retrieval_mode':request.get('retrieval_mode','dense')}
                if json.dumps(previous,ensure_ascii=False,sort_keys=True) != json.dumps(comparable,ensure_ascii=False,sort_keys=True):
                    raise ConversationBusy("同一提交标识对应不同输入，请使用新的提交标识。")
                if old["status"] == "completed":
                    return self.turn(old["id"])
                if old["status"] != "failed":
                    raise ConversationBusy("该轮次正在处理或已跳过，不能再次提交。")
                turn_id = old["id"]
            else:
                pending = db.execute("SELECT id FROM turns WHERE conversation_id=? AND status IN ('running','failed')",
                                     (identity,)).fetchone()
                if pending:
                    raise ConversationBusy("上一条输入尚未完成，请先重试或跳过，再提交新问题。")
                seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE conversation_id=?", (identity,)).fetchone()[0]
                turn_id = uuid4().hex
                db.execute("INSERT INTO turns(id,conversation_id,seq,request_id,status,request_json,created_at) "
                           "VALUES (?,?,?,?,'failed',?,?)", (turn_id, identity, seq, request_id, encoded, now()))
                db.execute("INSERT INTO messages VALUES (?,?, 'user',?,NULL,?)",
                           (uuid4().hex, turn_id, request["question"], now()))
            db.execute("UPDATE turns SET status='running',attempt=?,error=NULL WHERE id=?", (uuid4().hex, turn_id))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), identity))
        return self.turn(turn_id)

    def turn(self, turn_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError("轮次不存在。")
            result = dict(row)
            result["request"] = json.loads(result.pop("request_json"))
            result["context"] = json.loads(result.pop("context_json") or "null")
            result["messages"] = [dict(m) for m in db.execute("SELECT * FROM messages WHERE turn_id=? "
                                                            "ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END", (turn_id,))]
            for message in result["messages"]:
                message["result"] = json.loads(message.pop("result_json") or "null")
            return result

    def finish(self, turn_id, attempt, result, context):
        text = "\n\n".join([result["coverage_note"]] + [b["text"] for b in result["blocks"]])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for document in (context or {}).get('document_dependencies', []):
                row = db.execute('SELECT revision,removed FROM learning.document_states WHERE document_id=?', (document['id'],)).fetchone()
                if row is None or row['removed'] or row['revision'] != document['revision']:
                    raise ConversationBusy('本轮资料已替换或移除，旧回答不能发布；请重新打开资料再重试。')
            dependencies = (context or {}).get("memory_dependencies", [])
            self.check_dependencies(db, dependencies)
            changed = db.execute("UPDATE turns SET status='completed',completed_at=?,context_json=? "
                                 "WHERE id=? AND status='running' AND attempt=?",
                                 (now(), json.dumps(context, ensure_ascii=False), turn_id, attempt)).rowcount
            if not changed:
                raise ConversationBusy("轮次版本已变化，旧生成结果不能发布。")
            db.execute("INSERT INTO messages VALUES (?,?,'assistant',?,?,?)",
                       (uuid4().hex, turn_id, text, json.dumps(result, ensure_ascii=False), now()))
            owner = db.execute("SELECT conversation_id FROM turns WHERE id=?", (turn_id,)).fetchone()[0]
            for dependency in dependencies:
                if dependency["conversation_id"] != owner:
                    db.execute("INSERT OR IGNORE INTO memory_dependencies VALUES (?,?)", (turn_id, dependency["conversation_id"]))
            # Inherit prior derived-reply dependencies, even when only their
            # compressed summary was visible; this closes indirect recall paths.
            db.execute("INSERT OR IGNORE INTO memory_dependencies SELECT ?,d.source_conversation_id "
                       "FROM memory_dependencies d JOIN turns t ON t.id=d.turn_id "
                       "WHERE t.conversation_id=? AND t.memory_valid=1 AND t.status='completed' AND t.id<>?",
                       (turn_id, owner, turn_id))

    def fail(self, turn_id, attempt, error, context=None):
        with self.connection() as db:
            db.execute("UPDATE turns SET status='failed',error=?,context_json=?,attempt=NULL WHERE id=? AND status='running' AND attempt=?",
                       (error, json.dumps(context,ensure_ascii=False) if context is not None else None, turn_id, attempt))

    def skip(self, identity, turn_id):
        turn = self.turn(turn_id)
        if turn["conversation_id"] != identity or turn["status"] != "failed":
            raise ConversationBusy("只能跳过本聊天尚未完成的输入。")
        with self.connection() as db:
            db.execute("UPDATE turns SET status='cancelled' WHERE id=? AND status='failed'", (turn_id,))

    def turns(self, identity, *, after=0, limit=100):
        self.get_conversation(identity)
        with self.connection() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM turns WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?",
                                              (identity, after, limit))]
        return [self.turn(identity) for identity in ids]

    def history(self, identity):
        self.get_conversation(identity)
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT m.id,m.role,m.content,t.seq,t.id AS turn_id FROM messages m JOIN turns t ON t.id=m.turn_id "
                "WHERE t.conversation_id=? AND t.status='completed' AND (m.role='user' OR t.memory_valid=1) ORDER BY t.seq,CASE m.role WHEN 'user' THEN 0 ELSE 1 END",
                (identity,))]

    def summary(self, identity):
        self.get_conversation(identity)
        with self.connection() as db:
            row = db.execute("SELECT * FROM summaries WHERE conversation_id=?", (identity,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def publish_summary(self, identity, expected_version, prefix, payload, model, *, expected_revision=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            chat = db.execute("SELECT memory_revision FROM conversations WHERE id=?", (identity,)).fetchone()
            if chat is None or (expected_revision is not None and chat[0] != expected_revision):
                return False
            old = db.execute("SELECT version FROM summaries WHERE conversation_id=?", (identity,)).fetchone()
            if (old[0] if old else 0) != expected_version:
                return False
            live = [dict(row) for row in db.execute(
                "SELECT m.id,m.role,m.content,t.seq,t.id AS turn_id FROM messages m JOIN turns t ON t.id=m.turn_id "
                "WHERE t.conversation_id=? AND t.status='completed' AND (m.role='user' OR t.memory_valid=1) AND t.seq<=? "
                "ORDER BY t.seq,CASE m.role WHEN 'user' THEN 0 ELSE 1 END", (identity, prefix[-1]["seq"]))]
            if fingerprint(live) != fingerprint(prefix):
                return False
            db.execute("INSERT OR REPLACE INTO summaries VALUES (?,?,?,?,?,?,?)",
                       (identity, expected_version + 1, prefix[-1]["seq"], fingerprint(prefix),
                        json.dumps(payload, ensure_ascii=False), model, now()))
        return True
