"""Practice state and API tests use saved answers and controlled model results."""

import json
import unittest
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from openai import OpenAI

from chunking import Chunk
from practice_model import GradeDraft, QuestionDraft, _request_structured, validate_grade, validate_question
from practice_store import PracticeStore, public_session
from sources import Source
from test_support import temporary_notes
from web_app import create_app
from structured_output import FUNCTION_OUTPUT_PROVIDERS


SOURCE = Source("version-a", Path("note.md"), "# Heading\n\nAn idea needs evidence.\n")
CHUNK = Chunk("visible", SOURCE.source_id, (), 1, 3, "# Heading\n\nAn idea needs evidence.")


def question(prompt="Why does the idea need evidence?"):
    return QuestionDraft.model_validate({
        "objective": "Understand evidence", "question": prompt,
        "rubric_points": ["Identify the idea", "Explain the need for evidence"],
        "citations": [{"source_id": SOURCE.source_id, "line_start": 3, "line_end": 3}],
    })


def grade(follow_up=True):
    return GradeDraft.model_validate({
        "covered_indices": [1], "missing_indices": [2],
        "answer_quotes": ["The idea matters"],
        "feedback": "You named the idea but did not explain evidence.",
        "next_action": "follow_up" if follow_up else "finish",
        "teaching_note": "Evidence lets readers verify the claim.",
        "follow_up": question("How would you verify the idea?").model_dump() if follow_up else None,
        "overall_assessment": "developing", "suggested_rating": "Again",
        "rating_reason": "The answer does not explain the mechanism.", "target_fit": "within_scope",
    })


class PracticeTests(unittest.TestCase):
    def setUp(self):
        FUNCTION_OUTPUT_PROVIDERS.clear()

    def test_provider_without_schema_support_gets_exact_fallback_contract(self):
        calls = []

        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            if len(calls) == 1:
                return httpx.Response(400, json={"error": {
                    "message": "response_format is unsupported", "type": "invalid_request_error",
                }})
            self.assertEqual(body['messages'], [{'role': 'user', 'content': 'Ask'}])
            self.assertEqual(body['tools'][0]['function']['parameters'], QuestionDraft.model_json_schema())
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, 'tool_calls': [{
                        'id': 'result', 'type': 'function', 'function': {
                            'name': 'submit_result', 'arguments': question().model_dump_json(),
                        },
                    }],
                }}],
            })

        with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with patch("practice_model.OpenAI", return_value=client), \
                 patch("practice_model.model_settings", return_value=("test", {"api_key": "test"})):
                self.assertEqual(_request_structured([{"role": "user", "content": "Ask"}], QuestionDraft), question())
        self.assertEqual(len(calls), 2)

    def test_question_and_grade_reject_forged_citations_or_answer_quotes(self):
        validate_question(question(), [SOURCE], [CHUNK])
        bad = question().model_copy(update={"citations": [
            question().citations[0].model_copy(update={"line_start": 2, "line_end": 4})
        ]})
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_question(bad, [SOURCE], [CHUNK])
        self.assertEqual(validate_grade(grade(), question(), "The idea matters", [SOURCE], [CHUNK],
                                        can_follow_up=True), "developing")
        with self.assertRaisesRegex(ValueError, "quote"):
            validate_grade(grade(), question(), "Different answer", [SOURCE], [CHUNK],
                           can_follow_up=True)
        with self.assertRaisesRegex(ValueError, "question limit"):
            validate_grade(grade(), question(), "The idea matters", [SOURCE], [CHUNK],
                           can_follow_up=False)

    def test_raw_answer_survives_reopen_and_follow_up_waits_for_real_answer(self):
        with temporary_notes() as root:
            path = root / "practice.sqlite3"
            store = PracticeStore(path)
            session_id = store.create(
                "Understand evidence", SOURCE.source_id, 3, 3,
                [{"document_id": "note-id", "source_id": SOURCE.source_id,
                  "path": str(SOURCE.path), "name": "note.md", "text": SOURCE.text}],
                [{"source_id": SOURCE.source_id, "line_start": 1, "line_end": 3}],
                question(), response_language='en',
            )
            self.assertIsNone(public_session(store.get(session_id))["questions"][0]["rubric_points"])
            store.submit_answer(session_id, "The idea matters")
            with self.assertRaisesRegex(ValueError, "等待作答"):
                store.submit_answer(session_id, "A replacement answer")
            store = PracticeStore(path)
            record = store.get(session_id)
            self.assertEqual(record["status"], "grading")
            self.assertEqual(record['response_language'], 'en')
            self.assertEqual(record["questions"][0]["answer_text"], "The idea matters")
            store.save_grade(session_id, grade(), "developing", grade().follow_up)
            result = public_session(store.get(session_id))
            self.assertEqual(result["status"], "finished")
            self.assertEqual(len(result["questions"]), 1)
            self.assertTrue(result["questions"][0]["candidate_available"])
            store.continue_follow_up(session_id)
            result = public_session(store.get(session_id))
            self.assertEqual(result["status"], "waiting_for_answer")
            self.assertEqual(len(result["questions"]), 2)
            self.assertIsNone(result["questions"][1]["answer_text"])
            self.assertEqual(result["questions"][0]["citations"][0]["excerpt"],
                             "An idea needs evidence.")
            store.save_correction(session_id, result["questions"][0]["id"], "Please reconsider point 2.")
            self.assertIn("reconsider", public_session(store.get(session_id))["questions"][0]["correction_note"])
            store.finish(session_id)
            self.assertEqual(PracticeStore(path).get(session_id)["status"], "finished")

    def test_web_flow_saves_answer_before_failed_grading_and_allows_retry(self):
        with temporary_notes() as root:
            app = create_app(root / "uploads")
            try:
              with TestClient(app) as client:
                imported = client.post("/api/documents", files=[
                    ("files", ("note.md", SOURCE.text.encode(), "text/markdown")),
                ]).json()[0]
                note = client.get(f"/api/documents/{imported['id']}").json()
                source_id = note["source_id"]
                draft = question().model_copy(update={"citations": [
                    question().citations[0].model_copy(update={"source_id": source_id})
                ]})
                with patch("practice_service.generate_question", return_value=draft) as generate:
                    created = client.post("/api/practice", json={
                        "document_ids": [imported["id"]],
                        "focus_document_id": imported["id"],
                        "focus_source_id": source_id,
                        "line_start": 3, "line_end": 3,
                        "objective": "Understand evidence",
                        "response_language": "en",
                    })
                self.assertEqual(created.status_code, 201, created.text)
                self.assertEqual(generate.call_args.kwargs['response_language'], 'en')
                self.assertEqual(created.json()['response_language'], 'en')
                session_id = created.json()["id"]
                self.assertIsNone(created.json()["questions"][0]["rubric_points"])
                with patch("web_app.grade_pending_answer", side_effect=RuntimeError("offline")), \
                     patch("web_app.LOGGER.exception"):
                    failed = client.post(f"/api/practice/{session_id}/answers",
                                         json={"answer": "The idea matters", "response_language": "zh"})
                self.assertEqual(failed.status_code, 502)
                saved = client.get(f"/api/practice/{session_id}").json()
                self.assertEqual(saved["status"], "grading")
                self.assertEqual(saved['response_language'], 'zh')
                self.assertEqual(saved["questions"][0]["answer_text"], "The idea matters")
                with patch("practice_service.grade_answer") as assess:
                    draft_grade = grade().model_copy(update={"follow_up": None, "next_action": "finish"})
                    assess.return_value = (draft_grade, "developing")
                    retried = client.post(f"/api/practice/{session_id}/grade", json={'response_language': 'en'})
                self.assertEqual(retried.status_code, 200, retried.text)
                self.assertEqual(retried.json()["status"], "finished")
                self.assertEqual(assess.call_args.kwargs['response_language'], 'en')
                self.assertEqual(retried.json()['response_language'], 'en')
                self.assertEqual(len(client.get("/api/practice").json()), 1)
            finally:
                shutil.rmtree(root / "uploads", ignore_errors=True)

    def test_old_database_migration_preserves_records_and_defaults_language(self):
        with temporary_notes() as root:
            path = root / 'old.sqlite3'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute('''CREATE TABLE practice_sessions (
                    id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, status TEXT,
                    objective TEXT, focus_source_id TEXT, focus_start INTEGER, focus_end INTEGER,
                    sources_json TEXT, visible_json TEXT
                )''')
                conn.execute("INSERT INTO practice_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             ('old', 'then', 'then', 'finished', 'Old objective', SOURCE.source_id, 3, 3, '[]', '[]'))
            store = PracticeStore(path)
            self.assertEqual(store.get('old')['objective'], 'Old objective')
            self.assertEqual(store.get('old')['response_language'], 'zh')
            self.assertEqual(PracticeStore(path).get('old')['status'], 'finished')


if __name__ == "__main__":
    unittest.main()
