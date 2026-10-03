"""Web boundary tests; cloud model calls are replaced with a controlled Agent run."""

import shutil
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_loop import AgentRun, AgentStep
from answers import NoteAnswer
from test_support import temporary_notes
from web_app import create_app, render_markdown


class WebWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.notes = temporary_notes()
        self.root = self.notes.__enter__()
        self.client = TestClient(create_app(self.root / "uploads"))

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.root / "uploads", ignore_errors=True)
        self.notes.__exit__(None, None, None)

    def import_note(self, name="note.md", content=b"# Heading\n\nSelected idea.\n"):
        response = self.client.post(
            "/api/documents", files=[("files", (name, content, "text/markdown"))],
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()[0]

    def test_page_catalog_and_imported_note_keep_source_lines(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        imported = self.import_note()
        self.assertIn(imported, self.client.get("/api/documents").json())
        note = self.client.get(f"/api/documents/{imported['id']}").json()
        self.assertEqual(note["lines"], ["# Heading", "", "Selected idea."])
        self.assertIn('data-line-start="3" data-line-end="3"', note["html"])
        self.assertIn("Selected idea.", note["html"])

    def test_rendered_markdown_carries_block_ranges_and_escapes_html(self):
        html = render_markdown("# Title\n\nFirst line\nsecond line\n\n```py\nx < 2\n```\n\n| Name | Value |\n| --- | --- |\n| X | Y |\n\n<script>x</script>")
        self.assertIn('data-line-start="3" data-line-end="4"', html)
        self.assertIn('data-line-start="6" data-line-end="8"', html)
        self.assertIn('data-line-start="10" data-line-end="12"', html)
        self.assertIn("<table", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_import_rejects_invalid_content_and_document_ids(self):
        for name, content in [("note.txt", b"text"), ("note.md", b"\xff"), ("note.md", b"  ")]:
            response = self.client.post(
                "/api/documents", files=[("files", (name, content, "text/markdown"))],
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/documents/upload-../../etc/passwd").status_code, 404)
        before = self.client.get("/api/documents").json()
        response = self.client.post("/api/documents", files=[
            ("files", ("valid.md", b"# Valid", "text/markdown")),
            ("files", ("invalid.txt", b"Invalid", "text/plain")),
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/documents").json(), before)

    def test_ask_reuses_focus_snapshot_and_returns_traceable_citation(self):
        selected = self.import_note()
        other = self.import_note("other.md", b"# Other\n\nBackground.\n")
        note = self.client.get(f"/api/documents/{selected['id']}").json()
        payload = {
            "document_ids": [selected["id"], other["id"]],
            "focus_document_id": selected["id"],
            "focus_source_id": note["source_id"],
            "line_start": 3, "line_end": 3,
            "question": "Explain this idea.", "strict": True, "response_language": "en",
        }

        def answer(sources, question, tools, *, strict, focus, response_language):
            self.assertEqual(response_language, 'en')
            self.assertEqual(len(sources), 2)
            self.assertTrue(strict)
            self.assertEqual((focus.line_start, focus.line_end), (3, 3))
            self.assertEqual(focus.chunk.text.splitlines()[2], "Selected idea.")
            result = NoteAnswer.model_validate({
                "coverage": "sufficient", "coverage_note": "",
                "blocks": [{"kind": "grounded", "text": "An idea is selected.",
                            "citations": [{"source_id": focus.chunk.source_id,
                                           "line_start": 3, "line_end": 3}]}],
            })
            return AgentRun(result, (AgentStep("read_source", {"line_start": 3}),),
                            "model_finished", (focus.chunk,))

        with patch("web_app.run_agent", side_effect=answer) as agent:
            response = self.client.post("/api/ask", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        agent.assert_called_once()
        citation = response.json()["blocks"][0]["citations"][0]
        self.assertEqual(citation["document_id"], selected["id"])
        self.assertEqual(citation["excerpt"], "Selected idea.")
        self.assertEqual(response.json()["steps"][0]["name"], "read_source")

        payload["focus_source_id"] = "old-version"
        with patch("web_app.run_agent") as agent:
            stale = self.client.post("/api/ask", json=payload)
        self.assertEqual(stale.status_code, 409)
        agent.assert_not_called()

    def test_ask_rejects_out_of_scope_or_invalid_selection(self):
        selected = self.import_note()
        note = self.client.get(f"/api/documents/{selected['id']}").json()
        payload = {
            "document_ids": [selected["id"]], "focus_document_id": selected["id"],
            "focus_source_id": note["source_id"], "line_start": 3, "line_end": 4,
            "question": "Why?", "strict": False,
        }
        self.assertEqual(self.client.post("/api/ask", json=payload).status_code, 400)
        payload["line_end"] = 3
        payload["document_ids"] = ["example-project-notes"]
        self.assertEqual(self.client.post("/api/ask", json=payload).status_code, 400)

    def test_invalid_language_is_rejected_before_model_request(self):
        selected = self.import_note()
        note = self.client.get(f"/api/documents/{selected['id']}").json()
        payload = {
            'document_ids': [selected['id']], 'focus_document_id': selected['id'],
            'focus_source_id': note['source_id'], 'line_start': 3, 'line_end': 3,
            'question': 'Why?', 'response_language': 'fr',
        }
        with patch('web_app.run_agent') as agent:
            self.assertEqual(self.client.post('/api/ask', json=payload).status_code, 422)
        agent.assert_not_called()

    def test_ask_defaults_to_whole_document_without_selection(self):
        selected = self.import_note(content=b"# Start\n\nEarlier.\n\n# End\n\nLater fact.")
        note = self.client.get(f"/api/documents/{selected['id']}").json()
        payload = {'document_ids': [selected['id']], 'focus_document_id': selected['id'],
                   'focus_source_id': note['source_id'], 'question': 'What is the later fact?'}
        result = NoteAnswer(coverage='none', coverage_note='Test retrieval scope', blocks=[])
        with patch('web_app.run_agent', return_value=AgentRun(result, (), 'model_finished', ())) as agent:
            response = self.client.post('/api/ask', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(agent.call_args.kwargs['focus'])
        self.assertNotIn('initial_search', agent.call_args.kwargs)
        self.assertIn('Later fact.', agent.call_args.args[0][0].text)
        self.assertIsNone(response.json()['focus_context'])
        payload['line_start'] = 1
        with patch('web_app.run_agent') as agent:
            self.assertEqual(self.client.post('/api/ask', json=payload).status_code, 400)
        agent.assert_not_called()
        payload.pop('line_start')
        payload['focus_source_id'] = 'stale'
        self.assertEqual(self.client.post('/api/ask', json=payload).status_code, 409)

    def test_optional_web_selection_can_exceed_read_tool_line_limit(self):
        text = '\n'.join(f'Original line {i}' for i in range(240))
        selected = self.import_note(content=text.encode())
        note = self.client.get(f"/api/documents/{selected['id']}").json()
        result = NoteAnswer(coverage='none', coverage_note='Test focus', blocks=[])
        with patch('web_app.run_agent', return_value=AgentRun(result, (), 'model_finished', ())) as agent:
            response = self.client.post('/api/ask', json={
                'document_ids': [selected['id']], 'focus_document_id': selected['id'],
                'focus_source_id': note['source_id'], 'question': 'Explain',
                'line_start': 1, 'line_end': 240,
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(agent.call_args.kwargs['focus'].chunk.text, text)
        self.assertNotIn('initial_search', agent.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
