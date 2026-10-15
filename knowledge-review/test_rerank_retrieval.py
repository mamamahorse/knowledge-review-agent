"""Rerank engineering and real BEIR numeric fixtures, not real model semantics."""
import copy
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from quality_retrieval import main
from rerank_model import LocalReranker, RerankConfig
from rerank_retrieval import document_windows, rerank_pool, run_rerank, validate_hybrid_pool, validate_same_pool
from retrieval_benchmark_data import ReviewStore, digest
from retrieval_benchmark_metrics import score_run
from retrieval_comparison import compare_runs, comparison_markdown
from test_retrieval_benchmark import fixture, full_audit
from test_support import temporary_notes


class ControlledRanker:
    def __init__(self, config=RerankConfig(max_tokens=64)):
        self.config=config
        self.max_tokens=config.max_tokens
        self.inputs=[]

    def token_count(self,q,d):
        return len(q)+len(d)+3

    def predict(self,pairs):
        self.inputs.extend(pairs)
        return [10.0 if 'one' in d else 2.0 if 'three' in d else 1.0 for q,d in pairs]

    def profile(self):
        return {'controlled':True,'effective_max_tokens':self.max_tokens}


class RerankTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes()
        self.root=self.temp.__enter__()
        self.manifest=fixture(self.root)
        self.manifest['sources'][0]['text']+='\nseven\neight'
        self.manifest['chunks'].append({'id':'c3','file':'tcp.md','source_id':'source','line_start':7,'line_end':8,'text':'seven\neight'})
        self.manifest['fingerprint']=digest({k:v for k,v in self.manifest.items() if k!='fingerprint'})
        pool=[]
        for i in (1,2,3,0):
            c=self.manifest['chunks'][i]
            pool.append({'chunk_id':c['id'],'source_id':c['source_id'],'line_start':c['line_start'],
                         'line_end':c['line_end'],'text':c['text'],'rank':len(pool)+1,'score':1/(61+len(pool))})
        self.b={'system':'B_hybrid','id':'controlled-b','benchmark_fingerprint':self.manifest['fingerprint'],
                'protocol':{'candidate_m':50,'repeats':2,'quality_labels_used_during_retrieval':False},
                'queries':[{'query_id':q['id'],'scope':q['scope'],'status':'ok','hits':copy.deepcopy(pool),'warm_ms':[2.,3.]}
                           for q in self.manifest['suite']['queries']]}
        self.store=ReviewStore(self.root/'review.sqlite3')
        self.identity=self.store.create(self.manifest)
        self.b['benchmark_id']=self.identity

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def c_run(self,ranker=None):
        ranker=ranker or ControlledRanker()
        return {'system':'C_rerank','id':'controlled-c','benchmark_fingerprint':self.manifest['fingerprint'],
                'protocol':{'repeats':2,'quality_labels_used_during_retrieval':False},
                'input_b_run_digest':digest(self.b),'input_b_run':copy.deepcopy(self.b),
                'queries':run_rerank(self.manifest,self.b,ranker,repeats=2),'reranker':ranker.profile()}

    def freeze(self):
        self.store.adopt_agent_audit(self.identity,full_audit(self.manifest))
        return self.store.freeze(self.identity,'controlled actor','not real semantic labels',{'positive':1,'negative':1})

    def test_windows_cover_all_original_chars_and_repeat_whole_query(self):
        ranker=ControlledRanker(RerankConfig(max_tokens=12,max_windows=16))
        q='问答'
        text='第一行abcdef\n第二行ghijk\n末尾'
        spans=document_windows(q,text,ranker)
        self.assertGreater(len(spans),1)
        self.assertEqual(''.join(s['text'] for s in spans),text)
        self.assertTrue(all(s['tokens']<=12 for s in spans))
        for first,second in zip(spans,spans[1:]):
            self.assertEqual(first['char_end'],second['char_start'])

    def test_query_overflow_is_failure_not_truncation(self):
        ranker=ControlledRanker(RerankConfig(max_tokens=10))
        with self.assertRaises(ValueError): document_windows('abcdefg','one',ranker)
        self.assertEqual(ranker.inputs,[])

    def test_window_and_total_pair_limits_do_not_drop_tail_or_candidates(self):
        pool=self.b['queries'][0]['hits']
        with self.assertRaises(ValueError):
            rerank_pool('query',pool,ControlledRanker(RerankConfig(max_tokens=10,max_windows=1)))
        with self.assertRaises(ValueError):
            rerank_pool('query',pool,ControlledRanker(RerankConfig(max_pairs=2)))

    def test_scores_reorder_exact_pool_original_ranges_and_no_mutation(self):
        before=copy.deepcopy(self.b)
        c=self.c_run()
        self.assertEqual(self.b,before)
        self.assertEqual(c['queries'][0]['hits'][0]['chunk_id'],'c0')
        self.assertEqual(c['queries'][0]['hits'][0]['b_rank'],4)
        self.assertEqual(c['queries'][0]['hits'][0]['text'],'one\ntwo')
        validate_same_pool(self.manifest,self.b,c)

    def test_ties_preserve_B_order(self):
        ranker=ControlledRanker()
        ranker.predict=lambda pairs:[1.0]*len(pairs)
        hits=rerank_pool('query',self.b['queries'][0]['hits'],ranker)
        self.assertEqual([h['chunk_id'] for h in hits],[h['chunk_id'] for h in self.b['queries'][0]['hits']])

    def test_raw_complete_query_and_original_only_enter_model(self):
        ranker=ControlledRanker()
        self.c_run(ranker)
        texts={h['text'] for h in self.b['queries'][0]['hits']}
        self.assertTrue(all(q in ('positive','negative') and d in texts for q,d in ranker.inputs))

    def test_bad_scores_fail_instead_of_using_B_as_C(self):
        for values in ([float('nan')]*4,[1.]):
            ranker=ControlledRanker()
            ranker.predict=lambda pairs:values
            c=self.c_run(ranker)
            self.assertTrue(all(r['status']=='failed' and not r['hits'] for r in c['queries']))

    def test_B_missing_or_failed_queries_are_retained_without_new_search(self):
        self.b['queries'][0].update(status='failed',hits=[])
        self.b['queries'].pop()
        ranker=ControlledRanker()
        c=self.c_run(ranker)
        self.assertEqual(len(c['queries']),2)
        self.assertTrue(all(r['error']['type']=='InheritedRetrievalFailure' for r in c['queries']))
        self.assertEqual(ranker.inputs,[])

    def test_B_alien_text_duplicate_and_version_rejected_before_scoring(self):
        for change in ('scope','duplicate','text','version'):
            b=copy.deepcopy(self.b)
            if change=='scope': b['queries'][0]['scope']=['private.md']
            if change=='duplicate': b['queries'][0]['hits'][1]=copy.deepcopy(b['queries'][0]['hits'][0])
            if change=='text': b['queries'][0]['hits'][0]['text']='changed original'
            if change=='version': b['benchmark_fingerprint']='changed'
            with self.assertRaises(ValueError): validate_hybrid_pool(self.manifest,b)

    def test_changed_B_and_C_pool_or_window_rejected(self):
        c=self.c_run()
        b=copy.deepcopy(self.b)
        b['id']='changed'
        with self.assertRaises(ValueError): validate_same_pool(self.manifest,b,c)
        for change in ('candidate','window','drop'):
            bad=copy.deepcopy(c)
            if change=='candidate': bad['queries'][0]['input_pool'][0]['score']=2
            if change=='window': bad['queries'][0]['hits'][0]['rerank_windows'][0]['text']='fake'
            if change=='drop': bad['queries'][0]['hits'].pop()
            with self.assertRaises(ValueError): validate_same_pool(self.manifest,self.b,bad)

    def test_windows_score_maximum_and_trace_coverage(self):
        pool=copy.deepcopy(self.b['queries'][0]['hits'][:1])
        pool[0]['text']='abcdef\nghijkl'
        ranker=ControlledRanker(RerankConfig(max_tokens=10))
        ranker.predict=lambda pairs:[float(i) for i in range(len(pairs))]
        hit=rerank_pool('q',pool,ranker)[0]
        spans=hit['rerank_windows']
        self.assertEqual(hit['score'],len(spans)-1)
        self.assertEqual(''.join(s['text'] for s in spans),pool[0]['text'])
        self.assertEqual(spans[0]['line_start'],pool[0]['line_start'])

    def test_candidates_before_rerank_and_true_BEIR_rescue_denominators(self):
        frozen=self.freeze()
        c=self.c_run()
        metrics=score_run(self.manifest,frozen,c)
        rates=metrics['rerank_diagnostics']['splits']['test']
        self.assertEqual((rates['rescued'],rates['rescue_eligible']),(1,1))
        self.assertEqual(rates['rescue_rate_at3'],1)
        self.assertIsNone(rates['regression_rate_at3'])
        self.assertEqual(metrics['splits']['test']['metrics']['MRR@10'],1)
        self.assertEqual(len(metrics['negative_queries']),1)
        self.assertEqual(metrics['splits']['test']['metrics']['CandidateRecall@50'],1)

    def test_C_failure_stays_in_recall_and_rescue_denominator(self):
        frozen=self.freeze()
        ranker=ControlledRanker()
        ranker.predict=lambda pairs:[float('nan')]*len(pairs)
        metrics=score_run(self.manifest,frozen,self.c_run(ranker))
        self.assertEqual(metrics['splits']['test']['metrics']['Recall@10'],0)
        self.assertEqual(metrics['rerank_diagnostics']['splits']['test']['rescue_eligible'],1)
        self.assertEqual(metrics['rerank_diagnostics']['splits']['test']['rescue_rate_at3'],0)
        self.assertEqual(metrics['failure_count'],2)

    def test_ABC_compare_and_markdown_preserve_same_freeze(self):
        frozen=self.freeze()
        a=copy.deepcopy(self.b)
        a['system']='A_dense'
        c=self.c_run()
        report=compare_runs(self.manifest,a,self.b,frozen,c)
        self.assertEqual(report['quality']['C']['freeze_id'],frozen['id'])
        self.assertGreater(report['quality']['splits']['test']['delta_C_minus_B']['MRR@10'],0)
        text=comparison_markdown(report)
        self.assertIn('| 指标 | A | B | C |',text)
        self.assertIn('救回 1/1',text)
        self.assertIn('不是一次真实端到端请求',text)

    def test_ranking_does_not_read_annotations_or_gold(self):
        first=self.c_run()['queries']
        for q in self.manifest['suite']['queries']:
            q.update(audit=None,annotations=None,evidence_groups=None,answerable=None)
        second=self.c_run()['queries']
        self.assertEqual(first[0]['hits'],second[0]['hits'])

    def test_rerank_command_uses_saved_pool_no_embedding_load(self):
        path=self.root/'b.json'
        path.write_text(json.dumps(self.b),encoding='utf-8')
        with patch('quality_retrieval.OUTPUT',self.root), \
             patch('quality_retrieval.LocalReranker',side_effect=lambda config:ControlledRanker(config)), \
             patch('quality_retrieval.LocalEmbedder',side_effect=AssertionError('C must not retrieve/encode E5')):
            with redirect_stdout(StringIO()) as output:
                main(['--review-db',str(self.store.path),'rerank','--b-run',str(path),'--repeats','2'])
        result=json.loads(output.getvalue())
        self.assertEqual(result['failure_count'],0)
        run=json.loads(Path(result['run']).read_text(encoding='utf-8'))
        validate_same_pool(self.manifest,self.b,run)
        self.assertIsNone(run['quality'])

    def test_config_requires_pinned_version_and_positive_limits(self):
        for kwargs in ({'revision':'main'},{'batch_size':0},{'max_tokens':True},{'model_name':'../../private'}):
            with self.assertRaises(ValueError): RerankConfig(**kwargs)

    def test_C_candidate20_remains_B_pool20_when_gold_promoted_from_last(self):
        text=self.manifest['sources'][0]['text']
        for i in range(4,25):
            self.manifest['chunks'].append({'id':f'c{i}','file':'tcp.md','source_id':'source',
                                           'line_start':2*i+1,'line_end':2*i+2,'text':f'padding{i}\nextra{i}'})
            text+=f'\npadding{i}\nextra{i}'
        self.manifest['sources'][0]['text']=text
        self.manifest['fingerprint']=digest({k:v for k,v in self.manifest.items() if k!='fingerprint'})
        self.b['benchmark_fingerprint']=self.manifest['fingerprint']
        self.identity=self.store.create(self.manifest)
        for row in self.b['queries']:
            row['hits']=[{'chunk_id':c['id'],'source_id':c['source_id'],'line_start':c['line_start'],
                          'line_end':c['line_end'],'text':c['text'],'rank':rank,'score':1/(60+rank)}
                         for rank,c in enumerate(self.manifest['chunks'][1:]+self.manifest['chunks'][:1],1)]
        report=score_run(self.manifest,self.freeze(),self.c_run())
        metrics=report['splits']['test']['metrics']
        self.assertEqual(metrics['Recall@1'],1)
        self.assertEqual(metrics['CandidateRecall@20'],0)
        self.assertEqual(metrics['CandidateRecall@50'],1)

    def test_adapter_loads_only_local_safe_model_and_obeys_real_capacity(self):
        from types import SimpleNamespace
        model=unittest.mock.Mock()
        model.config=SimpleNamespace(num_labels=1,max_position_embeddings=34,model_type='xlm-roberta')
        model.to.return_value=model
        model.eval.return_value=model
        tokenizer=SimpleNamespace(model_max_length=512)
        with patch('transformers.AutoTokenizer.from_pretrained',return_value=tokenizer) as load_tokenizer, \
             patch('transformers.AutoModelForSequenceClassification.from_pretrained',return_value=model) as load_model, \
             patch('rerank_model.model_home',return_value=self.root), \
             patch('rerank_model.download_reranker',side_effect=AssertionError('normal load must not download')):
            ranker=LocalReranker(cache=self.root)
        self.assertEqual(ranker.max_tokens,32)
        self.assertTrue(load_model.call_args.kwargs['use_safetensors'])
        self.assertTrue(load_model.call_args.kwargs['local_files_only'])
        self.assertFalse(load_model.call_args.kwargs['trust_remote_code'])
        self.assertFalse(load_tokenizer.call_args.kwargs['token'])

    def test_tensor_adapter_disables_truncation_and_preserves_raw_logits(self):
        import torch
        from types import SimpleNamespace
        class Tokenizer:
            calls=[]
            def __call__(self,q,d,**kwargs):
                self.calls.append((q,d,kwargs))
                if isinstance(q,list):
                    return {'input_ids':torch.zeros((len(q),8),dtype=torch.long)}
                return {'input_ids':[0]*(len(q)+len(d)+3)}
        ranker=LocalReranker.__new__(LocalReranker)
        ranker.config=RerankConfig(batch_size=2)
        ranker.max_tokens=32
        ranker.tokenizer=Tokenizer()
        ranker.model=lambda **features:SimpleNamespace(logits=torch.tensor([[-2.],[3.]]))
        self.assertEqual(ranker.predict([('query','one'),('query','two')]),[-2.,3.])
        self.assertTrue(all(not call[2]['truncation'] for call in ranker.tokenizer.calls))
        actual=ranker.tokenizer.calls[-1]
        self.assertEqual(actual[:2],(['query','query'],['one','two']))
        with self.assertRaises(ValueError): ranker.predict([('query'*10,'one')])


if __name__=='__main__':
    unittest.main()
