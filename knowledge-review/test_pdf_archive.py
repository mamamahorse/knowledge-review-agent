"""File-backed PDF path, bounded readers, recovery and real API publication."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from pdf_archive import parse_pdf_stored
from pdf_parser import extract_page,raw_path
from source_reader import open_snapshot,snapshot,read_lines
from source_archive import ArchiveDocument
from test_pdf_documents import text_pdf
from test_support import temporary_notes


class PDFArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.addCleanup(self.clean_dirs)
        self.path=self.root/'source.pdf'; self.path.write_bytes(text_pdf(table=True))
        self.config=dict(engine='pymupdf',vlm=False)

    def clean_dirs(self):
        for path in self.root.iterdir():
            if path.is_dir(): shutil.rmtree(path)

    def parse(self,**kwargs):
        return parse_pdf_stored(self.path,self.root/'archive',config=self.config,**kwargs)

    def test_direct_archive_has_global_ranges_and_no_full_source_materialization(self):
        with patch.object(Path,'read_bytes',side_effect=AssertionError('full bytes')), \
             patch('source_reader.freeze',side_effect=AssertionError('full Source')):
            source=self.parse()
        self.assertFalse(hasattr(source,'text'))
        self.assertIsInstance(source.document,ArchiveDocument)
        first=source.document.page(1); second=source.document.page(2)
        self.assertEqual(first[1]+1,second[0])
        self.assertIn('Second-page',read_lines(source,*second))
        self.assertEqual(raw_path(source),self.path.resolve())
        restored=open_snapshot(snapshot(source))
        self.assertEqual(restored.source_id,source.source_id)
        from knowledge_tools import KnowledgeTools
        from embedding_config import select_embedding_config
        from context import validate_chunks
        with patch('knowledge_tools.range_chunks',side_effect=AssertionError('unbounded index')):
            whole=KnowledgeTools([source],select_embedding_config())
            self.assertEqual(whole.chunks,[])
        tools=KnowledgeTools([source],select_embedding_config(),reading_range=first)
        execution=tools.execute('read_source',dict(source_id=source.source_id,line_start=first[0],line_end=first[1]))
        validate_chunks(list(execution.visible_chunks),[source])

    def test_failed_second_page_resumes_only_it_and_sealed_result_is_reused(self):
        def fail(page):
            if page.number==1: raise RuntimeError('stop')
            return extract_page(page)
        with patch('pdf_parser.extract_page',side_effect=fail),self.assertRaises(RuntimeError): self.parse()
        build=next((self.root/'archive').glob('*.building.sqlite3'))
        with closing(sqlite3.connect(build)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM pages').fetchone()[0],1)
        with patch('pdf_parser.extract_page',wraps=extract_page) as extract:
            result=self.parse()
        self.assertEqual(extract.call_count,1)
        with patch('pdf_parser.extract_page',side_effect=AssertionError('sealed')):
            self.assertEqual(self.parse().source_id,result.source_id)

    def test_force_does_not_mutate_old_archive_and_damage_fails_range_read(self):
        first=self.parse(); original=first.archive.stat().st_mtime_ns
        again=self.parse(force=True)
        self.assertEqual(again.source_id,first.source_id)
        self.assertEqual(first.archive.stat().st_mtime_ns,original)
        with closing(sqlite3.connect(first.archive)) as db,db:
            db.execute("UPDATE lines SET text='broken' WHERE number=1")
        with self.assertRaisesRegex(ValueError,'损坏'): read_lines(first,1,1)

    def test_visual_unknown_attempt_is_not_silently_replayed(self):
        config=dict(engine='pymupdf',vlm=True)
        parsed=dict(items=[],issues=[dict(page=1,code='visual_content_needs_review')])
        with patch('pdf_parser.extract_page',return_value=parsed), \
             patch('pdf_vision.transcribe_page',side_effect=RuntimeError('network unknown')) as call:
            source=parse_pdf_stored(self.path,self.root/'archive',config=config)
        self.assertEqual(call.call_count,2)
        self.assertEqual(source.document.status,'partial')
        with patch('pdf_vision.transcribe_page',side_effect=AssertionError('no retry')):
            self.assertEqual(parse_pdf_stored(self.path,self.root/'archive',config=config).source_id,source.source_id)


class PDFArchiveWebTests(unittest.TestCase):
    def setUp(self):
        from test_pdf_documents import PDFStorageAndWebTests
        self.fixture=PDFStorageAndWebTests('runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client=self.fixture.client
        self.client.__enter__(); self.addCleanup(self.client.__exit__,None,None,None)
        self.addCleanup(self.client.app.state.stream_runner.pool.shutdown,wait=True)
        profile=patch('pdf_parser.profile',return_value=dict(engine='pymupdf',vlm=False))
        profile.start(); self.addCleanup(profile.stop)

    def test_import_paged_read_replace_reparse_and_history_version(self):
        from web_app import _source_for
        with patch.object(Path,'read_bytes',side_effect=AssertionError('whole file bytes')):
            response=self.client.post('/api/documents?import_id='+'a'*32,files={'files':('book.pdf',text_pdf(table=True),'application/pdf')})
        self.assertEqual(response.status_code,201,response.text)
        progress=self.client.get('/api/document-imports/'+'a'*32).json()
        self.assertEqual((progress['status'],progress['completed_pages'],progress['total_pages']),('completed',2,2))
        identity=response.json()[0]['id']
        first=self.client.get('/api/documents/'+identity).json()
        self.assertTrue(first['paged']); self.assertEqual(first['page'],1)
        second=self.client.get('/api/documents/'+identity+'?page=2').json()
        self.assertGreater(second['window_start'],first['window_end'])
        self.assertIn('Second-page','\n'.join(second['lines']))
        self.assertEqual(self.client.get('/api/documents/'+identity+'?page=3').status_code,400)
        image=self.client.get(second['assets'][0]['url'])
        self.assertEqual(image.status_code,200)
        old_id=first['source_id']
        changed=self.client.put('/api/documents/'+identity,params=dict(expected_source_id=old_id),
            files={'file':('book.pdf',text_pdf('Changed fact'),'application/pdf')})
        self.assertEqual(changed.status_code,200,changed.text)
        current=self.client.get('/api/documents/'+identity).json()
        self.assertNotEqual(current['source_id'],old_id)
        reparse=self.client.post('/api/documents/'+identity+'/reparse',params=dict(expected_source_id=current['source_id']))
        self.assertEqual(reparse.status_code,200,reparse.text)
        with self.client.app.state.practice_store.transaction() as db:
            old=db.execute('SELECT snapshot_json,text FROM document_versions WHERE source_id=?',(old_id,)).fetchone()
        self.assertEqual(old['text'],'')
        self.assertEqual(open_snapshot(json.loads(old['snapshot_json'])).source_id,old_id)

    def test_new_archive_learning_pipeline_uses_actual_source_and_commits_question(self):
        from test_learning_batches import controlled
        from structured_recovery import current_guard
        from test_agent_governance import wait_for
        response=self.client.post('/api/documents',files={'files':('book.pdf',text_pdf(),'application/pdf')})
        identity=response.json()[0]['id']; doc=self.client.get('/api/documents/'+identity).json()
        def model(client,name,messages,schema,**kwargs):
            data=json.loads(messages[1]['content'])
            if not data['evidence']:
                current_guard()()
                step=schema.model_validate(dict(action='read',tool='read_source',arguments_json=json.dumps(dict(
                    source_id=data['source']['source_id'],line_start=1,line_end=doc['line_count']))))
                kwargs['validator'](step); return step
            if data.get('stage','planning')!='planning':
                data['evidence']={k:v for k,v in data['evidence'].items() if not v.get('text','').startswith('## 第')}
                messages=[messages[0],{**messages[1],'content':json.dumps(data)}]
            return controlled(client,name,messages,schema,**kwargs)
        with patch('learning_planner.model_settings',return_value=('test',dict(api_key='test'))), \
             patch('learning_planner.request_structured',side_effect=model):
            response=self.client.post(f'/api/documents/{identity}/learning-plans',json=dict(source_id=doc['source_id'],request_id='archive'))
            self.assertEqual(response.status_code,202,response.text)
            job=response.json()['id']; store=self.client.app.state.planning_store
            wait_for(lambda:store.get(job)['status'] in ('completed','failed'))
            self.assertEqual(store.get(job)['status'],'completed',store.get(job)['error'])
            wait_for(lambda:('planning',job) not in self.client.app.state.stream_runner.keys)
            self.assertEqual(self.client.post(f'/api/learning-plans/{job}/questions',json=dict(goal_id='g')).status_code,202)
            wait_for(lambda:bool(store.get(job)['questions']))
        session=store.practice.get(store.get(job)['questions']['g']['session_id'])
        self.assertEqual(session['snapshots'][0]['reader']['version'],2)

    def test_full_pool_rejects_parse_without_publishing_document(self):
        runner=self.client.app.state.stream_runner
        leases=[runner._reserve('request') for _ in range(runner.workers)]
        before=self.client.get('/api/documents').json()
        try:
            response=self.client.post('/api/documents?import_id='+'b'*32,files={'files':('book.pdf',text_pdf(),'application/pdf')})
            self.assertEqual(response.status_code,503,response.text)
        finally:
            for lease in leases: runner._release(lease)
        self.assertEqual(self.client.get('/api/documents').json(),before)
        self.assertEqual(self.client.get('/api/document-imports/'+'b'*32).json()['status'],'interrupted')


if __name__=='__main__': unittest.main()
