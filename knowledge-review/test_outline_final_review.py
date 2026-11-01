"""Automatic final review uses isolated stores and controlled model outputs only."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from generation_budget import GenerationBudget
from outline_final_review import CoverageDraft, ConsolidationDraft, validate_consolidation, after_extraction
from outline_final_store import latest, record, resume, public, start
from outline_service import run_outline, public_outline
from outline_v2 import plan_direct
from outline_service import scope_fingerprint
import test_outline_context as context_tests
from test_outlines import proposal, wire_proposal


class FinalReviewTests(unittest.TestCase):
    tearDown=context_tests.OutlineContextTests.tearDown
    current=context_tests.OutlineContextTests.current
    create=context_tests.OutlineContextTests.create

    def setUp(self):
        context_tests.OutlineContextTests.setUp(self)
        self.auto_finalize.stop()  # Exercise the real automatic default in this suite.
        self.calls=[]; self.fail_final=False; self.add_missing=True; self.on_coverage=None

    def model(self,messages,schema,**kwargs):
        if kwargs.get('before_request'): kwargs['before_request']()
        self.calls.append(schema.__name__)
        if kwargs.get('telemetry'):
            kwargs['telemetry'](SimpleNamespace(model='test-model',id='controlled',
                usage=SimpleNamespace(model_dump=lambda:dict(prompt_tokens=100,completion_tokens=20))), 'tools')
        data=json.loads(messages[1]['content'])
        if schema is CoverageDraft:
            if self.on_coverage: self.on_coverage()
            if not self.add_missing: return schema(proposals=[],note='Controlled coverage check')
            registry=data['available_evidence']
            line=5 if any(c['line_start']<=5<=c['line_end'] for c in registry.values()) else min(c['line_start'] for c in registry.values())
            p=wire_proposal(proposal('version',line),registry)
            p['target']['title']='A distinct missing application target'; p['target']['capability']='apply'
            return schema(proposals=[p],note='Controlled addition')
        if schema is ConsolidationDraft:
            if self.fail_final: raise RuntimeError('controlled provider failure')
            ids=[c['id'] for c in data['candidates']]
            return schema(retained_ids=[ids[0],*ids[2:]],duplicates=[dict(canonical_id=ids[0],duplicate_ids=[ids[1]],reason='Controlled equivalent conditions')],note='Controlled whole-list result')
        registry=data['available_evidence']
        line=min(c['line_start'] for c in registry.values())
        p=wire_proposal(proposal('version',line),registry)
        return schema(proposals=[p,deepcopy(p)],note='Two controlled original targets')

    def generated(self,v2=False):
        text='# Rules\n\nACK is the next expected byte.\n\nLEN affects the next sequence.'
        identity=self.create(text)
        if v2:
            # Reuse the same frozen fixture under a fresh explicit algorithm task.
            sections,batches,plan=plan_direct([self.source],self.snapshots,'','en')
            # The queued V1 fixture is only a planning fixture; pause it first.
            self.store.pause(identity)
            identity=self.store.create('root','v2-final','','en',scope_fingerprint(self.snapshots),self.snapshots,sections,batches,plan)
        with patch('outline_service._request_structured',side_effect=self.model):
            run_outline(self.store,identity,self.current)
        return identity

    def test_v1_automatically_adds_and_deduplicates_without_deleting_originals(self):
        identity=self.generated(); job=self.store.get(identity); review=latest(self.store,identity)
        self.assertEqual(self.calls,['FullOutlineDraft','CoverageDraft','ConsolidationDraft'])
        self.assertEqual(review['status'],'completed'); self.assertEqual(review['calls_used'],2)
        self.assertEqual(len(job['candidates']),3)
        originals=[c for c in job['candidates'] if not c['origin_final_review_id']]
        self.assertEqual(len(originals),2); self.assertEqual(originals[0]['learning'],originals[1]['learning'])
        self.assertEqual(len(review['result']['retained_ids']),2)
        self.assertEqual(len(review['result']['added_ids']),1)
        visible=public_outline(self.store,identity,self.current)
        self.assertFalse(visible['final_review']['stale']); self.assertFalse(visible['processing'])
        self.assertEqual(sum(c['final_review_duplicate'] for c in visible['candidates']),1)
        self.assertEqual(self.practice.list_items(),[])

    def test_v2_has_the_same_automatic_second_stage_and_saved_budget(self):
        identity=self.generated(v2=True)
        self.assertEqual(self.calls,['DirectOutlineDraft','CoverageDraft','ConsolidationDraft'])
        review=latest(self.store,identity)
        self.assertEqual(review['plan']['budget']['context_tokens'],131072)
        self.assertEqual(review['plan']['budget']['output_tokens'],12288)
        self.assertEqual(review['status'],'completed')

    def test_repeated_delivery_is_idempotent_even_after_supplements_are_published(self):
        identity=self.generated(); before=list(self.calls)
        with patch('outline_service._request_structured',side_effect=self.model):
            run_outline(self.store,identity,self.current)
        self.assertEqual(self.calls,before)
        self.assertEqual(len(self.store.get(identity)['candidates']),3)

    def test_failed_final_call_preserves_raw_list_and_resumes_completed_coverage(self):
        self.fail_final=True; identity=self.generated(); review=latest(self.store,identity)
        self.assertEqual(review['status'],'failed')
        self.assertEqual(len(self.store.get(identity)['candidates']),2)
        self.assertEqual(len([s for s in review['steps'] if s['kind']=='coverage' and s['status']=='completed']),1)
        self.fail_final=False; resume(self.store,review['id'],self.current,1)
        with patch('outline_service._request_structured',side_effect=self.model): after_extraction(self.store,identity,self.current)
        self.assertEqual(latest(self.store,identity)['status'],'completed')
        self.assertEqual(self.calls.count('CoverageDraft'),1)
        self.assertEqual(len(self.store.get(identity)['candidates']),3)
        self.assertTrue(latest(self.store,identity)['steps'][-1]['previous_attempts'])

    def test_call_cap_pauses_without_discarding_raw_targets_or_resetting_usage(self):
        # Extract only, then run the same automatic review with one allowed request.
        with patch('outline_service.run_final_review_after_extraction'): identity=self.generated()
        review_id=start(self.store,self.store.get(identity))
        with self.practice.transaction() as db: db.execute('UPDATE outline_final_reviews SET allowance=1 WHERE id=?',(review_id,))
        with patch('outline_service._request_structured',side_effect=self.model): after_extraction(self.store,identity,self.current)
        review=record(self.store,review_id)
        self.assertEqual(review['status'],'paused'); self.assertEqual(review['calls_used'],1)
        self.assertEqual(len(self.store.get(identity)['candidates']),2)
        resume(self.store,review_id,self.current,1)
        with patch('outline_service._request_structured',side_effect=self.model): after_extraction(self.store,identity,self.current)
        self.assertEqual(record(self.store,review_id)['calls_used'],2)

    def test_edit_during_model_call_rejects_late_publication_without_long_write_lock(self):
        with patch('outline_service.run_final_review_after_extraction'): identity=self.generated()
        candidate=self.store.get(identity)['candidates'][0]
        def edit():
            with self.practice.transaction() as db:
                db.execute('UPDATE outline_candidates SET revision=revision+1 WHERE id=?',(candidate['id'],))
        self.on_coverage=edit
        with patch('outline_service._request_structured',side_effect=self.model): after_extraction(self.store,identity,self.current)
        self.assertEqual(latest(self.store,identity)['status'],'failed')
        self.assertEqual(len(self.store.get(identity)['candidates']),2)

    def test_source_revocation_prevents_final_publication(self):
        with patch('outline_service.run_final_review_after_extraction'): identity=self.generated()
        self.on_coverage=lambda:self.snapshots[0].update(source_id='revoked')
        with patch('outline_service._request_structured',side_effect=self.model): after_extraction(self.store,identity,self.current)
        self.assertEqual(latest(self.store,identity)['status'],'failed')
        self.assertEqual(len(self.store.get(identity)['candidates']),2)

    def test_large_source_is_budgeted_into_multiple_actual_source_windows(self):
        text='# Long\n\n'+'\n\n'.join(f'Fact {i}: '+('x'*600) for i in range(80))
        with patch('outline_service.run_final_review_after_extraction'): identity=self.create(text)
        # Use real extraction; disable only the second stage until the fixture is ready.
        with patch('outline_service.run_final_review_after_extraction'),patch('outline_service._request_structured',side_effect=self.model):
            run_outline(self.store,identity,self.current)
        self.add_missing=False
        with patch('outline_final_review.budget_for',return_value=GenerationBudget(32768,12288,2048)),patch('outline_service._request_structured',side_effect=self.model):
            after_extraction(self.store,identity,self.current)
        review=latest(self.store,identity)
        self.assertEqual(review['status'],'completed')
        self.assertGreater(len(review['plan']['windows']),1)
        self.assertEqual(sum(s['kind']=='coverage' for s in review['steps']),len(review['plan']['windows']))
        self.assertTrue(all(s['diagnostic']['estimated_input_tokens']<=s['diagnostic']['input_limit'] for s in review['steps']))

    def test_changed_target_invalidates_the_separate_final_view(self):
        identity=self.generated(); job=self.store.get(identity)
        with self.practice.transaction() as db:
            db.execute('UPDATE outline_candidates SET revision=revision+1 WHERE id=?',(job['candidates'][-1]['id'],))
        self.assertTrue(public(self.store,self.store.get(identity))['stale'])
        self.assertFalse(any(c['final_review_duplicate'] for c in public_outline(self.store,identity,self.current)['candidates']))

    def test_consolidation_rejects_silent_deletion_and_different_capabilities(self):
        candidates=[dict(id='a',learning=proposal('v',1).model_dump()),dict(id='b',learning=proposal('v',1).model_dump())]
        with self.assertRaises(ValueError): validate_consolidation(ConsolidationDraft(retained_ids=['a'],duplicates=[],note='bad'),candidates)
        candidates[1]['learning']['target']['capability']='apply'
        with self.assertRaises(ValueError): validate_consolidation(ConsolidationDraft(retained_ids=['a'],duplicates=[dict(canonical_id='a',duplicate_ids=['b'],reason='bad')],note='bad'),candidates)


if __name__=='__main__': unittest.main()
