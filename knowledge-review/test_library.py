"""Collection migration, opt-in learning, full-document context and title lifecycle."""

import shutil
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_loop import AgentRun
from answers import NoteAnswer
from conversation_store import ConversationStore
from conversation_title import auto_title
from generation_budget import GenerationBudget
from learning_service import enroll_question, start_review
from learning_store import LearningStore
from practice_model import question_messages, LearningQuestionDraft, validate_question, grade_answer
from practice_service import start_practice
from sources import Source
from structured_output import structured_protocols
from test_learning import learning_question
from test_practice import SOURCE, grade
from test_support import temporary_notes
from web_app import create_app


def from_visible(sources, visible, *args, **kwargs):
    draft = learning_question()
    chunk = visible[0]
    citation = draft.citations[0].model_copy(update={"source_id": chunk.source_id,
        "line_start": chunk.line_start, "line_end": chunk.line_end})
    return draft.model_copy(update={"citations": [citation], "learning": draft.learning.model_copy(update={
        "knowledge": draft.learning.knowledge.model_copy(update={"citations": [citation]})})})


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.app = create_app(self.root / "uploads")
        self.client = TestClient(self.app)
        self.store = self.app.state.practice_store
        self.library = self.app.state.library_store

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.root / "uploads", ignore_errors=True)
        self.temp.__exit__(None, None, None)

    def create_collection(self, name="网络", parent="root"):
        response = self.client.post("/api/collections", json={"name": name, "parent_id": parent})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def import_document(self, collection="root", text=SOURCE.text):
        response = self.client.post(f"/api/documents?collection_id={collection}", files=[
            ("files", ("note.md", text.encode(), "text/markdown"))])
        self.assertEqual(response.status_code, 201, response.text)
        return self.client.get(f"/api/documents/{response.json()[0]['id']}").json()

    def practice(self, collection="root"):
        return start_practice(self.store, [SOURCE], {SOURCE.source_id: "note"}, SOURCE.source_id,
                              None, None, "", collection_id=collection)

    def test_nested_collections_import_move_rename_and_restart(self):
        parent = self.create_collection()
        child = self.create_collection("TCP", parent)
        other = self.create_collection("操作系统")
        note = self.import_document(child)
        self.assertEqual([n["id"] for n in self.client.get(f"/api/documents?collection_id={parent}").json()], [note["id"]])
        self.assertEqual(self.client.get(f"/api/documents?collection_id={other}").json(), [])
        self.client.patch(f"/api/collections/{child}", json={"name": "传输层"})
        self.client.patch(f"/api/documents/{note['id']}/collection", json={"collection_id": other})
        self.assertEqual(self.client.get(f"/api/documents?collection_id={parent}").json(), [])
        restored = create_app(self.root / "uploads")
        self.assertEqual(restored.state.library_store.get(child)["name"], "传输层")
        self.assertEqual(restored.state.library_store.document_memberships()[note["id"]], other)
        self.assertEqual(self.client.get(f"/api/documents/{note['id']}").json()["source_id"], note["source_id"])

    def test_invalid_collection_and_empty_scope_have_no_generation_or_import(self):
        with patch("practice_service.generate_question") as model:
            empty = self.create_collection()
            self.assertEqual(self.client.post(f"/api/collections/{empty}/practice", json={}).status_code, 400)
            self.assertEqual(self.client.post("/api/collections/missing/practice", json={}).status_code, 404)
            self.assertEqual(self.client.post("/api/documents?collection_id=missing", files=[
                ("files", ("note.md", b"hello", "text/markdown"))]).status_code, 404)
            model.assert_not_called()
        self.assertFalse((self.root / "uploads").exists())
        self.assertEqual(self.client.post("/api/collections", json={"name": "  "}).status_code, 409)
        self.assertEqual(self.client.patch("/api/collections/root", json={"name": "x"}).status_code, 409)

    def test_whole_document_and_more_than_200_selected_lines_no_search(self):
        note = self.import_document(text="\n".join(f"Line {i}" for i in range(240)))
        payload = {"document_ids": [note["id"]], "focus_document_id": note["id"], "focus_source_id": note["source_id"]}
        with patch("practice_service.generate_question", side_effect=from_visible) as model, \
             patch("practice_service.KnowledgeTools") as tools:
            response = self.client.post("/api/practice", json=payload)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertTrue(model.call_args.kwargs["whole_scope"])
            self.assertEqual(model.call_args.args[1][0].line_end, 240)
            selected = self.client.post("/api/practice", json={**payload, "line_start": 1, "line_end": 220})
            self.assertEqual(selected.status_code, 201, selected.text)
            self.assertFalse(model.call_args.kwargs["whole_scope"])
            tools.assert_not_called()
            self.assertEqual(self.client.post("/api/practice", json={**payload, "line_start": 1}).status_code, 400)
            self.assertEqual(self.client.post("/api/practice", json={**payload, "focus_source_id": "stale"}).status_code, 409)

    def test_collection_practice_scope_excludes_sibling_and_stays_opt_in(self):
        parent = self.create_collection()
        child = self.create_collection("TCP", parent)
        first = self.import_document(parent)
        second = self.import_document(child)
        self.import_document(self.create_collection("Other"))
        with patch("practice_service.generate_question", side_effect=from_visible) as model:
            response = self.client.post(f"/api/collections/{parent}/practice", json={"response_language": "en"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual({s.source_id for s in model.call_args.args[0]}, {first["source_id"], second["source_id"]})
        self.assertEqual(response.json()["collection_id"], parent)
        self.assertEqual(model.call_args.kwargs["response_language"], "en")
        self.assertEqual(self.store.list_plan(), [])

    def test_long_document_request_budget_and_visible_citations(self):
        source = Source("long", self.root / "long.md", "\n\n".join(f"## Topic {i}\n" + "Useful evidence. " * 90 for i in range(35)))
        budget = GenerationBudget(context_tokens=18000, output_tokens=3000, margin_tokens=1500)
        with patch("practice_service.GenerationBudget.from_env", return_value=budget), \
             patch("practice_service.generate_question", side_effect=from_visible) as model:
            session = start_practice(self.store, [source], {"long": "doc"}, "long", None, None, "Topic 30")
        args, kwargs = model.call_args
        visible = args[1]
        self.assertLess(sum(len(c.text) for c in visible), len(source.text))
        messages = question_messages(*args, **kwargs)
        self.assertTrue(all(budget.fits(messages, **p) for p in structured_protocols(LearningQuestionDraft)))
        record = self.store.get(session["id"])
        self.assertEqual(record["snapshots"][0]["text"], source.text)
        self.assertEqual(record["visible_ranges"], [{"source_id": c.source_id, "line_start": c.line_start, "line_end": c.line_end} for c in visible])
        draft = record["questions"][0]["draft"]
        bad = draft.model_copy(update={"citations": [draft.citations[0].model_copy(update={"line_start": 1, "line_end": len(source.text.splitlines())})]})
        with self.assertRaises(ValueError):
            validate_question(bad, [source], visible)
        def assess(messages, schema):
            self.assertTrue(all(budget.fits(messages, **p) for p in structured_protocols(schema)))
            return grade(False)
        with patch("practice_model._request_structured", side_effect=assess):
            grade_answer(draft, "The idea matters", [source], visible, can_follow_up=True,
                         learning_context={"target": draft.learning.target.model_dump(), "recent_performance": []})

    def test_plan_collection_opt_in_retry_move_and_review_preserve_schedule(self):
        parent = self.create_collection()
        child = self.create_collection("TCP", parent)
        with patch("practice_service.generate_question", side_effect=from_visible):
            session = self.practice(child)
        qid = session["questions"][0]["id"]
        payload = {"question_id": qid, "collection_id": child}
        url = f"/api/practice/{session['id']}/enroll"
        item_id = self.client.post(url, json=payload).json()["item_id"]
        self.assertEqual(self.client.post(url, json=payload).json()["item_id"], item_id)
        before = self.store.item(item_id)
        self.assertEqual(len(self.store.list_plan(parent)), 1)
        self.assertEqual(self.store.list_plan(self.create_collection("Other")), [])
        self.library.move_review(item_id, "root")
        after = self.store.item(item_id)
        self.assertEqual(after["next_review_at"], before["next_review_at"])
        self.assertEqual(after["card_json"], before["card_json"])
        self.assertEqual(self.store.list_plan(parent), [])
        self.store.finish(session["id"])
        review = start_review(self.store, item_id, mode="original")
        self.assertEqual(review["collection_id"], "root")

    def test_existing_learning_database_migrates_without_replacing_records(self):
        with patch("practice_service.generate_question", side_effect=from_visible):
            session = self.practice()
        item_id = enroll_question(self.store, session["id"], session["questions"][0]["id"])
        before = self.store.item(item_id)
        with closing(self.store._connect()) as db, db:
            for table in ("practice_sessions", "review_items"):
                for action in ("insert", "update"):
                    db.execute(f"DROP TRIGGER valid_{table}_collection_{action}")
                db.execute(f"ALTER TABLE {table} DROP COLUMN collection_id")
            db.execute("DROP TABLE document_collections")
            db.execute("DROP TABLE collections")
        restored = LearningStore(self.store.path)
        self.assertEqual(restored.item(item_id)["collection_id"], "root")
        self.assertEqual(restored.item(item_id)["card_json"], before["card_json"])
        self.assertEqual(restored.get(session["id"])["questions"][0]["id"], session["questions"][0]["id"])
        with closing(restored._connect()) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE review_items SET collection_id='missing' WHERE id=?", (item_id,))

    def completed_chat(self):
        store = self.app.state.conversation_store
        identity = store.create()["id"]
        turn = store.begin(identity, "first", {"question": "SEQ=100 LEN=20"})
        store.finish(turn["id"], turn["attempt"], {"coverage_note": "TCP", "blocks": [{"text": "ACK=120"}], "steps": []}, {})
        return store, identity

    def test_auto_title_failure_retry_idempotency_and_manual_rename_wins(self):
        store, identity = self.completed_chat()
        with patch("conversation_title.generate_title", side_effect=RuntimeError("offline")):
            auto_title(store, identity, "en")
        self.assertEqual(store.get_conversation(identity)["title_state"], "failed")
        self.assertEqual(len(store.history(identity)), 2)
        with patch("conversation_title.generate_title", return_value="TCP sequence numbers") as model:
            auto_title(store, identity, "en")
            auto_title(store, identity, "zh")
            model.assert_called_once_with("SEQ=100 LEN=20", "TCP\n\nACK=120", "en")
        self.assertEqual(ConversationStore(store.path).get_conversation(identity)["title"], "TCP sequence numbers")
        store2, other = self.completed_chat()
        def user_renames(*args):
            store2.rename(other, "我自己的标题")
            return "模型标题"
        with patch("conversation_title.generate_title", side_effect=user_renames):
            auto_title(store2, other, "zh")
        self.assertEqual(store2.get_conversation(other)["title"], "我自己的标题")
        store3, last = self.completed_chat()
        old_attempt = store3.claim_title(last)
        self.assertIsNone(store3.claim_title(last))
        with store3.connection() as db:
            db.execute("UPDATE conversations SET title_started_at='2000-01-01' WHERE id=?", (last,))
        new_attempt = store3.claim_title(last)
        self.assertNotEqual(old_attempt, new_attempt)
        store3.publish_title(last, old_attempt, "过期标题")
        self.assertEqual(store3.get_conversation(last)["title"], "新聊天")
        store3.publish_title(last, new_attempt, "恢复后的标题")
        self.assertEqual(store3.get_conversation(last)["title"], "恢复后的标题")

    def test_empty_chat_not_named_and_api_names_only_after_answer_saved(self):
        identity = self.client.post("/api/conversations", json={}).json()["id"]
        store = self.app.state.conversation_store
        with patch("conversation_title.generate_title") as model:
            self.client.get(f"/api/conversations/{identity}/messages")
            model.assert_not_called()
        note = self.import_document()
        answer = NoteAnswer.model_validate({"coverage": "none", "coverage_note": "Chat", "blocks": [
            {"kind": "supplement", "text": "Hello", "citations": []}]})
        def title(*args):
            self.assertEqual(len(store.history(identity)), 2)
            return "A greeting"
        with patch("web_app.run_agent", return_value=AgentRun(answer, [], "model_finished", [])), \
             patch("conversation_title.generate_title", side_effect=title) as model:
            payload = {"request_id": "greeting", "document_ids": [note["id"]], "focus_document_id": note["id"],
                       "focus_source_id": note["source_id"], "question": "Hello", "response_language": "en"}
            response = self.client.post(f"/api/conversations/{identity}/turns", json=payload)
            self.assertEqual(response.json()["status"], "completed", response.text)
            self.client.post(f"/api/conversations/{identity}/turns", json=payload)
            model.assert_called_once()
        self.assertEqual(store.get_conversation(identity)["title"], "A greeting")


if __name__ == "__main__":
    unittest.main()
