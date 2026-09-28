import unittest
from pathlib import Path
from unittest.mock import Mock

from embedding_config import select_embedding_config
from knowledge_tools import (
    KnowledgeTools, MAX_READ_LINES, MAX_SEARCH_RESULTS, knowledge_tool_definitions,
)
from sources import load_sources
from test_support import temporary_notes


class KnowledgeToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory_context = temporary_notes()
        self.directory = self.directory_context.__enter__()
        self.addCleanup(self.directory_context.__exit__, None, None, None)
        self.path = self.directory / "notes.md"
        self.path.write_text("# Alpha\n\nFirst topic.\n\n# Beta\n\nSecond topic.", encoding="utf-8")
        self.sources = load_sources([self.path])
        self.embedder = Mock()
        self.embedder.config = select_embedding_config()
        self.embedder.max_tokens = 512
        self.embedder.embed_query.side_effect = lambda query: [1., 0.] if "first" in query else [0., 1.]
        self.embedder.embed_chunks.return_value = [[1., 0.], [0., 1.]]
        self.tools = KnowledgeTools(
            self.sources, self.embedder.config, min_chars=0, no_cache=True,
            embedder=self.embedder,
        )

    def test_repeated_search_prepares_document_vectors_once(self):
        first = self.tools.search_notes("first concept", 1)
        second = self.tools.search_notes("second concept", 1)
        self.assertEqual(first.hits[0].chunk.text, "# Alpha\n\nFirst topic.")
        self.assertEqual(second.hits[0].chunk.text, "# Beta\n\nSecond topic.")
        self.assertFalse(first.reused_in_memory)
        self.assertTrue(second.reused_in_memory)
        self.assertEqual((second.reused, second.computed), (2, 0))
        self.embedder.embed_chunks.assert_called_once()
        self.assertEqual(self.embedder.embed_query.call_count, 2)

    def test_search_result_exposes_source_identity_without_vectors(self):
        result = self.tools.search_notes("first concept", 1).as_dict(self.sources)
        hit = result["hits"][0]
        self.assertEqual(hit["source_id"], self.sources[0].source_id)
        self.assertEqual(hit["file"], "notes.md")
        self.assertEqual((hit["line_start"], hit["line_end"]), (1, 3))
        self.assertNotIn("vector", hit)
        self.assertIn("not an answer confidence", result["notice"])

    def test_search_validation(self):
        for query, top_k in [("", 1), ("   ", 1), (None, 1), ("query", 0),
                             ("query", MAX_SEARCH_RESULTS + 1), ("query", True)]:
            with self.subTest(query=query, top_k=top_k), self.assertRaises(ValueError):
                self.tools.search_notes(query, top_k)

    def test_read_source_uses_snapshot_and_exact_source_id(self):
        source = self.sources[0]
        result = self.tools.read_source(source.source_id, 2, 6)
        self.assertEqual(result.chunk.text, "\nFirst topic.\n\n# Beta\n")
        self.assertEqual(result.chunk.section_paths, (("Alpha",), ("Beta",)))
        self.path.write_text("changed on disk", encoding="utf-8")
        self.assertEqual(self.tools.read_source(source.source_id, 3, 3).chunk.text, "First topic.")
        for source_id in (source.source_id[:12], str(self.path), "missing"):
            with self.assertRaisesRegex(ValueError, "currently loaded"):
                self.tools.read_source(source_id, 1, 1)

    def test_read_validation_and_line_limit(self):
        source = self.sources[0]
        for start, end in [(0, 1), (2, 1), (1, 99), (True, 1), (1, 1.5)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.tools.read_source(source.source_id, start, end)
        long_path = self.directory / "long.md"
        long_path.write_text("\n".join(f"line {i}" for i in range(MAX_READ_LINES + 1)), encoding="utf-8")
        long_tools = KnowledgeTools(load_sources([long_path]), self.embedder.config, embedder=self.embedder)
        with self.assertRaisesRegex(ValueError, str(MAX_READ_LINES)):
            long_tools.read_source(long_tools.sources[0].source_id, 1, MAX_READ_LINES + 1)

    def test_execute_dispatches_strict_arguments_and_reports_visible_chunks(self):
        search = self.tools.execute("search_notes", {"query": "first concept", "top_k": 1})
        self.assertEqual(search.visible_chunks[0].text, "# Alpha\n\nFirst topic.")
        source = self.sources[0]
        read = self.tools.execute("read_source", {
            "source_id": source.source_id, "line_start": 3, "line_end": 3,
        })
        self.assertEqual(read.output["text"], "First topic.")
        self.assertEqual(read.visible_chunks[0].line_start, 3)
        for name, arguments in [
            ("missing", {}), ("search_notes", {"query": "x", "extra": 1}),
            ("read_source", {"source_id": source.source_id, "line_start": 1}),
            ("search_notes", "not an object"),
        ]:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValueError):
                self.tools.execute(name, arguments)

    def test_tool_definitions_match_dispatch_contract(self):
        definitions = knowledge_tool_definitions()
        self.assertEqual([item["name"] for item in definitions], ["search_notes", "read_source"])
        for item in definitions:
            self.assertFalse(item["parameters"]["additionalProperties"])
        search = definitions[0]["parameters"]["properties"]["top_k"]
        self.assertEqual(search["maximum"], MAX_SEARCH_RESULTS)


if __name__ == "__main__":
    unittest.main()
