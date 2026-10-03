"""Transactional evaluation and review bookkeeping shared by both stores."""

import json
from uuid import uuid4
from review_scheduler import replay, SCHEDULER_VERSION


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_units (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, draft_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learning_targets (
            id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES knowledge_units(id),
            draft_json TEXT NOT NULL, original_question_id TEXT NOT NULL,
            source_session_id TEXT NOT NULL REFERENCES practice_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS learning_questions (
            id TEXT PRIMARY KEY, draft_json TEXT NOT NULL,
            source_session_id TEXT NOT NULL REFERENCES practice_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS evaluation_revisions (
            id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES practice_questions(id),
            revision INTEGER NOT NULL, grade_json TEXT NOT NULL,
            origin TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(attempt_id, revision)
        );
        CREATE TABLE IF NOT EXISTS review_items (
            id TEXT PRIMARY KEY, target_id TEXT NOT NULL UNIQUE REFERENCES learning_targets(id),
            status TEXT NOT NULL CHECK(status IN ('active','paused')),
            enrolled_at TEXT NOT NULL, initial_card_json TEXT NOT NULL,
            card_json TEXT NOT NULL, parameters_json TEXT NOT NULL,
            scheduler_version TEXT NOT NULL, next_review_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_events (
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES review_items(id),
            attempt_id TEXT NOT NULL UNIQUE REFERENCES practice_questions(id),
            evaluation_id TEXT NOT NULL REFERENCES evaluation_revisions(id),
            reviewed_at TEXT NOT NULL, suggested_rating TEXT,
            effective_rating TEXT, rating_origin TEXT NOT NULL,
            eligible INTEGER NOT NULL, exclusion_reason TEXT NOT NULL
        );
    """)
    migrations = {
        "learning_targets": {"source_knowledge_json": "TEXT", "criterion_indices_json": "TEXT NOT NULL DEFAULT '[]'"},
        "practice_sessions": {"review_item_id": "TEXT", "review_mode": "TEXT"},
        "practice_questions": {
            "question_ref": "TEXT", "target_id": "TEXT", "candidate_json": "TEXT",
            "assistance": "TEXT NOT NULL DEFAULT 'none_recorded'",
            "effective_evaluation_id": "TEXT", "revision": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, additions in migrations.items():
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempt_target ON practice_questions(target_id, submitted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_item ON review_events(item_id, reviewed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_target_unit ON learning_targets(unit_id)")
    # Each old row is one existing attempt, and its immutable question retains its ID.
    conn.execute("""INSERT OR IGNORE INTO learning_questions
        SELECT id, draft_json, session_id FROM practice_questions WHERE question_ref IS NULL""")
    conn.execute("UPDATE practice_questions SET question_ref = id WHERE question_ref IS NULL")
    for row in conn.execute("""SELECT * FROM practice_questions
            WHERE grade_json IS NOT NULL AND effective_evaluation_id IS NULL""").fetchall():
        evaluation_id = uuid4().hex
        conn.execute("INSERT INTO evaluation_revisions VALUES (?, ?, 1, ?, 'legacy', '', ?)",
                     (evaluation_id, row["id"], row["grade_json"], row["graded_at"] or row["submitted_at"]))
        conn.execute("UPDATE practice_questions SET effective_evaluation_id=?, revision=1 WHERE id=?",
                     (evaluation_id, row["id"]))


def reschedule(conn, item_id):
    item = conn.execute("SELECT * FROM review_items WHERE id=?", (item_id,)).fetchone()
    if item["scheduler_version"] != SCHEDULER_VERSION:
        raise ValueError("复习算法版本不匹配，请保留记录并升级适配器。")
    events = [dict(row) for row in conn.execute("""SELECT * FROM review_events
        WHERE item_id=? AND eligible=1 ORDER BY reviewed_at, id""", (item_id,))]
    card = replay(json.loads(item["initial_card_json"]), json.loads(item["parameters_json"]), events)
    conn.execute("UPDATE review_items SET card_json=?, next_review_at=? WHERE id=?",
                 (json.dumps(card), card["due"], item_id))


def record_evaluation(conn, question, grade, timestamp, *, origin="model", reason=""):
    revision = question["revision"] + 1
    evaluation_id = uuid4().hex
    conn.execute("INSERT INTO evaluation_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (evaluation_id, question["id"], revision, grade.model_dump_json(), origin, reason, timestamp))
    conn.execute("UPDATE practice_questions SET effective_evaluation_id=?, revision=? WHERE id=?",
                 (evaluation_id, revision, question["id"]))
    session = conn.execute("SELECT * FROM practice_sessions WHERE id=?", (question["session_id"],)).fetchone()
    if not session["review_item_id"] or question["position"] != 1:
        return
    item_id = session["review_item_id"]
    eligible = (grade.overall_assessment not in (None, "uncertain") and
                grade.target_fit == "within_scope" and question["assistance"] == "none_recorded")
    exclusion = ("" if eligible else "本次未计入调度：题目范围/评估不确定，或已查看提示/解析。")
    event = conn.execute("SELECT * FROM review_events WHERE attempt_id=?", (question["id"],)).fetchone()
    if event:
        # A user's explicit difficulty choice survives content re-evaluation.
        chosen = event["effective_rating"] if event["rating_origin"] == "user" else grade.suggested_rating
        conn.execute("""UPDATE review_events SET evaluation_id=?, suggested_rating=?,
            effective_rating=?, eligible=?, exclusion_reason=? WHERE id=?""",
            (evaluation_id, grade.suggested_rating, chosen, int(eligible), exclusion, event["id"]))
    else:
        conn.execute("INSERT INTO review_events VALUES (?, ?, ?, ?, ?, ?, ?, 'model', ?, ?)",
                     (uuid4().hex, item_id, question["id"], evaluation_id, question["submitted_at"],
                      grade.suggested_rating, grade.suggested_rating, int(eligible), exclusion))
    reschedule(conn, item_id)
