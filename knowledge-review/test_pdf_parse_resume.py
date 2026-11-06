"""Interrupted local PDF extraction resumes without changing source coordinates."""
import json
import shutil
import unittest
from unittest.mock import patch

from agent_cancel import Cancellation, GenerationCancelled, cancellation_scope
from pdf_parser import parse_pdf, extract_page
from test_pdf_documents import text_pdf
from test_support import temporary_notes


class PDFPageResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.cache = self.root / 'pages'
        self.cache.mkdir()
        self.addCleanup(shutil.rmtree, self.cache)
        self.data = text_pdf(table=True)
        self.config = {'engine':'pymupdf', 'vlm':False}

    def parse(self, **kwargs):
        return parse_pdf(self.data, raw_file='source.pdf', config=self.config,
                         checkpoint_dir=self.cache, **kwargs)

    def test_failed_second_page_resumes_and_matches_uncached_snapshot(self):
        def interrupted(page):
            if page.number == 1:
                raise RuntimeError('private transport or file detail')
            return extract_page(page)
        with patch('pdf_parser.extract_page', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.parse()
        records = [json.loads(p.read_text(encoding='utf-8')) for p in self.cache.glob('*.json')]
        self.assertEqual(sorted(r['status'] for r in records), ['completed', 'failed'])
        self.assertNotIn('private transport', str(records))
        with patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            result = self.parse()
        self.assertEqual(extract.call_count, 1)
        baseline = parse_pdf(self.data, raw_file='source.pdf', config=self.config)
        self.assertEqual(result.parse_id, baseline.parse_id)
        # JSON persistence normalizes PyMuPDF cell tuples to lists, without changing identity.
        self.assertEqual(json.loads(json.dumps(result.to_dict())), json.loads(json.dumps(baseline.to_dict())))

    def test_completed_pages_are_reused_without_extraction(self):
        first = self.parse()
        with patch('pdf_parser.extract_page', side_effect=AssertionError('must reuse')):
            self.assertEqual(self.parse().parse_id, first.parse_id)

    def test_corrupt_page_reextracts_only_that_page(self):
        self.parse()
        path = next(self.cache.glob('*.json'))
        value = json.loads(path.read_text(encoding='utf-8'))
        value['result']['items'] = []
        path.write_text(json.dumps(value), encoding='utf-8')
        with patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            self.parse()
        self.assertEqual(extract.call_count, 1)

    def test_truncated_checkpoint_is_not_trusted(self):
        self.parse()
        next(self.cache.glob('*.json')).write_text('{', encoding='utf-8')
        with patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            self.parse()
        self.assertEqual(extract.call_count, 1)

    def test_configuration_and_raw_hash_separate_checkpoints(self):
        self.parse()
        self.config = {**self.config, 'algorithm':'new-version'}
        with patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            self.parse()
        self.assertEqual(extract.call_count, 2)
        self.data = text_pdf('Changed original.')
        with patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            self.parse()
        self.assertEqual(extract.call_count, 2)

    def test_explicit_force_reparses_all_pages(self):
        self.parse()
        with patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            self.parse(force=True)
        self.assertEqual(extract.call_count, 2)

    def test_cancellation_keeps_completed_page_without_publishing_result(self):
        token = Cancellation()
        def interrupted(page):
            result = extract_page(page)
            if page.number == 1:
                token.cancel()
            return result
        with patch('pdf_parser.extract_page', side_effect=interrupted), cancellation_scope(token):
            with self.assertRaises(GenerationCancelled):
                self.parse()
        self.assertEqual(len(list(self.cache.glob('*.json'))), 1)
        with patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            self.parse()
        self.assertEqual(extract.call_count, 1)

    def test_web_failed_import_does_not_publish_and_retry_reuses_local_page(self):
        from test_pdf_documents import PDFStorageAndWebTests
        fixture = PDFStorageAndWebTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        def fail(page):
            if page.number == 1:
                raise ValueError('synthetic local failure')
            return extract_page(page)
        with patch('pdf_parser.profile', return_value=self.config), patch('pdf_parser.extract_page', side_effect=fail):
            response = fixture.client.post('/api/documents', files=[('files',('source.pdf',self.data,'application/pdf'))])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list((fixture.root/'uploads').glob('*/document.json')), [])
        with patch('pdf_parser.profile', return_value=self.config), patch('pdf_parser.extract_page', wraps=extract_page) as extract:
            fixture.upload(self.data)
        self.assertEqual(extract.call_count, 1)


if __name__ == '__main__':
    unittest.main()
