"""Controlled transport and race tests; mock answers are not semantic evidence."""

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from openai import OpenAI

from agent_loop import run_agent
from answers import NoteAnswer, SourceOnlyAnswer
from conversation_context import build_context, update_summary
from conversation_memory import (ConversationMemory, MemoryReference, SourceMemoryAnswer,
                                 fit_memory, terms)
from conversation_store import ConversationBusy, ConversationStore
from conversation_title import auto_title
from embedding_config import select_embedding_config
from generation_budget import GenerationBudget
from knowledge_tools import KnowledgeTools
from sources import Source
from structured_output import FUNCTION_OUTPUT_PROVIDERS, request_structured
from test_conversations import answer_data, completion
from test_support import temporary_notes
from web_app import create_app


class FakeEmbedder:
    def __init__(self):
        self.computed = 0

    def cache_profile(self):
        return {"model": "controlled", "prefix": "passage", "version": 1}

    def embed_chunks(self, chunks):
        self.computed += len(chunks)
        return [[1., 0.] if "ACK" in c.text else [0., 1.] for c in chunks]

    def embed_query(self, query):
        return [1., 0.] if "ACK" in query or "下一期待" in query else [-1., 0.]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.app = create_app(self.root / "uploads")
        self.client = TestClient(self.app)
        self.store = self.app.state.conversation_store
        self.old = self.store.create("旧的 TCP 讨论")["id"]
        self.current = self.store.create("新的聊天")["id"]
        self.embedder = FakeEmbedder()
        self.source = Source("allowed", Path("allowed.md"), "# Current document\nAllowed fact.")
        FUNCTION_OUTPUT_PROVIDERS.clear()

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.root / "uploads", ignore_errors=True)
        self.temp.__exit__(None, None, None)

    def completed(self, identity, question="SEQ=100 LEN=20 ACK=120", answer="ACK=120", dependencies=()):
        turn = self.store.begin(identity, str(len(self.store.history(identity))), {"question": question})
        self.store.finish(turn["id"], turn["attempt"], answer_data(answer), {"memory_dependencies": list(dependencies)})
        return self.store.turn(turn["id"])

    def dep(self, identity):
        return {"conversation_id": identity, "revision": self.store.get_conversation(identity)["memory_revision"]}

    def memory(self):
        return ConversationMemory(self.store, self.current, [self.dep(self.current)], embedder=self.embedder)

    def test_archived_old_history_eligible_and_restart_restore_list(self):
        turn = self.completed(self.old)
        with self.store.connection() as db:
            db.execute("UPDATE messages SET created_at='1990-01-01' WHERE turn_id=?", (turn["id"],))
        self.assertEqual(self.client.post(f"/api/conversations/{self.old}/archive").status_code, 200)
        self.assertNotIn(self.old, [c["id"] for c in self.client.get("/api/conversations").json()])
        self.assertEqual([c["id"] for c in self.client.get("/api/conversations?archived=true").json()], [self.old])
        spans = self.memory().search("ACK", 3)
        self.assertTrue(any(s["conversation_id"] == self.old for s in spans))
        restarted = ConversationStore(self.store.path)
        self.assertEqual(restarted.get_conversation(self.old)["archived"], 1)
        self.client.post(f"/api/conversations/{self.old}/restore")
        self.assertEqual(len(self.store.list()), 2)

    def test_numeric_chinese_terms_complete_segments_and_cached_vectors(self):
        raw = "前言。" * 900 + "TAIL123=987 ACK=120。"
        turn = self.completed(self.old, raw)
        memory = self.memory()
        spans = memory.search("TAIL123", 2)
        chosen = next(s for s in spans if "TAIL123" in s["content"])
        self.assertEqual(chosen["content"], raw[chosen["start_offset"]:chosen["end_offset"]])
        first = self.embedder.computed
        self.memory().search("下一期待的字节 ACK", 2)
        self.assertEqual(self.embedder.computed, first)
        self.assertIn("期待", terms("下一期待的字节"))
        self.assertIn("987", terms("TAIL123=987"))
        self.assertEqual(self.store.turn(turn["id"])["messages"][0]["content"], raw)

    def test_keyword_fallback_and_zero_unrelated_results(self):
        self.completed(self.old)
        with patch("conversation_memory.LocalEmbedder", side_effect=RuntimeError("unavailable")):
            memory = ConversationMemory(self.store, self.current)
            self.assertTrue(memory.search("ACK", 2))
            self.assertIn("关键词", memory.diagnostics["warning"])
            self.assertEqual(memory.search("烘焙馅饼食谱", 2), [])

    def test_semantic_recall_without_literal_match_uses_injected_model(self):
        self.completed(self.old)
        spans = self.memory().search("下一期待的字节", 2)
        self.assertTrue(spans)  # Controlled vectors, not a real semantic result.

    def test_read_tool_bounds_neighbors_and_argument_validation(self):
        turn = self.completed(self.old)
        message = turn["messages"][0]
        memory = self.memory()
        output, spans = memory.execute("read_conversation", {"message_id": message["id"], "start_offset": 0, "end_offset": 7})
        self.assertEqual(spans[0]["content"], "SEQ=100")
        self.assertEqual(len(spans), 2)
        self.assertNotIn("source_id", output["history_spans"][0])
        for name, args in [("search_conversations", {"query": "ACK", "top_k": True}),
                           ("search_conversations", {"query": "ACK", "path": "private"}),
                           ("read_conversation", {"message_id": message["id"], "start_offset": 0, "end_offset": 9999})]:
            with self.assertRaises(ValueError):
                memory.execute(name, args)

    def test_references_exact_visible_range_role_and_deleted_reference_endpoint(self):
        turn = self.completed(self.old)
        memory = self.memory()
        span = memory.span(memory.message(turn["messages"][0]["id"]), 2, 12)
        memory.remember([span])
        ref = MemoryReference(block_index=0, conversation_id=self.old, message_id=span["message_id"],
                              message_revision=1, start_offset=2, end_offset=8)
        enriched = memory.validate_references([ref], 1)
        self.assertEqual(enriched[0]["excerpt"], span["content"][:6])
        self.assertEqual(enriched[0]["role"], "user")
        for changes in ({"end_offset": 13}, {"message_revision": 2}, {"block_index": 1}, {"conversation_id": self.current}):
            with self.assertRaises(ValueError):
                memory.validate_references([MemoryReference.model_validate({**ref.model_dump(), **changes})], 1)
        url = f"/api/history/{span['message_id']}?start_offset=2&end_offset=8"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.delete(f"/api/conversations/{self.old}")
        self.assertEqual(self.client.get(url).status_code, 410)
        with self.assertRaises(ConversationBusy):
            memory.guard()

    def test_delete_cleans_original_summary_cache_and_is_idempotent(self):
        turn = self.completed(self.old)
        prefix = self.store.history(self.old)
        self.store.publish_summary(self.old, 0, prefix, {"items": []}, "test")
        self.memory().search("ACK", 2)
        learning_before = self.client.get("/api/learning").json()
        self.assertEqual(self.client.delete(f"/api/conversations/{self.old}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/conversations/{self.old}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/conversations/{self.old}/messages").status_code, 404)
        self.assertEqual(self.client.get("/api/learning").json(), learning_before)
        with self.store.connection() as db:
            for table in ("messages", "summaries", "memory_vectors", "memory_dependencies"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertFalse(self.store.publish_summary(self.old, 0, prefix, {"items": []}, "late"))
        with self.assertRaises(ConversationBusy):
            self.store.finish(turn["id"], turn["attempt"], answer_data(), {})

    def test_indirect_derived_replies_summaries_vectors_revoked_raw_user_preserved(self):
        self.completed(self.old)
        b = self.completed(self.current, "沿用旧决定", "COPIED_ACK_120", [self.dep(self.old)])
        c_id = self.store.create("第三聊天")["id"]
        c = self.completed(c_id, "第三轮问题", "COPIED_AGAIN", [self.dep(self.current)])
        # A follow-up without another history tool inherits the prior provenance.
        follow = self.completed(self.current, "接着解释", "INHERITED_COPY")
        self.store.publish_summary(self.current, 0, self.store.history(self.current), {"items": []}, "test")
        self.memory().search("COPIED_ACK_120", 3)
        old_revision = self.dep(self.current)
        self.store.delete(self.old)
        for turn in (b, c, follow):
            live = self.store.turn(turn["id"])
            self.assertFalse(live["memory_valid"])
            self.assertIsNone(live["context"])
            self.assertEqual(len(live["messages"]), 2)
        self.assertIsNone(self.store.summary(self.current))
        self.assertEqual([m["role"] for m in self.store.history(self.current)], ["user", "user"])
        self.assertFalse(any(s["role"] == "assistant" for s in self.memory().search("COPIED", 5)))
        with self.assertRaises(ConversationBusy):
            self.store.assert_dependencies([old_revision])

    def test_inflight_delete_cross_chat_result_rejected_and_input_retryable(self):
        self.completed(self.old)
        memory = self.memory()
        _, spans = memory.execute("search_conversations", {"query": "ACK"})
        memory.remember(spans)
        turn = self.store.begin(self.current, "pending", {"question": "长度改成30呢？"})
        self.store.delete(self.old)
        with self.assertRaises(ConversationBusy):
            self.store.finish(turn["id"], turn["attempt"], answer_data(), {"memory_dependencies": memory.dependency_list()})
        self.store.fail(turn["id"], turn["attempt"], "来源删除，请重试")
        retry = self.store.begin(self.current, "pending", {"question": "长度改成30呢？"})
        self.store.finish(retry["id"], retry["attempt"], answer_data("没有旧来源"), {"memory_dependencies": [self.dep(self.current)]})
        self.assertEqual(len(self.store.turns(self.current)), 1)
        self.assertEqual(len(self.store.turn(turn["id"])["messages"]), 2)

    def test_delete_active_generation_does_not_wait_for_os_lock_and_cannot_resurrect(self):
        turn = self.store.begin(self.current, "running", {"question": "输入"})
        with self.store.generation_lock(self.current):
            self.assertEqual(self.client.delete(f"/api/conversations/{self.current}").status_code, 200)
            with self.assertRaises(ConversationBusy):
                self.store.finish(turn["id"], turn["attempt"], answer_data(), {})
        with self.assertRaises(KeyError):
            self.store.begin(self.current, "running", {"question": "输入"})

    def test_embedding_deletion_race_does_not_restore_vectors(self):
        self.completed(self.old)
        memory = self.memory()
        def encode(chunks):
            self.store.delete(self.old)
            return [[1., 0.] for _ in chunks]
        with patch.object(self.embedder, "embed_chunks", side_effect=encode):
            memory.search("ACK", 2)
        # Search candidates are checked again before becoming model-visible.
        with self.assertRaises(ConversationBusy):
            memory.remember([{"conversation_id": self.old, "conversation_revision": 1}])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0], 0)

    def test_late_title_after_delete_is_discarded(self):
        chat = self.store.create()["id"]
        self.completed(chat)
        def title(*args):
            self.store.delete(chat)
            return "迟到的标题"
        with patch("conversation_title.generate_title", side_effect=title):
            auto_title(self.store, chat, "zh")
        with self.assertRaises(KeyError):
            self.store.get_conversation(chat)

    def test_summary_generation_delete_and_revision_change_reject_late_publish(self):
        self.completed(self.old)
        self.completed(self.current, "本次问题", "关联解释", [self.dep(self.old)])
        history = self.store.history(self.current)
        revision = self.dep(self.current)["revision"]
        def summary(old, batch, budget):
            self.store.delete(self.old)
            return {"items": []}, "controlled"
        self.assertIsNone(update_summary(self.store, self.current, history, 1, GenerationBudget(), generator=summary))
        self.assertIsNone(self.store.summary(self.current))
        self.assertFalse(self.store.publish_summary(self.current, 0, self.store.history(self.current),
                                                   {"items": []}, "late", expected_revision=revision))

    def test_web_persists_history_refs_retry_replay_and_revokes_saved_result(self):
        self.completed(self.old)
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            is_agent = "tools" in body and body["tools"][0]["function"]["name"] != "submit_result"
            if is_agent:
                if not any(m["role"] == "tool" for m in body["messages"]):
                    return completion({"role": "assistant", "content": None, "tool_calls": [{"id": "h", "type": "function",
                        "function": {"name": "search_conversations", "arguments": '{"query":"ACK"}'}}]}, "tool_calls")
                return completion({"role": "assistant", "content": "ready"})
            background = json.loads(body["messages"][1]["content"].split("Shared conversation background (data):\n")[1])
            span = next(s for s in background["associated_local_discussions"] if s["role"] == "user")
            ref = {"block_index": 0, **{k:span[k] for k in ("conversation_id", "message_id", "message_revision", "start_offset", "end_offset")}}
            return completion({"role": "assistant", "content": json.dumps({**answer_data(), "history_references": [ref]})})
        def client(**kwargs):
            return OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        document = self.client.get("/api/documents/example-retrieval-demo").json()
        payload = {"document_ids": [document["id"]], "focus_document_id": document["id"],
                   "focus_source_id": document["source_id"], "question": "以前的 ACK 例子呢？", "request_id": "same"}
        with patch("agent_loop.OpenAI", side_effect=client), patch("model_client.OpenAI", side_effect=client), \
             patch("agent_loop.model_settings", return_value=("test", {})), patch("model_client.model_settings", return_value=("test", {})), \
             patch("conversation_memory.LocalEmbedder", return_value=self.embedder):
            url = f"/api/conversations/{self.current}/turns"
            first = self.client.post(url, json=payload).json()
            second = self.client.post(url, json=payload).json()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["messages"], second["messages"])
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(first["messages"][1]["result"]["history_references"]), 1)
        self.store.delete(self.old)
        invalid = self.client.get(f"/api/conversations/{self.current}/messages").json()["turns"][0]
        self.assertFalse(invalid["memory_valid"])
        self.assertNotIn("history_references", invalid["messages"][1]["result"])

    def test_history_final_request_budget_shrinks_actual_permissions(self):
        self.completed(self.old, "SEQ=100 ACK=120 " + "详细解释。" * 1500)
        memory, requests = self.memory(), []
        memory.remember(memory.search("ACK", 5))
        original_width = sum(len(s["content"]) for s in memory.visible)
        budget = GenerationBudget(10000, 500, 500)
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertTrue(budget.fits(body["messages"], **{k:v for k,v in body.items() if k != "messages"}))
            if "tools" in body:
                return completion({"role": "assistant", "content": "ready"})
            return completion({"role": "assistant", "content": json.dumps({**answer_data(), "history_references": []})})
        run = self.scripted(respond, memory, budget=budget)
        self.assertLessEqual(sum(len(s["content"]) for s in memory.visible), original_width)
        self.assertEqual(run.context_snapshot["history_visible_ranges"], [{k:s[k] for k in (
            "conversation_id", "message_id", "message_revision", "start_offset", "end_offset")} for s in memory.visible])

    def scripted(self, handler, memory, *, strict=False, budget=None):
        def client(**options):
            return OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        question = "以前的 ACK 例子，长度改成30呢？"
        context = build_context(self.store, self.current, question, "en", budget or GenerationBudget())
        with patch("agent_loop.OpenAI", side_effect=client), patch("model_client.OpenAI", side_effect=client), \
             patch("agent_loop.model_settings", return_value=("test", {})), patch("model_client.model_settings", return_value=("test", {})):
            return run_agent([self.source], question, KnowledgeTools([self.source], select_embedding_config()),
                             conversation_context=context, response_language="en", memory_tools=memory,
                             strict=strict, budget=budget)

    def test_history_search_both_phases_same_background_ack_and_no_document_lookup(self):
        self.completed(self.old)
        requests, memory = [], self.memory()
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertTrue(GenerationBudget().fits(body["messages"], **{k:v for k,v in body.items() if k != "messages"}))
            if len(requests) == 1:
                return completion({"role": "assistant", "content": None, "tool_calls": [{"id": "h", "type": "function",
                    "function": {"name": "search_conversations", "arguments": '{"query":"ACK"}'}}]}, "tool_calls")
            self.assertIn("SEQ=100", str(body["messages"]))
            self.assertIn("current_response_language", str(body["messages"]))
            if "tools" in body:
                return completion({"role": "assistant", "content": "ready"})
            span = next(s for s in memory.visible if s["role"] == "user")
            ref = {"block_index": 0, **{k:span[k] for k in ("conversation_id", "message_id", "message_revision", "start_offset", "end_offset")}}
            return completion({"role": "assistant", "content": json.dumps({**answer_data(), "history_references": [ref]})})
        with patch("knowledge_tools.LocalEmbedder", side_effect=AssertionError("history must not embed documents")):
            run = self.scripted(respond, memory)
        self.assertEqual([s.name for s in run.steps], ["search_conversations"])
        self.assertEqual(len(requests), 3)
        self.assertEqual(run.visible_chunks, ())
        self.assertEqual(run.history_references[0]["role"], "user")
        self.assertIn(self.dep(self.old), run.context_snapshot["memory_dependencies"])

    def test_general_chat_zero_tools_loads_no_history_embedding(self):
        self.completed(self.old)
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if "tools" in body:
                return completion({"role": "assistant", "content": "ready"})
            self.assertNotIn("SEQ=100", str(body["messages"]))
            return completion({"role": "assistant", "content": json.dumps(answer_data("Hello"))})
        with patch("conversation_memory.LocalEmbedder", side_effect=AssertionError("no forced memory lookup")), \
             patch("knowledge_tools.LocalEmbedder", side_effect=AssertionError("no forced RAG")):
            run = self.scripted(respond, self.memory())
        self.assertEqual(run.steps, ())
        self.assertEqual(run.history_references, ())
        self.assertEqual(len(requests), 2)

    def test_strict_schema_preserves_document_contract_and_history_cannot_ground_answer(self):
        self.completed(self.old)
        memory, requests = self.memory(), []
        _, spans = memory.execute("search_conversations", {"query": "ACK"})
        memory.remember(spans)
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if "tools" in body:
                return completion({"role": "assistant", "content": "ready"})
            schema = body["response_format"]["json_schema"]["schema"]
            self.assertEqual(schema["$defs"]["SourceAnswerBlock"]["properties"]["kind"]["enum"], ["grounded", "inference"])
            return completion({"role": "assistant", "content": json.dumps({"coverage": "none", "coverage_note": "No current document evidence", "blocks": [], "history_references": []})})
        run = self.scripted(respond, memory, strict=True)
        self.assertEqual(run.answer.coverage, "none")
        self.assertEqual(SourceOnlyAnswer.model_json_schema()["$defs"]["SourceAnswerBlock"], SourceMemoryAnswer.model_json_schema()["$defs"]["SourceAnswerBlock"])

    def test_budget_crop_revokes_history_reference_range(self):
        turn = self.completed(self.old, "ACK=" + "数值和解释。" * 500)
        memory = self.memory()
        original = memory.message(turn["messages"][0]["id"])
        span = memory.span(original, 0, len(original["content"]))
        cropped = fit_memory([span], lambda candidates: GenerationBudget.estimate(candidates) <= 900)
        self.assertLess(cropped[0]["end_offset"], span["end_offset"])
        memory.remember(cropped)
        with self.assertRaises(ValueError):
            memory.validate_references([MemoryReference(block_index=0, conversation_id=self.old,
                message_id=span["message_id"], message_revision=1, start_offset=0, end_offset=span["end_offset"])], 1)
        self.assertEqual(original["content"], self.store.turn(turn["id"])["messages"][0]["content"])

    def test_compat_fallback_checks_revocation_before_second_http_call(self):
        self.completed(self.old)
        memory = self.memory()
        memory.remember(memory.search("ACK", 2))
        calls = []
        def respond(request):
            calls.append(request)
            self.store.delete(self.old)
            return httpx.Response(400, json={"error": {"message": "response_format unsupported"}})
        with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with self.assertRaises(ConversationBusy):
                request_structured(client, "test", [{"role": "user", "content": "ACK"}], NoteAnswer, before_request=memory.guard)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
