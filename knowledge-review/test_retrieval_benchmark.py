"""Engineering, real BEIR numeric fixtures, and synthetic HTTP review contracts."""

import copy
import json
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from hashlib import sha256

from fastapi.testclient import TestClient

from chunking import Chunk
from embedding_config import select_embedding_config
from quality_retrieval import run_baseline, main
from retrieval_benchmark_data import ReviewStore, digest, initial_review, load_suite, make_manifest, validate_review
from retrieval_benchmark_metrics import percentile, rerank_diagnostics, score_run
from retrieval_review_app import create_review_app
from sources import Source
from test_support import temporary_notes


class FixtureEmbedder:
    config = select_embedding_config()
    max_tokens = 512

    def prepare_chunks(self, chunks):
        return chunks

    def cache_profile(self):
        return {'test_model': 'synthetic_vectors_not_semantic_validation'}

    def embed_query(self, text):
        if text == 'FAIL':
            raise RuntimeError('controlled failure')
        return [1.0, 0.0]

    def embed_chunks(self, chunks):
        return [[1.0, float(i+1)] for i, _ in enumerate(chunks)]


def fixture(root):
    source = Source('source', root / 'tcp.md', 'one\ntwo\nthree\nfour\nfive\nsix')
    manifest = {'schema': 1, 'suite': {'revision': 'synthetic-verification', 'queries': []},
                'sources': [{'file': 'tcp.md', 'source_id': 'source', 'text': source.text}],
                'chunks': [{'id': f'c{i}', 'file': 'tcp.md', 'source_id': 'source', 'line_start': i*2+1,
                            'line_end': i*2+2, 'text': '\n'.join(source.text.splitlines()[i*2:i*2+2])} for i in range(3)]}
    for identity, answerable in [('positive', True), ('negative', False)]:
        manifest['suite']['queries'].append({'id': identity, 'text': identity, 'split': 'test', 'family': identity,
            'category': 'synthetic', 'scope': ['tcp.md'], 'answerable': answerable,
            'annotations': [{'file': 'tcp.md', 'line_start': 1, 'line_end': 2, 'grade': 2, 'note': 'controlled'}] if answerable else [],
            'evidence_groups': [{'id': 'g1', 'label': 'fact', 'evidence': [{'file': 'tcp.md', 'line_start': 1, 'line_end': 2}]}] if answerable else [],
            'audit': {'status': 'checked', 'reason': 'controlled'}})
    manifest['fingerprint'] = digest(manifest)
    return manifest


def approved(manifest, query):
    payload = initial_review(manifest, query)
    payload['status'] = 'approved'
    payload['scope_checked'] = True
    for judgment in payload['judgments']:
        if judgment['grade'] is None:
            judgment['grade'] = 0
    return payload


def full_audit(manifest, pending=()):
    return {'version':'controlled-agent-audit', 'method':'controlled labels, not semantic evaluation',
            'benchmark_fingerprint':manifest['fingerprint'],
            'source_hashes':{s['file']:sha256(s['text'].encode()).hexdigest() for s in manifest['sources']},
            'cases':[{'query_id':q['id'],'decision':'needs_user' if q['id'] in pending else 'confident',
                      'reason':'controlled audit reason','review':approved(manifest,q),'focus_chunk_ids':['c0']}
                     for q in manifest['suite']['queries']]}


class RetrievalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.manifest = fixture(self.root)
        self.store = ReviewStore(self.root / 'review.sqlite3')
        self.identity = self.store.create(self.manifest)
        self.query = self.manifest['suite']['queries'][0]
        self.audit_dir = None

    def tearDown(self):
        if self.audit_dir:
            (self.audit_dir / 'agent-review.json').unlink()
            self.audit_dir.rmdir()
            self.audit_dir.parent.rmdir()
        self.temp.__exit__(None, None, None)

    def freeze(self):
        revisions = {}
        for query in self.manifest['suite']['queries']:
            result = self.store.save(self.identity, query['id'], approved(self.manifest, query), 0)
            revisions[query['id']] = result['revision']
        return self.store.freeze(self.identity, 'synthetic test actor', 'not real user gold', revisions)

    def test_initial_labels_do_not_mark_unknown_as_zero(self):
        payload = self.store.review(self.identity, 'positive')['payload']
        self.assertEqual([j['grade'] for j in payload['judgments']], [2, None, None])
        self.assertEqual(payload['status'], 'pending')

    def test_manifest_create_is_idempotent_and_tampering_rejected(self):
        self.assertEqual(self.identity, self.store.create(self.manifest))
        bad = copy.deepcopy(self.manifest)
        bad['chunks'][0]['text'] = 'changed'
        with self.assertRaises(ValueError): self.store.create(bad)

    def test_unjudged_cannot_be_approved(self):
        payload = initial_review(self.manifest, self.query)
        payload.update(status='approved', scope_checked=True)
        with self.assertRaises(ValueError): self.store.save(self.identity, 'positive', payload, 0)
        self.assertEqual(self.store.review(self.identity, 'positive')['revision'], 0)

    def test_scope_check_required(self):
        payload = approved(self.manifest, self.query)
        payload['scope_checked'] = False
        with self.assertRaises(ValueError): validate_review(self.manifest, self.query, payload)

    def test_extra_or_missing_judgment_rejected(self):
        payload = approved(self.manifest, self.query)
        payload['judgments'].pop()
        with self.assertRaises(ValueError): validate_review(self.manifest, self.query, payload)
        payload['judgments'].append({'chunk_id': 'outside', 'grade': 0})
        with self.assertRaises(ValueError): validate_review(self.manifest, self.query, payload)

    def test_dispute_needs_resolution(self):
        self.query['audit']['status'] = 'disputed'
        payload = approved(self.manifest, self.query)
        with self.assertRaises(ValueError): validate_review(self.manifest, self.query, payload)
        payload['issue_resolution'] = 'retain this condition'
        validate_review(self.manifest, self.query, payload)

    def test_initial_audit_suggestion_keeps_original_draft(self):
        self.query['audit']['suggested_annotations'] = []
        self.assertIsNone(initial_review(self.manifest, self.query)['judgments'][0]['grade'])
        self.assertEqual(self.query['annotations'][0]['grade'], 2)

    def test_groups_cannot_point_to_irrelevant_or_out_of_scope_chunks(self):
        payload = approved(self.manifest, self.query)
        payload['groups'][0]['bundles'] = [['c1']]
        with self.assertRaises(ValueError): validate_review(self.manifest, self.query, payload)
        payload['groups'][0]['bundles'] = [['outside']]
        with self.assertRaises(ValueError): validate_review(self.manifest, self.query, payload)

    def test_stale_retry_does_not_duplicate_review(self):
        payload = approved(self.manifest, self.query)
        self.store.save(self.identity, 'positive', payload, 0)
        with self.assertRaises(ValueError): self.store.save(self.identity, 'positive', payload, 0)
        self.assertEqual(self.store.review(self.identity, 'positive')['revision'], 1)

    def test_pending_prevents_freeze_and_exclusion_needs_reason(self):
        with self.assertRaises(ValueError): self.store.freeze(self.identity, 'test', 'note', {'positive': 0, 'negative': 0})
        payload = initial_review(self.manifest, self.query)
        payload['status'] = 'excluded'
        with self.assertRaises(ValueError): self.store.save(self.identity, 'positive', payload, 0)

    def test_freeze_immutable_idempotent_and_new_revision(self):
        frozen = self.freeze()
        self.assertEqual(frozen['id'], self.store.freeze(self.identity, 'synthetic test actor', 'not real user gold', {'positive': 1, 'negative': 1})['id'])
        payload = self.store.review(self.identity, 'positive')['payload']
        payload['note'] = 'changed later'
        self.store.save(self.identity, 'positive', payload, 1)
        self.assertEqual(self.store.frozen(frozen['id']), frozen)
        with self.assertRaises(ValueError): self.store.freeze(self.identity, 'test', 'new', {'positive': 1, 'negative': 1})
        new = self.store.freeze(self.identity, 'test', 'new version', {'positive': 2, 'negative': 1})
        self.assertNotEqual(new['id'], frozen['id'])

    def test_no_answer_cannot_have_positive_evidence(self):
        payload = approved(self.manifest, self.query)
        payload['answerable'] = False
        with self.assertRaises(ValueError): validate_review(self.manifest, self.query, payload)

    def test_complete_agent_audit_auto_adopts_and_reapplication_is_idempotent(self):
        audit = full_audit(self.manifest)
        result = self.store.adopt_agent_audit(self.identity, audit)
        self.assertEqual(result['auto_accepted'], 2)
        review = self.store.review(self.identity,'positive')
        self.assertEqual(review['revision'], 1)
        self.assertEqual(review['payload']['status'], 'auto_accepted')
        self.assertEqual(review['payload']['origin'], 'agent')
        self.assertEqual(self.store.adopt_agent_audit(self.identity,audit)['preserved_existing'],2)
        self.assertEqual(self.store.review(self.identity,'positive')['revision'],1)

    def test_agent_audit_does_not_fill_unknown_or_apply_partial_invalid_batch(self):
        for change in ('unknown','missing_query','wrong_source'):
            audit=full_audit(self.manifest)
            if change=='unknown': audit['cases'][1]['review']['judgments'][0]['grade']=None
            if change=='missing_query': audit['cases'].pop()
            if change=='wrong_source': audit['source_hashes']['tcp.md']='bad'
            with self.assertRaises(ValueError): self.store.adopt_agent_audit(self.identity,audit)
            self.assertEqual(self.store.review(self.identity,'positive')['revision'],0)

    def test_agent_adoption_keeps_existing_user_draft(self):
        payload=initial_review(self.manifest,self.query)
        payload['note']='user draft retained'
        saved=self.store.save(self.identity,'positive',payload,0)
        result=self.store.adopt_agent_audit(self.identity,full_audit(self.manifest))
        self.assertEqual(result['preserved_existing'],1)
        self.assertEqual(self.store.review(self.identity,'positive'),saved)

    def test_uncertainty_stays_pending_and_blocks_freeze(self):
        self.store.adopt_agent_audit(self.identity,full_audit(self.manifest,pending=['positive']))
        self.assertEqual(self.store.review(self.identity,'positive')['payload']['status'],'pending')
        with self.assertRaises(ValueError): self.store.freeze(self.identity,'test','mixed',{'positive':1,'negative':1})

    def test_human_decision_keeps_audit_pointer_and_marks_user_origin(self):
        self.store.adopt_agent_audit(self.identity,full_audit(self.manifest,pending=['positive']))
        payload=self.store.review(self.identity,'positive')['payload']
        real_audit=payload['audit_id']
        payload.update(status='approved',audit_id='client forged audit',origin='agent')
        result=self.store.save(self.identity,'positive',payload,1)
        self.assertEqual(result['payload']['origin'],'user')
        self.assertEqual(result['payload']['audit_id'],real_audit)
        payload['status']='auto_accepted'
        with self.assertRaises(ValueError): self.store.save(self.identity,'positive',payload,2)

    def test_auto_freeze_metrics_record_provenance_and_reject_forged_labels(self):
        self.store.adopt_agent_audit(self.identity,full_audit(self.manifest))
        frozen=self.store.freeze(self.identity,'version confirmer','auto adopted',{'positive':1,'negative':1})
        self.assertEqual(len(frozen['agent_audits']),1)
        run={'system':'A_dense','benchmark_fingerprint':self.manifest['fingerprint'],
             'queries':[{'query_id':'positive','status':'ok','hits':[{'chunk_id':'c0','score':1}]}]}
        report=score_run(self.manifest,frozen,run)
        self.assertEqual(report['label_provenance']['agent_auto_accepted'],2)
        self.assertEqual(report['label_provenance']['user_confirmed'],0)
        self.assertEqual(report['splits']['test']['metrics']['Recall@1'],1)
        frozen['cases'][0]['judgments'][0]['grade']=1
        frozen['fingerprint']=digest({k:v for k,v in frozen.items() if k not in ('id','fingerprint')})
        with self.assertRaises(ValueError): score_run(self.manifest,frozen,run)

    def test_http_cannot_forge_auto_status_and_returns_dispute_focus(self):
        self.store.adopt_agent_audit(self.identity,full_audit(self.manifest,pending=['positive']))
        with TestClient(create_review_app(self.store.path)) as client:
            endpoint='/api/retrieval-benchmark/'+self.identity+'/queries/positive'
            detail=client.get(endpoint).json()
            self.assertEqual(detail['agent_audit']['focus_chunk_ids'],['c0'])
            payload=detail['review']['payload']
            payload['status']='auto_accepted'
            self.assertEqual(client.put(endpoint,json={'expected_revision':1,'payload':payload}).status_code,422)
            payload['status']='approved'
            result=client.put(endpoint,json={'expected_revision':1,'payload':payload})
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json()['payload']['origin'],'user')

    def test_prepare_reuses_only_matching_audit_and_preserves_user_edit(self):
        # Follow the existing Windows-safe fixture: regular workspace mkdir,
        # explicit owned-file cleanup, no tempfile-created restricted directory.
        root = self.root
        self.audit_dir = root / 'evals/retrieval'
        self.audit_dir.mkdir(parents=True)
        audit_path = self.audit_dir / 'agent-review.json'
        audit = full_audit(self.manifest, pending=['positive'])
        audit_path.write_text(json.dumps(audit), encoding='utf-8')
        db_path = root / 'prepared.sqlite3'
        def prepare():
            output = StringIO()
            with redirect_stdout(output):
                main(['--review-db', str(db_path), 'prepare'])
            return json.loads(output.getvalue())
        with patch('quality_retrieval.ROOT', root), \
             patch('quality_retrieval.load_suite', return_value=(self.manifest['suite'], [])), \
             patch('quality_retrieval.LocalEmbedder', return_value=FixtureEmbedder()), \
             patch('quality_retrieval.make_manifest', return_value=self.manifest):
            first = prepare()
            self.assertEqual((first['needs_user'], first['auto_accepted']), (1, 1))
            self.assertEqual(first['status'], 'exceptions_only')
            store = ReviewStore(db_path)
            payload = store.review(first['benchmark'], 'positive')['payload']
            payload.update(status='approved', note='user decision retained')
            saved = store.save(first['benchmark'], 'positive', payload, 1)
            self.assertEqual(prepare()['needs_user'], 0)
            self.assertEqual(store.review(first['benchmark'], 'positive'), saved)
            audit['benchmark_fingerprint'] = 'different material version'
            audit_path.write_text(json.dumps(audit), encoding='utf-8')
            fresh_path = root / 'fresh.sqlite3'
            with redirect_stdout(StringIO()):
                main(['--review-db', str(fresh_path), 'prepare'])
            fresh = ReviewStore(fresh_path)
            identity = fresh.list()[0]['id']
            self.assertEqual(fresh.review(identity, 'positive')['revision'], 0)
            self.assertEqual(fresh.review(identity, 'negative')['payload']['status'], 'pending')

    def test_http_page_save_stale_and_freeze_contract(self):
        with TestClient(create_review_app(self.store.path)) as client:
            self.assertEqual(client.get('/retrieval-review').status_code, 200)
            base = '/api/retrieval-benchmark/' + self.identity
            details = client.get(base + '/queries/positive').json()
            self.assertNotIn('score', json.dumps(details))
            request = {'expected_revision': 0, 'payload': approved(self.manifest, self.query)}
            self.assertEqual(client.put(base + '/queries/positive', json=request).status_code, 200)
            self.assertEqual(client.put(base + '/queries/positive', json=request).status_code, 409)
            request['payload']['answerable'] = 'yes'
            self.assertEqual(client.put(base + '/queries/positive', json=request).status_code, 422)
            self.assertEqual(client.get(base + '/queries/not-found').status_code, 404)
            freeze = {'reviewer': 'test', 'note': 'test', 'expected_revisions': {'positive': 1, 'negative': 0}, 'confirmed': False}
            self.assertEqual(client.post(base + '/freeze', json=freeze).status_code, 422)
            request = {'expected_revision': 0, 'payload': approved(self.manifest, self.manifest['suite']['queries'][1])}
            self.assertEqual(client.put(base + '/queries/negative', json=request).status_code, 200)
            freeze.update(confirmed=True, expected_revisions={'positive': 1, 'negative': 1})
            self.assertEqual(client.post(base + '/freeze', json=freeze).status_code, 200)

    def test_real_beir_known_ranking_and_negative_separate(self):
        frozen = self.freeze()
        run = {'system': 'A_dense', 'benchmark_fingerprint': self.manifest['fingerprint'],
               'queries': [{'query_id': 'positive', 'status': 'ok', 'hits': [{'chunk_id':'c1','score':.8}, {'chunk_id':'c0','score':.7}], 'warm_ms':[10,20]},
                           {'query_id': 'negative', 'status': 'ok', 'hits': [{'chunk_id':'c2','score':.4}]}]}
        result = score_run(self.manifest, frozen, run)
        metrics = result['splits']['test']['metrics']
        self.assertEqual(result['backend']['beir'], '2.2.0')
        self.assertEqual(metrics['Recall@1'], 0)
        self.assertEqual(metrics['Recall@3'], 1)
        self.assertEqual(metrics['MRR@10'], .5)
        self.assertAlmostEqual(metrics['nDCG@5'], .63093, places=5)
        self.assertEqual(result['splits']['test']['query_count'], 1)
        self.assertEqual(len(result['negative_queries']), 1)
        self.assertEqual(result['latency']['test']['p50_ms'], 15)

    def test_beir_missing_and_failed_queries_remain_zero(self):
        frozen = self.freeze()
        run = {'system':'A_dense','benchmark_fingerprint':self.manifest['fingerprint'],'queries':[]}
        result = score_run(self.manifest, frozen, run)
        self.assertEqual(result['splits']['test']['query_count'], 1)
        self.assertEqual(result['splits']['test']['metrics']['Recall@10'], 0)
        self.assertEqual(result['failure_count'], 2)
        run['queries'] = [{'query_id':'positive','status':'failed','hits':[]}]
        self.assertEqual(score_run(self.manifest, frozen, run)['per_query'][0]['status'], 'failed')

    def test_missing_answerable_query_halves_mean_instead_of_disappearing(self):
        second = copy.deepcopy(self.query)
        second.update(id='second', family='second')
        self.manifest['suite']['queries'].append(second)
        self.manifest['fingerprint'] = digest({k:v for k,v in self.manifest.items() if k != 'fingerprint'})
        self.identity = self.store.create(self.manifest)
        frozen = self.freeze()
        run = {'system':'A_dense','benchmark_fingerprint':self.manifest['fingerprint'],
               'queries':[{'query_id':'positive','status':'ok','hits':[{'chunk_id':'c0','score':1}]}]}
        result = score_run(self.manifest, frozen, run)
        self.assertEqual(result['splits']['test']['query_count'], 2)
        self.assertEqual(result['splits']['test']['metrics']['Recall@10'], .5)

    def test_frozen_export_beir_files_and_old_run_scoring_without_model(self):
        self.store.adopt_agent_audit(self.identity, full_audit(self.manifest, pending=['negative']))
        negative = self.store.review(self.identity, 'negative')['payload']
        negative['status'] = 'approved'
        self.store.save(self.identity, 'negative', negative, 1)
        frozen = self.store.freeze(self.identity, 'controlled confirmer', 'mixed provenance', {'positive': 1, 'negative': 2})
        with patch('quality_retrieval.OUTPUT', self.root), patch('quality_retrieval.LocalEmbedder', side_effect=AssertionError('no model needed')):
            output = StringIO()
            with redirect_stdout(output):
                main(['--review-db', str(self.store.path), 'export', '--freeze', frozen['id']])
            folder = Path(output.getvalue().strip())
            self.assertEqual((folder/'qrels/test.tsv').read_text(encoding='utf-8').strip().splitlines(),
                             ['query-id\tcorpus-id\tscore', 'positive\tc0\t2'])
            self.assertEqual(len((folder/'queries.jsonl').read_text(encoding='utf-8').splitlines()), 1)
            self.assertEqual(len((folder/'corpus.jsonl').read_text(encoding='utf-8').splitlines()), 3)
            self.assertEqual(json.loads((folder/'labels.json').read_text(encoding='utf-8')), frozen)
            run = {'system':'A_dense','benchmark_fingerprint':self.manifest['fingerprint'],'queries':[]}
            run_path = self.root/'run.json'
            run_path.write_text(json.dumps(run),encoding='utf-8')
            with redirect_stdout(StringIO()):
                main(['--review-db',str(self.store.path),'score','--run',str(run_path),'--freeze',frozen['id']])
            self.assertEqual(len(list(self.root.glob('scored-*.json'))), 1)
            report = json.loads(next(self.root.glob('scored-*.json')).read_text(encoding='utf-8'))
            self.assertEqual(report['label_provenance']['agent_auto_accepted'], 1)
            self.assertEqual(report['label_provenance']['user_confirmed'], 1)
            # temporary_notes only removes files; remove this test's nested outputs.
            for path in (folder/'qrels').iterdir(): path.unlink()
            (folder/'qrels').rmdir()
            for path in folder.iterdir(): path.unlink()
            folder.rmdir()

    def test_complete_evidence_and_alternative_bundles(self):
        frozen = self.freeze()
        case = frozen['cases'][0]
        case['judgments'][1]['grade'] = 1
        case['groups'] = [{'id':'g1','label':'needs both','bundles':[['c0','c1']]}]
        frozen['fingerprint'] = digest({k:v for k,v in frozen.items() if k not in ('id','fingerprint')})
        run = {'system':'A_dense','benchmark_fingerprint':self.manifest['fingerprint'],
               'queries':[{'query_id':'positive','status':'ok','hits':[{'chunk_id':'c0','score':1},{'chunk_id':'c1','score':.5}]}]}
        result = score_run(self.manifest, frozen, run)['per_query'][0]['metrics']
        self.assertEqual(result['Hit@1'], 1)
        self.assertEqual(result['CompleteEvidenceHit@1'], 0)
        self.assertEqual(result['CompleteEvidenceHit@3'], 1)
        case['groups'][0]['bundles'] = [['c0'],['c1']]
        frozen['fingerprint'] = digest({k:v for k,v in frozen.items() if k not in ('id','fingerprint')})
        self.assertEqual(score_run(self.manifest, frozen, run)['per_query'][0]['metrics']['CompleteEvidenceHit@1'], 1)

    def test_different_manifest_and_duplicate_outside_hits_rejected(self):
        frozen = self.freeze()
        run = {'system':'A_dense','benchmark_fingerprint':'changed','queries':[]}
        with self.assertRaises(ValueError): score_run(self.manifest, frozen, run)
        run['benchmark_fingerprint'] = self.manifest['fingerprint']
        for identities in [['c0','c0'],['outside']]:
            run['queries'] = [{'query_id':'positive','status':'ok','hits':[{'chunk_id':i,'score':.5} for i in identities]}]
            with self.assertRaises(ValueError): score_run(self.manifest, frozen, run)


class RetrievalBaselineTests(unittest.TestCase):
    def test_public_fixture_validation_and_actual_prepared_units(self):
        suite, sources = load_suite()
        self.assertEqual(len(suite['queries']), 20)
        manifest = make_manifest(suite, sources, FixtureEmbedder())
        for query in suite['queries']:
            payload = initial_review(manifest, query)
            self.assertEqual(payload['status'], 'pending')
        cosine = next(q for q in suite['queries'] if q['id']=='cosine')
        self.assertEqual(cosine['audit']['status'], 'error')
        self.assertEqual(cosine['annotations'][1]['grade'], 1)
        self.assertEqual(cosine['audit']['suggested_annotations'][1]['grade'], 0)

    def test_dense_baseline_matches_existing_tool_and_keeps_failures(self):
        suite, sources = load_suite()
        suite = copy.deepcopy(suite)
        suite['queries'] = suite['queries'][:2]
        suite['queries'][1]['text'] = 'FAIL'
        embedder = FixtureEmbedder()
        manifest = make_manifest(suite, sources, embedder)
        with temporary_notes() as root:
            records = run_baseline(manifest, sources, embedder, root/'vectors.sqlite3', repeats=2)
        self.assertEqual(records[0]['status'], 'ok')
        self.assertEqual(len(records[0]['hits']), 50)
        self.assertEqual(len(records[0]['warm_ms']), 2)
        self.assertEqual(records[1]['status'], 'failed')
        self.assertEqual(records[1]['hits'], [])

    def test_percentile_and_rerank_eligible_denominators(self):
        self.assertIsNone(percentile([], .95))
        self.assertEqual(percentile([1,2,3], .5), 2)
        events = rerank_diagnostics(['x','y','z','g'], ['g','x','y'], ['x','y','z','g'], {'g'})
        self.assertTrue(events['rescue_eligible'] and events['rescued'])
        events = rerank_diagnostics(['g','x'], ['x'], ['g','x'], {'g'})
        self.assertTrue(events['regression_eligible'] and events['regressed'])
        with self.assertRaises(ValueError): rerank_diagnostics(['x'], ['outside'], ['x'], {'g'})


if __name__ == '__main__':
    unittest.main()
