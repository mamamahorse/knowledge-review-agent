"""PDF VLM workflows share chat capacity and cannot publish after cancellation."""
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from pdf_vision import PageTranscription
from test_pdf_documents import scan_pdf
from test_support import temporary_notes
from web_app import create_app


TRANSCRIPTION = PageTranscription(
    blocks=[{'kind': 'paragraph', 'text': 'Public raster evidence: 17.'}], unresolved=[],
)


class PdfAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_notes()
        self.root = self.directory.__enter__()
        self.environment = patch.dict(os.environ, {
            'KNOWLEDGE_REVIEW_MODEL_WORKERS': '1', 'PDF_PARSER': 'pymupdf', 'PDF_VLM_ENABLED': '0',
        })
        self.environment.start()
        self.dotenv = patch('pdf_parser.load_dotenv')
        self.dotenv.start()
        self.app = create_app(self.root / 'uploads')
        self.runner = self.app.state.stream_runner
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.pdf = scan_pdf()
        doc = self.client.get('/api/documents/example-retrieval-demo').json()
        self.question = {'question': 'Public question', 'document_ids': [doc['id']],
                         'focus_document_id': doc['id'], 'focus_source_id': doc['source_id']}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.runner.pool.shutdown(wait=True)
        self.dotenv.stop()
        self.environment.stop()
        for folder in self.root.iterdir():
            if folder.is_dir():
                shutil.rmtree(folder)
        self.directory.__exit__(None, None, None)

    def upload(self, name='public.pdf'):
        return self.client.post('/api/documents', files={'files': (name, self.pdf, 'application/pdf')})

    def test_full_chat_pool_rejects_all_three_vlm_paths_without_publishing(self):
        identity = self.upload().json()[0]['id']
        original = self.client.get(f'/api/documents/{identity}').json()
        files_before = sorted(p.relative_to(self.root) for p in self.root.rglob('*') if p.is_file())
        gate, started = threading.Event(), threading.Event()
        def occupy():
            started.set()
            gate.wait(5)
        future = self.runner._submit(occupy)
        self.assertTrue(started.wait(2))
        try:
            with patch.dict(os.environ, PDF_VLM_ENABLED='1'), patch('pdf_vision.transcribe_page') as transcribe:
                responses = [
                    self.upload('rejected.pdf'),
                    self.client.put(f'/api/documents/{identity}', params={'expected_source_id': original['source_id']},
                                    files={'file': ('replacement.pdf', self.pdf, 'application/pdf')}),
                    self.client.post(f'/api/documents/{identity}/reparse',
                                     params={'expected_source_id': original['source_id']}),
                ]
                for response in responses:
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(response.headers['retry-after'], '1')
                transcribe.assert_not_called()
            self.assertEqual(self.client.get(f'/api/documents/{identity}').json()['source_id'], original['source_id'])
            self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob('*') if p.is_file()), files_before)
            self.assertEqual(self.runner.pool._work_queue.qsize(), 0)
        finally:
            gate.set()
            future.result(3)

    def test_vlm_holds_chat_capacity_until_actual_completion(self):
        gate, started = threading.Event(), threading.Event()
        def transcribe(image):
            started.set()
            gate.wait(5)
            return TRANSCRIPTION
        with patch.dict(os.environ, PDF_VLM_ENABLED='1'), patch('pdf_vision.transcribe_page', side_effect=transcribe):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.upload)
                try:
                    self.assertTrue(started.wait(3))
                    self.assertEqual(self.runner.snapshot()['held_slots'], 1)
                    self.assertEqual(self.client.post('/api/ask', json=self.question).status_code, 503)
                    self.assertEqual(self.upload('second.pdf').status_code, 503)
                    self.assertEqual(self.client.get('/healthz').status_code, 200)
                    self.assertEqual(self.runner.pool._work_queue.qsize(), 0)
                finally:
                    gate.set()
                response = future.result(5)
                self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.runner.snapshot()['held_slots'], 0)
        identity = response.json()[0]['id']
        self.assertIn('Public raster evidence: 17.', self.client.get(f'/api/documents/{identity}').json()['lines'])

    def test_local_only_pdf_does_not_need_a_model_slot(self):
        gate, started = threading.Event(), threading.Event()
        def occupy():
            started.set()
            gate.wait(5)
        future = self.runner._submit(occupy)
        self.assertTrue(started.wait(2))
        try:
            self.assertEqual(self.upload().status_code, 201)
        finally:
            gate.set()
            future.result(3)

    def test_vlm_failure_keeps_partial_parse_and_releases_capacity(self):
        with patch.dict(os.environ, PDF_VLM_ENABLED='1'), patch('pdf_vision.transcribe_page', side_effect=RuntimeError('public failure')):
            response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        doc = self.client.get('/api/documents/' + response.json()[0]['id']).json()
        self.assertTrue(any(issue['code'] == 'vlm_failed' for issue in doc['issues']))
        self.assertEqual(self.runner.snapshot()['held_slots'], 0)

    def test_vlm_success_replaces_and_reparses_with_current_version_checks(self):
        def result(number):
            return PageTranscription(blocks=[{'kind': 'paragraph', 'text': f'Public revision {number}.'}], unresolved=[])
        with patch.dict(os.environ, PDF_VLM_ENABLED='1'), \
                patch('pdf_vision.transcribe_page', side_effect=[result(1), result(2), result(3)]) as transcribe:
            identity = self.upload().json()[0]['id']
            first = self.client.get(f'/api/documents/{identity}').json()
            replaced = self.client.put(f'/api/documents/{identity}', params={'expected_source_id': first['source_id']},
                                       files={'file': ('replacement.pdf', self.pdf, 'application/pdf')})
            self.assertEqual(replaced.status_code, 200, replaced.text)
            second = self.client.get(f'/api/documents/{identity}').json()
            self.assertNotEqual(first['source_id'], second['source_id'])
            reparsed = self.client.post(f'/api/documents/{identity}/reparse', params={'expected_source_id': second['source_id']})
            self.assertEqual(reparsed.status_code, 200, reparsed.text)
            third = self.client.get(f'/api/documents/{identity}').json()
            self.assertIn('Public revision 3.', third['lines'])
            self.assertNotEqual(second['source_id'], third['source_id'])
            stale = self.client.post(f'/api/documents/{identity}/reparse', params={'expected_source_id': second['source_id']})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(transcribe.call_count, 3)
        self.assertEqual(self.runner.snapshot()['held_slots'], 0)


    def test_disconnect_keeps_slot_until_exit_and_never_imports(self):
        gate, started = threading.Event(), threading.Event()
        disconnected, observed = threading.Event(), threading.Event()
        async def is_disconnected(request):
            if disconnected.is_set():
                observed.set()
                return True
            return False
        def transcribe(image):
            started.set()
            gate.wait(5)
            # Simulate a local operation returning after disconnect. The parse
            # boundary must catch cancellation even without another model call.
            return TRANSCRIPTION
        with patch.dict(os.environ, PDF_VLM_ENABLED='1'), \
                patch('pdf_vision.transcribe_page', side_effect=transcribe), \
                patch('starlette.requests.Request.is_disconnected', new=is_disconnected):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.upload)
                try:
                    self.assertTrue(started.wait(3))
                    disconnected.set()
                    self.assertTrue(observed.wait(2))
                    self.assertEqual(self.runner.snapshot()['held_slots'], 1)
                    self.assertEqual(self.client.post('/api/ask', json=self.question).status_code, 503)
                finally:
                    gate.set()
                self.assertEqual(future.result(5).status_code, 499)
        self.assertEqual(self.runner.snapshot()['held_slots'], 0)
        self.assertFalse(any(doc['id'].startswith('upload-') for doc in self.client.get('/api/documents').json()))


if __name__ == '__main__':
    unittest.main()
