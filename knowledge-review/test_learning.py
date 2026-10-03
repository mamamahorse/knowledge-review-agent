"""Learning lifecycle tests: distinct schedules, preserved evidence and replayable corrections."""

import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from learning_store import LearningStore
from learning_types import LearningDraft
from learning_service import enroll_question, start_review, correct_grade, EnrollmentDraft
from practice_model import grade_answer, validate_grade, validate_question, SemanticGradeDraft, ExerciseDraft
from practice_store import public_session
from review_scheduler import replay
from test_practice import SOURCE, CHUNK, question, grade
from test_support import temporary_notes
from web_app import create_app


def learning(capability="explain", source_id=SOURCE.source_id):
    return LearningDraft.model_validate({
        "knowledge": {"name": "Evidence", "statement": "Evidence supports a claim.", "content_tags": [],
            "citations": [{"source_id": source_id, "line_start": 3, "line_end": 3}]},
        "target": {"title": f"{capability} evidence", "capability": capability,
            "description": "Explain how evidence supports a claim.", "scope": "The supplied idea only.",
            "criteria": question().rubric_points},
    })


def learning_question(capability="explain"):
    return question().model_copy(update={"learning": learning(capability), "criterion_indices": [1, 2]})


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.notes = temporary_notes()
        self.root = self.notes.__enter__()
        self.store = LearningStore(self.root / "practice.sqlite3")

    def tearDown(self):
        self.notes.__exit__(None, None, None)

    def create(self, draft=None):
        session_id = self.store.create("Evidence", SOURCE.source_id, 3, 3,
            [{"document_id": "note-id", "source_id": SOURCE.source_id,
              "path": str(SOURCE.path), "name": "note.md", "text": SOURCE.text}],
            [{"source_id": SOURCE.source_id, "line_start": 1, "line_end": 3}], draft or learning_question())
        return session_id, self.store.get(session_id)["questions"][0]["id"]

    def save(self, session_id, draft_grade=None, assistance="none_recorded"):
        self.store.submit_answer(session_id, "The idea matters", assistance=assistance)
        result = draft_grade or grade(False)
        self.store.save_grade(session_id, result, result.overall_assessment, result.follow_up)
        return self.store.get(session_id)["questions"][0]

    def enroll_completed(self):
        session_id, question_id = self.create()
        self.save(session_id)
        return enroll_question(self.store, session_id, question_id)

    def test_opt_in_is_idempotent_and_does_not_invent_review_for_old_answer(self):
        session_id, question_id = self.create()
        self.save(session_id)
        self.assertEqual(self.store.list_plan(), [])
        with patch("learning_service._request_structured") as model:
            item_id = enroll_question(self.store, session_id, question_id)
            self.assertEqual(item_id, enroll_question(self.store, session_id, question_id))
            model.assert_not_called()
        self.assertEqual(len(self.store.list_plan()[0]["items"][0]["evidence"]), 1)
        self.assertIsNone(self.store.get(session_id)["questions"][0]["review_event"])
        self.assertTrue(self.store.list_plan()[0]["items"][0]["due"])

    def test_legacy_enrollment_extracts_general_criteria_without_rewriting_original_question(self):
        session_id, qid = self.create(question())
        extracted = EnrollmentDraft(learning=learning().model_copy(update={
            "target": learning().target.model_copy(update={"criteria": ["Use evidence to explain the claim"]})}),
            criterion_indices=[1, 1])
        with patch("learning_service._request_structured", return_value=extracted) as model:
            item_id = enroll_question(self.store, session_id, qid)
            enroll_question(self.store, session_id, qid)
            model.assert_called_once()
        original = start_review(self.store, item_id, mode="original")
        # This is the unfinished original enrollment, so finish then repeat it.
        self.store.finish(original["id"])
        repeated = start_review(self.store, item_id, mode="original")
        self.assertEqual(self.store.get(repeated["id"])["questions"][0]["draft"].criterion_indices, [1, 1])
        self.assertIsNone(self.store.get(session_id)["questions"][0]["draft"].learning)
        self.assertEqual(self.store.item(item_id)["target"]["criteria"], ["Use evidence to explain the claim"])

    def test_enrollment_before_answer_counts_first_real_answer_and_retries_do_not_duplicate(self):
        session_id, question_id = self.create()
        item_id = enroll_question(self.store, session_id, question_id)
        initial = self.store.item(item_id)["next_review_at"]
        result = self.save(session_id)
        self.assertTrue(result["review_event"]["eligible"])
        self.assertNotEqual(initial, self.store.item(item_id)["next_review_at"])
        with self.assertRaises(ValueError):
            self.store.save_grade(session_id, grade(False), "developing", None)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(len(self.store.list_plan()[0]["items"][0]["evidence"]), 1)

    def test_variant_and_original_share_target_but_each_answer_is_separate(self):
        item_id = self.enroll_completed()
        item = self.store.item(item_id)
        variant = question("How could you test this claim?").model_copy(update={"criterion_indices": [1, 2]})
        with patch("learning_service._request_structured", return_value=variant) as model:
            first = start_review(self.store, item_id)
            self.assertEqual(start_review(self.store, item_id)["id"], first["id"])
            model.assert_called_once()
        q1 = first["questions"][0]
        self.assertNotEqual(q1["question_ref"], item["original_question_id"])
        self.assertEqual(q1["target_title"], item["target"]["title"])
        self.save(first["id"])
        original = start_review(self.store, item_id, mode="original")
        self.assertEqual(original["questions"][0]["question_ref"], item["original_question_id"])
        self.assertNotEqual(original["questions"][0]["id"], q1["id"])
        LearningStore(self.store.path)
        with closing(self.store._connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM learning_questions").fetchone()[0], 2)

    def test_grouped_dimensions_have_independent_dates_and_source_grounding(self):
        explain_id = self.enroll_completed()
        session_id, question_id = self.create(learning_question("apply"))
        apply_id = self.store.enroll(session_id, question_id, learning("apply"), unit_id=self.store.item(explain_id)["unit_id"])
        explain_date = self.store.item(explain_id)["next_review_at"]
        self.save(session_id)
        self.assertEqual(self.store.item(explain_id)["next_review_at"], explain_date)
        self.assertNotEqual(self.store.item(apply_id)["next_review_at"], explain_date)
        self.assertEqual(len(self.store.list_plan()), 1)
        self.assertEqual(len(self.store.list_plan()[0]["items"]), 2)
        self.assertEqual(len(self.store.item(explain_id)["related_history"]), 1)

    def test_explicit_grouping_keeps_each_targets_own_source_snapshot(self):
        item_id = self.enroll_completed()
        draft = learning_question("apply").model_copy(update={"citations": [question().citations[0].model_copy(update={"source_id": "version-b"})]})
        own = learning("apply", "version-b")
        draft = draft.model_copy(update={"learning": own})
        session_id = self.store.create("Other source", "version-b", 3, 3,
            [{"document_id": "other", "source_id": "version-b", "path": str(SOURCE.path), "name": "other.md", "text": SOURCE.text}],
            [{"source_id": "version-b", "line_start": 1, "line_end": 3}], draft)
        qid = self.store.get(session_id)["questions"][0]["id"]
        self.save(session_id)
        other_id = enroll_question(self.store, session_id, qid, unit_id=self.store.item(item_id)["unit_id"])
        repeated = start_review(self.store, other_id, mode="original")
        self.assertEqual(repeated["questions"][0]["citations"][0]["source_id"], "version-b")
        self.assertEqual(self.store.item(other_id)["knowledge"]["citations"][0]["source_id"], "version-b")

    def test_uncertainty_invalid_scope_and_assistance_do_not_advance_schedule(self):
        for updates, assistance in [({"overall_assessment": "uncertain"}, "none_recorded"),
                                     ({"target_fit": "outside_scope"}, "none_recorded"), ({}, "hint_used")]:
            item_id = self.enroll_completed()
            old_due = self.store.item(item_id)["next_review_at"]
            session = start_review(self.store, item_id, mode="original")
            result = self.save(session["id"], grade(False).model_copy(update=updates), assistance)
            self.assertFalse(result["review_event"]["eligible"])
            self.assertEqual(self.store.item(item_id)["next_review_at"], old_due)

    def test_reference_disclosure_cannot_be_erased_by_answer_form(self):
        item_id = self.enroll_completed()
        session = start_review(self.store, item_id, mode="original")
        qid = session["questions"][0]["id"]
        self.store.reveal_reference(session["id"], qid)
        result = self.save(session["id"])
        self.assertEqual(result["assistance"], "reference_seen")
        self.assertFalse(result["review_event"]["eligible"])

    def test_semantic_grade_is_not_computed_from_point_counts_and_one_call_returns_feedback(self):
        holistic = grade(False).model_copy(update={"overall_assessment": "meets_standard", "suggested_rating": "Good"})
        with patch("practice_model._request_structured", return_value=holistic) as model:
            result, assessment = grade_answer(question(), "The idea matters", [SOURCE], [CHUNK], can_follow_up=True)
            self.assertEqual(assessment, "meets_standard")
            self.assertEqual(result.missing_indices, [2])
            model.assert_called_once()
        bad = learning_question().model_copy(update={"criterion_indices": [1, 1]})
        with self.assertRaisesRegex(ValueError, "criteria"):
            validate_question(bad, [SOURCE], [CHUNK])

    def test_teaching_follow_up_contract_does_not_request_another_long_term_target(self):
        schema = SemanticGradeDraft.model_json_schema()
        self.assertEqual(set(schema["$defs"]["ExerciseDraft"]["properties"]),
                         {"objective", "question", "rubric_points", "citations"})
        semantic = SemanticGradeDraft.model_validate({**grade(True).model_dump(),
            "follow_up": ExerciseDraft.model_validate(question().model_dump(include={"objective", "question", "rubric_points", "citations"})).model_dump()})
        with patch("practice_model._request_structured", return_value=semantic):
            result, _ = grade_answer(question(), "The idea matters", [SOURCE], [CHUNK], can_follow_up=True)
        self.assertIsNone(result.follow_up.learning)
        self.assertEqual(result.follow_up.criterion_indices, [])

    def test_follow_up_is_optional_and_does_not_count_as_second_scheduled_review(self):
        item_id = self.enroll_completed()
        session = start_review(self.store, item_id, mode="original")
        self.save(session["id"], grade(True))
        due = self.store.item(item_id)["next_review_at"]
        self.assertEqual(len(self.store.get(session["id"])["questions"]), 1)
        # A fresh review may coexist with an optional teaching follow-up.
        new_review = start_review(self.store, item_id, mode="original")
        self.store.continue_follow_up(session["id"])
        self.assertEqual(self.store.item(item_id)["pending_session_id"], new_review["id"])
        self.save(session["id"], grade(False))
        self.assertEqual(self.store.item(item_id)["next_review_at"], due)
        self.assertIsNone(self.store.get(session["id"])["questions"][1]["review_event"])

    def test_user_revision_preserves_answer_original_grade_and_explicit_rating(self):
        item_id = self.enroll_completed()
        session = start_review(self.store, item_id, mode="original")
        result = self.save(session["id"])
        event_id = result["review_event"]["id"]
        self.store.set_rating(event_id, "Easy", result["revision"])
        updates = {"overall_assessment": "meets_standard", "suggested_rating": "Good", "feedback": "An alternative explanation was valid."}
        corrected = correct_grade(self.store, session["id"], result["id"], updates, "Valid alternative reasoning", 1)
        q = corrected["questions"][0]
        self.assertEqual(q["answer_text"], "The idea matters")
        self.assertEqual(q["revision"], 2)
        self.assertEqual(q["review_event"]["effective_rating"], "Easy")
        self.assertEqual(q["evaluation_history"][0]["grade"]["feedback"], grade(False).feedback)
        self.assertEqual(q["evaluation_history"][1]["origin"], "user")
        with self.assertRaisesRegex(ValueError, "版本"):
            correct_grade(self.store, session["id"], result["id"], updates, "Stale update", 1)

    def test_correction_replays_later_reviews_using_original_times_and_parameters(self):
        item_id = self.enroll_completed()
        first = start_review(self.store, item_id, mode="original")
        q1 = self.save(first["id"])
        second = start_review(self.store, item_id, mode="original")
        q2 = self.save(second["id"], grade(False).model_copy(update={"suggested_rating": "Good"}))
        # Use actual fixed historical times to exercise replay over an interval.
        with closing(self.store._connect()) as conn, conn:
            first_at = datetime.fromisoformat(q1["submitted_at"])
            conn.execute("UPDATE review_events SET reviewed_at=? WHERE attempt_id=?", ((first_at + timedelta(days=2)).isoformat(), q2["id"]))
            conn.execute("UPDATE practice_questions SET submitted_at=? WHERE id=?", ((first_at + timedelta(days=2)).isoformat(), q2["id"]))
        correct_grade(self.store, first["id"], q1["id"], {"suggested_rating": "Easy"}, "Recall was successful", 1)
        item = self.store.item(item_id)
        with closing(self.store._connect()) as conn:
            events = [dict(row) for row in conn.execute("SELECT * FROM review_events WHERE item_id=? ORDER BY reviewed_at,id", (item_id,))]
        from fsrs import Scheduler, Card, Rating
        scheduler = Scheduler.from_dict(json.loads(item["parameters_json"]))
        card = Card.from_dict(json.loads(item["initial_card_json"]))
        card, _ = scheduler.review_card(card, Rating.Easy, review_datetime=first_at)
        card, _ = scheduler.review_card(card, Rating.Good, review_datetime=first_at + timedelta(days=2))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["reviewed_at"], q1["submitted_at"])
        self.assertEqual(json.loads(item["card_json"]), card.to_dict())
        self.assertEqual(item["next_review_at"], card.to_dict()["due"])

    def test_legacy_answer_migration_keeps_grade_and_adds_one_revision(self):
        session_id, qid = self.create(question())
        self.save(session_id)
        with closing(self.store._connect()) as conn, conn:
            old = grade(False).model_dump(exclude={"overall_assessment", "suggested_rating", "target_fit", "rating_reason", "dimension_observations"})
            conn.execute("UPDATE practice_questions SET grade_json=?,effective_evaluation_id=NULL,revision=0 WHERE id=?", (json.dumps(old), qid))
            conn.execute("DELETE FROM evaluation_revisions WHERE attempt_id=?", (qid,))
        reopened = LearningStore(self.store.path)
        reopened = LearningStore(self.store.path)
        q = public_session(reopened.get(session_id))["questions"][0]
        self.assertEqual(q["answer_text"], "The idea matters")
        self.assertEqual(len(q["evaluation_history"]), 1)
        self.assertEqual(q["evaluation_history"][0]["origin"], "legacy")
        self.assertIsNone(q["suggested_rating"])

    def test_pause_blocks_review_but_preserves_evidence(self):
        item_id = self.enroll_completed()
        self.store.set_status(item_id, "paused")
        with self.assertRaisesRegex(ValueError, "暂停"):
            start_review(self.store, item_id, mode="original")
        self.assertFalse(self.store.list_plan()[0]["items"][0]["due"])
        self.store.set_status(item_id, "active")
        self.assertEqual(start_review(self.store, item_id, mode="original")["status"], "waiting_for_answer")

    def test_api_enrollment_review_correction_and_rating_are_persistent(self):
        session_id, qid = self.create()
        self.save(session_id)
        with TestClient(create_app(self.root / "uploads", self.store.path)) as client:
            response = client.post(f"/api/practice/{session_id}/enroll", json={"question_id": qid})
            self.assertEqual(response.status_code, 200, response.text)
            item_id = response.json()["item_id"]
            started = client.post(f"/api/learning/{item_id}/review", json={"mode": "original"}).json()
            with patch("practice_service.grade_answer", return_value=(grade(False), "developing")) as model:
                graded = client.post(f"/api/practice/{started['id']}/answers", json={"answer": "The idea matters"})
                self.assertEqual(graded.status_code, 200, graded.text)
                model.assert_called_once()
            q = graded.json()["questions"][0]
            payload = {"question_id": q["id"], "expected_revision": 1, "overall_assessment": "meets_standard",
                "covered_indices": [1, 2], "missing_indices": [], "feedback": "Valid explanation.",
                "suggested_rating": "Good", "target_fit": "within_scope", "reason": "Accepted alternative."}
            edited = client.post(f"/api/practice/{started['id']}/evaluation", json=payload)
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(client.post(f"/api/practice/{started['id']}/evaluation", json=payload).status_code, 409)
            rating = client.patch(f"/api/review-events/{q['review_event']['id']}", json={"rating": "Hard", "expected_revision": 2})
            self.assertEqual(rating.status_code, 200, rating.text)
            self.assertEqual(client.get("/api/learning").json()[0]["items"][0]["recent_evidence_count"], 2)
        self.assertEqual(LearningStore(self.store.path).get(started["id"])["questions"][0]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
