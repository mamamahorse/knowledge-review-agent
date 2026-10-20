"""Table context and pagination preserve original ranges and honest coverage."""

import json
from pathlib import Path
import unittest

from chunking import chunk_source, with_table_context
from context import citation_is_visible, validate_chunks, format_context
from document_model import Document, digest, markdown_document
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from sources import Source


class TableReadingTests(unittest.TestCase):
    def setUp(self):
        text = '# Performance\n\nTable 1. Latency (unit: ms).\n\n| System | Latency |\n| --- | --- |\n| A | 11 |\n| B | 17 |\n| C | 23 |\n\nNote: measured with one worker.\n\nUnrelated next paragraph.'
        self.source = Source('version', Path('table.md'), text)
        self.block = next(b for b in self.source.document.blocks if b.kind == 'table')
        self.tools = KnowledgeTools([self.source], select_embedding_config())

    def read(self, start, end):
        return self.tools.execute('read_table', {'source_id': 'version', 'block_id': self.block.block_id,
                                                'row_start': start, 'row_end': end})

    def test_caption_unit_and_note_are_separate_real_excerpts(self):
        chunk = next(c for c in chunk_source(self.source) if c.kind == 'table')
        visible = with_table_context([chunk], [self.source])
        validate_chunks(visible, [self.source])
        text = '\n'.join(c.text for c in visible)
        self.assertIn('Latency (unit: ms)', text)
        self.assertIn('Note: measured with one worker.', text)
        self.assertNotIn('Unrelated next paragraph', text)
        self.assertEqual(len(self.block.context_ranges), 2)

    def test_partial_rows_report_coverage_and_do_not_grant_unread_row(self):
        result = self.read(2, 3)
        self.assertEqual(result.output['total_extracted_rows'], 3)
        self.assertFalse(result.output['complete_extracted_table'])
        self.assertEqual(result.output['rows'][0]['cells'], ['B', '17'])
        self.assertEqual(result.output['rows'][1]['cells'], ['C', '23'])
        validate_chunks(list(result.visible_chunks), [self.source])
        self.assertFalse(citation_is_visible('version', 7, 7, list(result.visible_chunks)))
        self.assertTrue(citation_is_visible('version', 5, 6, list(result.visible_chunks)))
        self.assertIn('"complete_extracted_table": false', format_context(list(result.visible_chunks), [self.source]))

    def test_all_extracted_rows_are_explicitly_reported(self):
        result = self.read(1, 3)
        self.assertTrue(result.output['complete_extracted_table'])
        self.assertEqual([r['cells'][0] for r in result.output['rows']], ['A', 'B', 'C'])
        self.assertIn('not proof that PDF extraction is complete', result.output['notice'])
        self.assertIn('"complete_extracted_table": true', format_context(list(result.visible_chunks), [self.source]))

    def test_invalid_range_and_alien_source_are_rejected(self):
        for start, end in [(0, 2), (1, 4), (True, 2), (3, 2)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.read(start, end)
        with self.assertRaises(ValueError):
            self.tools.execute('read_table', {'source_id': 'another-version', 'block_id': self.block.block_id, 'row_start': 1, 'row_end': 3})

    def test_new_optional_fields_do_not_change_old_snapshot_identity(self):
        raw = markdown_document('Plain original prose.').to_dict()
        self.assertNotIn('context_ranges', raw['blocks'][0])
        expected = digest(json.dumps(raw, sort_keys=True, ensure_ascii=False))
        restored = Document.from_dict(json.loads(json.dumps(raw)))
        self.assertEqual(restored.parse_id, expected)

    def test_context_links_survive_json_roundtrip(self):
        restored = Document.from_dict(json.loads(json.dumps(self.source.document.to_dict())))
        self.assertEqual(restored, self.source.document)


if __name__ == '__main__':
    unittest.main()
