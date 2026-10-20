"""Adaptive reading tests use controlled model responses, not semantic quality claims."""

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from answers import Citation
from generation_budget import GenerationBudget
from learning_store import LearningStore
from library_store import LibraryStore
from outline_context import DocumentBrief, BriefPoint, FullOutlineDraft, plan_outline, dependency
from outline_service import (OutlineDraft, run_outline, scope_fingerprint, public_outline, excerpt,
                             edit_candidate, enroll_candidate)
from outline_store import OutlineStore
from sources import Source
from test_outlines import proposal, fake_model
from test_support import temporary_notes


LONG = '# 定义\n\nACK 表示下一个期望字节；SEQ=100、LEN=20 时 ACK=120。\n\n' + '\n\n'.join(
    f'定义示例 {n}：字节序号连续，确认号依据已经收到的连续数据计算。' for n in range(10)) + '\n## 应用\n\n长度变为30时，应使用前章定义计算。\n\n' + '\n\n'.join(
    f'应用示例 {n}：请解释条件并计算连续字节后的确认号。' for n in range(10))


def controlled(messages, schema):
    data = json.loads(messages[1]['content'])
    if schema is DocumentBrief:
        if 'sources' in data:
            sid = re.search(r'Source ID: ([^\n]+)', data['sources'])[1]
            start = int(re.search(r'Visible lines: L(\d+)', data['sources'])[1])
            cite = Citation(source_id=sid, line_start=start, line_end=start)
        else:
            points = [p for overview in data['overviews'] for p in overview['points']]
            if not points:
                return DocumentBrief(points=[], unresolved=['没有足够可引用概念。'])
            cite = Citation.model_validate(points[0]['citations'][0])
        return DocumentBrief(points=[BriefPoint(text='ACK 是下一个期望字节，应用章节依赖此定义。', citations=[cite])], unresolved=[])
    return fake_model(messages, schema)


class OutlineContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.practice = LearningStore(self.root / 'practice.sqlite3')
        self.store = OutlineStore(self.practice)
        self.library = LibraryStore(self.practice)
        self.settings = patch('outline_service.model_settings', return_value=('test-model', {}))
        self.real_settings = patch('model_client.model_settings', return_value=('test-model', {}))
        self.settings.start()
        self.real_settings.start()
        self.source = Source('version', Path('tcp.md'), LONG)
        self.snapshots = [{'document_id': 'tcp', 'source_id': 'version', 'path': 'tcp.md', 'name': 'tcp.md', 'text': LONG}]

    def tearDown(self):
        self.settings.stop()
        self.real_settings.stop()
        self.temp.__exit__(None, None, None)

    def current(self, _):
        return self.snapshots

    def create(self, text=None, budget=None):
        if text is not None:
            self.source = Source('version', Path('tcp.md'), text)
            self.snapshots[0]['text'] = text
        sections, batches, plan = plan_outline([self.source], self.snapshots, '', 'en', budget)
        return self.store.create('root', 'req', '', 'en', scope_fingerprint(self.snapshots), self.snapshots, sections, batches, plan)

    def test_full_reading_uses_complete_document_schema_and_section_coverage(self):
        identity = self.create('# TCP\nACK 是下一个字节。\n## 例子\nSEQ=100,LEN=30,ACK=130。')
        with patch('outline_service._request_structured', side_effect=controlled) as model:
            run_outline(self.store, identity, self.current)
        self.assertEqual(model.call_count, 1)
        self.assertIs(model.call_args.args[1], FullOutlineDraft)
        self.assertIn('ACK=130', model.call_args.args[0][1]['content'])
        job = public_outline(self.store, identity, self.current)
        self.assertTrue(all(s['analyzed_lines'] == s['total_lines'] for s in job['coverage']))
        self.assertEqual(job['reading_plan']['documents'][0]['mode'], 'full')
        self.assertEqual(job['context_steps'], [])

    def test_output_pressure_selects_hierarchy_even_with_large_input_capacity(self):
        identity = self.create(budget=GenerationBudget(100000, 4096, 2048))
        self.assertEqual(self.store.get(identity)['plan']['documents'][0]['mode'], 'hierarchical')

    def test_shared_background_and_original_early_evidence_reach_later_extraction(self):
        identity = self.create()
        calls = []
        def capture(messages, schema):
            calls.append((messages, schema))
            return controlled(messages, schema)
        with patch('outline_service._request_structured', side_effect=capture):
            run_outline(self.store, identity, self.current)
        job = self.store.get(identity)
        self.assertEqual(job['status'], 'completed')
        extracted = [json.loads(m[1]['content']) for m, s in calls if s is OutlineDraft]
        later = next(data for data in extracted if data['chapter'][-1] == '应用')
        self.assertIn('document_overview', later)
        self.assertIn('chapter_overview', later)
        self.assertIn('[L1] # 定义', later['sources'])
        self.assertIn('overview_versions', later)
        self.assertTrue(all(m[-1]['content'].find('English') >= 0 for m, s in calls))
        reopened = OutlineStore(LearningStore(self.practice.path)).get(identity)
        self.assertEqual(reopened['context_steps'], job['context_steps'])
        self.assertEqual(reopened['snapshots'][0]['text'], LONG)
        for step in reopened['context_steps']:
            self.assertLessEqual(step['request']['estimated_input_tokens'], step['request']['input_limit'])

    def test_summary_failure_keeps_valid_steps_and_resumes_without_regeneration(self):
        identity = self.create()
        count = 0
        def fail(messages, schema):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('provider unavailable')
            return controlled(messages, schema)
        with patch('outline_service._request_structured', side_effect=fail):
            run_outline(self.store, identity, self.current)
        previous = self.store.get(identity)
        self.assertEqual(previous['status'], 'failed')
        saved = next(s for s in previous['context_steps'] if s['status'] == 'completed')
        self.assertEqual(previous['candidates'], [])
        self.store.resume(identity)
        with patch('outline_service._request_structured', side_effect=controlled):
            run_outline(self.store, identity, self.current)
        last = self.store.get(identity)
        self.assertEqual(last['status'], 'completed')
        self.assertEqual(next(s for s in last['context_steps'] if s['id'] == saved['id'])['result_json'], saved['result_json'])

    def test_fabricated_summary_pointer_is_rejected_before_extraction(self):
        identity = self.create()
        bad = DocumentBrief(points=[BriefPoint(text='未看到的资料', citations=[Citation(source_id='version', line_start=999, line_end=999)])], unresolved=[])
        with patch('outline_service._request_structured', return_value=bad) as model:
            run_outline(self.store, identity, self.current)
        self.assertEqual(model.call_count, 1)
        job = self.store.get(identity)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['candidates'], [])
        self.assertFalse(any(s['result'] for s in job['context_steps']))

    def test_pause_during_summary_has_no_long_write_lock_and_can_resume(self):
        identity = self.create()
        def pause(messages, schema):
            self.library.create('网络期间可写')
            self.store.pause(identity)
            return controlled(messages, schema)
        with patch('outline_service._request_structured', side_effect=pause) as model:
            run_outline(self.store, identity, self.current)
        self.assertEqual(model.call_count, 1)
        job = self.store.get(identity)
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(job['context_steps'][0]['status'], 'failed')
        self.assertIsNone(job['context_steps'][0]['result'])
        self.store.resume(identity)
        with patch('outline_service._request_structured', side_effect=controlled):
            run_outline(self.store, identity, self.current)
        self.assertEqual(self.store.get(identity)['status'], 'completed')

    def test_structural_windows_keep_tables_and_code_whole_when_they_fit(self):
        text = '# 数据\n\n' + '\n\n'.join('普通段落内容。' for _ in range(10)) + '\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\na = 1\nb = 2\n```\n'
        sections, batches, plan = plan_outline([Source('v', Path('s.md'), text)], [{'document_id': 's', 'name': 's.md'}], '', 'zh')
        lines = text.splitlines()
        for start, end in [(lines.index('| A | B |') + 1, lines.index('| 1 | 2 |') + 1),
                           (lines.index('```python') + 1, lines.index('```') + 1)]:
            self.assertTrue(any(b['line_start'] <= start <= end <= b['line_end'] for b in batches))
        self.assertEqual([n for b in batches for n in range(b['line_start'], b['line_end'] + 1)], list(range(1, len(lines) + 1)))

    def test_cross_chapter_citations_can_be_edited_and_enrolled_only_after_original_delivery(self):
        identity = self.create()
        def early_citation(messages, schema):
            if schema is OutlineDraft:
                data = json.loads(messages[1]['content'])
                self.assertIn('[L1] # 定义', data['sources'])
                return OutlineDraft(proposals=[proposal('version', 1)], note='结合前章原文。')
            return controlled(messages, schema)
        with patch('outline_service._request_structured', side_effect=early_citation):
            run_outline(self.store, identity, self.current)
        job = self.store.get(identity)
        later = next(c for c in job['candidates'] if next(b for b in job['batches'] if b['id'] == c['batch_id'])['line_start'] > 1)
        edit_candidate(self.store, later['id'], proposal('version', 1), 1)
        with patch('outline_service._request_structured', side_effect=controlled):
            enrolled = enroll_candidate(self.store, later['id'], 2, 'root', None, 'en', self.current)
            self.assertEqual(enroll_candidate(self.store, later['id'], 2, 'root', None, 'en', self.current), enrolled)
        self.assertEqual(self.practice.item(enrolled['item_id'])['history'], [])

    def test_actual_background_pressure_splits_batch_without_losing_tail(self):
        identity = self.create()
        self.store.start(identity)
        batch = self.store.claim_batch(identity)
        original_end = batch['line_end']
        shortened = self.store.split_batch(batch, batch['line_start'] + 2)
        self.assertEqual(shortened['line_end'], batch['line_start'] + 2)
        ranges = self.store.get(identity)['batches']
        self.assertEqual(ranges[1]['line_start'], shortened['line_end'] + 1)
        self.assertEqual(ranges[1]['line_end'], original_end)
        self.assertEqual([n for b in ranges for n in range(b['line_start'], b['line_end'] + 1)], list(range(1, len(LONG.splitlines()) + 1)))

    def test_source_change_rejects_late_brief_publication(self):
        identity = self.create()
        def changed(messages, schema):
            self.snapshots[0]['source_id'] = 'changed'
            return controlled(messages, schema)
        with patch('outline_service._request_structured', side_effect=changed):
            run_outline(self.store, identity, self.current)
        self.assertFalse(any(s['result'] for s in self.store.get(identity)['context_steps']))
        self.assertTrue(public_outline(self.store, identity, self.current)['stale'])

    def test_real_request_pressure_splits_extraction_and_retains_all_original_lines(self):
        text = '# 大章节\n\n```text\n' + '\n'.join(f'{n:03} ' + 'x' * 95 for n in range(90)) + '\n```'
        budget = GenerationBudget(18000, 4096, 1024)
        identity = self.create(text, budget)
        count_before = len(self.store.get(identity)['batches'])
        def verbose(messages, schema):
            result = controlled(messages, schema)
            if schema is DocumentBrief:
                point = result.points[0]
                return DocumentBrief(points=[point.model_copy(update={'text': f'定义{n} ' + 'x' * 220}) for n in range(6)], unresolved=[])
            return result
        with patch('outline_context.GenerationBudget.from_env', return_value=budget), patch('outline_service._request_structured', side_effect=verbose):
            run_outline(self.store, identity, self.current)
        job = self.store.get(identity)
        self.assertEqual(job['status'], 'completed')
        self.assertGreater(len(job['batches']), count_before)
        self.assertEqual([n for b in job['batches'] for n in range(b['line_start'], b['line_end'] + 1)], list(range(1, len(text.splitlines()) + 1)))
        for batch in job['batches']:
            diagnostic = json.loads(batch['request_json'])
            self.assertLessEqual(diagnostic['estimated_input_tokens'], budget.input_limit)

    def test_reduction_is_hierarchical_and_all_leaf_dependencies_remain_readable(self):
        text = '# 多段\n' + '\n\n'.join(f'资料 {n}：' + '内容' * 45 for n in range(220))
        identity = self.create(text)
        with patch('outline_service._request_structured', side_effect=controlled):
            run_outline(self.store, identity, self.current)
        job = self.store.get(identity)
        self.assertEqual(job['status'], 'completed')
        leaves = [s for s in job['context_steps'] if s['kind'] == 'excerpt']
        self.assertGreater(len(leaves), 4)
        linked = {item['step_id'] for s in job['context_steps'] for item in s['inputs'] if 'step_id' in item}
        self.assertTrue(all(s['id'] in linked for s in leaves))
        self.assertTrue(all(s['status'] == 'completed' for s in job['context_steps']))

    def test_dead_context_worker_recovery_and_late_attempt_rejection(self):
        identity = self.create()
        self.store.start(identity)
        self.store.context_step(identity, 'a' * 32, 'excerpt', 'chapter', [])
        claimed = self.store.claim_context('a' * 32)
        restored = public_outline(self.store, identity, self.current)
        self.assertEqual(restored['status'], 'paused')
        self.assertFalse(restored['processing'])
        result = DocumentBrief(points=[], unresolved=[])
        self.assertFalse(self.store.finish_context(claimed, result, {}))
        self.store.resume(identity)
        self.store.start(identity)
        retry = self.store.claim_context('a' * 32)
        self.assertNotEqual(claimed['attempt'], retry['attempt'])
        self.assertTrue(self.store.finish_context(retry, result, {}))

    def test_changed_dependency_version_prevents_stale_merge_publication(self):
        identity = self.create()
        self.store.start(identity)
        self.store.context_step(identity, 'a' * 32, 'excerpt', 'chapter', [])
        leaf = self.store.claim_context('a' * 32)
        result = DocumentBrief(points=[], unresolved=[])
        self.store.finish_context(leaf, result, {})
        published = self.store.get(identity)['context_steps'][0]
        self.store.context_step(identity, 'b' * 32, 'chapter', 'chapter', [dependency(published)])
        merge = self.store.claim_context('b' * 32)
        with self.practice.transaction() as db:
            db.execute('UPDATE outline_context_steps SET result_json=? WHERE id=?',
                (DocumentBrief(points=[], unresolved=['新的疑点']).model_dump_json(), published['id']))
        self.assertFalse(self.store.finish_context(merge, result, {}))

    def test_empty_markdown_completes_without_model_or_fake_source_range(self):
        identity = self.create('')
        with patch('outline_service._request_structured') as model:
            run_outline(self.store, identity, self.current)
        model.assert_not_called()
        job = public_outline(self.store, identity, self.current)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['candidates'], [])
        self.assertTrue(all(s['total_lines'] == 0 for s in job['coverage']))

    def test_superseded_failed_merge_can_be_used_again_after_budget_changes(self):
        identity = self.create()
        self.store.start(identity)
        step = self.store.context_step(identity, 'a' * 32, 'chapter', 'scope', [])
        claimed = self.store.claim_context(step['id'])
        self.store.fail_context(claimed, '旧预算失败')
        self.store.retire_context_attempts(identity)
        self.assertEqual(self.store.get(identity)['context_steps'][0]['status'], 'superseded')
        requested = self.store.context_step(identity, 'a' * 32, 'chapter', 'scope', [])
        self.assertEqual(requested['status'], 'pending')
        retry = self.store.claim_context(requested['id'])
        self.assertTrue(self.store.finish_context(retry, DocumentBrief(points=[], unresolved=[]), {}))


if __name__ == '__main__':
    unittest.main()
