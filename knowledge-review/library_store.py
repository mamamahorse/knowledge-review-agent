"""Nested local collections; document identity is independent of source versions."""

from contextlib import closing
from uuid import uuid4
import json

ROOT_COLLECTION = "root"


def initialize_library(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS collections (
            id TEXT PRIMARY KEY, parent_id TEXT REFERENCES collections(id),
            name TEXT NOT NULL CHECK(length(trim(name)) > 0));
        INSERT OR IGNORE INTO collections VALUES ('root', NULL, '总知识库');
        CREATE TABLE IF NOT EXISTS document_collections (
            document_id TEXT PRIMARY KEY,
            collection_id TEXT NOT NULL REFERENCES collections(id));
        CREATE TABLE IF NOT EXISTS document_states (
            document_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
            removed INTEGER NOT NULL DEFAULT 0, text_override TEXT);
        CREATE TABLE IF NOT EXISTS document_versions (
            document_id TEXT NOT NULL, source_id TEXT NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY(document_id,source_id));
    """)
    for table, column in (('document_states', 'source_json'), ('document_versions', 'snapshot_json')):
        if column not in {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
    for table in ("practice_sessions", "review_items"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "collection_id" not in columns:
            # SQLite cannot add a non-null REFERENCES column to a populated table.
            # Keep old rows in place; equivalent insert/update checks guard membership.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN collection_id TEXT NOT NULL DEFAULT 'root'")
        for action in ("INSERT", "UPDATE OF collection_id"):
            suffix = action.split()[0].lower()
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS valid_{table}_collection_{suffix}
                BEFORE {action} ON {table} WHEN NOT EXISTS (
                    SELECT 1 FROM collections WHERE id=NEW.collection_id)
                BEGIN SELECT RAISE(ABORT, 'unknown collection'); END""")


class LibraryStore:
    def __init__(self, store):
        self.store = store

    def get(self, identity):
        with closing(self.store._connect()) as conn:
            row = conn.execute("SELECT * FROM collections WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("找不到这个知识库或集合。")
        return dict(row)

    def list(self):
        with closing(self.store._connect()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM collections ORDER BY rowid")]

    def create(self, name, parent_id=ROOT_COLLECTION):
        self.get(parent_id)
        name = self.validate_name(name)
        identity = uuid4().hex
        with closing(self.store._connect()) as conn, conn:
            conn.execute("INSERT INTO collections VALUES (?,?,?)", (identity, parent_id, name))
        return self.get(identity)

    @staticmethod
    def validate_name(name):
        if not name.strip() or len(name.strip()) > 100:
            raise ValueError("集合名称需为 1–100 个字符。")
        return name.strip()

    def rename(self, identity, name):
        self.get(identity)
        if identity == ROOT_COLLECTION:
            raise ValueError("总知识库是固定的总览入口。")
        with closing(self.store._connect()) as conn, conn:
            conn.execute("UPDATE collections SET name=? WHERE id=?", (self.validate_name(name), identity))
        return self.get(identity)

    def descendants(self, identity):
        self.get(identity)
        with closing(self.store._connect()) as conn:
            return [row[0] for row in conn.execute("""WITH RECURSIVE tree(id) AS (
                SELECT id FROM collections WHERE id=? UNION ALL
                SELECT c.id FROM collections c JOIN tree t ON c.parent_id=t.id)
                SELECT id FROM tree""", (identity,))]

    def document_memberships(self):
        with closing(self.store._connect()) as conn:
            return dict(conn.execute("SELECT document_id,collection_id FROM document_collections"))

    def assign_documents(self, document_ids, collection_id):
        self.get(collection_id)
        with closing(self.store._connect()) as conn, conn:
            conn.executemany("""INSERT INTO document_collections VALUES (?,?)
                ON CONFLICT(document_id) DO UPDATE SET collection_id=excluded.collection_id""",
                [(identity, collection_id) for identity in document_ids])

    def move_review(self, item_id, collection_id):
        self.get(collection_id)
        with closing(self.store._connect()) as conn, conn:
            if not conn.execute("UPDATE review_items SET collection_id=? WHERE id=?",
                                (collection_id, item_id)).rowcount:
                raise KeyError("找不到复习项。")

    def move(self, identity, parent_id):
        with self.store.transaction() as db:
            rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM collections')}
            if identity not in rows or parent_id not in rows:
                raise KeyError('找不到集合。')
            if identity == ROOT_COLLECTION:
                raise ValueError('总知识库不能移动。')
            ancestor = parent_id
            while ancestor:
                if ancestor == identity:
                    raise ValueError('不能把集合放入自身或自己的子集合。')
                ancestor = rows[ancestor]['parent_id']
            db.execute('UPDATE collections SET parent_id=? WHERE id=?', (parent_id, identity))
        return self.get(identity)

    def remove_collection(self, identity):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM collections WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise KeyError('找不到集合。')
            if identity == ROOT_COLLECTION:
                raise ValueError('总知识库不能删除。')
            parent = row['parent_id']
            db.execute('UPDATE collections SET parent_id=? WHERE parent_id=?', (parent, identity))
            for table in ('document_collections', 'practice_sessions', 'review_items'):
                db.execute(f'UPDATE {table} SET collection_id=? WHERE collection_id=?', (parent, identity))
            # Old outlines remain reviewable, but their deleted scope cannot publish.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='learning_outlines'").fetchone():
                db.execute("UPDATE learning_outlines SET collection_id=?,request_id=id,"
                           "scope_fingerprint='removed:'||scope_fingerprint,"
                           "status=CASE WHEN status IN ('queued','running') THEN 'paused' ELSE status END "
                           "WHERE collection_id=?", (parent, identity))
            db.execute('DELETE FROM collections WHERE id=?', (identity,))
        return {'parent_id': parent}

    def document_state(self, identity):
        with closing(self.store._connect()) as db:
            row = db.execute('SELECT * FROM document_states WHERE document_id=?', (identity,)).fetchone()
            if row:
                return dict(row)
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO document_states(document_id) VALUES (?)', (identity,))
            return dict(db.execute('SELECT * FROM document_states WHERE document_id=?', (identity,)).fetchone())

    def change_document(self, identity, old_source, expected_source_id, *, expected_revision, text=None, remove=False, document=None):
        if old_source.source_id != expected_source_id:
            raise ValueError('资料版本已变化，请刷新后再操作。')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM document_states WHERE document_id=?', (identity,)).fetchone()
            if row['removed'] or row['revision'] != expected_revision:
                raise ValueError('资料已被其他请求修改，请刷新。')
            snapshot = old_source.document.to_dict() if old_source.document and old_source.document.format == 'pdf' else None
            db.execute('INSERT OR IGNORE INTO document_versions(document_id,source_id,text,snapshot_json) VALUES (?,?,?,?)',
                       (identity, old_source.source_id, old_source.text, json.dumps(snapshot, ensure_ascii=False) if snapshot else None))
            db.execute('UPDATE document_states SET revision=revision+1,removed=?,text_override=?,source_json=? WHERE document_id=?',
                       (int(remove), None if remove else text,
                        json.dumps(document.to_dict(), ensure_ascii=False) if document is not None and not remove else None, identity))
        return {'id': identity, 'removed': remove}
