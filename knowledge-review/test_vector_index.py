import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import Mock

from chunking import chunk_source
from embedding_config import select_embedding_config
from retrieval import rank_chunks
from sources import load_sources
from test_support import temporary_notes
from vector_index import get_document_vectors


class VectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory_context = temporary_notes()
        self.directory = self.directory_context.__enter__()
        self.addCleanup(self.directory_context.__exit__, None, None, None)
        self.path = self.directory / "vectors.sqlite3"
        self.notes = [self.directory / "a.md", self.directory / "b.md"]
        for path, text in zip(self.notes, ["# A\n\nAlpha", "# B\n\nBeta"]):
            path.write_text(text, encoding="utf-8")
        self.chunks = self.read_chunks()
        self.embedder = Mock()
        self.embedder.config = select_embedding_config()
        self.embedder.cache_profile.return_value = {"weights": "version-1", "tokens": 512}
        self.embedder.embed_chunks.side_effect = lambda chunks: [[1.0, float(c.text.endswith("Beta"))] for c in chunks]

    def read_chunks(self):
        return [c for s in load_sources(self.notes) for c in chunk_source(s, min_chars=0)]

    def get(self, chunks=None, **kwargs):
        options = {"max_chars": 1200, "min_chars": 0, "path": self.path, **kwargs}
        return get_document_vectors(self.chunks if chunks is None else chunks, self.embedder, 2, **options)

    def test_reopen_reuses_vectors_preserves_order_and_rank(self):
        first = self.get()
        self.assertEqual((first.reused, first.computed), (0, 2))
        self.embedder.embed_chunks.reset_mock()
        second = self.get(list(reversed(self.chunks)))
        self.assertEqual((second.reused, second.computed), (2, 0))
        self.assertEqual(second.vectors, list(reversed(first.vectors)))
        self.embedder.embed_chunks.assert_not_called()
        self.assertEqual(rank_chunks(self.chunks, first.vectors, [0, 1])[0].chunk, self.chunks[1])

    def test_changed_document_only_recomputes_that_document_and_omitted_files_stay_out(self):
        self.get()
        self.notes[0].write_text("# A\n\nChanged", encoding="utf-8")
        updated = self.read_chunks()
        result = self.get(updated)
        self.assertEqual((result.reused, result.computed), (1, 1))
        self.assertEqual(self.embedder.embed_chunks.call_args.args[0], [updated[0]])
        result = self.get([updated[1]])
        self.assertEqual(len(result.vectors), 1)
        self.assertEqual(result.reused, 1)

    def test_weight_settings_dimension_and_input_changes_do_not_reuse(self):
        self.get()
        self.embedder.cache_profile.return_value = {"weights": "version-2", "tokens": 512}
        self.assertEqual(self.get().computed, 2)
        self.embedder.cache_profile.return_value = {"weights": "version-2", "tokens": 128}
        self.assertEqual(self.get().computed, 2)
        self.assertEqual(self.get(max_chars=600).computed, 2)
        self.assertEqual(self.get(min_chars=100).computed, 2)
        self.embedder.config = replace(self.embedder.config, passage_prefix="document: ")
        self.assertEqual(self.get().computed, 2)
        changed = [replace(self.chunks[0], text="Changed input"), self.chunks[1]]
        self.assertEqual(self.get(changed).computed, 1)
        self.embedder.embed_chunks.side_effect = lambda chunks: [[1., 0., 0.] for _ in chunks]
        result = get_document_vectors(self.chunks, self.embedder, 3, max_chars=1200, min_chars=0, path=self.path)
        self.assertEqual(result.computed, 2)

    def test_damaged_vector_row_is_recomputed(self):
        self.get()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE vectors SET vector=? WHERE chunk_id=?", ("[0.0, 999.0]", self.chunks[0].chunk_id))
        result = self.get()
        self.assertEqual((result.reused, result.computed), (1, 1))
        self.assertEqual(result.vectors[0], [1., 0.])

    def test_failed_rebuild_leaves_previous_data_intact(self):
        first = self.get()
        for invalid in ([[1., 0.]], [[0., 0.], [1., 0.]], [[True, 0.], [1., 0.]], [[float("nan"), 0.], [1., 0.]]):
            self.embedder.embed_chunks.side_effect = None
            self.embedder.embed_chunks.return_value = invalid
            with self.assertRaises(ValueError):
                self.get(rebuild=True)
            self.assertEqual(self.get().vectors, first.vectors)
        self.embedder.embed_chunks.side_effect = RuntimeError("inference failed")
        with self.assertRaises(RuntimeError):
            self.get(rebuild=True)
        self.assertEqual(self.get().reused, 2)

    def test_rebuild_replaces_only_selected_rows_and_records_profile(self):
        self.get()
        self.embedder.embed_chunks.side_effect = lambda chunks: [[0., 1.] for _ in chunks]
        rebuilt = self.get([self.chunks[0]], rebuild=True)
        self.assertEqual((rebuilt.reused, rebuilt.computed), (0, 1))
        self.assertEqual(self.get().vectors, [[0., 1.], [1., 1.]])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            metadata = json.loads(connection.execute("SELECT metadata FROM profiles").fetchone()[0])
        self.assertEqual(metadata["dimensions"], 2)
        self.assertEqual(metadata["chunking"]["min_chars"], 0)

    def test_broken_database_is_reported_without_overwrite(self):
        self.path.write_text("not a SQLite file", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "--no-cache"):
            self.get()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not a SQLite file")

    def test_database_write_failure_rolls_back_partial_rebuild(self):
        original = self.get().vectors
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON vectors
                WHEN NEW.chunk_id != (SELECT MIN(chunk_id) FROM vectors)
                BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
        self.embedder.embed_chunks.side_effect = lambda chunks: [[0., 1.] for _ in chunks]
        with self.assertRaisesRegex(RuntimeError, "simulated write failure"):
            self.get(sorted(self.chunks, key=lambda c: c.chunk_id), rebuild=True)
        self.assertEqual(self.get().vectors, original)


if __name__ == "__main__":
    unittest.main()
