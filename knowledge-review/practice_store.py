"""SQLite state transitions for resumable practice sessions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from practice_model import GradeDraft, QuestionDraft, parse_question, parse_grade
from practice_provenance import initialize as initialize_provenance, check_session, basis_record, check_dependencies
from sources import Source
from response_preferences import ResponseLanguage, validate_language
from learning_state import initialize, record_evaluation
from library_store import initialize_library

DEFAULT_PRACTICE_DB = Path(__file__).resolve().parent / ".data" / "practice.sqlite3"
MAX_QUESTIONS_PER_SESSION = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class PracticeStore:
    def __init__(self, path: Path = DEFAULT_PRACTICE_DB):
        self.path = path
        sibling = path.parent / 'conversations.sqlite3'
        self.conversation_path = sibling if sibling.is_file() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS practice_sessions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('waiting_for_answer', 'grading', 'finished')),
                    objective TEXT NOT NULL,
                    focus_source_id TEXT NOT NULL,
                    focus_start INTEGER NOT NULL,
                    focus_end INTEGER NOT NULL,
                    sources_json TEXT NOT NULL,
                    visible_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS practice_questions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES practice_sessions(id),
                    position INTEGER NOT NULL,
                    draft_json TEXT NOT NULL,
                    answer_text TEXT,
                    submitted_at TEXT,
                    grade_json TEXT,
                    rating TEXT,
                    graded_at TEXT,
                    correction_note TEXT,
                    UNIQUE (session_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_practice_updated ON practice_sessions(updated_at DESC);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(practice_sessions)")}
            if "response_language" not in columns:
                conn.execute("""
                    ALTER TABLE practice_sessions ADD COLUMN response_language TEXT
                    NOT NULL DEFAULT 'zh' CHECK (response_language IN ('zh', 'en'))
                """)
            initialize(conn)
            initialize_library(conn)
            initialize_provenance(conn)
            conn.execute("DROP INDEX IF EXISTS uq_active_review")
            conn.execute("""CREATE UNIQUE INDEX uq_active_review
                ON practice_sessions(review_item_id)
                WHERE review_item_id IS NOT NULL AND review_mode != 'follow_up'
                AND status IN ('waiting_for_answer','grading')""")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if getattr(self, 'conversation_path', None):
            conn.execute('ATTACH DATABASE ? AS memory', (str(self.conversation_path),))
        return conn

    def assert_basis(self, session_id):
        with closing(self._connect()) as db:
            check_session(db, session_id)

    @contextmanager
    def transaction(self, connection=None):
        """Compose seed question, enrollment and approval in one existing transaction."""
        if connection is not None:
            yield connection
            return
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def create(
        self, objective: str, focus_source_id: str, focus_start: int, focus_end: int,
        snapshots: list[dict], visible_ranges: list[dict], draft: QuestionDraft,
        *, response_language: ResponseLanguage = "zh",
        review_item_id: str | None = None, review_mode: str | None = None,
        target_id: str | None = None, question_ref: str | None = None,
        collection_id: str = "root",
        basis=None,
        document_dependencies=(),
        _connection=None,
    ) -> str:
        validate_language(response_language)
        session_id, question_id, timestamp = uuid4().hex, uuid4().hex, _now()
        with self.transaction(_connection) as conn:
            for document in document_dependencies:
                row = conn.execute('SELECT revision,removed FROM document_states WHERE document_id=?', (document['id'],)).fetchone()
                if row is None or row['removed'] or row['revision'] != document['revision']:
                    raise ValueError('出题资料已替换或移除，请刷新后重新生成。')
            if basis:
                check_dependencies(conn, basis['dependencies'])
            if review_item_id:
                item = conn.execute("SELECT status FROM review_items WHERE id=?", (review_item_id,)).fetchone()
                if item is None or item["status"] != "active":
                    raise ValueError("该复习项不存在或已暂停。")
                pending = conn.execute("""SELECT id FROM practice_sessions WHERE review_item_id=?
                    AND review_mode != 'follow_up'
                    AND status IN ('waiting_for_answer','grading')""", (review_item_id,)).fetchone()
                if pending:
                    return pending["id"]
            conn.execute("""
                INSERT INTO practice_sessions (
                    id, created_at, updated_at, status, objective, focus_source_id,
                    focus_start, focus_end, sources_json, visible_json, response_language, collection_id
                ) VALUES (?, ?, ?, 'waiting_for_answer', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, timestamp, timestamp, objective, focus_source_id,
                  focus_start, focus_end, json.dumps(snapshots, ensure_ascii=False),
                  json.dumps(visible_ranges, ensure_ascii=False), response_language, collection_id))
            conn.execute("""
                INSERT INTO practice_questions (id, session_id, position, draft_json)
                VALUES (?, ?, 1, ?)
            """, (question_id, session_id, draft.model_dump_json()))
            if question_ref is None:
                question_ref = question_id
                conn.execute("INSERT INTO learning_questions VALUES (?, ?, ?)",
                             (question_ref, draft.model_dump_json(), session_id))
            conn.execute("UPDATE practice_questions SET question_ref=?, target_id=? WHERE id=?",
                         (question_ref, target_id, question_id))
            conn.execute("UPDATE practice_sessions SET review_item_id=?, review_mode=? WHERE id=?",
                         (review_item_id, review_mode, session_id))
            if basis:
                conn.execute('INSERT INTO practice_basis VALUES (?,?,?,?,0)',
                             (session_id, json.dumps(basis['dependencies']), json.dumps(basis['references']), basis['kind']))
        return session_id

    def get(self, session_id: str) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM practice_sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError("找不到这次练习。")
            questions = conn.execute(
                "SELECT * FROM practice_questions WHERE session_id = ? ORDER BY position", (session_id,),
            ).fetchall()
            details = {}
            basis = basis_record(conn, session_id)
            for question in questions:
                target = conn.execute("""SELECT t.draft_json, u.name, t.unit_id FROM learning_targets t
                    JOIN knowledge_units u ON u.id=t.unit_id WHERE t.id=?""", (question["target_id"],)).fetchone()
                event = conn.execute("SELECT * FROM review_events WHERE attempt_id=?", (question["id"],)).fetchone()
                revisions = [dict(item) for item in conn.execute("""SELECT revision, origin, reason, grade_json, created_at
                    FROM evaluation_revisions WHERE attempt_id=? ORDER BY revision""", (question["id"],))]
                details[question["id"]] = {
                    "target": json.loads(target["draft_json"]) if target else None,
                    "unit_name": target["name"] if target else None,
                    "unit_id": target["unit_id"] if target else None,
                    "review_event": dict(event) if event else None,
                    "evaluation_history": revisions,
                }
        return {
            **dict(row),
            "basis": basis,
            "snapshots": json.loads(row["sources_json"]),
            "visible_ranges": json.loads(row["visible_json"]),
            "questions": [{
                **dict(question),
                **details[question["id"]],
                "draft": parse_question(question["draft_json"]),
                "grade": parse_grade(question["grade_json"])
                if question["grade_json"] else None,
            } for question in questions],
        }

    def list(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("""
                SELECT s.id, s.objective, s.status, s.updated_at,
                       q.draft_json, q.position
                FROM practice_sessions s JOIN practice_questions q
                  ON q.session_id = s.id
                 AND q.position = (SELECT MAX(position) FROM practice_questions
                                   WHERE session_id = s.id)
                ORDER BY s.updated_at DESC, s.id DESC
            """).fetchall()
        return [{
            "id": row["id"], "objective": row["objective"], "status": row["status"],
            "updated_at": row["updated_at"], "position": row["position"],
            "question": parse_question(row["draft_json"]).question,
        } for row in rows]

    def submit_answer(
        self, session_id: str, answer: str, *, response_language: ResponseLanguage | None = None,
        assistance: str = "none_recorded",
    ) -> None:
        validate_language(response_language)
        if assistance not in {"none_recorded", "hint_used", "reference_seen"}:
            raise ValueError("未知的提示使用状态。")
        if not answer.strip():
            raise ValueError("请先填写你的回答。")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            check_session(conn, session_id)
            session = conn.execute(
                "SELECT status FROM practice_sessions WHERE id = ?", (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError("找不到这次练习。")
            if session["status"] != "waiting_for_answer":
                raise ValueError("当前练习不在等待作答状态。")
            question = conn.execute("""
                SELECT id, answer_text, assistance FROM practice_questions WHERE session_id = ?
                ORDER BY position DESC LIMIT 1
            """, (session_id,)).fetchone()
            if question["answer_text"] is not None:
                raise ValueError("这道题已经提交过答案。")
            timestamp = _now()
            conn.execute("""
                UPDATE practice_questions SET answer_text = ?, submitted_at = ?, assistance=? WHERE id = ?
            """, (answer, timestamp, "reference_seen" if question["assistance"] == "reference_seen" else assistance, question["id"]))
            conn.execute("""
                UPDATE practice_sessions SET status = 'grading', updated_at = ?,
                    response_language = COALESCE(?, response_language) WHERE id = ?
            """, (timestamp, response_language, session_id))

    def set_grading_language(self, session_id: str, response_language: ResponseLanguage) -> None:
        validate_language(response_language)
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            check_session(conn, session_id)
            changed = conn.execute("""
                UPDATE practice_sessions SET response_language = ?
                WHERE id = ? AND status = 'grading'
            """, (response_language, session_id)).rowcount
            if changed != 1:
                # Preserve the distinction between a missing session and invalid state.
                if conn.execute("SELECT id FROM practice_sessions WHERE id = ?", (session_id,)).fetchone() is None:
                    raise KeyError("找不到这次练习。")
                raise ValueError("当前没有等待评分的答案。")

    def save_grade(
        self, session_id: str, grade: GradeDraft, rating: str,
        follow_up: QuestionDraft | None,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            check_session(conn, session_id)
            session = conn.execute(
                "SELECT status FROM practice_sessions WHERE id = ?", (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError("找不到这次练习。")
            if session["status"] != "grading":
                raise ValueError("当前练习没有等待评分的答案。")
            question = conn.execute("""
                SELECT * FROM practice_questions
                WHERE session_id = ? ORDER BY position DESC LIMIT 1
            """, (session_id,)).fetchone()
            if question["answer_text"] is None or question["grade_json"] is not None:
                raise ValueError("当前答案不能重复评分。")
            timestamp = _now()
            conn.execute("""
                UPDATE practice_questions SET grade_json = ?, rating = ?, graded_at = ? WHERE id = ?
            """, (grade.model_dump_json(), rating, timestamp, question["id"]))
            record_evaluation(conn, question, grade, timestamp)
            if follow_up is not None:
                if question["position"] >= MAX_QUESTIONS_PER_SESSION:
                    raise ValueError("这次练习已达到题目上限。")
                conn.execute("UPDATE practice_questions SET candidate_json=? WHERE id=?",
                             (follow_up.model_dump_json(), question["id"]))
            status = "finished"
            conn.execute("""
                UPDATE practice_sessions SET status = ?, updated_at = ? WHERE id = ?
            """, (status, timestamp, session_id))

    def continue_follow_up(self, session_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            check_session(conn, session_id)
            session = conn.execute("SELECT * FROM practice_sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise KeyError("找不到这次练习。")
            question = conn.execute("SELECT * FROM practice_questions WHERE session_id=? ORDER BY position DESC LIMIT 1",
                                    (session_id,)).fetchone()
            if session["status"] != "finished" or not question["candidate_json"]:
                raise ValueError("当前没有可继续的追问。")
            if question["position"] >= MAX_QUESTIONS_PER_SESSION:
                raise ValueError("本轮追问已达到上限，可以自由开始新的练习。")
            question_id = uuid4().hex
            conn.execute("INSERT INTO learning_questions VALUES (?, ?, ?)",
                         (question_id, question["candidate_json"], session_id))
            conn.execute("""INSERT INTO practice_questions
                (id, session_id, position, draft_json, question_ref, target_id)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (question_id, session_id, question["position"] + 1, question["candidate_json"], question_id, question["target_id"]))
            conn.execute("UPDATE practice_questions SET candidate_json=NULL WHERE id=?", (question["id"],))
            conn.execute("UPDATE practice_sessions SET status='waiting_for_answer', review_mode='follow_up', updated_at=? WHERE id=?",
                         (_now(), session_id))

    def reveal_reference(self, session_id: str, question_id: str) -> list[str]:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            check_session(conn, session_id)
            row = conn.execute("SELECT * FROM practice_questions WHERE session_id=? AND id=?",
                               (session_id, question_id)).fetchone()
            if row is None:
                raise KeyError("找不到这道题。")
            if row["answer_text"] is None:
                conn.execute("UPDATE practice_questions SET assistance='reference_seen' WHERE id=?", (question_id,))
            return parse_question(row["draft_json"]).rubric_points

    def finish(self, session_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute("SELECT status FROM practice_sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise KeyError("找不到这次练习。")
            if session["status"] != "waiting_for_answer":
                raise ValueError("当前练习不能结束。")
            conn.execute("""
                UPDATE practice_sessions SET status = 'finished', updated_at = ? WHERE id = ?
            """, (_now(), session_id))

    def save_correction(self, session_id: str, question_id: str, correction: str) -> None:
        if not correction.strip():
            raise ValueError("请填写需要更正的内容。")
        with closing(self._connect()) as conn, conn:
            changed = conn.execute("""
                UPDATE practice_questions SET correction_note = ?
                WHERE id = ? AND session_id = ? AND grade_json IS NOT NULL
            """, (correction, question_id, session_id)).rowcount
            if changed != 1:
                raise KeyError("找不到可更正的评语。")
            conn.execute("UPDATE practice_sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))


def archived_sources(snapshots: list[dict]) -> list[Source]:
    return [Source(item["source_id"], Path(item["path"]), item["text"]) for item in snapshots]


def public_session(record: dict) -> dict:
    snapshots = {item["source_id"]: item for item in record["snapshots"]}
    questions = []
    for question in record["questions"]:
        draft = question["draft"]
        citations = []
        for citation in draft.citations:
            source = snapshots[citation.source_id]
            citations.append({
                "document_id": source["document_id"], "source_id": citation.source_id,
                "name": source["name"], "line_start": citation.line_start,
                "line_end": citation.line_end,
                "excerpt": "\n".join(source["text"].splitlines()[
                    citation.line_start - 1:citation.line_end
                ]),
            })
        grade = question["grade"]
        questions.append({
            "id": question["id"], "position": question["position"],
            "objective": draft.objective, "question": draft.question,
            "rubric_points": draft.rubric_points if question["answer_text"] is not None else None,
            "citations": citations,
            "answer_text": question["answer_text"],
            "submitted_at": question["submitted_at"],
            "rating": question["rating"],
            "feedback": grade.feedback if grade else None,
            "covered_indices": grade.covered_indices if grade else None,
            "missing_indices": grade.missing_indices if grade else None,
            "answer_quotes": grade.answer_quotes if grade else None,
            "next_action": grade.next_action if grade else None,
            "teaching_note": grade.teaching_note if grade else None,
            "correction_note": question["correction_note"],
            "revision": question["revision"],
            "question_ref": question["question_ref"],
            "unit_id": question["unit_id"], "unit_name": question["unit_name"],
            "target_title": question["target"]["title"] if question["target"] else None,
            "capability": question["target"]["capability"] if question["target"] else None,
            "proposed_target": ({'knowledge_name': draft.learning.knowledge.name,
                                 **draft.learning.target.model_dump(exclude={'criteria'})} if draft.learning else None),
            "candidate_available": bool(question["candidate_json"]),
            "assistance": question["assistance"],
            "suggested_rating": grade.suggested_rating if grade else None,
            "rating_reason": grade.rating_reason if grade else None,
            "target_fit": grade.target_fit if grade else None,
            "dimension_observations": [item.model_dump() for item in grade.dimension_observations] if grade else [],
            "review_event": question["review_event"],
            "evaluation_history": [{**{key: value for key, value in item.items() if key != "grade_json"}, "grade": json.loads(item["grade_json"])}
                                   for item in question["evaluation_history"]],
        })
    return {
        "id": record["id"], "objective": record["objective"],
        "status": record["status"], "created_at": record["created_at"],
        "response_language": record["response_language"],
        "collection_id": record["collection_id"],
        "updated_at": record["updated_at"], "questions": questions,
        "review_item_id": record["review_item_id"], "review_mode": record["review_mode"],
        "basis": ({'kind': record['basis']['kind'], 'revoked': bool(record['basis']['revoked']),
                   'references': record['basis']['references']} if record.get('basis') else None),
    }
