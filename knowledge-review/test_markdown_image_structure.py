import unittest

from document_model import markdown_document, Document


class MarkdownImageStructureTests(unittest.TestCase):
    def test_nested_and_html_images_are_linked_to_original_blocks(self):
        text = ('# Guide\n\n- First\n  ![one](a.png)\n  - ![two](b.png)\n\n'
                '> ![quote](c.png)\n\nInline <img src="d.png"> text\n\n'
                '<div><img src="e.png" /></div>\n\n'
                '```html\n<img src="not-an-image.png">\n```\n')
        doc = markdown_document(text)
        self.assertEqual({a.uri for a in doc.assets}, {'a.png', 'b.png', 'c.png', 'd.png', 'e.png'})
        by_id = {a.asset_id: a.uri for a in doc.assets}
        self.assertEqual({by_id[i] for i in doc.blocks[1].asset_ids}, {'a.png', 'b.png'})
        self.assertEqual({by_id[i] for i in doc.blocks[2].asset_ids}, {'c.png'})
        self.assertEqual(doc.text, text)
        self.assertEqual(Document.from_dict(doc.to_dict()), doc)

    def test_repeated_reference_has_one_asset_and_one_reference_per_block(self):
        doc = markdown_document('- ![one](same.png) and ![two](same.png)\n')
        self.assertEqual(len(doc.assets), 1)
        self.assertEqual(doc.blocks[0].asset_ids, (doc.assets[0].asset_id,))


if __name__ == '__main__':
    unittest.main()
