"""Controlled workflow tests; relationship fixtures are not real semantic evidence."""

import json
import shutil
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from generation_budget import GenerationBudget
from learning_store import LearningStore
from learning_types import LearningDraft
from outline_grouping import GroupDraft, GroupProposal, start_grouping, run_grouping, retry_grouping, public_grouping
from outline_group_plan import ComparisonPage, comparisons, work_scopes, make_plan
from outline_group_store import get_run, rows, recover_runs, review_group
from outline_group_steps import pause_grouping, steps
from outline_integration import validate_page
from outline_service import section_manifest, scope_fingerprint, edit_candidate, enroll_candidate
from outline_store import OutlineStore, OutlineBusy
from sources import Source


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / '.data' / 'evaluations' / ('integration-test-' + uuid4().hex)
        self.root.mkdir(parents=True)
        self.practice = LearningStore(self.root / 'practice.sqlite3')
        self.store = OutlineStore(self.practice)
        self.settings = patch('outline_service.model_settings', return_value=('controlled-model', {}))
        self.settings.start()
        self.settings2 = patch('model_client.model_settings', return_value=('controlled-model', {}))
        self.settings2.start()
        self.budget = GenerationBudget()
        self.env = patch.object(GenerationBudget, 'from_env', side_effect=lambda: self.budget)
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.settings2.stop(); self.settings.stop()
        self.assertTrue(self.root.resolve().is_relative_to((Path(__file__).resolve().parent / '.data' / 'evaluations').resolve()))
        shutil.rmtree(self.root)

    def seed(self, n=12, body='RESET 终止当前连接，不表示成功接收字节。', one_section=False):
        text = '# synthetic\n' + body if one_section else '\n'.join(f'## Chapter {i}\n{body}' for i in range(n))
        source = Source('integration-source', Path('synthetic.md'), text)
        self.snapshots = [{'document_id': 'synthetic', 'source_id': source.source_id, 'path': str(source.path), 'name': source.path.name, 'text': text}]
        sections = section_manifest([source], self.snapshots)
        batches = [{**s, 'section_id': s['id']} for s in sections]
        identity = self.store.create('root', 'fixtures', '按固定资料整理目标', 'zh', scope_fingerprint(self.snapshots), self.snapshots, sections, batches)
        self.store.start(identity)
        count = 0
        while (batch := self.store.claim_batch(identity)):
            proposals = []
            for _ in range(n if one_section else 1):
                proposals.append(LearningDraft.model_validate({'knowledge': {'name': 'RESET', 'statement': 'RESET 终止连接，不确认字节。',
                    'content_tags': ['protocol'], 'citations': [{'source_id': source.source_id, 'line_start': batch['line_start'] + 1, 'line_end': batch['line_start'] + 1}]},
                    'target': {'title': f'目标 {count}', 'capability': 'explain', 'description': '解释 RESET 的职责。',
                        'scope': '仅限本样例的数据传输连接，不涉及 GUI。', 'criteria': ['说明终止连接', '区分字节确认']}}))
                count += 1
            self.store.finish_batch(batch, proposals, 'Controlled candidates', visible=[{'source_id': source.source_id,
                'line_start': batch['line_start'], 'line_end': batch['line_end']}])
        self.store.settle(identity)
        self.candidates = self.store.get(identity)['candidates']
        self.outline = identity
        return identity

    def current(self, collection):
        return self.snapshots

    def begin(self, limit=200):
        return start_grouping(self.store, self.outline, uuid4().hex, 'zh', None, self.current, limit)

    def model(self, messages, schema, relations=True):
        data = json.loads(messages[1]['content'])
        own = data['candidates']
        proposals = []
        if relations:
            ids = [r['id'] for r in own]
            proposals.append(GroupProposal(kind='duplicate_targets', candidate_ids=ids, name='RESET', reason='受控结果：固定条件和目标相同。', canonical_id=ids[0]))
        return schema(suggestions=proposals, note='Controlled response, no semantic quality claim.',
            checked_indices=[p['index'] for p in data['comparisons']], deferred_indices=[])

    def execute(self, run, model=None):
        with patch('outline_grouping._request_structured', side_effect=model or self.model):
            run_grouping(self.store, run, self.current)
        return get_run(self.store, run)

    def test_small_input_and_output_use_direct_with_receipt(self):
        self.seed(3, one_section=True)
        run = self.begin()
        result = self.execute(run)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['plan_json'])['mode'], 'direct')
        self.assertTrue(all(json.loads(result['plan_json'])['routing'].values()))
        self.assertEqual(result['calls_used'], 1)
        public = public_grouping(self.store, self.outline)['runs'][0]
        self.assertEqual(public['progress']['handled_pairs'], 3)
        self.assertEqual(public['progress']['verified_pairs'], 3)

    def test_hierarchy_handles_distant_pairs_singletons_and_large_group(self):
        self.seed(12)
        run = self.begin()
        result = self.execute(run)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['plan_json'])['mode'], 'hierarchical')
        public = public_grouping(self.store, self.outline)
        self.assertEqual(public['runs'][0]['progress']['handled_pairs'], 66)
        self.assertEqual(public['runs'][0]['progress']['pending_verification'], 0)
        self.assertEqual(len(public['suggestions']), 1)
        self.assertEqual(len(public['suggestions'][0]['members']), 12)
        self.assertTrue(public['suggestions'][0]['support'])
        self.assertEqual(len(self.store.get(self.outline)['candidates']), 12)
        self.assertEqual(public['runs'][0]['progress']['verified_pairs'], 66)

    def test_input_pressure_routes_without_cutting_required_evidence(self):
        self.seed(3, body='RESET original evidence. ' * 350)
        run = self.begin()
        seen = []
        def model(messages, schema):
            data = json.loads(messages[1]['content']); seen.append(data)
            return self.model(messages, schema)
        result = self.execute(run, model)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['plan_json'])['mode'], 'hierarchical')
        self.assertTrue(any('original_evidence' not in c for d in seen for c in d['candidates']))
        self.assertFalse(json.loads(result['plan_json'])['routing']['input_fits'])
        actual = [c for d in seen for c in d['candidates'] if 'original_evidence' in c]
        self.assertTrue(actual)
        self.assertTrue(all(len(c['original_evidence'][0]['text']) > 8000 for c in actual))

    def test_output_pressure_alone_selects_hierarchy(self):
        self.seed(3, one_section=True)
        self.budget = GenerationBudget(50000, 2400, 2048)
        run = self.begin()
        result = self.execute(run)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['plan_json'])['mode'], 'hierarchical')
        self.assertFalse(json.loads(result['plan_json'])['routing']['estimated_output_fits'])
        self.assertIn('输出估计', json.loads(result['plan_json'])['reason'])
        self.assertGreater(result['calls_used'], 1)

    def test_cards_preserve_full_conditions_and_never_publish_unverified_hypothesis(self):
        self.seed(9)
        ids = {c['id'] for c in self.candidates}
        conditions = self.candidates[0]['learning']['target']['scope']
        observed = []
        def model(messages, schema):
            data = json.loads(messages[1]['content'])
            self.assertTrue({c['id'] for c in data['candidates']} <= ids)
            self.assertTrue(all(c['learning']['target']['scope'] == conditions for c in data['candidates']))
            route = 'original_evidence' not in data['candidates'][0]
            observed.append(route)
            return self.model(messages, schema, route)  # Original text refutes all hypotheses.
        run = self.begin()
        self.assertEqual(self.execute(run, model)['status'], 'completed')
        self.assertTrue(any(observed) and not all(observed))
        self.assertEqual(rows(self.store, self.outline)[1], [])

    def test_call_allowance_pause_restart_continue_reuses_completed_steps(self):
        self.seed(9)
        run = self.begin(1)
        self.assertEqual(self.execute(run)['status'], 'paused')
        first = {s['id'] for s in steps(self.store, run) if s['status'] == 'completed'}
        self.assertTrue(first)
        self.assertEqual(rows(self.store, self.outline)[1], [])
        self.store = OutlineStore(self.practice)
        retry_grouping(self.store, run, self.current, 200)
        self.assertEqual(self.execute(run)['status'], 'completed')
        final = {s['id'] for s in steps(self.store, run) if s['status'] == 'completed'}
        self.assertTrue(first <= final)
        with patch('outline_grouping._request_structured') as model:
            run_grouping(self.store, run, self.current)
            model.assert_not_called()

    def test_pause_discards_inflight_return_and_preserves_pending(self):
        self.seed(9)
        run = self.begin()
        def pause(messages, schema):
            result = self.model(messages, schema)
            pause_grouping(self.store, run)
            return result
        result = self.execute(run, pause)
        self.assertEqual(result['status'], 'paused')
        self.assertFalse(any(s['status'] == 'completed' for s in steps(self.store, run)))
        self.assertEqual(rows(self.store, self.outline)[1], [])
        retry_grouping(self.store, run, self.current)
        self.assertEqual(self.execute(run)['status'], 'completed')

    def test_missing_receipt_is_failure_not_independent_candidates(self):
        self.seed(3)
        run = self.begin()
        result = self.execute(run, lambda messages, schema: GroupDraft(suggestions=[], note='No receipt'))
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(any(s['status'] == 'completed' for s in steps(self.store, run)))
        self.assertEqual(rows(self.store, self.outline)[1], [])

    def test_deferred_work_automatically_continues_and_no_progress_is_bounded(self):
        self.seed(3, one_section=True)
        run = self.begin()
        calls = []
        def deferred(messages, schema):
            data = json.loads(messages[1]['content']); calls.append(len(data['comparisons']))
            if len(calls) == 1:
                return schema(suggestions=[], note='Remaining work', checked_indices=[],
                              deferred_indices=[p['index'] for p in data['comparisons']])
            return self.model(messages, schema)
        self.assertEqual(self.execute(run, deferred)['status'], 'completed')
        self.assertEqual(calls[0], 3)
        self.assertTrue(all(n == 1 for n in calls[1:]))
        other = self.begin()
        def no_progress(messages, schema):
            indices = [p['index'] for p in json.loads(messages[1]['content'])['comparisons']]
            return schema(suggestions=[], note='Cannot complete', checked_indices=[], deferred_indices=indices)
        result = self.execute(other, no_progress)
        self.assertEqual(result['status'], 'failed')
        self.assertLessEqual(result['calls_used'], 2)

    def test_explicit_truncation_splits_but_other_errors_do_not(self):
        self.seed(3, one_section=True)
        run = self.begin()
        count = []
        def truncated(messages, schema):
            count.append(1)
            if len(count) == 1:
                raise RuntimeError('practice result was truncated')
            return self.model(messages, schema)
        result = self.execute(run, truncated)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(any(s['status'] == 'failed' for s in steps(self.store, run)))
        other = self.begin()
        result = self.execute(other, lambda messages, schema: (_ for _ in ()).throw(RuntimeError('PRIVATE-KEY-MARKER')))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['calls_used'], 1)
        self.assertNotIn('PRIVATE-KEY-MARKER', result['error'])

    def test_nontransitive_relations_do_not_union_into_one_group(self):
        self.seed(3, one_section=True)
        a, b, c = [candidate['id'] for candidate in self.candidates]
        run = self.begin()
        result = ComparisonPage(suggestions=[GroupProposal(kind='same_knowledge', candidate_ids=[a, b], name='AB', reason='Controlled AB', canonical_id=None),
            GroupProposal(kind='same_knowledge', candidate_ids=[b, c], name='BC', reason='Controlled BC', canonical_id=None)],
            note='No AC relation', checked_indices=[1, 2, 3], deferred_indices=[])
        self.assertEqual(self.execute(run, lambda messages, schema: result)['status'], 'completed')
        groups = rows(self.store, self.outline)[1]
        self.assertEqual({frozenset(g['members']) for g in groups}, {frozenset([a, b]), frozenset([b, c])})

    def test_version_change_during_request_rejects_old_step_and_retry(self):
        self.seed(3)
        run = self.begin()
        def changed(messages, schema):
            result = self.model(messages, schema)
            candidate = self.candidates[0]
            edit_candidate(self.store, candidate['id'], LearningDraft.model_validate(candidate['learning']), candidate['revision'])
            return result
        self.assertEqual(self.execute(run, changed)['status'], 'failed')
        self.assertEqual(rows(self.store, self.outline)[1], [])
        with self.assertRaises(OutlineBusy):
            retry_grouping(self.store, run, self.current)

    def test_every_request_counts_complete_protocol_and_real_visibility(self):
        self.seed(9)
        run = self.begin()
        self.assertEqual(self.execute(run)['status'], 'completed')
        for s in steps(self.store, run):
            diagnostic = s['diagnostic']
            self.assertLessEqual(diagnostic['estimated_input_tokens'], diagnostic['input_limit'])
            self.assertGreater(diagnostic['output_reserved'], 0)
            self.assertEqual(bool(diagnostic['visible']), s['kind'] != 'route')
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM review_items').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM review_events').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_legacy_null_plan_uses_original_contract(self):
        self.seed(3, one_section=True)
        run = self.begin()
        with self.practice.transaction() as db:
            db.execute('UPDATE outline_group_runs SET plan_json=NULL WHERE id=?', (run,))
        with patch('outline_grouping._request_structured', return_value=GroupDraft(suggestions=[], note='Legacy preserved')):
            run_grouping(self.store, run, self.current)
        self.assertEqual(get_run(self.store, run)['status'], 'completed')
        self.assertEqual(steps(self.store, run), [])

    def test_hierarchy_pair_assignment_is_exact_and_not_adjacent_only(self):
        ids = [str(i) for i in range(13)]
        plan = {'mode': 'hierarchical', 'leaves': [ids[:4], ids[4:8], ids[8:]], 'candidate_ids': ids}
        assigned = [tuple(sorted(p)) for _, _, pairs in work_scopes(plan) for p in pairs]
        self.assertEqual(len(assigned), 78)
        self.assertEqual(len(set(assigned)), 78)
        self.assertIn(('0', '12'), assigned)

    def test_unknown_duplicate_omitted_and_unprocessed_receipts_rejected(self):
        records = [{'id': x, 'learning': {'target': {'capability': 'explain'}}} for x in ['a', 'b', 'c']]
        pairs = comparisons(['a', 'b', 'c'])
        for checked, deferred in ([([1, 1], [2, 3]), ([1], []), ([1, 2, 3, 99], []), ([1], [1, 2, 3])]):
            with self.assertRaises(ValueError):
                validate_page(ComparisonPage(suggestions=[], note='bad', checked_indices=checked, deferred_indices=deferred), records, pairs)
        bad = GroupProposal(kind='same_knowledge', candidate_ids=['a', 'b'], name='bad', reason='unprocessed', canonical_id=None)
        with self.assertRaises(ValueError):
            validate_page(ComparisonPage(suggestions=[bad], note='bad', checked_indices=[2, 3], deferred_indices=[1]), records, pairs)

    def test_full_suggestion_slots_revisit_unreported_comparisons(self):
        self.seed(4, one_section=True)
        run = self.begin()
        observed = []
        def model(messages, schema):
            data = json.loads(messages[1]['content']); observed.append(data['comparisons'])
            proposals = [GroupProposal(kind='same_knowledge', candidate_ids=[p['left'], p['right']], name='Same', reason='Controlled pair', canonical_id=None)
                         for p in data['comparisons'][:4]]
            return schema(suggestions=proposals, note='Slots filled', checked_indices=[p['index'] for p in data['comparisons']], deferred_indices=[])
        self.assertEqual(self.execute(run, model)['status'], 'completed')
        self.assertGreater(len(observed), 1)
        self.assertEqual(len(rows(self.store, self.outline)[1][0]['members']), 4)
        self.assertEqual(public_grouping(self.store, self.outline)['runs'][0]['progress']['verified_pairs'], 6)

    def test_large_duplicate_group_review_and_opt_in_keep_originals_and_single_item(self):
        self.seed(9)
        run = self.begin()
        self.assertEqual(self.execute(run)['status'], 'completed')
        group = rows(self.store, self.outline)[1][0]
        proposal = group['proposal']
        chosen = self.candidates[-1]
        review_group(self.store, group['id'], group['revision'], 'accepted', proposal['name'], proposal['reason'], chosen['id'], None)
        with self.assertRaises(OutlineBusy):
            enroll_candidate(self.store, self.candidates[0]['id'], 1, 'root', None, 'zh', self.current)
        def question(messages, schema):
            learning = json.loads(messages[1]['content'])['approved_learning']
            return schema(objective='RESET', question='解释 RESET 的职责及与字节确认的区别。',
                rubric_points=learning['target']['criteria'], citations=learning['knowledge']['citations'], criterion_indices=[1, 2])
        with patch('outline_service._request_structured', side_effect=question):
            item = enroll_candidate(self.store, chosen['id'], 1, 'root', None, 'zh', self.current)
            self.assertEqual(enroll_candidate(self.store, chosen['id'], 1, 'root', None, 'zh', self.current), item)
        self.assertEqual(len(self.store.get(self.outline)['candidates']), 9)
        with closing(self.practice._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM review_items').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM review_events').fetchone()[0], 0)

    def test_dead_worker_recovery_preserves_completed_step(self):
        self.seed(9)
        run = self.begin(1)
        self.assertEqual(self.execute(run)['status'], 'paused')
        completed = {s['id'] for s in steps(self.store, run) if s['status'] == 'completed'}
        with self.practice.transaction() as db:
            db.execute("UPDATE outline_group_runs SET status='running',attempt='dead-attempt' WHERE id=?", (run,))
        recover_runs(self.store, self.outline)
        self.assertEqual(get_run(self.store, run)['status'], 'failed')
        self.assertEqual({s['id'] for s in steps(self.store, run) if s['status'] == 'completed'}, completed)
        retry_grouping(self.store, run, self.current, 200)
        self.assertEqual(self.execute(run)['status'], 'completed')

    def test_http_pause_unknown_run_and_invalid_allowance(self):
        from fastapi.testclient import TestClient
        from web_app import create_app
        self.seed(9)
        run = self.begin()
        with TestClient(create_app(self.root / 'notes', self.practice.path)) as client:
            self.assertEqual(client.post(f'/api/outline-group-runs/{run}/pause').status_code, 200)
            self.assertEqual(get_run(self.store, run)['status'], 'paused')
            self.assertEqual(client.post('/api/outline-group-runs/unknown/pause').status_code, 404)
            self.assertEqual(client.post(f'/api/outline-group-runs/{run}/retry', json={'call_limit': 0}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
