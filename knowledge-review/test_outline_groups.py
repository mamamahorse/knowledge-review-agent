"""Controlled interface tests; mock grouping is not real semantic evaluation."""

import json
import unittest
from contextlib import closing
from unittest.mock import patch

from fastapi.testclient import TestClient
from generation_budget import GenerationBudget
from learning_types import LearningDraft
from outline_store import OutlineStore, OutlineBusy
from outline_service import public_outline, edit_candidate, enroll_candidate
from outline_grouping import (GroupDraft, GroupProposal, start_grouping, run_grouping,
                             retry_grouping, group_messages, validate_result)
from outline_group_store import rows, get_run, review_group
from outline_group_plan import ComparisonPage
import test_outlines as fixtures
from test_outlines import fake_model, TEXT
from web_app import create_app


def page_fixture(messages, result):
    return ComparisonPage(**result.model_dump(), checked_indices=[p['index'] for p in json.loads(messages[1]['content'])['comparisons']], deferred_indices=[])


class OutlineGroupTests(unittest.TestCase):
    setUp = fixtures.OutlineTests.setUp
    tearDown = fixtures.OutlineTests.tearDown
    current = fixtures.OutlineTests.current
    create = fixtures.OutlineTests.create
    generated = fixtures.OutlineTests.generated
    counts = fixtures.OutlineTests.counts

    def prepare(self, kind='same_knowledge', different=False):
        identity, _ = self.generated()
        candidates = self.store.get(identity)['candidates']
        if different:
            c = candidates[1]
            draft = LearningDraft.model_validate(c['learning'])
            edit_candidate(self.store, c['id'], draft.model_copy(update={'target': draft.target.model_copy(update={'capability': 'apply'})}), 1)
            candidates = self.store.get(identity)['candidates']
        run_id = start_grouping(self.store, identity, 'group-first', 'en', [c['id'] for c in candidates[:2]], self.current)
        result = GroupDraft(suggestions=[GroupProposal(kind=kind, candidate_ids=[c['id'] for c in candidates[:2]],
            name='Shared evidence', reason='Same concept, independent criteria and original source evidence.',
            canonical_id=candidates[0]['id'] if kind == 'duplicate_targets' else None)], note='Selected input only.')
        with patch('outline_grouping._request_structured', side_effect=lambda messages, schema: page_fixture(messages, result)):
            run_grouping(self.store, run_id, self.current)
        self.assertEqual(get_run(self.store, run_id)['status'], 'completed')
        return identity, candidates, run_id, rows(self.store, identity)[1][0]

    def decide(self, group, decision='accepted', **changes):
        p = {**group['proposal'], **changes}
        return review_group(self.store, group['id'], group['revision'], decision,
                            p['name'], p['reason'], p['canonical_id'], changes.get('unit_id'))

    def enroll(self, c, unit=None):
        with patch('outline_service._request_structured', side_effect=fake_model):
            return enroll_candidate(self.store, c['id'], c['revision'], 'root', unit, 'en', self.current)

    def test_suggestions_preserve_evidence_versions_and_do_not_enroll(self):
        identity, candidates, run_id, group = self.prepare(different=True)
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        self.assertEqual(group['members'], {c['id']: c['revision'] for c in candidates[:2]})
        self.assertEqual(len(self.store.get(identity)['candidates']), 3)
        messages = group_messages(self.store, get_run(self.store, run_id))
        data = json.loads(messages[1]['content'])
        self.assertEqual(len(data['candidates']), 2)
        for record in data['candidates']:
            for e in record['original_evidence']:
                self.assertEqual(e['text'], '\n'.join(TEXT.splitlines()[e['line_start'] - 1:e['line_end']]))
        self.assertIn('English', str(messages))
        self.decide(group)
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])

    def test_distinct_dimensions_share_one_unit_and_keep_independent_targets_fsrs(self):
        identity, candidates, _, group = self.prepare(different=True)
        self.decide(group)
        first, second = self.enroll(candidates[0]), self.enroll(candidates[1])
        self.assertNotEqual(first['item_id'], second['item_id'])
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_units').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM learning_targets').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM review_items').fetchone()[0], 2)
        self.assertEqual(self.counts()[3], 0)
        self.assertEqual(self.enroll(candidates[0]), first)
        self.assertEqual(len(self.store.get(identity)['candidates']), 3)

    def test_duplicates_keep_user_selected_representative_and_original_records(self):
        identity, candidates, _, group = self.prepare('duplicate_targets')
        self.decide(group, canonical_id=candidates[1]['id'])
        with patch('outline_service._request_structured') as model:
            with self.assertRaises(OutlineBusy):
                enroll_candidate(self.store, candidates[0]['id'], 1, 'root', None, 'en', self.current)
            model.assert_not_called()
        self.enroll(candidates[1])
        public = public_outline(self.store, identity, self.current)
        self.assertTrue(public['candidates'][0]['suppressed'])
        self.assertEqual(public['candidates'][0]['status'], 'proposed')
        self.assertEqual(len(public['candidates']), 3)
        self.assertEqual(self.counts()[2], 1)

    def test_duplicate_different_dimensions_and_unknown_ids_are_rejected(self):
        identity, candidates, run_id, _ = self.prepare(different=True)
        inputs = json.loads(get_run(self.store, run_id)['inputs_json'])
        bad = GroupProposal(kind='duplicate_targets', candidate_ids=[c['id'] for c in candidates[:2]],
            canonical_id=candidates[0]['id'], name='Bad', reason='Not duplicates')
        with self.assertRaises(ValueError):
            validate_result(GroupDraft(suggestions=[bad], note='bad'), inputs)
        bad = bad.model_copy(update={'candidate_ids': ['invented', candidates[0]['id']]})
        with self.assertRaises(ValueError):
            validate_result(GroupDraft(suggestions=[bad], note='bad'), inputs)

    def test_uncertain_suggestion_cannot_be_accepted_and_rejection_is_idempotent(self):
        _, _, _, group = self.prepare('needs_review')
        with self.assertRaises(ValueError):
            self.decide(group)
        revision = self.decide(group, 'rejected')
        self.assertEqual(self.decide(group, 'rejected'), revision)
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])

    def test_review_edit_and_stale_browser_revision(self):
        identity, _, _, group = self.prepare()
        self.decide(group, 'proposed', name='Edited grouping')
        with self.assertRaises(OutlineBusy):
            self.decide(group)
        group = rows(self.store, identity)[1][0]
        self.assertEqual(group['proposal']['name'], 'Edited grouping')
        self.assertEqual(group['model_proposal']['name'], 'Shared evidence')
        self.decide(group)

    def test_candidate_edit_invalidates_accepted_group_without_rewriting_old_plan(self):
        identity, candidates, _, group = self.prepare()
        self.decide(group)
        first = self.enroll(candidates[0])
        before = self.counts()
        draft = LearningDraft.model_validate(candidates[1]['learning'])
        edit_candidate(self.store, candidates[1]['id'], draft.model_copy(update={'target': draft.target.model_copy(update={'title': 'Changed scope'})}), 1)
        self.assertEqual(rows(self.store, identity)[1][0]['status'], 'stale')
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.enroll(candidates[0]), first)

    def test_approval_changed_during_initial_question_rolls_back_every_record(self):
        identity, candidates, _, group = self.prepare()
        self.decide(group)
        accepted = rows(self.store, identity)[1][0]
        def changed(messages, schema):
            self.decide(accepted, 'rejected')
            return fake_model(messages, schema)
        with patch('outline_service._request_structured', side_effect=changed):
            with self.assertRaises(OutlineBusy):
                enroll_candidate(self.store, candidates[0]['id'], 1, 'root', None, 'en', self.current)
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        self.assertEqual(self.store.candidate(candidates[0]['id'])['status'], 'proposed')
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_units').fetchone()[0], 0)

    def test_concurrent_first_targets_bind_same_unit_with_no_network_write_lock(self):
        identity, candidates, _, group = self.prepare(different=True)
        self.decide(group)
        def nested(messages, schema):
            self.enroll(candidates[1])
            self.library.create('Write during network', 'root')
            return fake_model(messages, schema)
        with patch('outline_service._request_structured', side_effect=nested):
            enroll_candidate(self.store, candidates[0]['id'], 1, 'root', None, 'en', self.current)
        self.assertEqual(self.counts()[2], 2)
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_units').fetchone()[0], 1)

    def test_fail_retry_and_repeated_delivery_publish_once(self):
        identity, _ = self.generated()
        candidates = self.store.get(identity)['candidates'][:2]
        run_id = start_grouping(self.store, identity, 'failure', 'zh', None, self.current)
        with patch('outline_grouping._request_structured', side_effect=RuntimeError('private provider body')):
            run_grouping(self.store, run_id, self.current)
        self.assertEqual(get_run(self.store, run_id)['status'], 'failed')
        self.assertNotIn('private', get_run(self.store, run_id)['error'])
        self.assertEqual(len(self.store.get(identity)['candidates']), 3)
        retry_grouping(self.store, run_id, self.current)
        result = GroupDraft(suggestions=[GroupProposal(kind='same_knowledge', candidate_ids=[c['id'] for c in candidates],
            name='Same knowledge', reason='Evidence', canonical_id=None)], note='No complete coverage claim')
        with patch('outline_grouping._request_structured', side_effect=lambda messages, schema: page_fixture(messages, result)) as model:
            run_grouping(self.store, run_id, self.current)
            run_grouping(self.store, run_id, self.current)
            self.assertEqual(model.call_count, 1)
        self.assertEqual(len(rows(self.store, identity)[1]), 1)
        self.assertEqual(start_grouping(self.store, identity, 'failure', 'zh', None, self.current), run_id)

    def test_changed_input_discards_late_grouping_and_requires_new_request(self):
        identity, _ = self.generated()
        candidates = self.store.get(identity)['candidates']
        run_id = start_grouping(self.store, identity, 'late', 'zh', None, self.current)
        def late(messages, schema):
            self.library.create('Network is not a write transaction', 'root')
            draft = LearningDraft.model_validate(candidates[0]['learning'])
            edit_candidate(self.store, candidates[0]['id'], draft, 1)
            return page_fixture(messages, GroupDraft(suggestions=[], note='Late result'))
        with patch('outline_grouping._request_structured', side_effect=late):
            run_grouping(self.store, run_id, self.current)
        self.assertEqual(get_run(self.store, run_id)['status'], 'failed')
        self.assertEqual(rows(self.store, identity)[1], [])
        with self.assertRaises(OutlineBusy):
            retry_grouping(self.store, run_id, self.current)

    def test_recovery_and_restart_preserve_suggestions_and_originals(self):
        identity, candidates, _, group = self.prepare()
        self.decide(group)
        reopened = OutlineStore(self.practice)
        self.assertEqual(public_outline(reopened, identity, self.current)['grouping']['suggestions'][0]['status'], 'accepted')
        self.assertEqual(len(reopened.get(identity)['candidates']), 3)

    def test_abandoned_worker_recovered_and_concurrent_task_rejected(self):
        identity, _ = self.generated()
        run_id = start_grouping(self.store, identity, 'abandoned', 'zh', None, self.current)
        with self.assertRaises(OutlineBusy):
            start_grouping(self.store, identity, 'other', 'zh', None, self.current)
        with self.store.lock(run_id):
            with self.practice.transaction() as db:
                db.execute("UPDATE outline_group_runs SET status='running' WHERE id=?", (run_id,))
            self.assertTrue(public_outline(self.store, identity, self.current)['processing'])
        recovered = public_outline(self.store, identity, self.current)
        self.assertEqual(recovered['grouping']['runs'][0]['status'], 'failed')
        retry_grouping(self.store, run_id, self.current)
        self.assertEqual(get_run(self.store, run_id)['status'], 'queued')

    def test_complete_schema_budget_failure_does_not_truncate_candidates_or_call_model(self):
        identity, _ = self.generated()
        run_id = start_grouping(self.store, identity, 'budget', 'zh', None, self.current)
        before = get_run(self.store, run_id)['inputs_json']
        with patch('outline_grouping.GenerationBudget.from_env', return_value=GenerationBudget(3000, 512, 256)), patch('outline_grouping._request_structured') as model:
            run_grouping(self.store, run_id, self.current)
            model.assert_not_called()
        self.assertEqual(get_run(self.store, run_id)['status'], 'failed')
        self.assertIn('预算', get_run(self.store, run_id)['error'])
        self.assertEqual(before, get_run(self.store, run_id)['inputs_json'])

    def test_scope_change_prevents_publish_and_acceptance_http(self):
        identity, candidates, run_id, group = self.prepare()
        with self.assertRaises(OutlineBusy):
            start_grouping(self.store, identity, 'new', 'zh', None, lambda _: [])
        with patch('outline_grouping._request_structured') as model:
            retry_grouping(self.store, run_id, self.current)
            run_grouping(self.store, run_id, lambda _: [])
            model.assert_not_called()
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])

    def test_existing_unit_choice_keeps_prior_item_and_shared_source_snapshots(self):
        identity, candidates, _, group = self.prepare()
        old_item = self.enroll(candidates[2])
        with closing(self.practice._connect()) as db:
            unit_id = db.execute('SELECT id FROM knowledge_units').fetchone()[0]
        self.decide(group, unit_id=unit_id)
        self.enroll(candidates[0])
        self.enroll(candidates[1])
        self.assertEqual(self.enroll(candidates[2]), old_item)
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_units').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_overlap_acceptance_blocked_and_withdrawal_leaves_enrolled_items_untouched(self):
        identity, candidates, _, first = self.prepare()
        run_id = start_grouping(self.store, identity, 'overlap', 'zh', None, self.current)
        result = GroupDraft(suggestions=[GroupProposal(kind='same_knowledge', candidate_ids=[c['id'] for c in candidates[1:]],
            name='Overlap', reason='Potential relation', canonical_id=None)], note='Overlap')
        with patch('outline_grouping._request_structured', side_effect=lambda messages, schema: page_fixture(messages, result)):
            run_grouping(self.store, run_id, self.current)
        second = rows(self.store, identity)[1][1]
        self.decide(first)
        with self.assertRaises(OutlineBusy):
            self.decide(second)
        self.enroll(candidates[0])
        before = self.counts()
        self.decide(rows(self.store, identity)[1][0], 'rejected')
        self.assertEqual(self.counts(), before)

    def test_http_generate_review_refresh_and_duplicate_post(self):
        app = create_app(self.root / 'uploads', self.practice.path)
        collection = self.library.create('HTTP scope', 'root')['id']
        with TestClient(app) as client:
            response = client.post(f'/api/documents?collection_id={collection}', files=[('files', ('tcp.md', TEXT.encode(), 'text/markdown'))])
            self.assertEqual(response.status_code, 201, response.text)
            document = client.get(f"/api/documents/{response.json()[0]['id']}").json()
        from sources import load_sources
        path = next((self.root / 'uploads').glob('*/*.md'))
        self.source = load_sources([path])[0]
        self.snapshots = [{'document_id': document['id'], 'source_id': self.source.source_id,
                           'path': str(path), 'name': 'tcp.md', 'text': TEXT}]
        fixture_id = self.create(collection=collection)
        with patch('outline_service._request_structured', side_effect=fake_model):
            fixtures.run_outline(self.store, fixture_id, self.current)
        with TestClient(app) as client:
            ids = [c['id'] for c in self.store.get(fixture_id)['candidates'][:2]]
            result = GroupDraft(suggestions=[GroupProposal(kind='same_knowledge', candidate_ids=ids,
                name='HTTP group', reason='Reviewable evidence', canonical_id=None)], note='Mock interface')
            with patch('outline_grouping._request_structured', side_effect=lambda messages, schema: page_fixture(messages, result)):
                payload = {'request_id': 'group-api', 'candidate_ids': ids, 'response_language': 'en'}
                response = client.post(f'/api/outlines/{fixture_id}/grouping', json=payload)
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(client.post(f'/api/outlines/{fixture_id}/grouping', json=payload).json(), response.json())
            job = client.get(f'/api/outlines/{fixture_id}').json()
            group = job['grouping']['suggestions'][0]
            payload = {k: group['proposal'][k] for k in ('name', 'reason', 'canonical_id')}
            payload.update(revision=group['revision'], decision='accepted')
            url = f"/api/outline-groups/{group['id']}"
            approved = client.patch(url, json=payload)
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(client.patch(url, json=payload).json(), approved.json())
            self.assertEqual(client.get(f'/api/outlines/{fixture_id}').json()['grouping']['suggestions'][0]['status'], 'accepted')

    def test_forged_evidence_outside_original_visibility_is_not_sent_to_group_model(self):
        identity, c = self.generated()
        draft = c['learning']
        draft['knowledge']['citations'][0]['line_start'] = 7
        draft['knowledge']['citations'][0]['line_end'] = 7
        with self.practice.transaction() as db:
            db.execute('UPDATE outline_candidates SET proposal_json=? WHERE id=?', (json.dumps(draft), c['id']))
        run_id = start_grouping(self.store, identity, 'forged', 'zh', None, self.current)
        with patch('outline_grouping._request_structured') as model:
            run_grouping(self.store, run_id, self.current)
            model.assert_not_called()
        self.assertEqual(get_run(self.store, run_id)['status'], 'failed')
        self.assertEqual(rows(self.store, identity)[1], [])

    def test_first_seed_failure_does_not_bind_unit_and_retries_create_once(self):
        identity, candidates, _, group = self.prepare()
        self.decide(group)
        with patch('outline_service._request_structured', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                enroll_candidate(self.store, candidates[0]['id'], 1, 'root', None, 'en', self.current)
        self.assertIsNone(rows(self.store, identity)[1][0]['unit_id'])
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        self.enroll(candidates[0])
        self.enroll(candidates[0])
        self.assertEqual(self.counts()[2], 1)

    def test_database_failure_rolls_back_new_group_unit_and_seed_atomically(self):
        identity, candidates, _, group = self.prepare()
        self.decide(group)
        with patch('outline_service._request_structured', side_effect=fake_model), patch.object(self.practice, 'enroll', side_effect=RuntimeError('transaction failed')):
            with self.assertRaises(RuntimeError):
                enroll_candidate(self.store, candidates[0]['id'], 1, 'root', None, 'en', self.current)
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        self.assertIsNone(rows(self.store, identity)[1][0]['unit_id'])
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_units').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
