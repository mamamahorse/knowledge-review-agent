from contextlib import closing
import json
import os
import shutil
import sqlite3
import time
import unittest
from unittest.mock import patch
import httpx

from test_support import temporary_notes
from test_pdf_documents import text_pdf
from test_archive_retrieval import Embedder
from pdf_archive import parse_pdf_stored
from visual_discovery import RetrievalDiscovery
from pdf_vision import VisualReading
from visual_descriptions import run
from knowledge_tools import KnowledgeTools


class VisualDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.addCleanup(lambda:[shutil.rmtree(p) for p in self.root.iterdir() if p.is_dir()])
        path=self.root/'book.pdf'; path.write_bytes(text_pdf(table=True))
        self.source=parse_pdf_stored(path,self.root/'archive',config=dict(engine='pymupdf',vlm=False))
        self.db=self.root/'practice.sqlite3'
        settings=patch('pdf_vision.vision_settings',return_value=('test',dict(api_key='test')))
        settings.start(); self.addCleanup(settings.stop)

    def discovery(self,detail=False):
        return RetrievalDiscovery(status='reviewed',objects=[dict(kind='chart',label='Throughput',
            bbox=[.1,.1,.6,.6],description='Rareformula violet throughput plateau versus concurrency',
            needs_detail=detail,uncertainty='Need legend' if detail else '')],note='Inspected, not exhaustive')

    def execute(self,selected=None,limit=4):
        return run(self.source,self.db,selected or ['page-1'],limit,lambda value:None)

    def test_one_scan_cached_and_retrievable_without_granting_evidence(self):
        with patch('pdf_vision.vision_request',return_value=self.discovery()) as call:
            first=self.execute(); again=self.execute()
        self.assertEqual(call.call_count,1)
        self.assertEqual(first['status'],'completed'); self.assertEqual(again['reused'],1)
        embedder=Embedder()
        tools=KnowledgeTools([self.source],embedder.config,embedder=embedder,index_path=self.root/'vectors.sqlite3',
            object_db=self.db,retrieval_mode='hybrid')
        result=tools.execute('search_notes',dict(query='Rareformula violet',top_k=10))
        found=next(r for r in result.output['object_candidates'] if r['origin']=='visual_description')
        self.assertTrue(found['asset_id'].startswith('crop1-'))
        self.assertNotIn('evidence_id',found)
        self.assertTrue(all('Rareformula' not in c.text for c in result.visible_chunks))

    def test_failed_detail_retries_only_detail_and_model_selects_another_page(self):
        with patch('pdf_vision.vision_request',side_effect=[self.discovery(True),ValueError('broken schema')]):
            first=self.execute()
        self.assertEqual(first['status'],'partial')
        with patch('pdf_vision.vision_request',side_effect=[
            VisualReading(observation='Partial reading',limitations='',next_read='Need experimental conditions',suggested_pages=[2]),
            VisualReading(observation='Conditions on page two',limitations='')]) as call:
            second=self.execute()
        self.assertEqual(second['status'],'completed'); self.assertEqual(call.call_count,2)
        self.assertIn('page-2',second['units'][0]['read'])
        with closing(sqlite3.connect(self.db)) as db:
            records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM object_descriptions')]
        self.assertTrue(any(r['page']==2 and r['origin']=='visual_observation' for r in records))

    def test_actual_http_limit_stops_before_next_send_and_keeps_unknown_usage(self):
        from model_request_budget import attach
        sent=[]
        def model(*args):
            def transport(request):
                sent.append(request)
                return httpx.Response(200,json={'model':'test','usage':{'prompt_tokens':7,'completion_tokens':2}})
            with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                attach({'http_client':client})['http_client'].post('https://test.invalid/chat',json={'model':'test'})
            return self.discovery()
        with patch('pdf_vision.vision_request',side_effect=model):
            result=self.execute(['page-1','page-2'],limit=1)
        self.assertEqual(len(sent),1); self.assertEqual(result['completed'],1)
        self.assertEqual(result['status'],'partial'); self.assertEqual(result['unprocessed'],['page-2'])
        self.assertEqual(result['requests'][0]['input_tokens'],7)
        self.assertIsNone(result['requests'][0]['reasoning_tokens'])
        with patch('pdf_vision.vision_request',side_effect=model):
            resumed=self.execute(['page-1','page-2'],limit=1)
        self.assertEqual(resumed['status'],'completed'); self.assertEqual(resumed['reused'],1)

    def test_unknown_asset_and_stale_source_do_not_call_model(self):
        with patch('pdf_vision.vision_request') as call:
            with self.assertRaises(ValueError): self.execute(['page-999'])
            def changed(): raise ValueError('source changed')
            result=run(self.source,self.db,['page-1'],4,lambda value:None,guard=changed)
        self.assertEqual(result['status'],'failed'); call.assert_not_called()

    def test_uncertain_region_falls_back_to_actual_page_without_guessed_coordinates(self):
        value=self.discovery(); value.objects[0].bbox=None
        with patch('pdf_vision.vision_request',return_value=value): result=self.execute()
        obj=result['units'][0]['objects'][0]
        self.assertEqual(obj['asset_id'],'page-1'); self.assertIsNone(obj['bbox'])

    def test_successful_chat_visual_read_publishes_best_effort_navigation(self):
        tools=KnowledgeTools([self.source],Embedder().config,object_db=self.db)
        with patch('pdf_vision.vision_request',return_value=VisualReading(observation='Chat-only diagram finding',limitations='Partial')):
            tools.execute('inspect_visual',dict(source_id=self.source.source_id,asset_id='page-1',question='Read diagram'))
        with closing(sqlite3.connect(self.db)) as db:
            records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM object_descriptions')]
        self.assertEqual(len(records),1); self.assertIn('Chat-only',records[0]['description'])

    def test_unrelated_prior_observation_does_not_satisfy_requested_detail(self):
        from pdf_regions import select_region
        region=select_region(self.source,'page-1',[.1,.1,.6,.6])
        tools=KnowledgeTools([self.source],Embedder().config,object_db=self.db)
        with patch('pdf_vision.vision_request',return_value=VisualReading(observation='Only the title',limitations='Did not read the legend')):
            tools.execute('inspect_visual',dict(source_id=self.source.source_id,asset_id=region.asset_id,question='Read title only'))
        with patch('pdf_vision.vision_request',side_effect=[self.discovery(True),VisualReading(observation='Legend inspected',limitations='')]) as call:
            result=self.execute()
        self.assertEqual(result['status'],'completed'); self.assertEqual(call.call_count,2)
        self.assertIn('Only the title',call.call_args_list[0].args[1])

    def test_markdown_images_and_pdf_api_share_the_same_enrichment_path(self):
        from test_markdown_images import png
        from markdown_images import import_images
        from sources import load_sources
        from visual_descriptions import targets
        path=self.root/'note.md'; body='![chart](chart.png)'; path.write_text(body,encoding='utf-8')
        import_images(path,body,{'chart.png':png('blue')})
        source=load_sources([path])[0]; scope=targets(source)
        with patch('pdf_vision.vision_request',return_value=self.discovery()):
            result=run(source,self.db,[scope[0]['asset_id']],2,lambda value:None)
        self.assertEqual(result['status'],'completed'); self.assertIsNone(result['units'][0]['page'])

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from agent_stream import StreamRunner
        from types import SimpleNamespace
        from visual_description_api import install
        streams=StreamRunner(); app=FastAPI(lifespan=streams.lifespan)
        document=SimpleNamespace(revision=1)
        install(app,SimpleNamespace(get=lambda identity:document),lambda doc:self.source,
                SimpleNamespace(path=self.db),streams,self.root)
        with TestClient(app) as client,patch('pdf_vision.vision_request',return_value=self.discovery()) as model:
            listing=client.get('/api/documents/book/visual-descriptions').json()
            payload=dict(source_id=listing['source_id'],request_id='1'*32,asset_ids=['page-1'],max_requests=2)
            response=client.post('/api/documents/book/visual-descriptions',json=payload)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['status'],'completed')
            self.assertEqual(client.get('/api/visual-descriptions/'+'1'*32).json()['completed'],1)
            self.assertEqual(client.post('/api/documents/book/visual-descriptions',json=payload).status_code,409)
            self.assertEqual(model.call_count,1)
            from agent_cancel import GenerationCancelled
            model.side_effect=GenerationCancelled()
            payload.update(request_id='2'*32,asset_ids=['page-2'])
            self.assertEqual(client.post('/api/documents/book/visual-descriptions',json=payload).status_code,499)
            stopped=client.get('/api/visual-descriptions/'+'2'*32).json()
            self.assertEqual(stopped['status'],'interrupted')
            self.assertEqual(len(stopped['units']),1); self.assertIn('requests',stopped)

    def test_real_sdk_protocol_fallback_counts_failed_attempt_and_respects_limit(self):
        from openai import OpenAI
        from model_request_budget import attach
        from structured_output import FUNCTION_OUTPUT_PROVIDERS
        from structured_recovery import audit_scope
        sent=[]
        def transport(request):
            payload=json.loads(request.content); sent.append(payload)
            if 'response_format' in payload:
                return httpx.Response(400,json={'error':{'message':'response_format unsupported','type':'invalid_request_error'}})
            return httpx.Response(200,json=dict(id='test',object='chat.completion',created=0,model='test',
                choices=[dict(index=0,finish_reason='tool_calls',message=dict(role='assistant',content=None,
                    tool_calls=[dict(id='submit',type='function',function=dict(name='submit_result',arguments=self.discovery().model_dump_json()))]))],
                usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30)))
        def sdk(**options):
            options['http_client'].close()
            options['base_url']='https://test.invalid/v1'
            options.update(attach({'http_client':httpx.Client(transport=httpx.MockTransport(transport))}))
            return OpenAI(**options)
        with patch.dict(FUNCTION_OUTPUT_PROVIDERS,{},clear=True),audit_scope(self.root/'audit'),patch('pdf_vision.OpenAI',side_effect=sdk):
            limited=self.execute(limit=1)
            self.assertEqual(len(sent),1); self.assertEqual(limited['status'],'partial')
            self.assertEqual(limited['requests'][0]['http_status'],400)
            complete=self.execute(limit=2)
        self.assertEqual(complete['status'],'completed')
        self.assertEqual([r['http_status'] for r in complete['requests']],[400,200])
        self.assertEqual(complete['requests'][1]['output_tokens'],10)
        self.assertIsNone(complete['requests'][0]['input_tokens'])

    def test_cleanup_only_owned_stale_scratch_and_rebuildable_rows(self):
        from cache_maintenance import cleanup,temporary_use
        old=time.time()-10*86400
        scratch=self.root/('object-query-'+'a'*32+'.sqlite3'); scratch.write_text('temporary')
        original=self.root/'original.pdf'; original.write_text('preserve')
        for path in (scratch,original): os.utime(path,(old,old))
        with temporary_use(scratch): self.assertEqual(cleanup(self.root)['files'],0)
        self.assertTrue(scratch.exists())
        self.assertEqual(cleanup(self.root)['files'],1); self.assertTrue(original.exists())
        embedder=Embedder(); index=self.root/'vectors.sqlite3'
        tools=KnowledgeTools([self.source],embedder.config,embedder=embedder,index_path=index)
        tools.execute('search_notes',dict(query='Second-page',top_k=3))
        with closing(sqlite3.connect(index)) as db,db:
            db.execute('UPDATE ar_access SET last_used=?',(old,)); db.execute('UPDATE vector_access SET last_used=?',(old,))
        cleaned=cleanup(self.root,index)
        self.assertGreater(cleaned['index_scopes'],0); self.assertGreater(cleaned['vectors'],0)
        self.assertTrue(tools.execute('search_notes',dict(query='Second-page',top_k=3)).visible_chunks)


if __name__=='__main__': unittest.main()
