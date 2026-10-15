"""Source coverage, resumable proposals, and atomic user-selected enrollment."""

import json
import re
import shutil
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from answers import Citation
from generation_budget import BudgetError, GenerationBudget
from learning_store import LearningStore
from library_store import LibraryStore
from outline_service import (OutlineDraft, LearningProposal, prepare_outline, section_manifest, scope_fingerprint,
    outline_messages, fits_request, run_outline, public_outline, edit_candidate, enroll_candidate, excerpt)
from outline_store import OutlineStore, OutlineBusy
from outline_context import FullOutlineDraft
from sources import Source
from test_learning import learning, learning_question
from test_support import temporary_notes
from web_app import create_app


TEXT = '# TCP\n\nSEQ=100, LEN=20, ACK=120.\n## 长度\nLEN=30 时 ACK=130.\n## 边界\nACK 表示下一个期望字节。'


def proposal(source_id, start):
    draft = learning(source_id=source_id)
    return LearningProposal.model_validate(draft.model_copy(update={'knowledge': draft.knowledge.model_copy(update={
        'citations': [Citation(source_id=source_id, line_start=start, line_end=start)]})}).model_dump())


def fake_model(messages, schema):
    data = json.loads(messages[1]['content'])
    source_id = re.search(r'Source ID: ([^\n]+)', data['sources'])[1]
    start = int(re.search(r'Visible lines: L(\d+)', data['sources'])[1])
    if schema in (OutlineDraft, FullOutlineDraft):
        return schema(proposals=[proposal(source_id, start)], note='本段可生成一个目标。')
    draft = learning_question()
    return draft.model_copy(update={'citations': [Citation(source_id=source_id, line_start=start, line_end=start)]})


class OutlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.practice = LearningStore(self.root / 'practice.sqlite3')
        self.store = OutlineStore(self.practice)
        self.library = LibraryStore(self.practice)
        self.source = Source('tcp-version', Path('tcp.md'), TEXT)
        self.snapshots = [{'document_id': 'tcp', 'source_id': self.source.source_id, 'path': 'tcp.md', 'name': 'tcp.md', 'text': TEXT}]
        self.settings = patch('outline_service.model_settings', return_value=('test-model', {}))
        self.settings.start()

    def tearDown(self):
        self.settings.stop()
        if (self.root / 'uploads').exists():
            shutil.rmtree(self.root / 'uploads')
        self.temp.__exit__(None, None, None)

    def current(self, collection):
        return self.snapshots

    def create(self, request_id='first', collection='root', source=None, snapshots=None, budget=None):
        sources = [source or self.source]
        snapshots = snapshots or self.snapshots
        sections, batches = prepare_outline(sources, snapshots, '', 'zh', budget)
        return self.store.create(collection, request_id, '', 'zh', scope_fingerprint(snapshots), snapshots, sections, batches)

    def generated(self):
        identity = self.create()
        with patch('outline_service._request_structured', side_effect=fake_model):
            run_outline(self.store, identity, self.current)
        return identity, self.store.get(identity)['candidates'][0]

    def counts(self):
        with closing(self.practice._connect()) as db:
            return [db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in
                    ('practice_sessions', 'practice_questions', 'review_items', 'review_events', 'learning_questions')]

    def test_markdown_manifest_covers_every_line_once_and_ignores_embedded_headings(self):
        text = '前言\n\n标题\n===\n正文\n```md\n# fake\n```\n> # quoted\n## 子节\n内容\n# 尾节\n'
        source = Source('version', Path('s.md'), text)
        sections = section_manifest([source], [{'document_id': 's', 'name': 's.md'}])
        self.assertEqual([s['heading_path'] for s in sections], [['前言 / 无标题'], ['标题'], ['标题', '子节'], ['尾节']])
        self.assertEqual([n for s in sections for n in range(s['line_start'], s['line_end'] + 1)], list(range(1, 13)))
        self.assertFalse(sections[-1]['has_content'])

    def test_budget_windows_examine_entire_long_chapter_and_account_for_both_protocols(self):
        text = '# Long\n' + '\n'.join('资料行' * 40 for _ in range(180))
        source = Source('v', Path('s.md'), text)
        budget = GenerationBudget(18000, 4096, 1024)
        sections, batches = prepare_outline([source], [{'document_id': 's', 'name': 's.md'}], '了解范围', 'en', budget)
        self.assertGreater(len(batches), 2)
        self.assertEqual([n for b in batches for n in range(b['line_start'], b['line_end'] + 1)], list(range(1, 182)))
        for batch in batches:
            chunk = excerpt(source, batch['line_start'], batch['line_end'], sections[0]['heading_path'])
            self.assertTrue(fits_request(budget, outline_messages(source, sections[0], chunk, '了解范围', 'en'), OutlineDraft))

    def test_oversize_line_is_explicitly_unanalyzed_without_losing_following_lines(self):
        source = Source('v', Path('s.md'), '# 章节\n' + 'x' * 40000 + '\n正常内容')
        sections, batches = prepare_outline([source], [{'document_id': 's', 'name': 's.md'}], '', 'zh')
        self.assertEqual([(b['line_start'], b['line_end']) for b in batches], [(1, 1), (2, 2), (3, 3)])
        self.assertEqual(batches[1]['status'], 'failed')
        with self.assertRaises(BudgetError):
            prepare_outline([source], [{'document_id': 's', 'name': 's.md'}], '', 'zh', GenerationBudget(2000, 512, 256))

    def test_generation_is_opt_in_deduplicated_and_recovers_after_reopening(self):
        identity, candidate = self.generated()
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        self.assertEqual(self.create(), identity)
        reopened = OutlineStore(LearningStore(self.practice.path))
        job = public_outline(reopened, identity, self.current)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(len(job['coverage']), 3)
        self.assertTrue(all(s['analyzed_lines'] == s['total_lines'] for s in job['coverage']))
        self.assertEqual(reopened.get(identity)['snapshots'][0]['text'], TEXT)
        self.assertTrue(all(s['enrolled_targets'] == 0 for s in job['coverage']))

    def test_failure_keeps_prior_proposals_and_retry_only_reprocesses_remaining(self):
        identity = self.create()
        calls = []
        def failing(messages, schema):
            calls.append(messages)
            if len(calls) == 2:
                raise RuntimeError('provider error')
            return fake_model(messages, schema)
        with patch('outline_service._request_structured', side_effect=failing):
            run_outline(self.store, identity, self.current)
        first = self.store.get(identity)
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(len(first['candidates']), 1)
        self.store.resume(identity)
        with patch('outline_service._request_structured', side_effect=fake_model) as model:
            run_outline(self.store, identity, self.current)
            self.assertEqual(model.call_count, 2)
        last = self.store.get(identity)
        self.assertEqual(last['candidates'][0]['id'], first['candidates'][0]['id'])
        self.assertEqual(len(last['candidates']), 3)

    def test_invalid_citation_does_not_publish_model_proposals(self):
        identity = self.create()
        invalid = OutlineDraft(proposals=[proposal(self.source.source_id, 7)], note='bad')
        with patch('outline_service._request_structured', return_value=invalid):
            run_outline(self.store, identity, self.current)
        self.assertEqual(self.store.get(identity)['candidates'], [])
        self.assertEqual(self.store.get(identity)['status'], 'failed')

    def test_edit_revision_and_immutable_evidence(self):
        identity, candidate = self.generated()
        edited = proposal(self.source.source_id, 1)
        edited = edited.model_copy(update={'target': edited.target.model_copy(update={'title': '用户确认的目标'})})
        self.assertEqual(edit_candidate(self.store, candidate['id'], edited, 1), 2)
        with self.assertRaises(OutlineBusy):
            edit_candidate(self.store, candidate['id'], edited, 1)
        with self.assertRaises(ValueError):
            edit_candidate(self.store, candidate['id'], proposal(self.source.source_id, 3), 2)
        self.assertEqual(self.store.candidate(candidate['id'])['learning']['target']['title'], '用户确认的目标')

    def test_selected_enrollment_is_atomic_idempotent_and_has_no_mastery_evidence(self):
        identity, candidate = self.generated()
        destination = self.library.create('目标集合', 'root')['id']
        with patch('outline_service._request_structured', side_effect=fake_model) as model:
            result = enroll_candidate(self.store, candidate['id'], 1, destination, None, 'en', self.current)
            self.assertEqual(enroll_candidate(self.store, candidate['id'], 1, destination, None, 'en', self.current), result)
            self.assertEqual(model.call_count, 1)
        self.assertEqual(self.counts(), [1, 1, 1, 0, 1])
        self.assertEqual(self.practice.get(result['session_id'])['response_language'], 'en')
        item = self.practice.item(result['item_id'])
        self.assertEqual(item['collection_id'], destination)
        self.assertEqual(item['history'], [])
        self.assertEqual(item['pending_session_id'], result['session_id'])
        self.assertEqual(sum(s['enrolled_targets'] for s in public_outline(self.store, identity, self.current)['coverage']), 1)

    def test_failed_seed_or_enrollment_rolls_back_all_rows_and_retry_succeeds(self):
        identity, candidate = self.generated()
        with patch('outline_service._request_structured', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                enroll_candidate(self.store, candidate['id'], 1, 'root', None, 'zh', self.current)
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        with patch('outline_service._request_structured', side_effect=fake_model):
            with self.assertRaises(KeyError):
                enroll_candidate(self.store, candidate['id'], 1, 'root', 'missing-unit', 'zh', self.current)
            self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
            self.assertEqual(self.store.candidate(candidate['id'])['status'], 'proposed')
            enroll_candidate(self.store, candidate['id'], 1, 'root', None, 'zh', self.current)
        self.assertEqual(self.counts(), [1, 1, 1, 0, 1])

    def test_changed_scope_blocks_enrollment_and_changes_during_generation_reject_publication(self):
        identity, candidate = self.generated()
        changed = lambda _: [{**self.snapshots[0], 'source_id': 'new-version'}]
        self.assertTrue(public_outline(self.store, identity, changed)['stale'])
        with patch('outline_service._request_structured') as model:
            with self.assertRaises(OutlineBusy):
                enroll_candidate(self.store, candidate['id'], 1, 'root', None, 'zh', changed)
            model.assert_not_called()
        second = self.create('second')
        def change_during_request(messages, schema):
            self.snapshots[0]['source_id'] = 'new-version'
            return fake_model(messages, schema)
        with patch('outline_service._request_structured', side_effect=change_during_request):
            run_outline(self.store, second, self.current)
        self.assertEqual(self.store.get(second)['candidates'], [])

    def test_locks_pause_stale_attempts_and_dead_worker_recovery(self):
        identity = self.create()
        with self.store.lock(identity):
            with self.assertRaises(OutlineBusy):
                with self.store.lock(identity):
                    pass
            run_outline(self.store, identity, self.current)
            self.assertEqual(self.store.get(identity)['status'], 'queued')
        with self.assertRaises(OutlineBusy):
            self.create('another')
        self.store.pause(identity)
        with patch('outline_service._request_structured') as model:
            run_outline(self.store, identity, self.current)
            model.assert_not_called()
        self.store.resume(identity)
        self.store.start(identity)
        batch = self.store.claim_batch(identity)
        restored = public_outline(self.store, identity, self.current)
        self.assertEqual(restored['status'], 'paused')
        self.assertFalse(self.store.finish_batch(batch, [proposal(self.source.source_id, 1)], 'stale'))
        self.store.resume(identity)
        self.store.start(identity)
        claimed = self.store.claim_batch(identity)
        self.assertNotEqual(claimed['attempt'], batch['attempt'])
        self.store.pause(identity)
        restored = public_outline(self.store, identity, self.current)
        self.assertEqual(restored['status'], 'paused')
        self.assertFalse(restored['processing'])

    def test_network_does_not_hold_database_write_lock_and_pause_keeps_completed_window(self):
        identity = self.create()
        def stop(messages, schema):
            self.store.pause(identity)
            self.library.create('网络期间可写', 'root')
            return fake_model(messages, schema)
        with patch('outline_service._request_structured', side_effect=stop) as model:
            run_outline(self.store, identity, self.current)
            self.assertEqual(model.call_count, 1)
        self.assertEqual(self.store.get(identity)['status'], 'paused')
        self.assertEqual(len(self.store.get(identity)['candidates']), 1)

    def test_http_nested_collection_empty_scope_and_corrected_hint(self):
        app = create_app(self.root / 'uploads', self.practice.path)
        parent = self.library.create('网络', 'root')['id']
        child = self.library.create('TCP', parent)['id']
        with TestClient(app) as client, patch('outline_service._request_structured', side_effect=fake_model):
            self.assertEqual(client.post(f'/api/collections/{child}/outlines', json={'request_id': 'empty'}).status_code, 409)
            imported = client.post(f'/api/documents?collection_id={child}', files=[('files', ('tcp.md', TEXT.encode(), 'text/markdown'))]).json()[0]
            response = client.post(f'/api/collections/{parent}/outlines', json={'request_id': 'api', 'response_language': 'en'})
            self.assertEqual(response.status_code, 202, response.text)
            identity = response.json()['id']
            job = client.get(f'/api/outlines/{identity}').json()
            self.assertEqual(job['response_language'], 'en')
            self.assertEqual({s['document_id'] for s in job['coverage']}, {imported['id']})
            self.assertEqual(client.get(f'/api/collections/{child}/outlines').json(), [])
            candidate = job['candidates'][0]
            approved = candidate['learning']
            approved['target']['title'] = '已审核'
            self.assertEqual(client.patch(f"/api/outline-candidates/{candidate['id']}", json={'learning': approved, 'revision': 1}).status_code, 200)
            enrolled = client.post(f"/api/outline-candidates/{candidate['id']}/enroll", json={'revision': 2, 'collection_id': child})
            self.assertEqual(enrolled.status_code, 200, enrolled.text)
            self.assertNotIn('每次问答仅使用已勾选的资料', client.get('/').text)
            self.assertIn('模型按需要查阅', client.get('/').text)


if __name__ == '__main__':
    unittest.main()
