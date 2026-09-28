import unittest
from pathlib import Path

import sources
from test_support import temporary_notes


class SourcesTests(unittest.TestCase):
    def test_identity_is_order_independent_and_versioned(self):
        with temporary_notes() as directory:
            a, b = Path(directory) / "a.md", Path(directory) / "b.md"
            a.write_text("# A\n\nFirst", encoding="utf-8-sig")
            b.write_text("# B\nSecond", encoding="utf-8")
            first = sources.load_sources([a, b, a])
            second = sources.load_sources([b, a])
            self.assertEqual(len(first), 2)
            self.assertEqual(first[0].source_id, second[1].source_id)
            self.assertEqual(first[0].text, "# A\n\nFirst")
            a.write_text("# A\nChanged", encoding="utf-8")
            self.assertNotEqual(first[0].source_id, sources.load_sources([a])[0].source_id)
            self.assertIn("[L3] First", sources.format_sources(first))
            self.assertIn(first[1].source_id, sources.format_sources(first))

    def test_rejects_bad_inputs(self):
        with temporary_notes() as directory:
            empty = Path(directory) / "empty.md"
            empty.write_text("  \n", encoding="utf-8")
            for paths in [[], [empty], [Path(directory) / "x.txt"]]:
                with self.subTest(paths=paths), self.assertRaises(ValueError):
                    sources.load_sources(paths)
            with self.assertRaises(OSError):
                sources.load_sources([Path(directory) / "missing.md"])


if __name__ == "__main__":
    unittest.main()
