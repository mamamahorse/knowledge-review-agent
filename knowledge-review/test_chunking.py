import unittest
from pathlib import Path

from chunking import chunk_source
from sources import Source


class ChunkingTests(unittest.TestCase):
    def split(self, text, size=60):
        return chunk_source(Source("version-a", Path("note.md"), text), size, min_chars=0)

    def test_ranges_cover_original_nonblank_lines_once(self):
        text = "\n[ref]: https://example.com\n\n# A\n\nFirst paragraph.\n\nSecond paragraph.\n\n## B\n\nEnd.\n"
        chunks = self.split(text, 20)
        lines = text.splitlines()
        covered = []
        for chunk in chunks:
            self.assertEqual(chunk.text, "\n".join(lines[chunk.line_start - 1:chunk.line_end]))
            covered.extend(i for i in range(chunk.line_start, chunk.line_end + 1) if lines[i - 1].strip())
        self.assertEqual(covered, [i for i, line in enumerate(lines, 1) if line.strip()])
        self.assertEqual(chunks[-1].heading_path, ("A", "B"))

    def test_code_table_list_and_quote_stay_whole(self):
        blocks = [
            "```python\n# Not a heading\nprint('hello')\n```",
            "| A | B |\n| --- | --- |\n| long value | other value |",
            "- first item\n  continuation\n- second item",
            "> # Nested heading\n> quoted paragraph",
        ]
        for block in blocks:
            with self.subTest(block=block):
                chunks = self.split("# Section\n\n" + block + "\n\nAfter.", 10)
                containing = [chunk for chunk in chunks if block in chunk.text]
                self.assertEqual(len(containing), 1)
                self.assertEqual(containing[0].heading_path, ("Section",))
                self.assertGreater(len(containing[0].text), 10)

    def test_setext_and_sibling_headings(self):
        chunks = self.split("Title\n=====\n\nIntro\n\n## Child\n\nA\n\n# Next\n\nB")
        self.assertEqual([c.heading_path for c in chunks], [("Title",), ("Title", "Child"), ("Next",)])

    def test_identity_and_no_heading(self):
        text = "First paragraph.\n\nSecond paragraph."
        chunks = self.split(text, 20)
        self.assertEqual(chunks, self.split(text, 20))
        self.assertEqual(len({c.chunk_id for c in chunks}), 2)
        self.assertEqual(chunks[0].heading_path, ())
        other = chunk_source(Source("version-b", Path("other.md"), text), 20, min_chars=0)
        self.assertNotEqual(chunks[0].chunk_id, other[0].chunk_id)

    def test_empty_and_invalid_budget(self):
        self.assertEqual(self.split("\n\n"), [])
        for size in (0, -1):
            with self.assertRaises(ValueError):
                self.split("text", size)

    def test_dense_headings_merge_without_losing_sections_or_source_positions(self):
        text = "# Root\n\n## A\n\nOne.\n\n## B\n\nTwo.\n\n## C\n\nThree."
        source = Source("doc", Path("note.md"), text)
        chunks = chunk_source(source, max_chars=70, min_chars=40)
        self.assertLess(len(chunks), len(chunk_source(source, 70, 0)))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, text)
        self.assertEqual(chunks[0].heading_path, ("Root",))
        self.assertIn(("Root", "A"), chunks[0].section_paths)
        self.assertIn(("Root", "C"), chunks[0].section_paths)
        self.assertEqual(chunks, chunk_source(source, 70, 40))

    def test_merge_does_not_exceed_max_or_force_merge_two_large_chunks(self):
        text = "# A\n\n" + "a" * 25 + "\n\n# B\n\n" + "b" * 10
        source = Source("doc", Path("note.md"), text)
        chunks = chunk_source(source, max_chars=40, min_chars=20)
        self.assertEqual(len(chunks), 2)
        self.assertLess(len(chunks[-1].text), 20)  # Soft minimum yields to maximum.
        self.assertTrue(all(len(c.text) <= 40 for c in chunks))
        self.assertEqual(len(chunk_source(source, 100, 10)), 2)

    def test_small_tail_merges_backwards_including_blank_gap(self):
        text = "# A\n\n" + "a" * 30 + "\n\n\n# B\n\nEnd."
        source = Source("doc", Path("note.md"), text)
        chunks = chunk_source(source, 80, 20)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, text)
        self.assertEqual(chunks[0].heading_path, ())
        self.assertEqual(chunks[0].section_paths, (("A",), ("B",)))

    def test_minimum_does_not_split_oversized_code_or_drop_small_document(self):
        block = "```\n" + "x" * 100 + "\n```"
        source = Source("doc", Path("note.md"), "# A\n\n" + block + "\n\n# B\n\nEnd")
        chunks = chunk_source(source, 60, 30)
        self.assertTrue(any(block in c.text for c in chunks))
        self.assertEqual(chunks[-1].text, "# B\n\nEnd")
        self.assertEqual(chunk_source(Source("tiny", Path("tiny.md"), "Hi"))[0].text, "Hi")
        for minimum in (-1, 61):
            with self.assertRaises(ValueError):
                chunk_source(source, 60, minimum)


if __name__ == "__main__":
    unittest.main()
