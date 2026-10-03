import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from agent_loop import focus_on_lines, run_agent
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from sources import Source
from structured_output import FUNCTION_OUTPUT_PROVIDERS


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        FUNCTION_OUTPUT_PROVIDERS.clear()
        self.source = Source("doc-a", Path("notes.md"), "# Alpha\n\nVisible fact.\n\n# Secret\n\nHIDDEN")
        self.sources = [self.source]
        embedder = Mock()
        embedder.config = select_embedding_config()
        embedder.max_tokens = 512
        embedder.embed_query.return_value = [1.0, 0.0]
        embedder.embed_chunks.return_value = [[1.0, 0.0], [0.0, 1.0]]
        self.tools = KnowledgeTools(self.sources, embedder.config, min_chars=0, no_cache=True, embedder=embedder)

    @staticmethod
    def completion(message, finish_reason="stop"):
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{
                "index": 0, "finish_reason": finish_reason, "message": message,
            }],
        })

    @staticmethod
    def call(call_id, name, arguments):
        return {
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def run_script(self, handler, **kwargs):
        transport = httpx.MockTransport(handler)

        def make_client(**options):
            return OpenAI(api_key="test", http_client=httpx.Client(transport=transport))

        with patch("agent_loop.OpenAI", side_effect=make_client), \
             patch("model_client.OpenAI", side_effect=make_client), \
             patch("model_client.load_dotenv"), \
             patch.dict("os.environ", {"LLM_API_KEY": "test", "LLM_MODEL": "test-model"}):
            return run_agent(self.sources, "Explain Alpha", self.tools, **kwargs)

    def test_search_then_read_then_grounded_answer_uses_only_visible_lines(self):
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                self.assertIn("tools", body)
                self.assertNotIn("Visible fact.", str(body["messages"]))
                self.assertNotIn("HIDDEN", str(body["messages"]))
                return self.completion({
                    "role": "assistant", "content": None,
                    "tool_calls": [self.call("one", "search_notes", {"query": "Alpha", "top_k": 1})],
                }, "tool_calls")
            if len(requests) == 2:
                self.assertEqual(body["messages"][-1]["tool_call_id"], "one")
                self.assertIn("Visible fact.", body["messages"][-1]["content"])
                return self.completion({
                    "role": "assistant", "content": None,
                    "tool_calls": [self.call("two", "read_source", {
                        "source_id": "doc-a", "line_start": 2, "line_end": 3,
                    })],
                }, "tool_calls")
            if len(requests) == 3:
                self.assertEqual(body["messages"][-1]["tool_call_id"], "two")
                return self.completion({"role": "assistant", "content": "READY"})
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertIn("[L3] Visible fact.", body["messages"][1]["content"])
            self.assertNotIn("HIDDEN", body["messages"][1]["content"])
            return self.completion({"role": "assistant", "content": json.dumps({
                "coverage": "sufficient", "coverage_note": "", "blocks": [{
                    "kind": "grounded", "text": "Alpha says a visible fact.",
                    "citations": [{"source_id": "doc-a", "line_start": 3, "line_end": 3}],
                }],
            })})

        result = self.run_script(respond, strict=True)
        self.assertEqual(len(requests), 4)
        self.assertEqual([step.name for step in result.steps], ["search_notes", "read_source"])
        self.assertEqual(result.stop_reason, "model_finished")
        self.assertEqual(result.answer.blocks[0].citations[0].line_start, 3)

    def test_tool_error_is_returned_to_model_then_retry_succeeds(self):
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return self.completion({"role": "assistant", "content": None, "tool_calls": [
                    self.call("bad", "read_source", {"source_id": "missing", "line_start": 1, "line_end": 1}),
                ]}, "tool_calls")
            if len(requests) == 2:
                self.assertIn("error", body["messages"][-1]["content"])
                return self.completion({"role": "assistant", "content": None, "tool_calls": [
                    self.call("good", "read_source", {"source_id": "doc-a", "line_start": 3, "line_end": 3}),
                ]}, "tool_calls")
            if len(requests) == 3:
                return self.completion({"role": "assistant", "content": "READY"})
            return self.completion({"role": "assistant", "content": json.dumps({
                "coverage": "sufficient", "coverage_note": "", "blocks": [{
                    "kind": "grounded", "text": "Visible fact.", "citations": [
                        {"source_id": "doc-a", "line_start": 3, "line_end": 3},
                    ],
                }],
            })})

        result = self.run_script(respond)
        self.assertIsNotNone(result.steps[0].error)
        self.assertIsNone(result.steps[1].error)
        self.assertEqual(len(result.visible_chunks), 1)

    def test_budget_stops_tools_and_final_citation_cannot_use_unseen_lines(self):
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if "tools" in body:
                return self.completion({"role": "assistant", "content": None, "tool_calls": [
                    self.call("one", "read_source", {"source_id": "doc-a", "line_start": 3, "line_end": 3}),
                ]}, "tool_calls")
            return self.completion({"role": "assistant", "content": json.dumps({
                "coverage": "sufficient", "coverage_note": "", "blocks": [{
                    "kind": "grounded", "text": "Hidden fact.", "citations": [
                        {"source_id": "doc-a", "line_start": 7, "line_end": 7},
                    ],
                }],
            })})

        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.run_script(respond, max_tool_calls=1)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("HIDDEN", requests[1]["messages"][1]["content"])

    def test_no_tool_call_allows_strict_insufficient_answer(self):
        def respond(request):
            body = json.loads(request.content)
            if "tools" in body:
                return self.completion({"role": "assistant", "content": "READY"})
            return self.completion({"role": "assistant", "content": json.dumps({
                "coverage": "none", "coverage_note": "No source lines were read.", "blocks": [],
            })})

        result = self.run_script(respond, strict=True)
        self.assertEqual(result.visible_chunks, ())
        self.assertEqual(result.answer.coverage, "none")
        self.tools.embedder.embed_query.assert_not_called()
        self.tools.embedder.embed_chunks.assert_not_called()

    def test_selected_lines_are_visible_before_tools_and_in_final_answer(self):
        focus = focus_on_lines(self.tools, "doc-a", 3, 3, context_lines=0)
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            prompt = body["messages"][1]["content"]
            self.assertIn("User-selected lines: source_id=doc-a, L3-L3", prompt)
            self.assertIn("[L3] Visible fact.", prompt)
            self.assertNotIn("HIDDEN", prompt)
            if "tools" in body:
                return self.completion({"role": "assistant", "content": "READY"})
            return self.completion({"role": "assistant", "content": json.dumps({
                "coverage": "sufficient", "coverage_note": "", "blocks": [{
                    "kind": "grounded", "text": "Visible fact.", "citations": [
                        {"source_id": "doc-a", "line_start": 3, "line_end": 3},
                    ],
                }],
            })})

        result = self.run_script(respond, strict=True, focus=focus)
        self.assertEqual(len(requests), 2)
        self.assertEqual(result.steps, ())
        self.assertEqual(result.visible_chunks, (focus.chunk,))
        self.assertEqual(result.answer.blocks[0].citations[0].line_start, 3)
        self.tools.embedder.embed_query.assert_not_called()
        self.tools.embedder.embed_chunks.assert_not_called()

    def test_focus_validates_source_lines_and_context_limit(self):
        for source_id, start, end in [("missing", 1, 1), ("doc-a", 0, 1),
                                      ("doc-a", 3, 2), ("doc-a", 3, 99)]:
            with self.subTest(source_id=source_id, start=start, end=end), self.assertRaises(ValueError):
                focus_on_lines(self.tools, source_id, start, end)
        focus = focus_on_lines(self.tools, "doc-a", 3, 3)
        self.assertEqual((focus.chunk.line_start, focus.chunk.line_end), (1, 7))

    def test_general_answer_does_not_load_embedding_model_or_search(self):
        self.tools._embedder = None
        def respond(request):
            body = json.loads(request.content)
            self.assertNotIn('Visible fact.', str(body['messages']))
            if 'tools' in body:
                self.assertEqual(body['tool_choice'], 'auto')
                return self.completion({'role': 'assistant', 'content': 'READY'})
            return self.completion({'role': 'assistant', 'content': json.dumps({
                'coverage': 'none', 'coverage_note': 'A general response without source evidence.', 'blocks': [{
                    'kind': 'supplement', 'text': 'General background explanation.', 'citations': [],
                }],
            })})
        with patch('knowledge_tools.LocalEmbedder', side_effect=RuntimeError('Unavailable')) as loader:
            result = self.run_script(respond)
        loader.assert_not_called()
        self.assertEqual(result.steps, ())
        self.assertEqual(result.visible_chunks, ())
        self.assertIsNone(self.tools._embedder)

    def test_model_can_read_missing_evidence_without_semantic_search(self):
        self.tools.embedder.embed_query.side_effect = RuntimeError('Local model unavailable')
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return self.completion({'role': 'assistant', 'content': None, 'tool_calls': [
                    self.call('read', 'read_source', {'source_id': 'doc-a', 'line_start': 3, 'line_end': 3}),
                ]}, 'tool_calls')
            if 'tools' in body:
                return self.completion({'role': 'assistant', 'content': 'READY'})
            return self.completion({'role': 'assistant', 'content': json.dumps({
                'coverage': 'sufficient', 'coverage_note': '', 'blocks': [{
                    'kind': 'grounded', 'text': 'Visible fact.', 'citations': [
                        {'source_id': 'doc-a', 'line_start': 3, 'line_end': 3},
                    ],
                }],
            })})
        result = self.run_script(respond, strict=True)
        self.assertEqual([step.name for step in result.steps], ['read_source'])
        self.tools.embedder.embed_query.assert_not_called()
        self.tools.embedder.embed_chunks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
