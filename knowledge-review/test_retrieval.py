import unittest
from pathlib import Path

from chunking import chunk_source
from retrieval import rank_chunks, format_hits
from sources import Source


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.source = Source("doc", Path("notes.md"), "# A\n\nAlpha\n\n# B\n\nBeta")
        self.chunks = chunk_source(self.source, min_chars=0)

    def test_cosine_ranking_ignores_vector_magnitude(self):
        hits = rank_chunks(self.chunks, [[100, 0], [1, 2]], [0, 7], top_k=1)
        self.assertEqual(hits[0].chunk, self.chunks[1])
        self.assertAlmostEqual(hits[0].score, 2 / 5 ** 0.5)
        output = format_hits(hits, [self.source], 1200)
        self.assertIn("[L7] Beta", output)
        self.assertIn("notes.md", output)

    def test_ties_keep_order_and_large_k_returns_only_existing_chunks(self):
        hits = rank_chunks(self.chunks, [[1, 0], [2, 0]], [1, 0], 20)
        self.assertEqual([hit.chunk for hit in hits], self.chunks)
        self.assertEqual(rank_chunks([], [], [1, 0]), [])

    def test_invalid_vectors_are_rejected(self):
        for vectors, query in [
            ([[1, 0]], [1, 0]),
            ([[1], [1]], [1, 0]),
            ([[0, 0], [1, 0]], [1, 0]),
            ([[float("nan"), 0], [1, 0]], [1, 0]),
            ([[1, 0], [1, 0]], [float("inf"), 0]),
            ([[], []], []),
        ]:
            with self.subTest(vectors=vectors, query=query), self.assertRaises(ValueError):
                rank_chunks(self.chunks, vectors, query)
        with self.assertRaises(ValueError):
            rank_chunks(self.chunks, [[1], [1]], [1], 0)


if __name__ == "__main__":
    unittest.main()
