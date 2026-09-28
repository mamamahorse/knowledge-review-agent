import unittest
from dataclasses import replace
from pathlib import Path

from answers import NoteAnswer, validate_answer
from chunking import Chunk
from context import format_context, validate_chunks, citation_is_visible
from sources import Source


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.source = Source("doc", Path("notes.md"), "HIDDEN FIRST\nVisible two\nVisible three\nHIDDEN GAP\nVisible five\nHIDDEN LAST")
        self.chunk = Chunk("chunk", "doc", (), 2, 3, "Visible two\nVisible three")
        self.tail = Chunk("tail", "doc", (), 5, 5, "Visible five")

    def answer(self, start, end, source_id="doc"):
        return NoteAnswer.model_validate({
            "coverage": "sufficient", "coverage_note": "", "blocks": [{
                "kind": "grounded", "text": "Explanation", "citations": [{
                    "source_id": source_id, "line_start": start, "line_end": end,
                }],
            }],
        })

    def test_context_contains_only_selected_original_lines(self):
        result = format_context([self.chunk, self.tail], [self.source])
        self.assertNotIn("HIDDEN", result)
        self.assertIn("[L2] Visible two", result)
        self.assertIn("[L5] Visible five", result)
        self.assertIn("Source ID: doc", result)
        self.assertNotIn("HIDDEN", format_context([], [self.source]))

    def test_forged_or_stale_chunks_fail(self):
        for chunk in [replace(self.chunk, source_id="old"), replace(self.chunk, text="forged"),
                      replace(self.chunk, line_start=0), replace(self.chunk, line_end=7)]:
            with self.subTest(chunk=chunk), self.assertRaises(ValueError):
                validate_chunks([chunk], [self.source])

    def test_citations_must_not_cross_hidden_gaps_or_other_sources(self):
        chunks = [self.tail, self.chunk]
        validate_answer(self.answer(2, 3), [self.source], chunks=chunks)
        validate_answer(self.answer(5, 5), [self.source], chunks=chunks, strict=True)
        other = Source("other", Path("other.md"), "Exists but not retrieved")
        for start, end, source_id in [(1, 1, "doc"), (2, 5, "doc"), (1, 1, "other")]:
            with self.assertRaisesRegex(RuntimeError, "outside"):
                validate_answer(self.answer(start, end, source_id), [self.source, other], chunks=chunks)

    def test_visible_range_union_allows_adjacency_and_overlap(self):
        adjacent = replace(self.tail, line_start=4)
        self.assertTrue(citation_is_visible("doc", 2, 5, [adjacent, self.chunk, self.chunk]))
        self.assertFalse(citation_is_visible("other", 2, 3, [self.chunk]))

    def test_empty_retrieval_never_means_full_text(self):
        validate_answer(self.answer(1, 1), [self.source])
        with self.assertRaisesRegex(RuntimeError, "outside"):
            validate_answer(self.answer(1, 1), [self.source], chunks=[])
        abstention = NoteAnswer(coverage="none", coverage_note="Retrieved excerpts are insufficient.", blocks=[])
        validate_answer(abstention, [self.source], chunks=[], strict=True)


if __name__ == "__main__":
    unittest.main()
