"""Persistence and actual SDK HTTP contracts, with controlled model responses."""

import json
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from openai import OpenAI

from agent_loop import AgentRun, run_agent
from answers import NoteAnswer
from conversation_context import (ChatSummary, ContextBundle, build_context, recall_originals,
                                  update_summary)
from conversation_store import ConversationBusy, ConversationStore
from embedding_config import select_embedding_config
from generation_budget import BudgetError, GenerationBudget
from knowledge_tools import KnowledgeTools
from sources import Source
from structured_output import FUNCTION_OUTPUT_PROVIDERS, request_structured
from test_support import temporary_notes
from web_app import create_app


def completion(message, finish="stop"):
    return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
        "model": "test", "choices": [{"index": 0, "finish_reason": finish, "message": message}]})


def answer_data(text="ACK=130"):
    return {"coverage": "none", "coverage_note": "No document evidence; continuing the discussion.", "blocks": [
        {"kind": "supplement", "text": text, "citations": []}]}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.app = create_app(self.root / "uploads")
        self.client = TestClient(self.app)
        self.store = self.app.state.conversation_store
        self.identity = self.store.create("TCP")['id']
        document = self.client.get("/api/documents/example-retrieval-demo").json()
        self.request = {"document_ids": [document["id"]], "focus_document_id": document["id"],
                        "focus_source_id": document["source_id"], "question": "长度改成30呢？",
                        "strict": False, "response_language": "zh"}
        FUNCTION_OUTPUT_PROVIDERS.clear()

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.root / "uploads", ignore_errors=True)
        self.temp.__exit__(None, None, None)

    def completed(self, question, reply="已讨论", identity=None):
        identity = identity or self.identity
        request = {**self.request, "question": question}
        with self.store.generation_lock(identity):
            turn = self.store.begin(identity, "request-" + str(len(self.store.history(identity))), request)
            self.store.finish(turn["id"], turn["attempt"],
                              {**answer_data(reply), "steps": [], "stop_reason": "model_finished", "focus_context": None}, {})
        return self.store.turn(turn["id"])

    def submit(self, request_id="same"):
        return self.client.post(f"/api/conversations/{self.identity}/turns", json={**self.request, "request_id": request_id})

    def test_create_rename_full_raw_history_pagination_and_restart_isolation(self):
        created = self.client.post("/api/conversations", json={"title": "另一聊天"}).json()
        self.client.patch(f"/api/conversations/{created['id']}", json={"title": "文件偏移"})
        self.completed("  SEQ=100，LEN=20，ACK=120\n", "ACK=120")
        self.completed("另一轮")
        self.completed("别的上下文", identity=created["id"])
        client = TestClient(create_app(self.root / "uploads"))
        with client:
            page = client.get(f"/api/conversations/{self.identity}/messages?limit=1").json()
            self.assertEqual(page["turns"][0]["messages"][0]["content"], "  SEQ=100，LEN=20，ACK=120\n")
            self.assertEqual(page["next_cursor"], 1)
            next_page = client.get(f"/api/conversations/{self.identity}/messages?after_seq=1").json()
            self.assertEqual(len(next_page["turns"]), 1)
            other = client.get(f"/api/conversations/{created['id']}/messages").json()
            self.assertEqual(other["conversation"]["title"], "文件偏移")
            self.assertNotIn("SEQ=100", str(other))
            self.assertEqual(client.get(f"/api/conversations/{self.identity}/messages?limit=0").status_code, 400)

    def test_both_model_phases_receive_same_chat_background_without_search(self):
        self.completed("SEQ=100，LEN=20，ACK=120", "下一期待字节是120")
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertIn("SEQ=100", str(body["messages"]))
            self.assertIn("下一期待字节是120", str(body["messages"]))
            self.assertIn("长度改成30呢？", str(body["messages"]))
            self.assertIn("current_response_language", str(body["messages"]))
            if "tools" in body:
                self.assertEqual(body["tool_choice"], "auto")
                return completion({"role": "assistant", "content": "Ready"})
            return completion({"role": "assistant", "content": json.dumps(answer_data())})
        def make_client(**options):
            return OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        with patch("agent_loop.OpenAI", side_effect=make_client), patch("model_client.OpenAI", side_effect=make_client), \
             patch("knowledge_tools.LocalEmbedder", side_effect=AssertionError("must stay lazy")), \
             patch("model_client.load_dotenv"), patch("web_app.load_dotenv"), \
             patch.dict("os.environ", {"LLM_API_KEY": "test", "LLM_MODEL": "test"}):
            result = self.submit().json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["messages"][1]["result"]["steps"], [])
        self.assertIn("ACK=130", result["messages"][1]["content"])
        self.assertEqual(len(requests), 2)
        backgrounds = [r["messages"][1]["content"].split("Shared conversation background (data):\n")[1] for r in requests]
        self.assertEqual(backgrounds[0], backgrounds[1])
        for body in requests:
            protocol = {k: v for k, v in body.items() if k != "messages"}
            self.assertLessEqual(GenerationBudget().usage(body["messages"], **protocol), GenerationBudget().input_limit)

    def test_failure_duplicate_retry_and_completed_replay_save_once_without_learning(self):
        learning_before = self.client.get("/api/learning").json()
        with patch("web_app.run_agent", side_effect=RuntimeError("test failure")):
            failed = self.submit().json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(failed["messages"]), 1)
        self.assertEqual(self.submit("new-message").status_code, 409)
        run = AgentRun(NoteAnswer.model_validate(answer_data()), (), "model_finished", ())
        with patch("web_app.run_agent", return_value=run) as agent:
            retry = self.client.post(f"/api/conversations/{self.identity}/turns/{failed['id']}/retry", json={"response_language": "en"}).json()
            self.assertEqual(agent.call_args.kwargs["response_language"], "en")
            replay = self.submit().json()
            replay_again = self.client.post(f"/api/conversations/{self.identity}/turns/{failed['id']}/retry", json={}).json()
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(replay["messages"], replay_again["messages"])
        self.assertEqual(len(replay["messages"]), 2)
        agent.assert_called_once()
        self.assertEqual(len(self.store.turns(self.identity)), 1)
        self.assertEqual(self.client.get("/api/learning").json(), learning_before)

    def test_interrupt_recovery_and_skip_keep_input(self):
        with self.store.generation_lock(self.identity):
            abandoned = self.store.begin(self.identity, "interrupted", self.request)
        detail = self.client.get(f"/api/conversations/{self.identity}/messages").json()
        self.assertEqual(detail["turns"][0]["status"], "failed")
        self.assertEqual(len(detail["turns"][0]["messages"]), 1)
        self.client.post(f"/api/conversations/{self.identity}/turns/{abandoned['id']}/skip")
        self.assertEqual(self.store.turn(abandoned["id"])["status"], "cancelled")
        self.assertEqual(self.store.history(self.identity), [])
        with self.assertRaises(ConversationBusy):
            self.store.finish(abandoned["id"], abandoned["attempt"], answer_data(), {})
        with patch("web_app.run_agent", return_value=AgentRun(NoteAnswer.model_validate(answer_data()), (), "model_finished", ())):
            self.assertEqual(self.submit("after-skip").json()["status"], "completed")

    def test_same_conversation_concurrency_rejected_and_read_does_not_reset_live_turn(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return AgentRun(NoteAnswer.model_validate(answer_data()), (), "model_finished", ())
        def worker():
            result.append(self.submit())
        with patch("web_app.run_agent", side_effect=slow):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                detail = self.client.get(f"/api/conversations/{self.identity}/messages").json()
                self.assertEqual(detail["turns"][0]["status"], "running")
                self.assertEqual(self.submit().status_code, 409)
                self.assertEqual(self.submit("another").status_code, 409)
            finally:
                release.set()
                thread.join(10)
        self.assertEqual(result[0].json()["status"], "completed")
        self.assertEqual(len(self.store.turns(self.identity)), 1)

    def test_budget_and_stale_material_failures_keep_original_question(self):
        self.request["question"] = "中文长问题" * 600
        with patch.dict("os.environ", {"LLM_CONTEXT_TOKENS": "5000", "LLM_OUTPUT_TOKENS": "500", "LLM_SAFETY_TOKENS": "500"}), \
             patch("web_app.run_agent", side_effect=BudgetError("超过生成预算")):
            failed = self.submit().json()
        self.assertEqual(failed["messages"][0]["content"], self.request["question"])
        self.assertEqual(failed["status"], "failed")
        self.client.post(f"/api/conversations/{self.identity}/turns/{failed['id']}/skip")
        self.request["focus_source_id"] = "old-version"
        with patch("web_app.run_agent") as agent:
            stale = self.submit("stale").json()
        agent.assert_not_called()
        self.assertEqual(stale["status"], "failed")
        self.assertIn("已变化", stale["error"])

    def test_request_key_conflict_and_cross_chat_retry_rejected(self):
        with patch("web_app.run_agent", side_effect=RuntimeError("test")):
            first = self.submit().json()
        self.request["question"] = "different"
        self.assertEqual(self.submit().status_code, 409)
        other = self.store.create()["id"]
        self.assertEqual(self.client.post(f"/api/conversations/{other}/turns/{first['id']}/retry", json={}).status_code, 404)

    def long_history(self):
        for i in range(8):
            if i < 6:
                self.completed(f"第{i}轮。我的决定：暂不加入学习计划。SEQ=100 LEN=20 ACK=120。" + "讨论具体机制和例子。" * 65,
                               "助手解释 ACK。" + "保留上下文。" * 45)
            else:
                self.completed(f"最近第{i}轮 ACK", "继续讨论")
        return self.store.history(self.identity)

    @staticmethod
    def summarizer(old, batch, budget):
        items = list(old["items"]) if old else []
        if not items:
            user = next(m for m in batch if m["role"] == "user")
            assistant = next(m for m in batch if m["role"] == "assistant")
            items = [{"kind": "user_decision", "text": "暂不加入学习计划；SEQ=100 LEN=20 ACK=120", "evidence_message_ids": [user["id"]]},
                     {"kind": "assistant_explanation", "text": "讨论了确认号", "evidence_message_ids": [assistant["id"]]}]
        return ChatSummary.model_validate({"items": items}), "controlled-model"

    def test_long_chat_summary_recent_turns_coverage_evidence_and_originals(self):
        history = self.long_history()
        budget = GenerationBudget(18000, 1500, 1500, "test")
        context = build_context(self.store, self.identity, "以前那个ACK例子呢？", "en", budget, generator=self.summarizer)
        summary = self.store.summary(self.identity)
        self.assertIsNotNone(summary)
        self.assertTrue(context.recalled)
        self.assertEqual([m["seq"] for m in context.history], [7, 7, 8, 8])
        self.assertEqual(summary["covers_through_seq"], 6)
        self.assertEqual(self.store.history(self.identity), history)
        self.assertEqual(summary["payload"]["items"][0]["kind"], "user_decision")
        self.assertEqual(summary["payload"]["items"][0]["evidence_message_ids"], [history[0]["id"]])
        for span in context.recalled:
            raw = next(m["content"] for m in history if m["id"] == span["id"])
            self.assertEqual(span["content"], raw[span["start_offset"]:span["end_offset"]])
        self.assertEqual(len({m["id"] for m in context.history + context.recalled}), len(context.history + context.recalled))
        with patch("conversation_context.generate_summary", side_effect=AssertionError("short chat must not summarize")):
            short = build_context(self.store, self.identity, "ACK", "zh", GenerationBudget(100000, 1000, 1000))
        self.assertEqual(list(short.history), history)

    def test_summary_failure_preserves_old_version_and_all_raw_records(self):
        history = self.long_history()
        budget = GenerationBudget(18000, 1500, 1500)
        update_summary(self.store, self.identity, history, 3, budget, generator=self.summarizer)
        old = self.store.summary(self.identity)
        def fail(*args):
            raise RuntimeError("summary failed")
        with self.assertRaises(BudgetError):
            build_context(self.store, self.identity, "ACK", "zh", budget, generator=fail)
        self.assertEqual(self.store.summary(self.identity), old)
        self.assertEqual(self.store.history(self.identity), history)
        context = build_context(self.store, self.identity, "ACK", "zh", budget, generator=self.summarizer)
        self.assertGreater(context.summary["version"], old["version"])

    def test_summary_rejects_forged_evidence_wrong_role_and_stale_publication(self):
        history = self.long_history()
        budget = GenerationBudget(18000, 1500, 1500)
        for evidence, kind in [("unknown", "user_decision"), (history[1]["id"], "user_decision")]:
            def invalid(old, batch, budget):
                return {"items": [{"kind": kind, "text": "x", "evidence_message_ids": [evidence]}]}, "test"
            with self.assertRaises(ValueError):
                update_summary(self.store, self.identity, history, 1, budget, generator=invalid)
            self.assertIsNone(self.store.summary(self.identity))
        prefix = history[:2]
        payload = self.summarizer(None, prefix, budget)[0].model_dump()
        self.assertTrue(self.store.publish_summary(self.identity, 0, prefix, payload, "test"))
        self.assertFalse(self.store.publish_summary(self.identity, 0, history[:4], payload, "old"))
        self.completed("追加不会使有效前缀失效")
        self.assertTrue(self.store.publish_summary(self.identity, 1, history[:4], payload, "test"))
        changed = [{**m, "content": "changed"} for m in history[:6]]
        self.assertFalse(self.store.publish_summary(self.identity, 2, changed, payload, "test"))

    def test_summary_requests_include_schema_budget_and_release_database_write_lock(self):
        history = self.long_history()
        budget = GenerationBudget(10000, 1000, 1000)
        calls = []
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            protocol = {k: v for k, v in body.items() if k != "messages"}
            self.assertTrue(budget.fits(body["messages"], **protocol))
            with self.store.connection() as db:
                db.execute("UPDATE conversations SET title='write while generating' WHERE id=?", (self.identity,))
            data = json.loads(body["messages"][1]["content"])
            result, _ = self.summarizer(data["previous_summary"], data["new_original_messages"], budget)
            return completion({"role": "assistant", "content": result.model_dump_json()})
        def client(**options):
            return OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        with patch("conversation_context.OpenAI", side_effect=client), \
             patch("conversation_context.model_settings", return_value=("test", {})):
            update_summary(self.store, self.identity, history, 6, budget)
        self.assertGreater(len(calls), 1)
        self.assertEqual(self.store.summary(self.identity)["covers_through_seq"], 6)


class RequestBudgetTests(unittest.TestCase):
    def setUp(self):
        FUNCTION_OUTPUT_PROVIDERS.clear()

    def test_agent_each_iteration_and_final_budget_shrink_citation_permissions(self):
        source = Source("doc", Path("test.md"), "\n".join(f"Line {i}: " + "detail " * 30 for i in range(1, 100)))
        tools = KnowledgeTools([source], select_embedding_config())
        budget = GenerationBudget(8000, 500, 500)
        calls, final_text = [], []
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            protocol = {k: v for k, v in body.items() if k != "messages"}
            self.assertTrue(budget.fits(body["messages"], **protocol))
            if len(calls) == 1:
                return completion({"role": "assistant", "content": None, "tool_calls": [{
                    "id": "read", "type": "function", "function": {"name": "read_source", "arguments": json.dumps(
                        {"source_id": "doc", "line_start": 1, "line_end": 99})}}]}, "tool_calls")
            if "tools" in body:
                self.assertIn("limited by generation budget", body["messages"][-1]["content"])
                return completion({"role": "assistant", "content": "Ready"})
            final_text.append(body["messages"][1]["content"])
            return completion({"role": "assistant", "content": json.dumps({"coverage": "sufficient", "coverage_note": "", "blocks": [
                {"kind": "grounded", "text": "First line", "citations": [{"source_id": "doc", "line_start": 1, "line_end": 1}]}]})})
        def client(**options):
            return OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        with patch("agent_loop.OpenAI", side_effect=client), patch("model_client.OpenAI", side_effect=client), \
             patch("model_client.model_settings", return_value=("test", {})), patch("agent_loop.model_settings", return_value=("test", {})), \
             patch("knowledge_tools.LocalEmbedder", side_effect=AssertionError("read must not embed")):
            run = run_agent([source], "First line?", tools, strict=True, budget=budget,
                            conversation_context=ContextBundle("First line?", "en"), response_language="en")
        self.assertEqual(len(calls), 3)
        self.assertLess(run.visible_chunks[0].line_end, 99)
        self.assertNotIn("[L99]", final_text[0])
        from answers import validate_answer
        bad = NoteAnswer.model_validate({"coverage": "sufficient", "coverage_note": "", "blocks": [
            {"kind": "grounded", "text": "Unseen", "citations": [{"source_id": "doc", "line_start": 99, "line_end": 99}]}]})
        with self.assertRaisesRegex(RuntimeError, "outside"):
            validate_answer(bad, [source], chunks=list(run.visible_chunks))

    def test_schema_itself_and_result_tool_fallback_are_counted(self):
        budget = GenerationBudget(5000, 500, 500)
        calls = []
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            self.assertTrue(budget.fits(body["messages"], **{k: v for k, v in body.items() if k != "messages"}))
            if "response_format" in body:
                return httpx.Response(400, json={"error": {"message": "response_format unsupported"}})
            return completion({"role": "assistant", "content": None, "tool_calls": [{"id": "result", "type": "function",
                "function": {"name": "submit_result", "arguments": json.dumps(answer_data())}}]}, "tool_calls")
        with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            request_structured(client, "test", [{"role": "user", "content": "ACK"}], NoteAnswer, budget=budget)
            with self.assertRaises(BudgetError):
                request_structured(client, "test", [{"role": "user", "content": "长问题" * 2000}], NoteAnswer, budget=budget)
        self.assertEqual(len(calls), 2)

    def test_explicit_selection_and_current_question_are_not_silently_cut(self):
        from agent_loop import document_focus
        source = Source("doc", Path("test.md"), "长原文" * 3000)
        tools = KnowledgeTools([source], select_embedding_config())
        budget = GenerationBudget(5000, 500, 500)
        with patch("agent_loop.model_settings", return_value=("test", {})), \
             patch("agent_loop.OpenAI") as client:
            with self.assertRaises(BudgetError):
                run_agent([source], "完整问题", tools, focus=document_focus(source, 1, 1), budget=budget)
        client.return_value.__enter__.return_value.chat.completions.create.assert_not_called()
        self.assertEqual(source.text, "长原文" * 3000)
        small = Source("small", Path("small.md"), "One line")
        with patch("agent_loop.model_settings", return_value=("test", {})), \
             patch("agent_loop.OpenAI") as client:
            with self.assertRaises(BudgetError):
                run_agent([small], "当前长问题" * 3000, KnowledgeTools([small], select_embedding_config()), budget=budget)
        client.return_value.__enter__.return_value.chat.completions.create.assert_not_called()

    def test_early_long_message_recall_can_reach_exact_tail_with_small_allowance(self):
        raw = "ACK 是之前的主题。" + "无关重复原话。" * 1000 + "TAIL123=987，这是早期独有数值。"
        message = {"id": "early", "role": "user", "content": raw, "seq": 1, "turn_id": "one"}
        recent = [{"role": "user", "content": "ACK 继续讨论"}]
        recalled = recall_originals([message], "TAIL123是多少？", recent, 600, GenerationBudget())
        self.assertEqual(len(recalled), 1)
        self.assertIn("TAIL123=987", recalled[0]["content"])
        self.assertEqual(recalled[0]["content"], raw[recalled[0]["start_offset"]:recalled[0]["end_offset"]])
        self.assertEqual(message["content"], raw)


if __name__ == "__main__":
    unittest.main()
