"""Production scope, lazy search, budget/citation and retry compatibility checks.

Controlled vectors/transports verify engineering, not semantic quality.
"""
import copy
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import httpx
from openai import OpenAI

from agent_loop import AgentRun
from answers import NoteAnswer, validate_answer
from conversation_store import ConversationBusy
from embedding_config import select_embedding_config
from generation_budget import GenerationBudget
from agent_loop import run_agent
from hybrid_retrieval import BM25Index
from knowledge_tools import KnowledgeTools
from quality_retrieval import main, run_hybrid
from retrieval_benchmark_data import ReviewStore, load_suite, make_manifest
from sources import Source
from test_support import temporary_notes
import test_agent_loop
import test_conversations
import test_retrieval_benchmark
import test_web_app


class ProductionToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes();self.root=self.temp.__enter__()
        self.embedder=test_retrieval_benchmark.FixtureEmbedder()
        self.source=Source('current',self.root/'current.md','# Token\n\njob_key locks one request.\n\n# Lease\n\nfence_token blocks late writes.\n\n# Cache\n\ncache_key shares computed data.')

    def tearDown(self):self.temp.__exit__(None,None,None)

    def tool(self,sources=None):
        return KnowledgeTools(sources or [self.source],self.embedder.config,embedder=self.embedder,
                              retrieval_mode='hybrid',min_chars=0,no_cache=True)

    def test_lazy_read_then_search_reuses_exact_instance_bm25(self):
        with patch('knowledge_tools.LocalEmbedder',side_effect=AssertionError('lazy')),patch('knowledge_tools.BM25Index',wraps=BM25Index) as index:
            lazy=KnowledgeTools([self.source],self.embedder.config,retrieval_mode='hybrid',min_chars=0)
            lazy.read_source('current',1,3)
            self.assertIsNone(lazy._bm25);index.assert_not_called()
            tool=self.tool();tool.search_notes('job_key');tool.search_notes('cache_key')
            self.assertEqual(index.call_count,1)
            self.assertTrue(tool.retrieval_trace[0]['bm25_built'])
            self.assertFalse(tool.retrieval_trace[1]['bm25_built'])

    def test_original_text_topk_scope_and_score_units(self):
        tool=self.tool();execution=tool.execute('search_notes',{'query':'fence_token','top_k':1})
        self.assertEqual(len(execution.visible_chunks),1)
        self.assertEqual(execution.output['score_kind'],'rrf')
        hit=execution.output['hits'][0]
        self.assertEqual(hit['text'],'\n'.join(self.source.text.splitlines()[hit['line_start']-1:hit['line_end']]))
        self.assertAlmostEqual(hit['score'],sum(b['rrf_contribution'] for b in hit['branches'].values()))
        self.assertNotIn('query',tool.retrieval_trace[0])
        second=Source('new',self.root/'new.md','# Token\n\njob_key is a different version.')
        newer=self.tool([second]);newer.search_notes('job_key')
        self.assertEqual(newer.retrieval_trace[0]['source_ids'],['new'])
        self.assertEqual(len(newer._bm25.chunks),1)
        with self.assertRaises(ValueError):newer.read_source('current',1,3)

    def test_production_top10_matches_independent_frozen_protocol_b(self):
        suite,sources=load_suite(Path(__file__).parent/'evals/retrieval/expanded-v1/suite.json')
        suite=copy.deepcopy(suite);suite['queries']=suite['queries'][20:22]+suite['queries'][-3:-2]
        manifest=make_manifest(suite,sources,self.embedder)
        rows=run_hybrid(manifest,sources,self.embedder,self.root/'vectors.sqlite3',repeats=1)
        for q,row in zip(suite['queries'],rows):
            tool=KnowledgeTools([s for s in sources if s.path.name in q['scope']],self.embedder.config,
                                embedder=self.embedder,index_path=self.root/'vectors.sqlite3',retrieval_mode='hybrid')
            result=tool.search_notes(q['text'],10)
            self.assertEqual([h.chunk.chunk_id for h in result.hits],[h['chunk_id'] for h in row['hits'][:10]])
            for h,expected in zip(result.hits,row['hits']):self.assertAlmostEqual(h.score,expected['score'])

    def test_no_lexical_hits_preserve_dense_order_failure_no_fallback(self):
        tool=self.tool();result=tool.search_notes('!?!',3)
        self.assertEqual(tool.retrieval_trace[0]['bm25_candidates'],0)
        self.assertEqual([h.chunk.chunk_id for h in result.hits],[c.chunk_id for c in tool.chunks])
        with patch.object(tool._bm25,'search',side_effect=ValueError('controlled failure')):
            with self.assertRaises(ValueError):tool.search_notes('job_key')
        self.assertEqual(tool.retrieval_trace[-1]['status'],'failed')
        self.assertNotIn('returned',tool.retrieval_trace[-1])


class ProductionWebTests(unittest.TestCase):
    def setUp(self):test_conversations.ConversationTests.setUp(self)
    def tearDown(self):test_conversations.ConversationTests.tearDown(self)
    def completed(self,*args,**kwargs):return test_conversations.ConversationTests.completed(self,*args,**kwargs)
    def submit(self,*args,**kwargs):return test_conversations.ConversationTests.submit(self,*args,**kwargs)

    def test_modes_validate_persist_and_retry_same_turn(self):
        url=f'/api/conversations/{self.identity}/turns'
        request={**self.request,'request_id':'hybrid','retrieval_mode':'hybrid'}
        def failed_run(sources,question,tools,**kwargs):
            tools.retrieval_trace.append({'mode':'hybrid','status':'failed','error_type':'RuntimeError'})
            raise RuntimeError('controlled')
        with patch('web_app.run_agent',side_effect=failed_run):
            failed=self.client.post(url,json=request).json()
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['context']['retrieval']['mode'],'hybrid')
        self.assertEqual(failed['context']['retrieval']['searches'][0]['status'],'failed')
        self.assertEqual(self.store.turn(failed['id'])['request']['retrieval_mode'],'hybrid')
        run=AgentRun(NoteAnswer.model_validate(test_conversations.answer_data()),(),'model_finished',())
        with patch('web_app.run_agent',return_value=run) as agent:
            retry=self.client.post(url+f"/{failed['id']}/retry",json={'retrieval_mode':'dense'}).json()
            self.assertEqual(agent.call_args.args[2].retrieval_mode,'dense')
            self.assertEqual(retry['messages'][-1]['result']['retrieval']['mode'],'dense')
            replay=self.client.post(url,json=request).json()
            self.assertEqual(replay['messages'],retry['messages']);agent.assert_called_once()
        self.assertEqual(len(self.store.turns(self.identity)),1)
        self.assertEqual(len(retry['messages']),2)
        self.assertEqual(self.client.post(url,json={**request,'request_id':'c','retrieval_mode':'rerank'}).status_code,422)

    def test_legacy_request_missing_mode_replays_without_archival_rewrite(self):
        legacy={**self.request,'line_start':None,'line_end':None}
        with self.store.generation_lock(self.identity):
            old=self.store.begin(self.identity,'legacy',legacy)
            self.store.fail(old['id'],old['attempt'],'controlled old failure')
        with patch('web_app.run_agent',return_value=AgentRun(NoteAnswer.model_validate(test_conversations.answer_data()),(),'model_finished',())) as agent:
            result=self.client.post(f'/api/conversations/{self.identity}/turns',json={**self.request,'request_id':'legacy'}).json()
            self.assertEqual(result['status'],'completed')
            self.assertEqual(agent.call_args.args[2].retrieval_mode,'dense')
        self.assertNotIn('retrieval_mode',self.store.turn(old['id'])['request'])
        with self.assertRaises(ConversationBusy):self.store.begin(self.identity,'legacy',{**legacy,'retrieval_mode':'hybrid'})
        with self.assertRaises(ConversationBusy):self.store.begin(self.identity,'legacy',{**legacy,'strict':0})

    def test_selected_mode_zero_tools_never_loads_embedding(self):
        # Exercise the actual two-phase Agent with a controlled transport.
        self.request['retrieval_mode']='hybrid'
        test_conversations.ConversationTests.test_both_model_phases_receive_same_chat_background_without_search(self)


class ProductionBudgetTests(unittest.TestCase):
    def test_hybrid_topk_trimmed_to_actual_visible_lines_in_both_requests(self):
        source=Source('long',Path('public.md'),'\n'.join(f'Line {i}: '+'detail '*30 for i in range(1,100)))
        embedder=test_retrieval_benchmark.FixtureEmbedder()
        tool=KnowledgeTools([source],embedder.config,embedder=embedder,no_cache=True,retrieval_mode='hybrid')
        budget=GenerationBudget(8000,500,500)
        calls=[]
        def respond(request):
            body=json.loads(request.content);calls.append(body)
            self.assertTrue(budget.fits(body['messages'],**{k:v for k,v in body.items() if k!='messages'}))
            if len(calls)==1:
                return test_agent_loop.AgentLoopTests.completion({'role':'assistant','content':None,'tool_calls':[
                    test_agent_loop.AgentLoopTests.call('s','search_notes',{'query':'detail','top_k':10})]},'tool_calls')
            if len(calls)==2:return test_agent_loop.AgentLoopTests.completion({'role':'assistant','content':'READY'})
            self.assertIn('Completed evidence actions',body['messages'][1]['content'])
            self.assertIn('"name": "search_notes", "status": "ok"',body['messages'][1]['content'])
            return test_agent_loop.AgentLoopTests.completion({'role':'assistant','content':json.dumps({
                'coverage':'sufficient','coverage_note':'','blocks':[{'kind':'grounded','text':'First line',
                'citations':[{'source_id':'long','line_start':1,'line_end':1}]}]})})
        def client(**kwargs):return OpenAI(api_key='controlled',http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        with patch('agent_loop.OpenAI',side_effect=client),patch('model_client.OpenAI',side_effect=client), \
             patch('agent_loop.model_settings',return_value=('controlled',{})),patch('model_client.model_settings',return_value=('controlled',{})):
            run=run_agent([source],'First line?',tool,strict=True,budget=budget)
        trace=run.context_snapshot['retrieval']['searches'][0]
        self.assertLess(max(r[2] for r in trace['agent_visible_ranges']),max(r['line_end'] for r in trace['returned']))
        final=next(r for r in run.context_snapshot['generation_requests'] if r['phase']=='final')['visible_ranges']
        self.assertTrue(final)
        bad=NoteAnswer.model_validate({'coverage':'sufficient','coverage_note':'','blocks':[{'kind':'grounded','text':'Unseen',
                     'citations':[{'source_id':'long','line_start':99,'line_end':99}]}]})
        with self.assertRaises(RuntimeError):validate_answer(bad,[source],chunks=list(run.visible_chunks))


class AutomaticAuditTests(unittest.TestCase):
    def test_agent_adjudication_exclusion_freeze_keeps_user_and_original(self):
        with temporary_notes() as root:
            manifest=test_retrieval_benchmark.fixture(root);store=ReviewStore(root/'review.sqlite3')
            identity=store.create(manifest)
            initial=test_retrieval_benchmark.full_audit(manifest,pending=['positive','negative'])
            store.adopt_agent_audit(identity,initial)
            audit=copy.deepcopy(initial);audit['version']='Agent adjudicated with reason'
            audit['cases'][0]['decision']='confident'
            audit['cases'][1]['decision']='excluded'
            audit['cases'][1]['reason']='No reliable relevance interpretation; excluded explicitly'
            with self.assertRaises(ValueError):store.adopt_agent_audit(identity,audit,{'positive':1,'negative':1})
            result=store.adopt_agent_audit(identity,audit,{'positive':1,'negative':1},resolve_agent_disputes=True)
            self.assertEqual(result['corrected_agent'],2)
            self.assertEqual(store.review(identity,'positive')['payload']['origin'],'agent')
            self.assertEqual(store.review(identity,'negative')['payload']['status'],'excluded')
            out=StringIO()
            with redirect_stdout(out),patch('quality_retrieval.LocalEmbedder',side_effect=AssertionError('no models')):
                main(['--review-db',str(store.path),'freeze','--benchmark',identity,'--note','Agent decisions, not human gold'])
            frozen=store.frozen(json.loads(out.getvalue())['freeze'])
            self.assertEqual(frozen['cases'][0]['status'],'auto_accepted')
            payload=store.review(identity,'positive')['payload'];payload['status']='approved'
            store.save(identity,'positive',payload,2)
            with self.assertRaises(ValueError):store.adopt_agent_audit(identity,audit,{'positive':3},resolve_agent_disputes=True)
            self.assertEqual(store.agent_audit_case(identity,'positive',initial.get('id') or frozen['cases'][0]['audit_id'])['decision'],'confident')


if __name__=='__main__':unittest.main()
