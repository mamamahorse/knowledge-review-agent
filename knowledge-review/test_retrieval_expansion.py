"""Fixture provenance, explicit version selection and frozen failure reporting."""
import copy
import json
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from quality_retrieval import main
from retrieval_benchmark_data import ReviewStore, load_suite, make_manifest
from retrieval_fixture_audit import bind_section_audit
from retrieval_comparison import compare_runs, comparison_markdown
from test_retrieval_benchmark import FixtureEmbedder, fixture, full_audit
import test_hybrid_retrieval
from test_support import temporary_notes

EXPANDED = Path(__file__).parent / 'evals/retrieval/expanded-v1/suite.json'


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.suite,self.sources = load_suite(EXPANDED)
        self.manifest = make_manifest(self.suite,self.sources,FixtureEmbedder())
        self.sections = json.loads(EXPANDED.with_name('section-audit.json').read_text(encoding='utf-8'))

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def test_counts_family_isolation_and_original_pilot_preserved(self):
        self.assertEqual(len(load_suite()[0]['queries']),20)
        self.assertEqual(len(self.suite['queries']),100)
        for split,count in [('dev',20),('test',80)]:
            self.assertEqual(sum(q['split']==split for q in self.suite['queries']),count)
        self.assertEqual(sum(not q['answerable'] for q in self.suite['queries']),8)
        self.assertEqual(sum(q['category']=='multi_evidence' for q in self.suite['queries']),12)
        dev={q['family'] for q in self.suite['queries'] if q['split']=='dev'}
        test={q['family'] for q in self.suite['queries'] if q['split']=='test'}
        self.assertFalse(dev & test)
        self.assertGreater(len(self.manifest['chunks']),50)

    def test_complete_audit_and_only_two_uncertain(self):
        audit=bind_section_audit(self.manifest,self.sections)
        self.assertEqual(sum(c['decision']=='needs_user' for c in audit['cases']),2)
        for c,q in zip(audit['cases'],self.suite['queries']):
            allowed={ch['id'] for ch in self.manifest['chunks'] if ch['file'] in q['scope']}
            self.assertEqual({j['chunk_id'] for j in c['review']['judgments']},allowed)
            self.assertTrue(all(j['grade'] in (0,1,2) and j['note'] for j in c['review']['judgments']))

    def test_missing_or_duplicate_section_never_defaults_to_zero(self):
        for mutation in ('missing','duplicate'):
            manifest=copy.deepcopy(self.manifest)
            ledger=manifest['suite']['queries'][0]['section_judgments']
            if mutation=='missing': ledger.pop()
            else: ledger[-1]=copy.deepcopy(ledger[0])
            with self.assertRaises(ValueError): bind_section_audit(manifest,self.sections)

    def test_source_changed_rejects_old_editorial_ledger(self):
        sections=copy.deepcopy(self.sections)
        sections['import-1']['meaning']='different original text'
        with self.assertRaises(ValueError): bind_section_audit(self.manifest,sections)

    def test_explicit_multi_grade_corrections_preserve_initial_ledger(self):
        corrections=json.loads(EXPANDED.with_name('grade-corrections.json').read_text(encoding='utf-8'))
        original=copy.deepcopy(self.manifest)
        audit=bind_section_audit(self.manifest,self.sections,corrections)
        self.assertEqual(self.manifest,original)
        for c,q in zip(audit['cases'],self.suite['queries']):
            if q['category']=='multi_evidence':
                self.assertEqual({j['grade'] for j in c['review']['judgments'] if j['grade']>0},{1})
        bad=copy.deepcopy(corrections)
        first=next(iter(bad))
        bad[first]['grades']['out-of-scope']=1
        with self.assertRaises(ValueError):bind_section_audit(self.manifest,self.sections,bad)

    def test_prepare_explicit_suite_uses_sibling_audit_preserves_edits(self):
        audit=bind_section_audit(self.manifest,self.sections)
        # Ordinary flat files avoid Windows restrictive TemporaryDirectory ACLs.
        suite_path=self.root/'suite.json'
        suite_path.write_text('{}',encoding='utf-8')
        (self.root/'agent-review.json').write_text(json.dumps(audit),encoding='utf-8')
        db=self.root/'review.sqlite3'
        with patch('quality_retrieval.load_suite',return_value=(self.suite,self.sources)) as loader, \
             patch('quality_retrieval.make_manifest',return_value=self.manifest), \
             patch('quality_retrieval.LocalEmbedder',return_value=FixtureEmbedder()):
            def prepare():
                output=StringIO()
                with redirect_stdout(output): main(['--review-db',str(db),'prepare','--suite',str(suite_path)])
                return json.loads(output.getvalue())
            first=prepare()
            loader.assert_called_with(suite_path)
            self.assertEqual((first['auto_accepted'],first['needs_user']),(98,2))
            store=ReviewStore(db)
            qid='cache-4-q2'
            payload=store.review(first['benchmark'],qid)['payload']
            payload.update(status='approved',note='controlled user correction')
            saved=store.save(first['benchmark'],qid,payload,1)
            self.assertEqual(prepare()['needs_user'],1)
            self.assertEqual(store.review(first['benchmark'],qid),saved)
            with self.assertRaises(ValueError):
                store.freeze(first['benchmark'],'test actor','cannot freeze pending',
                             {q['id']:store.review(first['benchmark'],q['id'])['revision'] for q in self.suite['queries']})

    def test_validate_explicit_suite_without_models(self):
        with patch('quality_retrieval.LocalEmbedder',side_effect=AssertionError('no inference')), redirect_stdout(StringIO()) as out:
            main(['validate','--suite',str(EXPANDED)])
        self.assertEqual(json.loads(out.getvalue())['queries'],100)

    def test_agent_correction_appends_original_and_freeze_immutable(self):
        manifest=fixture(self.root)
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(manifest)
        audit=full_audit(manifest)
        store.adopt_agent_audit(identity,audit)
        frozen=store.freeze(identity,'controlled actor','old snapshot',{'positive':1,'negative':1})
        corrected=copy.deepcopy(audit)
        corrected['version']='explicit semantic correction'
        corrected['cases'][0]['review']['judgments'][0]['grade']=1
        result=store.adopt_agent_audit(identity,corrected,{'positive':1})
        self.assertEqual(result['corrected_agent'],1)
        self.assertEqual(store.review(identity,'positive')['revision'],2)
        self.assertEqual(store.frozen(frozen['id']),frozen)
        with closing(store.connect()) as db:
            first=json.loads(db.execute('SELECT payload_json FROM reviews WHERE query_id=? AND revision=1',('positive',)).fetchone()[0])
        self.assertEqual(first['judgments'][0]['grade'],2)

    def test_agent_correction_cas_and_user_draft_protection(self):
        manifest=fixture(self.root)
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(manifest)
        audit=full_audit(manifest)
        store.adopt_agent_audit(identity,audit)
        with self.assertRaises(ValueError): store.adopt_agent_audit(identity,audit,{'positive':2})
        payload=store.review(identity,'positive')['payload']
        payload['status']='pending'
        saved=store.save(identity,'positive',payload,1)
        with self.assertRaises(ValueError): store.adopt_agent_audit(identity,audit,{'positive':2})
        self.assertEqual(store.review(identity,'positive'),saved)

    def test_agent_correction_cannot_bypass_pending_dispute(self):
        manifest=fixture(self.root)
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(manifest)
        audit=full_audit(manifest,pending=['positive'])
        store.adopt_agent_audit(identity,audit)
        with self.assertRaises(ValueError): store.adopt_agent_audit(identity,audit,{'positive':1})
        self.assertEqual(store.review(identity,'positive')['revision'],1)

    def test_bind_command_write_once_no_model_and_no_label_publication(self):
        # Reuse real complete fixture so the projection itself is exercised.
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(self.manifest)
        sections_path=self.root/'sections.json'
        sections_path.write_text(json.dumps(self.sections),encoding='utf-8')
        command=['--review-db',str(store.path),'bind-section-audit','--benchmark',identity,
                 '--sections',str(sections_path),'--output',str(self.root/'bound.json')]
        with patch('quality_retrieval.LocalEmbedder',side_effect=AssertionError('no inference')), redirect_stdout(StringIO()):
            main(command)
            with self.assertRaises(FileExistsError): main(command)
        self.assertEqual(store.review(identity,self.suite['queries'][0]['id'])['revision'],0)

    def test_failure_inventory_frozen_missing_kept_negative_separate(self):
        manifest,a,b=test_hybrid_retrieval.HybridPipelineTests.trace_pair(self)
        store=ReviewStore(self.root/'review.sqlite3')
        identity=store.create(manifest)
        store.adopt_agent_audit(identity,full_audit(manifest))
        frozen=store.freeze(identity,'controlled actor','not real gold',{'positive':1,'negative':1})
        self.assertNotIn('failure_inventory',compare_runs(manifest,a,b))
        b['queries']=[]
        report=compare_runs(manifest,a,b,frozen)
        inventory=report['failure_inventory']
        self.assertEqual([c['query_id'] for c in inventory['cases']],['positive'])
        case=inventory['cases'][0]
        self.assertIn('B: execution_missing',case['reasons'])
        self.assertIn('B-A: ndcg5_regression',case['reasons'])
        self.assertEqual(case['missing_groups_at3']['B'],['g1'])
        self.assertEqual(inventory['negatives'][0]['query_id'],'negative')
        self.assertIn('失败案例清单',comparison_markdown(report))


if __name__=='__main__':
    unittest.main()
