"""Learning plans and user revisions in the same database as immutable answers."""

import json
from datetime import datetime
from contextlib import closing
from uuid import uuid4

from learning_types import LearningDraft
from practice_model import GradeDraft
from practice_store import PracticeStore
from learning_state import record_evaluation, reschedule
from review_scheduler import initial_schedule, now, SCHEDULER_VERSION


class LearningStore(PracticeStore):
    def enroll(self, session_id, attempt_id, learning: LearningDraft, *, unit_id=None, criterion_indices=None):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute("SELECT * FROM practice_questions WHERE id=? AND session_id=?",
                                   (attempt_id, session_id)).fetchone()
            if attempt is None:
                raise KeyError("找不到这次作答或题目。")
            if attempt["target_id"]:
                existing = conn.execute("SELECT id FROM review_items WHERE target_id=?", (attempt["target_id"],)).fetchone()
                if existing:
                    return existing["id"]
            timestamp = now()
            if unit_id:
                if not conn.execute("SELECT id FROM knowledge_units WHERE id=?", (unit_id,)).fetchone():
                    raise KeyError("找不到选择的知识点。")
            else:
                unit_id = uuid4().hex
                conn.execute("INSERT INTO knowledge_units VALUES (?, ?, ?, ?)",
                             (unit_id, learning.knowledge.name, learning.knowledge.model_dump_json(), timestamp))
            target_id, item_id = uuid4().hex, uuid4().hex
            if criterion_indices is None:
                draft = json.loads(attempt["draft_json"])
                criterion_indices = draft.get("criterion_indices") or list(range(1, len(draft["rubric_points"]) + 1))
            conn.execute("""INSERT INTO learning_targets
                (id, unit_id, draft_json, original_question_id, source_session_id, source_knowledge_json, criterion_indices_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (target_id, unit_id, learning.target.model_dump_json(), attempt["question_ref"], session_id,
                 learning.knowledge.model_dump_json(), json.dumps(criterion_indices)))
            initial, parameters = initial_schedule(timestamp)
            conn.execute("INSERT INTO review_items VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                         (item_id, target_id, timestamp, json.dumps(initial), json.dumps(initial),
                          json.dumps(parameters), SCHEDULER_VERSION, initial["due"]))
            conn.execute("UPDATE practice_questions SET target_id=? WHERE id=?", (target_id, attempt_id))
            if attempt["answer_text"] is None and attempt["position"] == 1:
                conn.execute("""UPDATE practice_sessions SET review_item_id=?, review_mode='original'
                    WHERE id=? AND review_item_id IS NULL AND status='waiting_for_answer'""", (item_id, session_id))
            # Old answers remain evidence, but enrollment does not invent prior scheduled reviews.
            return item_id

    def item(self, item_id):
        with closing(self._connect()) as conn:
            row = conn.execute("""SELECT r.*, t.unit_id, t.draft_json AS target_json,
                t.original_question_id, t.source_session_id, t.criterion_indices_json,
                COALESCE(t.source_knowledge_json,u.draft_json) AS knowledge_json
                FROM review_items r JOIN learning_targets t ON t.id=r.target_id
                JOIN knowledge_units u ON u.id=t.unit_id WHERE r.id=?""", (item_id,)).fetchone()
            if row is None:
                raise KeyError("找不到复习项。")
            original = conn.execute("SELECT draft_json FROM learning_questions WHERE id=?", (row["original_question_id"],)).fetchone()
            pending = conn.execute("""SELECT id FROM practice_sessions WHERE review_item_id=?
                AND review_mode != 'follow_up'
                AND status IN ('waiting_for_answer','grading')""", (item_id,)).fetchone()
            history = [dict(item) for item in conn.execute("""SELECT q.answer_text, q.assistance, q.grade_json, q.submitted_at,
                q.draft_json, e.origin FROM practice_questions q
                LEFT JOIN evaluation_revisions e ON e.id=q.effective_evaluation_id
                WHERE q.target_id=? AND q.grade_json IS NOT NULL ORDER BY q.submitted_at DESC LIMIT 8""", (row["target_id"],))]
            related = [dict(entry) for entry in conn.execute("""SELECT q.answer_text, q.assistance, q.grade_json,
                q.submitted_at, q.draft_json, t.draft_json AS target_json FROM practice_questions q
                JOIN learning_targets t ON t.id=q.target_id
                WHERE t.unit_id=? AND t.id!=? AND q.grade_json IS NOT NULL
                ORDER BY q.submitted_at DESC LIMIT 8""", (row["unit_id"], row["target_id"]))]
        return {**dict(row), "knowledge": json.loads(row["knowledge_json"]), "target": json.loads(row["target_json"]),
                "original": json.loads(original["draft_json"]), "pending_session_id": pending["id"] if pending else None,
                "history": history, "related_history": related,
                "original_criterion_indices": json.loads(row["criterion_indices_json"])}

    def list_plan(self):
        with closing(self._connect()) as conn:
            units = [dict(row) for row in conn.execute("SELECT * FROM knowledge_units ORDER BY created_at DESC")]
            item_ids = [row["id"] for row in conn.execute("SELECT id FROM review_items ORDER BY next_review_at")]
        groups = {row["id"]: {"id": row["id"], "name": row["name"],
                   "statement": json.loads(row["draft_json"])["statement"], "items": []} for row in units}
        moment = now()
        for item_id in item_ids:
            item = self.item(item_id)
            evidence = []
            for entry in item["history"]:
                grade = GradeDraft.model_validate_json(entry["grade_json"])
                evidence.append({"submitted_at": entry["submitted_at"], "assistance": entry["assistance"],
                    "assessment": grade.overall_assessment or "legacy", "feedback": grade.feedback,
                    "origin": entry["origin"], "dimension_observations": [x.model_dump() for x in grade.dimension_observations]})
            groups[item["unit_id"]]["items"].append({
                "id": item_id, "target_id": item["target_id"], **item["target"],
                "status": item["status"], "next_review_at": item["next_review_at"],
                "due": datetime.fromisoformat(item["next_review_at"]) <= datetime.fromisoformat(moment) and item["status"] == "active",
                "pending_session_id": item["pending_session_id"], "evidence": evidence,
                "recent_evidence_count": len(evidence),
            })
        return list(groups.values())

    def set_status(self, item_id, status):
        with closing(self._connect()) as conn, conn:
            changed = conn.execute("UPDATE review_items SET status=? WHERE id=?", (status, item_id)).rowcount
            if not changed:
                raise KeyError("找不到复习项。")

    def set_rating(self, event_id, rating, expected_revision):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute("""SELECT e.*, q.revision FROM review_events e
                JOIN practice_questions q ON q.id=e.attempt_id WHERE e.id=?""", (event_id,)).fetchone()
            if event is None:
                raise KeyError("找不到复习记录。")
            if event["revision"] != expected_revision:
                raise ValueError("评分已发生变化，请刷新后再修改。")
            if not event["eligible"]:
                raise ValueError("本次未进入调度，不能仅修改档位使其变成有效复习。")
            conn.execute("UPDATE review_events SET effective_rating=?, rating_origin='user' WHERE id=?", (rating, event_id))
            reschedule(conn, event["item_id"])

    def revise_grade(self, session_id, attempt_id, grade, reason, expected_revision):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            question = conn.execute("SELECT * FROM practice_questions WHERE id=? AND session_id=?",
                                    (attempt_id, session_id)).fetchone()
            if question is None:
                raise KeyError("找不到作答。")
            if not question["grade_json"] or question["revision"] != expected_revision:
                raise ValueError("评分版本已变化，请刷新后修改。")
            conn.execute("UPDATE practice_questions SET grade_json=?, rating=?, correction_note=? WHERE id=?",
                         (grade.model_dump_json(), grade.overall_assessment, reason, attempt_id))
            record_evaluation(conn, question, grade, now(), origin="user", reason=reason)
