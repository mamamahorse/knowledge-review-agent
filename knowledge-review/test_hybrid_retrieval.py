"""Deterministic lexical math, scope safety, real pipeline and BEIR integration.

Controlled embeddings test engineering, not semantic gains of real E5.
"""
import copy
import json
from io import StringIO
from contextlib import redirect_stdout
from math import log
from pathlib import Path
import unittest
from unittest.mock import patch

from chunking import Chunk
from hybrid_retrieval import BM25Index, HybridConfig, lexical_tokens, reciprocal_rank_fusion
from knowledge_tools import KnowledgeTools
from quality_retrieval import main, run_baseline, run_hybrid
from retrieval import SearchHit
from retrieval_benchmark_data import ReviewStore, load_suite, make_manifest
from retrieval_comparison import compare_runs, comparison_markdown
from sources import Source
from test_retrieval_benchmark import FixtureEmbedder, fixture, full_audit
from test_support import temporary_notes


def chunk(identity, text, source='source'):
    return Chunk(identity, source, (), 1, 1, text)


class HybridLexicalTests(unittest.TestCase):
    def test_chinese_phrase_code_identifiers_and_nfkc(self):
        terms = lexical_tokens('缓存过期 ＡＣＫ=120 std::string_view user_id C++ C#')
        self.assertIn('缓存', terms)
        self.assertIn('过期', terms)
        self.assertIn('缓', terms)
        for term in ('ack', '120', 'std::string_view', 'std', 'string', 'view', 'user_id', 'user', 'id', 'c++', 'c#'):
            self.assertIn(term, terms)
        self.assertEqual(lexical_tokens('ACK'), ['ack'])

    def test_bm25_known_term_frequency_math_and_no_zero_fill(self):
        index = BM25Index([chunk('a','rare rare'), chunk('b','other')], HybridConfig(b=0))
        hits = index.search('rare rare')
        self.assertEqual([h.chunk.chunk_id for h in hits], ['a'])
        # N=2, df=1, tf=2, k1=1.2, b=0 => log(2) * 4.4/3.2.
        self.assertAlmostEqual(hits[0].score, log(2)*4.4/3.2)
        self.assertEqual(index.search('unseen'), [])
        self.assertEqual(index.search('!?!'), [])

    def test_bm25_length_normalization_common_terms_and_empty_documents(self):
        short, long = chunk('short','term'), chunk('long','term padding padding padding')
        index = BM25Index([long, short, chunk('empty','')])
        hits = index.search('term')
        self.assertEqual([h.chunk.chunk_id for h in hits], ['short','long'])
        self.assertTrue(all(h.score > 0 for h in hits))
        self.assertEqual(BM25Index([chunk('empty','')]).search('term'), [])

    def test_bm25_chinese_technical_query_matches_original_evidence(self):
        relevant = chunk('expiry','缓存条目十秒后过期')
        distractor = chunk('network','网络连接握手')
        hits = BM25Index([distractor,relevant]).search('缓存过期')
        self.assertIs(hits[0].chunk, relevant)
        self.assertEqual(hits[0].chunk.text, '缓存条目十秒后过期')

    def test_rrf_uses_ranks_deduplicates_and_records_both_contributions(self):
        a,b,c = [chunk(i,i) for i in ('a','b','c')]
        hits, trace = reciprocal_rank_fusion([a,b,c], [SearchHit(a,.99), SearchHit(b,.01)],
                                            [SearchHit(b,50000), SearchHit(c,1)])
        self.assertEqual([h.chunk.chunk_id for h in hits], ['b','a','c'])
        self.assertAlmostEqual(hits[0].score, 1/62 + 1/61)
        self.assertEqual(trace['b']['dense']['rank'],2)
        self.assertEqual(trace['b']['bm25']['raw_score'],50000)

    def test_rrf_empty_lexical_preserves_dense_and_scope_ties_are_stable(self):
        a,b = chunk('a','a'), chunk('b','b')
        dense = [SearchHit(b,.9),SearchHit(a,.8)]
        fused,_ = reciprocal_rank_fusion([a,b],dense,[])
        self.assertEqual([h.chunk.chunk_id for h in fused],['b','a'])
        fused,_ = reciprocal_rank_fusion([a,b],[SearchHit(b,1)],[SearchHit(a,1)])
        self.assertEqual([h.chunk.chunk_id for h in fused],['a','b'])

    def test_rrf_rejects_alien_duplicate_conflicting_and_nonfinite_candidates(self):
        a = chunk('a','a')
        for candidates in ([SearchHit(chunk('alien','private'),1)],
                           [SearchHit(a,1),SearchHit(a,.5)],
                           [SearchHit(chunk('a','changed original'),1)],
                           [SearchHit(a,float('nan'))]):
            with self.assertRaises(ValueError): reciprocal_rank_fusion([a],candidates,[])

    def test_invalid_parameters_and_real_candidate_limits(self):
        for kwargs in ({'k1':float('nan')},{'b':1.1},{'rrf_k':-1},{'branch_m':True},{'candidate_m':51}):
            with self.assertRaises(ValueError): HybridConfig(**kwargs)
        chunks=[chunk(str(i),'term') for i in range(5)]
        config=HybridConfig(branch_m=3,candidate_m=2)
        dense=[SearchHit(c,1) for c in chunks[:3]]
        lexical=[SearchHit(c,1) for c in chunks[2:]]
        hits, trace=reciprocal_rank_fusion(chunks,dense,lexical,config)
        self.assertEqual(len(hits),2)
        self.assertEqual(len(trace),5)
        with self.assertRaises(ValueError): reciprocal_rank_fusion(chunks,[SearchHit(c,1) for c in chunks],[],config)


class HybridPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes()
        self.root=self.temp.__enter__()

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def test_actual_tool_dense_branch_trace_and_scoped_bm25_statistics(self):
        suite,sources=load_suite()
        suite=copy.deepcopy(suite)
        suite['queries']=suite['queries'][:1]
        suite['queries'][0]['scope']=['tcp.md']
        embedder=FixtureEmbedder()
        manifest=make_manifest(suite,sources,embedder)
        index_path=self.root/'vectors.sqlite3'
        b=run_hybrid(manifest,sources,embedder,index_path,repeats=2)[0]
        self.assertEqual(b['status'],'ok')
        tool=KnowledgeTools([s for s in sources if s.path.name=='tcp.md'],embedder.config,
                            embedder=embedder,index_path=index_path)
        a=tool.search_notes(suite['queries'][0]['text'],top_k=10)
        self.assertEqual([h['chunk_id'] for h in b['dense_hits'][:10]],[h.chunk.chunk_id for h in a.hits])
        sparse=BM25Index(tool.chunks).search(suite['queries'][0]['text'])
        self.assertEqual(b['bm25_hits'][0]['score'],sparse[0].score)
        allowed={c['id']:c for c in manifest['chunks'] if c['file']=='tcp.md'}
        for name in ('hits','dense_hits','bm25_hits'):
            self.assertTrue({h['chunk_id'] for h in b[name]} <= allowed.keys())
        for hit in b['hits']:
            self.assertEqual(hit['text'],allowed[hit['chunk_id']]['text'])
            self.assertAlmostEqual(hit['score'],sum(v['rrf_contribution'] for v in hit['branches'].values()))
        self.assertEqual(len(b['warm_ms']),2)
        self.assertEqual(len(b['warm_rrf_ms']),2)

    def test_query_failure_retained_no_partial_success_or_hidden_fallback(self):
        suite,sources=load_suite()
        suite=copy.deepcopy(suite)
        suite['queries']=suite['queries'][:2]
        suite['queries'][1]['text']='FAIL'
        embedder=FixtureEmbedder()
        manifest=make_manifest(suite,sources,embedder)
        rows=run_hybrid(manifest,sources,embedder,self.root/'vectors.sqlite3',repeats=1)
        self.assertEqual([r['status'] for r in rows],['ok','failed'])
        self.assertEqual(rows[1]['hits'],[])
        self.assertEqual(rows[1]['bm25_hits'],[])

    def test_repeated_scope_reuses_bm25_and_embeds_each_warm_query_once(self):
        suite,sources=load_suite()
        suite=copy.deepcopy(suite)
        suite['queries']=suite['queries'][:2]
        embedder=FixtureEmbedder()
        manifest=make_manifest(suite,sources,embedder)
        with patch.object(embedder,'embed_query',wraps=embedder.embed_query) as encode, \
             patch('quality_retrieval.BM25Index', wraps=BM25Index) as builder:
            rows=run_hybrid(manifest,sources,embedder,self.root/'vectors.sqlite3',repeats=2)
        self.assertTrue(all(r['status']=='ok' for r in rows))
        self.assertEqual(builder.call_count,1)
        self.assertEqual(encode.call_count,6)  # Two bootstrap plus four measured warm requests.
        self.assertFalse(rows[1]['first_call']['bm25_built'])

    def test_ranking_does_not_consume_labels(self):
        suite,sources=load_suite()
        suite=copy.deepcopy(suite)
        suite['queries']=suite['queries'][:1]
        embedder=FixtureEmbedder()
        manifest=make_manifest(suite,sources,embedder)
        first=run_hybrid(manifest,sources,embedder,self.root/'vectors.sqlite3',repeats=1)[0]
        query=manifest['suite']['queries'][0]
        query.update(annotations=None,evidence_groups=None,answerable=None,audit=None)
        second=run_hybrid(manifest,sources,embedder,self.root/'vectors.sqlite3',repeats=1)[0]
        self.assertEqual(first['hits'],second['hits'])

    def test_prepared_evidence_mismatch_is_failure(self):
        suite,sources=load_suite()
        suite=copy.deepcopy(suite)
        suite['queries']=suite['queries'][:1]
        embedder=FixtureEmbedder()
        manifest=make_manifest(suite,sources,embedder)
        manifest['chunks'][0]['text']='mismatched evidence'
        row=run_hybrid(manifest,sources,embedder,self.root/'vectors.sqlite3',repeats=1)[0]
        self.assertEqual(row['status'],'failed')
        self.assertEqual(row['hits'],[])

    def trace_pair(self):
        manifest=fixture(self.root)
        source=Source('source',self.root/'tcp.md',manifest['sources'][0]['text'])
        # Use exact recorded prepared units with a controlled embedding adapter.
        class PreparedEmbedder(FixtureEmbedder):
            def prepare_chunks(self,chunks):
                return [Chunk(c['id'],c['source_id'],(),c['line_start'],c['line_end'],c['text']) for c in manifest['chunks']]
        embedder=PreparedEmbedder()
        protocol={'quality_labels_used_during_retrieval':False,'repeats':1}
        a={'id':'a','system':'A_dense','benchmark_fingerprint':manifest['fingerprint'],'protocol':protocol,
           'queries':run_baseline(manifest,[source],embedder,self.root/'vectors.sqlite3',1)}
        b={'id':'b','system':'B_hybrid','benchmark_fingerprint':manifest['fingerprint'],'protocol':protocol,
           'queries':run_hybrid(manifest,[source],embedder,self.root/'vectors.sqlite3',1)}
        return manifest,a,b

    def test_unfrozen_comparison_no_quality_claims_and_invalid_runs_rejected(self):
        manifest,a,b=self.trace_pair()
        report=compare_runs(manifest,a,b)
        self.assertIsNone(report['quality'])
        self.assertIn('质量结果为 null',comparison_markdown(report))
        self.assertEqual(report['latency']['B']['test']['sample_count'],2)
        self.assertEqual(len(report['per_query']),2)
        bad=copy.deepcopy(b)
        bad['benchmark_fingerprint']='changed'
        with self.assertRaises(ValueError): compare_runs(manifest,a,bad)
        bad=copy.deepcopy(b)
        bad['queries'][0]['scope']=['private.md']
        with self.assertRaises(ValueError): compare_runs(manifest,a,bad)
        bad=copy.deepcopy(b)
        bad['queries'][0]['hits'][0]['text']='fake citation'
        with self.assertRaises(ValueError): compare_runs(manifest,a,bad)

    def test_frozen_comparison_real_beir_and_missing_query_zero(self):
        manifest,a,b=self.trace_pair()
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(manifest)
        store.adopt_agent_audit(identity,full_audit(manifest))
        frozen=store.freeze(identity,'controlled test','not real gold',{'positive':1,'negative':1})
        b['queries']=[]
        report=compare_runs(manifest,a,b,frozen)
        delta=report['quality']['splits']['test']
        self.assertEqual(delta['query_count'],1)
        self.assertEqual(delta['delta_B_minus_A']['Recall@10'],-1)
        self.assertEqual(report['quality']['B']['failure_count'],2)
        self.assertIsNone(report['latency']['B']['test']['p95_ms'])
        self.assertEqual(report['quality']['B']['backend']['beir'],'2.2.0')
        self.assertIn('| Recall@10 |',comparison_markdown(report))

    def test_compare_command_does_not_load_model(self):
        manifest,a,b=self.trace_pair()
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(manifest)
        for name,run in (('a',a),('b',b)):
            run['benchmark_id']=identity
            (self.root/f'{name}.json').write_text(json.dumps(run),encoding='utf-8')
        with patch('quality_retrieval.OUTPUT',self.root), \
             patch('quality_retrieval.LocalEmbedder',side_effect=AssertionError('comparison must not load model')):
            with redirect_stdout(StringIO()) as output:
                main(['--review-db',str(store.path),'compare','--a-run',str(self.root/'a.json'),'--b-run',str(self.root/'b.json')])
        report=json.loads(Path(output.getvalue().strip()).read_text(encoding='utf-8'))
        self.assertEqual(report['quality_status'],'pending_label_freeze')
        self.assertTrue(Path(output.getvalue().strip()).with_suffix('.md').is_file())

    def test_hybrid_command_records_system_config_and_no_unfrozen_scores(self):
        suite,sources=load_suite()
        suite=copy.deepcopy(suite)
        suite['queries']=suite['queries'][:2]
        embedder=FixtureEmbedder()
        manifest=make_manifest(suite,sources,embedder)
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(manifest)
        with patch('quality_retrieval.OUTPUT',self.root), \
             patch('quality_retrieval.LocalEmbedder',return_value=embedder), \
             patch('quality_retrieval.load_suite',return_value=(suite,sources)):
            with redirect_stdout(StringIO()) as output:
                main(['--review-db',str(store.path),'hybrid','--benchmark',identity,'--repeats','1'])
        result=json.loads(output.getvalue())
        run=json.loads(Path(result['run']).read_text(encoding='utf-8'))
        self.assertEqual(run['system'],'B_hybrid')
        self.assertEqual(result['failure_count'],0)
        self.assertIsNone(run['quality'])
        self.assertEqual(run['protocol']['hybrid']['rrf_k'],60)
        self.assertEqual(store.review(identity,suite['queries'][0]['id'])['revision'],0)

    def test_invalid_hybrid_parameters_fail_before_model_loading(self):
        with patch('quality_retrieval.LocalEmbedder',side_effect=AssertionError('no model loading')):
            with self.assertRaises(ValueError):
                main(['--review-db',str(self.root/'review.sqlite3'),'hybrid','--benchmark','irrelevant','--bm25-b','2'])


if __name__=='__main__':
    unittest.main()
