"""Evaluation plumbing tests use controlled responses, never live semantics."""

import copy
import io
import json
import shutil
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch
from uuid import uuid4

from evaluation_cases import CORPUS, file_for, load_suite, sources_for, validate_suite
from evaluation_report import apply_review, relation_checks, review_template
from generation_budget import GenerationBudget
import quality_eval


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.root = quality_eval.OUTPUT_ROOT / ('test-' + uuid4().hex)
        self.root.mkdir(parents=True)
        self.suite, self.digest = load_suite()

    def tearDown(self):
        # A literal path owned by this test, under the evaluations directory.
        self.assertTrue(self.root.resolve().is_relative_to(quality_eval.OUTPUT_ROOT.resolve()))
        shutil.rmtree(self.root)

    def run_case(self, **kwargs):
        return quality_eval.run_suite(output_root=self.root, case_ids=['small_tcp'], **kwargs)

    def test_default_validates_offline_without_provider_or_database(self):
        with patch('quality_eval.REAL_REQUEST') as provider, patch('quality_eval.LearningStore') as database, redirect_stdout(io.StringIO()):
            self.assertEqual(quality_eval.main([]), 0)
        provider.assert_not_called()
        database.assert_not_called()

    def test_versioned_inputs_and_reference_ranges_are_valid(self):
        self.assertEqual(len(self.suite.cases), 7)
        self.assertEqual(sum(len(c.grades) for c in self.suite.cases), 4)
        self.assertEqual(len(self.digest), 64)
        broken = self.suite.model_copy(deep=True)
        broken.cases[0].reference_points[0].evidence[0].line_end = 9000
        with self.assertRaises(ValueError):
            validate_suite(broken, CORPUS.parent)

    def test_file_changes_invalidate_old_reference_version(self):
        broken = self.suite.model_copy(deep=True)
        broken.file_sha256['notes/small-tcp.md'] = 'outdated'
        with self.assertRaisesRegex(ValueError, '内容已改变'):
            validate_suite(broken, CORPUS.parent)

    def test_traversal_and_undeclared_relations_are_rejected(self):
        with self.assertRaises(ValueError):
            file_for(CORPUS.parent, '../README.md')
        broken = self.suite.model_copy(deep=True)
        broken.cases[0].relations[0].pair[0] = 'unknown'
        with self.assertRaises(ValueError):
            validate_suite(broken, CORPUS.parent)

    def test_smoke_runs_services_in_new_db_without_enrolling(self):
        with patch('quality_eval.REAL_REQUEST') as provider:
            directory, report = self.run_case()
        provider.assert_not_called()
        self.assertTrue(report['completed'])
        self.assertEqual(len(report['calls']), 6)
        self.assertEqual(report['cases'][0]['learning_records'], {'review_items': 0, 'review_events': 0})
        with closing(sqlite3.connect(directory / 'small_tcp.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM practice_questions WHERE answer_text IS NOT NULL').fetchone()[0], 4)
        self.assertTrue((directory / 'review-template.json').exists())
        self.assertEqual(json.loads((directory / 'results.json').read_text(encoding='utf-8')), report)

    def test_complete_protocol_budget_recorded_for_every_call(self):
        _, report = self.run_case()
        self.assertIn('SemanticGradeDraft', {c['schema'] for c in report['calls']})
        for call in report['calls']:
            d = call['diagnostic']
            self.assertLessEqual(d['estimated_input_tokens'], d['input_limit'])
            self.assertGreater(d['estimated_input_tokens'], GenerationBudget.estimate(call['messages']))
            self.assertGreater(d['output_reserved'], 0)
            self.assertGreater(d['margin_tokens'], 0)

    def test_live_request_has_no_reference_answers_or_relation_labels(self):
        case = self.suite.cases[0]
        _, _, aliases = sources_for(case, CORPUS.parent)
        def fake_live(messages, schema):
            raw = json.dumps(messages, ensure_ascii=False)
            for field in ('expected_assessments', 'expected_covered', 'reference_points', 'forbidden', 'rationale'):
                self.assertNotIn(field, raw)
            fixture_ids, example = {}, None
            if issubclass(schema, quality_eval.GroupDraft):
                candidates = json.loads(messages[1]['content'])['candidates']
                for alias, draft in case.candidate_fixtures.items():
                    fixture_ids[alias] = next(c['id'] for c in candidates if c['learning']['target']['title'] == draft.target.title)
            if schema is quality_eval.SemanticGradeDraft:
                example = next(g for g in case.grades if '\n' + g.answer + '\n' in messages[1]['content'])
            return quality_eval.controlled_response(case, aliases, fixture_ids, example, messages, schema)
        with patch('quality_eval.REAL_REQUEST', side_effect=fake_live), patch('model_client.model_settings', return_value=('test-live-boundary', {})), patch('outline_service.model_settings', return_value=('test-live-boundary', {})), patch.object(GenerationBudget, 'from_env', return_value=GenerationBudget()):
            _, report = self.run_case(mode='live')
        self.assertTrue(report['completed'])
        self.assertEqual(report['model'], 'test-live-boundary')

    def test_call_limit_preserves_completed_results_and_does_not_score_partial(self):
        directory, report = self.run_case(max_calls=1)
        self.assertFalse(report['completed'])
        self.assertEqual(len(report['calls']), 1)
        self.assertTrue(report['call_limit_reached'])
        self.assertEqual(report['cases'][0]['extraction']['status'], 'completed')
        self.assertEqual(report['cases'][0]['grouping']['status'], 'not_run')
        self.assertTrue(all(g['status'] == 'not_run' for g in report['cases'][0]['grading']))
        self.assertTrue((directory / 'small_tcp.sqlite3').exists())

    def test_hierarchical_limit_retains_briefs_and_marks_partial_extraction(self):
        directory, report = quality_eval.run_suite(output_root=self.root, case_ids=['cross_chapter'],
                                                  stage='extraction', max_calls=1)
        self.assertFalse(report['completed'])
        self.assertTrue(report['call_limit_reached'])
        output = report['cases'][0]['extraction']
        self.assertEqual(output['reading_plan']['documents'][0]['mode'], 'hierarchical')
        self.assertTrue(any(s['status'] == 'completed' for s in output['context_steps']))
        # Heading-only ranges are completed without a model task, not semantic extraction.
        self.assertLess(output['analyzed_lines'], output['total_lines'])
        self.assertTrue((directory / 'cross_chapter.sqlite3').exists())

    def test_provider_failure_redacted_and_saved_answers_survive(self):
        secret = 'PRIVATE-KEY-MARKER https://private.example'
        with patch('quality_eval.REAL_REQUEST', side_effect=RuntimeError(secret)), patch('model_client.model_settings', return_value=('fake-provider', {})), patch('outline_service.model_settings', return_value=('fake-provider', {})), patch.object(GenerationBudget, 'from_env', return_value=GenerationBudget()):
            directory, report = self.run_case(mode='live', stage='grading')
        self.assertFalse(report['completed'])
        self.assertNotIn(secret, (directory / 'results.json').read_text(encoding='utf-8'))
        self.assertNotIn(secret, (directory / 'calls.json').read_text(encoding='utf-8'))
        with closing(sqlite3.connect(directory / 'small_tcp.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM practice_sessions WHERE status='grading'").fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT count(*) FROM practice_questions WHERE answer_text IS NOT NULL AND grade_json IS NULL').fetchone()[0], 4)

    def test_service_rejects_invalid_citation_even_when_request_returned(self):
        original = quality_eval.controlled_response
        def invalid(*args):
            value = original(*args)
            if value.__class__.__name__ in ('FullOutlineDraft', 'OutlineDraft') and value.proposals:
                value.proposals[0].knowledge.citations[0].line_end = 9000
            return value
        with patch('quality_eval.controlled_response', side_effect=invalid):
            _, report = self.run_case(stage='extraction')
        self.assertEqual(report['calls'][0]['status'], 'returned')
        self.assertEqual(report['cases'][0]['extraction']['status'], 'failed')
        self.assertEqual(report['cases'][0]['extraction']['candidates'], [])

    def test_forbidden_merges_and_unannotated_pairs_are_separate(self):
        reference = [{'pair': ['a', 'b'], 'expected': [], 'forbidden': ['same_knowledge'], 'reason': '不同条件'}]
        groups = [{'proposal': {'candidate_ids': ['a', 'b', 'c'], 'kind': 'same_knowledge'}}]
        result = relation_checks(reference, groups, {'a': 'a', 'b': 'b', 'c': 'c'})
        self.assertFalse(result['labelled_pairs'][0]['agrees_with_draft'])
        self.assertEqual(len(result['unlabelled_predicted_pairs']), 2)
        self.assertIsNone(relation_checks(reference, groups, {'a': 'a', 'b': 'b', 'c': 'c'}, False)['labelled_pairs'][0]['agrees_with_draft'])

    def full_review(self, report):
        review = review_template(report)
        review['reviewer'] = 'controlled-test'
        for case in review['cases']:
            for point in case['points']:
                point.update(decision='missing')
            for candidate in case['candidates']:
                candidate.update(acceptable=True, conditions_correct=True, evidence_supports_claim=True)
        return review

    def test_smoke_never_emits_semantic_quality_ratios(self):
        _, report = self.run_case(stage='extraction')
        reviewed = apply_review(report, self.full_review(report))
        self.assertIsNone(reviewed['cases'][0]['reference_recall'])
        self.assertIsNone(reviewed['cases'][0]['candidate_precision'])

    def test_old_review_and_unknown_candidates_cannot_be_reused(self):
        _, report = self.run_case(stage='extraction')
        review = self.full_review(report)
        old = copy.deepcopy(report)
        old['model'] = 'changed'
        with self.assertRaises(ValueError):
            apply_review(old, review)
        review['cases'][0]['points'][0].update(decision='matched', candidate_ids=['invented'])
        with self.assertRaises(ValueError):
            apply_review(report, review)

    def test_incomplete_pipeline_or_review_withholds_recall(self):
        _, report = self.run_case(stage='extraction')
        report['mode'] = 'live'  # Only tests report arithmetic, no live generation.
        review = self.full_review(report)
        self.assertEqual(apply_review(report, review)['cases'][0]['reference_recall'], 0)
        review['cases'][0]['points'][0]['decision'] = 'unreviewed'
        self.assertIsNone(apply_review(report, review)['cases'][0]['reference_recall'])
        report['cases'][0]['extraction']['status'] = 'failed'
        review = self.full_review(report)
        self.assertIsNone(apply_review(report, review)['cases'][0]['reference_recall'])

    def test_invalid_cli_call_count_is_not_silently_replaced(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(quality_eval.main(['run', '--max-calls', '0']), 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_review_command_writes_separate_artifact_without_model(self):
        directory, report = self.run_case(stage='extraction')
        review_path = directory / 'review.json'
        review_path.write_text(json.dumps(self.full_review(report), ensure_ascii=False), encoding='utf-8')
        with patch('quality_eval.REAL_REQUEST') as model, redirect_stdout(io.StringIO()):
            self.assertEqual(quality_eval.main(['review', str(directory), str(review_path)]), 0)
        model.assert_not_called()
        reviewed = json.loads((directory / 'reviewed.json').read_text(encoding='utf-8'))
        self.assertIsNone(reviewed['cases'][0]['reference_recall'])
        self.assertTrue((directory / 'reviewed.md').exists())
        self.assertEqual(json.loads((directory / 'results.json').read_text(encoding='utf-8')), report)


if __name__ == '__main__':
    unittest.main()
