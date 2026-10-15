"""Controlled review decisions verify engineering, not model semantic quality."""
import json
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_outline_groups as fixtures
from outline_group_store import get_run, rows
from outline_review import (start_source_review, run_source_review, continue_source_review,
    apply_source_review, public_source_reviews, run_grouping_with_reviews)
from outline_review_store import get_review, review_steps, pause_review
from outline_review_model import ReviewAction, ReviewConclusion, ReviewLookup, validate_action, execute_lookup
from outline_service import edit_candidate, public_outline, fits_request
from outline_store import OutlineStore, OutlineBusy
from generation_budget import BudgetError, GenerationBudget
from learning_types import LearningDraft
from sources import Source
from web_app import create_app


class SourceReviewTests(unittest.TestCase):
    setUp = fixtures.OutlineGroupTests.setUp
    tearDown = fixtures.OutlineGroupTests.tearDown
    current = fixtures.OutlineGroupTests.current
    create = fixtures.OutlineGroupTests.create
    generated = fixtures.OutlineGroupTests.generated
    counts = fixtures.OutlineGroupTests.counts
    prepare = fixtures.OutlineGroupTests.prepare
    enroll = fixtures.OutlineGroupTests.enroll

    def begin(self, different=False, second=False):
        if second:
            self.snapshots.append({'document_id': 'definition', 'source_id': 'definition-v1', 'path': 'definition.md',
                'name': 'definition.md', 'text': '# 定义\n\nACK 表示下一个期望字节，仅计算已接收数据；不含 SYN/FIN。'})
        self.outline, self.candidates, self.run, self.group = self.prepare('needs_review', different)
        self.review = start_source_review(self.store, self.group['id'], 'review-first', 'en', self.current)
        return self.review

    def read(self, query=None, source='tcp-version', start=3, end=3):
        return ReviewAction(action='read', issue='确认数据段条件和 ACK 定义。', requests=[ReviewLookup(
            kind='locate' if query else 'read', source_id=None if query else source,
            line_start=None if query else start, line_end=None if query else end, query=query)],
            outcome=None, name='', reason='', canonical_id=None, citations=[], clarifications=[])

    def conclude(self, outcome='same_knowledge', source='tcp-version', start=1, end=1):
        return ReviewAction(action='conclude', issue='确认数据段条件和 ACK 定义。', requests=[], outcome=outcome,
            name='ACK', reason='受控结论：条件及能力已比较，原文范围真实可见。',
            canonical_id=self.candidates[0]['id'] if outcome == 'duplicate_targets' else None,
            citations=[{'source_id': source, 'line_start': start, 'line_end': end}], clarifications=[])

    def execute(self, actions):
        with patch('outline_review._request_structured', side_effect=actions) as model:
            run_source_review(self.store, self.review, self.current)
        return get_review(self.store, self.review), model

    def test_read_and_verdict_preserve_originals_and_require_user_adoption(self):
        self.begin(different=True)
        before = self.store.get(self.outline)['candidates']
        result, model = self.execute([self.read(), self.conclude(start=3, end=3)])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.counts(), [0] * 5)
        self.assertEqual(self.store.get(self.outline)['candidates'], before)
        self.assertEqual(rows(self.store, self.outline)[1][0]['proposal'], self.group['proposal'])
        second = json.loads(model.call_args_list[1].args[0][1]['content'])
        self.assertTrue(any(e['line_start'] == 3 and 'ACK=120' in e['text'] for e in second['actual_original_excerpts']))
        self.assertIn('English', str(model.call_args_list[0].args[0]))
        new = apply_source_review(self.store, self.review, self.group['revision'], self.current)
        self.assertNotEqual(new, self.group['id'])
        self.assertEqual(apply_source_review(self.store, self.review, 999, self.current), new)
        groups = rows(self.store, self.outline)[1]
        self.assertEqual([g['status'] for g in groups], ['rejected', 'accepted'])
        self.assertEqual(groups[0]['model_proposal'], self.group['model_proposal'])
        self.assertEqual(self.counts(), [0] * 5)
        self.enroll(self.candidates[0]); self.enroll(self.candidates[1])
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_units').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM review_items').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM review_events').fetchone()[0], 0)

    def test_locate_then_cross_source_read_only_actual_read_authorizes_citation(self):
        self.begin(second=True)
        result, model = self.execute([self.read(query='下一个期望字节'), self.read(source='definition-v1', start=3, end=3), self.conclude(source='definition-v1', start=3, end=3)])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(model.call_args_list[-1].args[1], ReviewConclusion)
        intermediate = json.loads(model.call_args_list[1].args[0][1]['content'])
        self.assertFalse(any(c['source_id'] == 'definition-v1' for c in intermediate['actual_original_excerpts']))
        self.assertTrue(intermediate['lookup_observations'][0]['lookups'][0]['matches'])
        public = public_source_reviews(self.store, self.outline)[0]
        self.assertEqual(public['rounds'], 2)
        self.assertEqual(public['steps'][0]['citations'], [])
        self.assertIn('SYN/FIN', public['citations'][0]['excerpt'])

    def test_location_pointer_is_not_citable(self):
        self.begin(second=True)
        r, _ = self.execute([self.read(query='下一个期望字节'), self.conclude(source='definition-v1', start=3, end=3)])
        self.assertEqual(r['status'], 'failed')
        self.assertIn('校验', r['error'])  # Safe public error does not echo provider/request text.
        self.assertIsNone(r['result'])

    def test_two_read_rounds_cannot_execute_third_read(self):
        self.begin()
        r, _ = self.execute([self.read(), self.read(start=5, end=5), self.read(start=7, end=7)])
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(sum(s['status'] == 'completed' for s in review_steps(self.store, self.review)), 2)
        self.assertEqual(public_source_reviews(self.store, self.outline)[0]['rounds'], 2)
        continue_source_review(self.store, self.review, self.current, 1)
        r, m = self.execute([self.conclude('unresolved')])
        self.assertEqual(m.call_args.args[1], ReviewConclusion)
        self.assertEqual(r['result']['outcome'], 'unresolved')
        with self.assertRaises(ValueError): apply_source_review(self.store, self.review, 1, self.current)

    def test_read_explanation_is_preserved_but_never_publishes_a_verdict(self):
        self.begin()
        action = self.read().model_copy(update={'outcome': 'unresolved', 'name': '补读说明', 'reason': '需要查看条件。'})
        with self.practice.transaction() as db: db.execute('UPDATE outline_group_runs SET allowance=calls_used+1 WHERE id=?', (self.run,))
        r, _ = self.execute([action])
        self.assertEqual(r['status'], 'paused'); self.assertIsNone(r['result'])
        self.assertEqual(review_steps(self.store, self.review)[0]['action']['reason'], action.reason)
        self.assertEqual(rows(self.store, self.outline)[1][0]['status'], 'proposed')

    def test_shared_allowance_pause_continue_resume_and_idempotence(self):
        self.begin()
        with self.practice.transaction() as db:
            db.execute('UPDATE outline_group_runs SET allowance=calls_used+1 WHERE id=?', (self.run,))
        r, m = self.execute([self.read()])
        self.assertEqual(r['status'], 'paused'); self.assertEqual(m.call_count, 1)
        first = review_steps(self.store, self.review)[0]['id']
        self.assertEqual(start_source_review(self.store, self.group['id'], 'review-first', 'en', self.current), self.review)
        self.store = OutlineStore(self.practice)
        continue_source_review(self.store, self.review, self.current, 1)
        r, _ = self.execute([self.conclude(start=3, end=3)])
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(review_steps(self.store, self.review)[0]['id'], first)
        with patch('outline_review._request_structured') as model:
            continue_source_review(self.store, self.review, self.current, 9)
            run_source_review(self.store, self.review, self.current)
            model.assert_not_called()
        self.assertEqual(len(review_steps(self.store, self.review)), 2)

    def test_failed_generation_keeps_reads_and_reuses_failed_step_identity(self):
        self.begin()
        r, _ = self.execute([self.read(), RuntimeError('controlled provider failure')])
        self.assertEqual(r['status'], 'failed')
        old = review_steps(self.store, self.review)
        continue_source_review(self.store, self.review, self.current, 1)
        r, m = self.execute([self.conclude(start=3, end=3)])
        self.assertEqual(r['status'], 'completed'); self.assertEqual(m.call_count, 1)
        self.assertEqual([s['id'] for s in review_steps(self.store, self.review)], [s['id'] for s in old])

    def test_pause_during_request_revokes_late_result_and_releases_write_lock(self):
        self.begin()
        def pause(messages, schema):
            pause_review(self.store, self.review)  # A separate short writer works while the model runs.
            return self.conclude()
        r, _ = self.execute(pause)
        self.assertEqual(r['status'], 'paused'); self.assertIsNone(r['result'])
        self.assertEqual(review_steps(self.store, self.review)[0]['status'], 'failed')

    def test_edit_during_request_revokes_result(self):
        self.begin()
        def edit(messages, schema):
            c = self.candidates[0]
            edit_candidate(self.store, c['id'], LearningDraft.model_validate(c['learning']), c['revision'])
            return self.conclude()
        r, _ = self.execute(edit)
        self.assertEqual(r['status'], 'failed'); self.assertIsNone(r['result'])
        self.assertTrue(public_source_reviews(self.store, self.outline)[0]['stale'])

    def test_queued_source_change_fails_without_model_or_lost_input(self):
        self.begin(); before = get_review(self.store, self.review)['inputs_json']
        with patch('outline_review._request_structured') as model:
            run_source_review(self.store, self.review, lambda _: [])
            model.assert_not_called()
        self.assertEqual(get_review(self.store, self.review)['status'], 'failed')
        self.assertEqual(get_review(self.store, self.review)['inputs_json'], before)

    def test_changed_group_invalidates_paused_task_and_allows_new_version(self):
        self.begin(); pause_review(self.store, self.review)
        with self.practice.transaction() as db:
            db.execute('UPDATE outline_groups SET revision=revision+1 WHERE id=?', (self.group['id'],))
        new = start_source_review(self.store, self.group['id'], 'new-version', 'en', self.current)
        self.assertNotEqual(new, self.review)
        self.assertEqual(get_review(self.store, self.review)['status'], 'stale')

    def test_budget_checks_schema_and_required_evidence_without_model(self):
        self.begin(); before = get_review(self.store, self.review)['inputs_json']
        with patch('outline_review.GenerationBudget.from_env', return_value=GenerationBudget(2500, 512, 256)), patch('outline_review._request_structured') as model:
            run_source_review(self.store, self.review, self.current)
            model.assert_not_called()
        self.assertEqual(get_review(self.store, self.review)['status'], 'failed')
        self.assertEqual(get_review(self.store, self.review)['inputs_json'], before)

    def test_actual_full_requests_fit_both_protocols_and_diagnostics(self):
        self.begin()
        r, m = self.execute([self.read(), self.conclude(start=3, end=3)])
        self.assertEqual(r['status'], 'completed')
        for call, step in zip(m.call_args_list, review_steps(self.store, self.review)):
            self.assertTrue(fits_request(GenerationBudget.from_env(), *call.args))
            self.assertLessEqual(step['diagnostic']['estimated_input_tokens'], step['diagnostic']['input_limit'])
            self.assertTrue(step['diagnostic']['visible'])

    def test_block_boundary_trim_and_permissions(self):
        source = Source('s', Path('s.md'), '# Title\n\nshort paragraph\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nlast paragraph')
        request = ReviewLookup(kind='read', source_id='s', line_start=3, line_end=9, query=None)
        observed, chunks = execute_lookup(request, {'s': source}, [], lambda chunks, o: chunks[-1].line_end < 5)
        self.assertEqual(chunks[0].line_end, 3); self.assertTrue(observed['limited'])
        self.begin()
        with self.assertRaises(ValueError): validate_action(self.conclude(source='s', start=5, end=7), self.candidates[:2], chunks, 1)
        request = request.model_copy(update={'line_start': 6, 'line_end': 6})
        observed, chunks = execute_lookup(request, {'s': source}, [], lambda chunks, o: True)
        self.assertEqual((chunks[0].line_start, chunks[0].line_end), (5, 7))
        with self.assertRaises(BudgetError): execute_lookup(request, {'s': source}, [], lambda chunks, o: False)

    def test_unknown_source_and_large_or_invalid_ranges(self):
        self.begin()
        for action in [self.read(source='outside'), self.read(start=0), self.read(end=999)]:
            with self.subTest(action=action):
                with self.assertRaises(ValueError): execute_lookup(action.requests[0], {'tcp-version': self.source}, [], lambda chunks, o: True)

    def test_large_saved_group_does_not_inherit_page_output_member_limit(self):
        self.begin()
        records = [{**self.candidates[0], 'id': f'fixed-{i}'} for i in range(12)]
        from outline_review import original_evidence
        evidence = original_evidence(get_review(self.store, self.review), {'tcp-version': self.source})
        result = self.conclude('duplicate_targets').model_copy(update={'canonical_id': 'fixed-11'})
        validate_action(result, records, evidence, 0)
        records[-1]['learning'] = {**records[-1]['learning'], 'target': {**records[-1]['learning']['target'], 'capability': 'apply'}}
        with self.assertRaises(ValueError): validate_action(result, records, evidence, 0)

    def test_distinct_and_clarifications_do_not_create_learning_records(self):
        self.begin()
        self.execute([self.conclude('distinct')])
        self.assertEqual(apply_source_review(self.store, self.review, 1, self.current), self.group['id'])
        self.assertEqual(rows(self.store, self.outline)[1][0]['status'], 'rejected')
        self.assertEqual(self.counts(), [0] * 5)

    def test_clarification_cannot_be_adopted_or_silently_edit_candidate(self):
        self.begin()
        result = ReviewAction.model_validate({**self.conclude('revise_candidates').model_dump(), 'clarifications': [
            {'candidate_id': self.candidates[0]['id'], 'suggestion': '限定为无 SYN/FIN 的数据段。'}]})
        before = self.store.get(self.outline)['candidates']
        r, _ = self.execute([result]); self.assertEqual(r['status'], 'completed')
        with self.assertRaises(ValueError): apply_source_review(self.store, self.review, 1, self.current)
        self.assertEqual(self.store.get(self.outline)['candidates'], before)

    def test_unresolved_can_include_nonbinding_clarifications_without_acceptance(self):
        self.begin()
        result = ReviewAction.model_validate({**self.conclude('unresolved').model_dump(), 'clarifications': [
            {'candidate_id': self.candidates[0]['id'], 'suggestion': '补充缺失的适用版本后重新核对。'}]})
        r, _ = self.execute([result]); self.assertEqual(r['status'], 'completed')
        with self.assertRaises(ValueError): apply_source_review(self.store, self.review, 1, self.current)
        self.assertEqual(self.counts(), [0] * 5)

    def test_duplicates_can_choose_other_representative_and_enroll_once(self):
        self.begin(); self.execute([self.conclude('duplicate_targets')])
        apply_source_review(self.store, self.review, 1, self.current, canonical_id=self.candidates[1]['id'])
        with self.assertRaises(OutlineBusy): self.enroll(self.candidates[0])
        first = self.enroll(self.candidates[1]); self.assertEqual(self.enroll(self.candidates[1]), first)
        self.assertEqual(self.counts()[2], 1)

    def test_completed_review_stale_cannot_apply(self):
        self.begin(); self.execute([self.conclude()])
        c = self.candidates[0]; edit_candidate(self.store, c['id'], LearningDraft.model_validate(c['learning']), 1)
        with self.assertRaises(OutlineBusy): apply_source_review(self.store, self.review, 1, self.current)
        self.assertEqual(self.counts(), [0] * 5)

    def test_recovery_processing_and_http_contract(self):
        self.begin()
        self.assertTrue(public_outline(self.store, self.outline, self.current)['processing'])
        with self.practice.transaction() as db: db.execute("UPDATE outline_reviews SET status='running',attempt='lost' WHERE id=?", (self.review,))
        self.assertEqual(public_source_reviews(OutlineStore(self.practice), self.outline)[0]['status'], 'failed')
        app = create_app(self.root / 'uploads')
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/outline-groups/missing/source-reviews', json={'request_id': 'x'}).status_code, 404)
            self.assertEqual(client.post('/api/outline-source-reviews/missing/pause').status_code, 404)
            self.assertEqual(client.post('/api/outline-source-reviews/missing/apply', json={'group_revision': 1}).status_code, 404)
            self.assertEqual(client.post('/api/outline-source-reviews/missing/continue', json={'call_limit': True}).status_code, 422)
            self.assertEqual(client.post('/api/outline-groups/missing/source-reviews', json={'request_id': 'x', 'paths': ['outside']}).status_code, 422)

    def test_web_background_wrapper_auto_reviews_needs_review_only(self):
        self.begin()
        with patch('outline_review._request_structured', return_value=self.conclude('unresolved')) as model:
            run_grouping_with_reviews(self.store, self.run, self.current)
        # Existing pending manual task is reused rather than duplicated.
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(public_source_reviews(self.store, self.outline)), 1)
        self.assertEqual(get_run(self.store, self.run)['calls_used'], 2)


if __name__ == '__main__': unittest.main()
