"""Synthetic real PDFs and mocked cloud responses; no personal source or API calls."""

from dataclasses import replace
from hashlib import sha256
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

from answers import NoteAnswer, validate_answer
from chunking import chunk_source, with_table_context
from context import validate_chunks
from document_model import Document, markdown_document, snapshot_metadata
from embedding_config import select_embedding_config
from embeddings import LocalEmbedder, passage_text
from generation_budget import fit_chunks
from hybrid_retrieval import BM25Index
from knowledge_tools import KnowledgeTools
from pdf_parser import load_pdf, parse_pdf, profile, raw_path, render_asset
from pdf_vision import PageTranscription, VisualReading, VisionBudget, inspect_visual
from practice_store import archived_sources
from sources import Source, load_sources, source_from_document
from test_support import temporary_notes
from web_app import create_app


def setUpModule():
    global _pdf_environment, _pdf_dotenv
    _pdf_environment = patch.dict('os.environ', {'PDF_PARSER': 'pymupdf', 'PDF_VLM_ENABLED': '0'})
    _pdf_dotenv = patch('pdf_parser.load_dotenv')
    _pdf_environment.start(); _pdf_dotenv.start()


def tearDownModule():
    _pdf_dotenv.stop(); _pdf_environment.stop()


def text_pdf(label='Synthetic fact: 42 requests.', table=False):
    with pymupdf.open() as doc:
        page = doc.new_page(width=420, height=600)
        page.insert_text((30, 35), label)
        if table:
            xs, ys = [30, 180, 360], [70 + 25 * n for n in range(8)]
            for x in xs:
                page.draw_line((x, ys[0]), (x, ys[-1]))
            for y in ys:
                page.draw_line((xs[0], y), (xs[-1], y))
            rows = [('System', 'Requests per second')] + [(f'System-{i}', str(100 + i)) for i in range(6)]
            for row, y in zip(rows, ys):
                page.insert_text((40, y + 17), row[0], fontsize=10)
                page.insert_text((190, y + 17), row[1], fontsize=10)
        page = doc.new_page(width=420, height=600)
        page.insert_text((30, 40), 'Second-page evidence.')
        return doc.tobytes()


def scan_pdf(count=1):
    with pymupdf.open() as original:
        page = original.new_page(width=420, height=600)
        page.insert_text((40, 80), 'Raster-only evidence: 17.')
        image = page.get_pixmap().tobytes('png')
    with pymupdf.open() as scan:
        for _ in range(count):
            page = scan.new_page(width=420, height=600)
            page.insert_image(page.rect, stream=image)
        return scan.tobytes()


class DocumentStructureTests(unittest.TestCase):
    def test_markdown_structure_preserves_original_identity_and_positions(self):
        text = '# Heading\r\n\r\n| Name | Unit |\r\n| --- | --- |\r\n| A | 7 |\r\n\r\n![plot](figure.png)'
        source = Source('legacy', Path('example.md'), text)
        self.assertEqual(source.document.format, 'markdown')
        self.assertEqual(source.document.text, text)
        table = next(b for b in source.document.blocks if b.kind == 'table')
        self.assertEqual((table.line_start, table.header_end, table.line_end), (3, 4, 5))
        self.assertIsNone(table.locations[0].page)
        self.assertEqual(source.document.assets[0].uri, 'figure.png')
        self.assertEqual(Document.from_dict(json.loads(json.dumps(source.document.to_dict()))), source.document)

    def test_pdf_structure_has_real_tables_and_page_locations(self):
        doc = parse_pdf(text_pdf(table=True), raw_file='fixture.pdf', config={**profile(), 'vlm': False})
        self.assertEqual(doc.page_count, 2)
        table = next(b for b in doc.blocks if b.kind == 'table')
        self.assertIn('Requests per second', doc.text)
        self.assertEqual(table.locations[0].page, 1)
        self.assertEqual(table.table_data['rows'][1], ['System-0', '100'])
        self.assertIsNone(table.locations[0].line_start)
        source = source_from_document(Path('fixture.pdf'), doc)
        chunks = chunk_source(source)
        validate_chunks(chunks, [source])
        self.assertTrue(next(c for c in chunks if c.kind == 'table').atomic)
        self.assertFalse(any(c.kind == 'page' for c in chunks))
        self.assertIn('Second-page evidence.', doc.text)

    def test_snapshot_validation_rejects_unmapped_ranges_and_assets(self):
        data = markdown_document('hello').to_dict()
        data['blocks'][0]['line_end'] = 99
        with self.assertRaises(ValueError):
            Document.from_dict(data)

    def test_scan_is_partial_and_does_not_invent_searchable_text(self):
        doc = parse_pdf(scan_pdf(), raw_file='scan.pdf', config={**profile(), 'vlm': False})
        self.assertEqual(doc.status, 'partial')
        self.assertIn('visual_content_needs_review', [i['code'] for i in doc.issues])
        self.assertNotIn('Raster-only evidence', doc.text)
        self.assertTrue(any(a.kind == 'figure' for a in doc.assets))

    def test_invalid_and_encrypted_pdfs_are_explicit_errors(self):
        for data in (b'%PDF-corrupt', b''):
            with self.assertRaises(ValueError):
                parse_pdf(data, raw_file='invalid.pdf', config={**profile(), 'vlm': False})
        with pymupdf.open() as doc:
            doc.new_page()
            data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='reader')
        with self.assertRaises(ValueError):
            parse_pdf(data, raw_file='locked.pdf', config={**profile(), 'vlm': False})


class TableWindowTests(unittest.TestCase):
    def setUp(self):
        self.source = Source('table-version', Path('table.md'), '# Capacity\n\n| System | Requests_per_second |\n| --- | --- |\n' + '\n'.join(f'| System_{i} | {100 + i} |' for i in range(30)))
        self.embedder = LocalEmbedder.__new__(LocalEmbedder)
        self.embedder.config = select_embedding_config()
        self.embedder.fits = lambda text: len(text) <= 190
        self.chunks = self.embedder.prepare_chunks(chunk_source(self.source, max_chars=200, min_chars=0))

    def test_every_table_window_keeps_header_mapping_and_complete_rows(self):
        table = [c for c in self.chunks if c.kind == 'table']
        self.assertGreater(len(table), 2)
        last = table[-1]
        self.assertTrue(last.atomic)
        self.assertIn('Requests_per_second', passage_text(last, ''))
        self.assertTrue(last.context_ranges)
        visible = with_table_context([last], [self.source])
        validate_chunks(visible, [self.source])
        self.assertEqual(len(visible), 3)
        self.assertIn('# Capacity', visible[0].text)
        header = next(c for c in visible if c.kind == 'table_header')
        self.assertIn('Requests_per_second', header.text)
        self.assertNotIn('Requests_per_second', last.text)
        self.assertGreater(last.line_start, header.line_end + 1)
        lexical = BM25Index(self.chunks).search('Requests_per_second', 50)
        self.assertIn(last.chunk_id, [hit.chunk.chunk_id for hit in lexical])

    def test_generation_budget_does_not_cut_a_table_row_or_drop_required_header(self):
        last = [c for c in self.chunks if c.kind == 'table'][-1]
        visible = with_table_context([last], [self.source])
        fitted = fit_chunks(visible, lambda chunks: sum(len(c.text) for c in chunks) <= len(last.text))
        self.assertNotIn(last, fitted)
        fitted = fit_chunks(list(reversed(visible)), lambda chunks: True)
        self.assertIn(last, fitted)
        self.assertIn(visible[0], fitted)
        self.assertEqual(fit_chunks([last], lambda chunks: True), [])

    def test_explicit_table_read_supplies_header_without_widening_contiguous_range(self):
        tools = KnowledgeTools([self.source], self.embedder.config)
        last = [c for c in self.chunks if c.kind == 'table'][-1]
        execution = tools.execute('read_source', {'source_id': self.source.source_id,
                                  'line_start': last.line_start, 'line_end': last.line_end})
        self.assertEqual(len(execution.visible_chunks), 3)
        validate_chunks(list(execution.visible_chunks), [self.source])
        self.assertIn('Requests_per_second', execution.output['supporting_context'])
        self.assertIn('# Capacity', execution.output['supporting_context'])

    def test_focus_on_a_late_row_keeps_header_as_separate_required_evidence(self):
        from agent_loop import document_focus
        focus = document_focus(self.source, 32, 33)
        self.assertTrue(focus.support_chunks)
        self.assertTrue(focus.chunk.atomic)
        validate_chunks([*focus.support_chunks, focus.chunk], [self.source])
        with self.assertRaises(ValueError):
            validate_chunks([focus.chunk], [self.source])


class PDFStorageAndWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes(); self.root = self.temp.__enter__()
        self.client = TestClient(create_app(self.root / 'uploads'))

    def tearDown(self):
        self.client.close()
        for folder in list(self.root.iterdir()):
            if folder.is_dir():
                self.assertTrue(folder.resolve().is_relative_to(self.root.resolve()))
                shutil.rmtree(folder)
        self.temp.__exit__(None, None, None)

    def upload(self, data=None):
        response = self.client.post('/api/documents', files=[('files', ('fixture.pdf', data or text_pdf(table=True), 'application/pdf'))])
        self.assertEqual(response.status_code, 201, response.text)
        identity = response.json()[0]['id']
        return identity, self.client.get('/api/documents/' + identity).json()

    def test_real_import_read_asset_and_stale_version_access(self):
        identity, doc = self.upload()
        self.assertEqual(doc['format'], 'pdf')
        self.assertIn('原始页面', doc['html'])
        self.assertEqual(doc['page_count'], 2)
        self.assertTrue(any(b['table_data'] for b in doc['blocks']))
        asset = doc['assets'][0]
        response = self.client.get(asset['url'])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'\x89PNG'))
        bad = self.client.get(asset['url'].replace(doc['source_id'], 'stale'))
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(self.client.get(f'/api/documents/{identity}/assets/missing?source_id={doc["source_id"]}').status_code, 404)

    def test_bad_pdf_in_batch_publishes_no_documents(self):
        before = self.client.get('/api/documents').json()
        response = self.client.post('/api/documents', files=[('files', ('valid.pdf', text_pdf(), 'application/pdf')),
            ('files', ('bad.pdf', b'%PDF-broken', 'application/pdf'))])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/documents').json(), before)

    def test_replace_pdf_keeps_original_and_old_snapshot_but_revokes_old_asset_url(self):
        identity, original = self.upload()
        replacement = text_pdf('Replacement fact: 81 requests.')
        response = self.client.put(f'/api/documents/{identity}?expected_source_id={original["source_id"]}',
                                  files={'file': ('new.pdf', replacement, 'application/pdf')})
        self.assertEqual(response.status_code, 200, response.text)
        current = self.client.get('/api/documents/' + identity).json()
        self.assertNotEqual(current['source_id'], original['source_id'])
        self.assertIn('Replacement fact', '\n'.join(current['lines']))
        self.assertEqual(self.client.get(original['assets'][0]['url']).status_code, 409)
        store = self.client.app.state.practice_store
        with store.transaction() as db:
            row = db.execute('SELECT snapshot_json FROM document_versions WHERE source_id=?', (original['source_id'],)).fetchone()
        saved = Document.from_dict(json.loads(row[0]))
        source = source_from_document(next((self.root / 'uploads').glob('*/fixture.pdf')), saved)
        self.assertTrue(raw_path(source).is_file())
        self.assertIn('System-0', saved.text)
        self.assertEqual(self.client.delete(f'/api/documents/{identity}?expected_source_id={current["source_id"]}').status_code, 200)
        self.assertEqual(self.client.get(current['assets'][0]['url']).status_code, 410)

    def test_cached_parse_is_reused_and_raw_hash_guards_visual_evidence(self):
        path = self.root / 'cached.pdf'; path.write_bytes(text_pdf())
        first = load_pdf(path)
        with patch('pdf_parser.parse_pdf', side_effect=AssertionError('cache must be reused')):
            second = load_pdf(path)
        self.assertEqual(first.parse_id, second.parse_id)
        source = source_from_document(path, first)
        archive = archived_sources([{'source_id': source.source_id, 'path': str(path), 'text': source.text, **snapshot_metadata(source)}])[0]
        self.assertEqual(archive.document, source.document)
        self.assertTrue(render_asset(source, 'page-1').startswith(b'\x89PNG'))
        path.write_bytes(text_pdf('Changed bytes.'))
        with self.assertRaises(ValueError):
            raw_path(source)

    def test_ask_reuses_pdf_snapshot_and_exposes_page_citation(self):
        from agent_loop import AgentRun
        identity, doc = self.upload()
        textblock = next(b for b in doc['blocks'] if b['kind'] == 'paragraph')
        answer = NoteAnswer.model_validate({'coverage': 'sufficient', 'coverage_note': '', 'blocks': [{
            'kind': 'grounded', 'text': 'Synthetic result.', 'citations': [{'source_id': doc['source_id'],
                'line_start': textblock['line_start'], 'line_end': textblock['line_end']}]}]})
        with patch('web_app.run_agent', return_value=AgentRun(answer, (), 'model_finished', ())):
            response = self.client.post('/api/ask', json={'document_ids': [identity], 'focus_document_id': identity,
                'focus_source_id': doc['source_id'], 'question': 'What is the fact?'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['blocks'][0]['citations'][0]['locations'][0]['page'], 1)

    def test_reparse_publishes_new_snapshot_and_rejects_stale_request(self):
        identity, original = self.upload()
        with patch('pdf_parser.PARSER_VERSION', 'test-reparse-v2'):
            response = self.client.post(f'/api/documents/{identity}/reparse?expected_source_id={original["source_id"]}')
        self.assertEqual(response.status_code, 200, response.text)
        current = self.client.get('/api/documents/' + identity).json()
        self.assertNotEqual(current['source_id'], original['source_id'])
        self.assertEqual(current['lines'], original['lines'])
        with patch('pdf_parser.parse_pdf', side_effect=AssertionError('stale requests must not parse')):
            response = self.client.post(f'/api/documents/{identity}/reparse?expected_source_id={original["source_id"]}')
        self.assertEqual(response.status_code, 409)


class VisualFallbackTests(unittest.TestCase):
    def test_fallback_is_bounded_and_preserves_unresolved_pages(self):
        config = {**profile(), 'vlm': True}
        transcription = PageTranscription(blocks=[{'kind': 'paragraph', 'text': 'Visible transcribed fact.'}], unresolved=[])
        with patch('pdf_vision.transcribe_page', return_value=transcription) as call:
            doc = parse_pdf(scan_pdf(4), raw_file='scan.pdf', config=config)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(sum(b.method == 'vlm_transcription' for b in doc.blocks), 3)
        self.assertIn('vlm_page_budget_exhausted', [i['code'] for i in doc.issues])
        self.assertTrue(any(i['page'] == 4 for i in doc.issues))
        self.assertEqual(doc.status, 'partial')
        self.assertEqual(len(doc.attempts), 3)
        self.assertTrue(doc.attempts[0]['original_items'])
        self.assertEqual(doc.attempts[0]['candidate']['blocks'][0]['text'], 'Visible transcribed fact.')

    def test_fallback_error_keeps_original_and_explicit_error_type(self):
        with patch('pdf_vision.transcribe_page', side_effect=RuntimeError('private transport detail')):
            doc = parse_pdf(scan_pdf(), raw_file='scan.pdf', config={**profile(), 'vlm': True})
        self.assertIn('vlm_failed', [i['code'] for i in doc.issues])
        self.assertNotIn('private transport detail', json.dumps(doc.to_dict()))
        self.assertTrue(any(a.kind == 'page' for a in doc.assets))

    def test_docling_failure_retains_explicitly_labelled_basic_candidate(self):
        with patch('pdf_parser.docling_pages', side_effect=RuntimeError('not installed in test')):
            doc = parse_pdf(text_pdf(), raw_file='fixture.pdf', config={**profile(), 'engine': 'docling', 'vlm': False})
        self.assertEqual(doc.status, 'partial')
        self.assertTrue(any(i['code'] == 'docling_failed' for i in doc.issues))
        self.assertIn('Synthetic fact', doc.text)
        self.assertEqual(next(b for b in doc.blocks if b.kind == 'paragraph').method, 'pymupdf')

    def test_figure_navigation_cannot_be_used_as_text_question_evidence(self):
        from practice_model import QuestionDraft, validate_question
        doc = parse_pdf(scan_pdf(), raw_file='scan.pdf', config={**profile(), 'vlm': False})
        source = source_from_document(Path('scan.pdf'), doc)
        chunk = next(c for c in chunk_source(source) if c.kind == 'figure')
        draft = QuestionDraft.model_validate({'objective': 'Read a diagram', 'question': 'What is its conclusion?',
            'rubric_points': ['Explain the conclusion', 'Give its condition'], 'citations': [
                {'source_id': source.source_id, 'line_start': chunk.line_start, 'line_end': chunk.line_end}]})
        with self.assertRaises(ValueError):
            validate_question(draft, [source], [chunk])

    def test_visual_citation_requires_successful_receipt(self):
        with temporary_notes() as root:
            path = root / 'fixture.pdf'; path.write_bytes(text_pdf())
            doc = parse_pdf(path.read_bytes(), raw_file=path.name, config={**profile(), 'vlm': False})
            source = source_from_document(path, doc)
            with patch('pdf_vision.vision_request', return_value=VisualReading(observation='The visible label says 42.', limitations='')) as call:
                receipt, block = inspect_visual(source, 'page-1', 'Read the number.')
            self.assertEqual(len(call.call_args.args[0]), 1)
            from chunking import block_chunk
            visible = [block_chunk(source, block)]
            answer = NoteAnswer.model_validate({'coverage': 'sufficient', 'coverage_note': '', 'blocks': [{
                'kind': 'grounded', 'text': '42 is visible.', 'citations': [{'source_id': source.source_id,
                    'line_start': block.line_start, 'line_end': block.line_end, 'asset_id': 'page-1'}]}]})
            with self.assertRaises(RuntimeError):
                validate_answer(answer, [source], chunks=visible)
            validate_answer(answer, [source], chunks=visible, visual_evidence=[receipt])
            with self.assertRaises(ValueError):
                inspect_visual(source, 'page-99', 'Read.')

    def test_image_budget_uses_separate_allowance_instead_of_base64_length(self):
        budget = VisionBudget()
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'A' * 200000}}
        usage = budget.usage([{'role': 'user', 'content': [image]}])
        self.assertLess(usage, 10000)
        self.assertGreater(usage, budget.image_allowance)

    def test_successful_visual_tool_reaches_final_request_and_persisted_receipt(self):
        import httpx
        from openai import OpenAI
        from agent_loop import run_agent
        from test_agent_loop import AgentLoopTests
        from structured_output import FUNCTION_OUTPUT_PROVIDERS
        FUNCTION_OUTPUT_PROVIDERS.clear()
        with temporary_notes() as root:
            path = root / 'fixture.pdf'; path.write_bytes(text_pdf())
            source = source_from_document(path, parse_pdf(path.read_bytes(), raw_file=path.name, config={**profile(), 'vlm': False}))
            tools = KnowledgeTools([source], select_embedding_config())
            requests = []
            def respond(request):
                body = json.loads(request.content); requests.append(body)
                if len(requests) == 1:
                    self.assertIn('inspect_visual', [t['function']['name'] for t in body['tools']])
                    return AgentLoopTests.completion({'role': 'assistant', 'content': None, 'tool_calls': [
                        AgentLoopTests.call('visual', 'inspect_visual', {'source_id': source.source_id, 'asset_id': 'page-1', 'question': 'Read the number.'})]}, 'tool_calls')
                if len(requests) == 2:
                    self.assertIn('VISUAL_RESULT_42', str(body['messages']))
                    return AgentLoopTests.completion({'role': 'assistant', 'content': 'Evidence acquired.'})
                self.assertIn('VISUAL_RESULT_42', str(body['messages']))
                answer = {'coverage': 'sufficient', 'coverage_note': '', 'blocks': [{'kind': 'grounded', 'text': '42 is visible.',
                    'citations': [{'source_id': source.source_id, 'line_start': 1, 'line_end': 1, 'asset_id': 'page-1'}]}]}
                return AgentLoopTests.completion({'role': 'assistant', 'content': json.dumps(answer)})
            transport = httpx.MockTransport(respond)
            def client(**options):
                return OpenAI(api_key='test', http_client=httpx.Client(transport=transport))
            with patch('agent_loop.OpenAI', side_effect=client), patch('model_client.OpenAI', side_effect=client), \
                 patch('agent_loop.model_settings', return_value=('test', {})), patch('model_client.model_settings', return_value=('test', {})), \
                 patch('pdf_vision.enabled', return_value=True), patch('pdf_vision.vision_request', return_value=VisualReading(observation='VISUAL_RESULT_42', limitations='Synthetic observation.')):
                result = run_agent([source], 'Read this page.', tools, strict=True)
            self.assertEqual(len(requests), 3)
            self.assertEqual(result.answer.blocks[0].citations[0].asset_id, 'page-1')
            self.assertEqual(result.context_snapshot['visual_observations'][0]['observation'], 'VISUAL_RESULT_42')
        FUNCTION_OUTPUT_PROVIDERS.clear()


if __name__ == '__main__':
    unittest.main()
