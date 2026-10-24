"""Synthetic interface/lifecycle checks; no claims about model teaching quality."""

import json
import shutil
import unittest
from contextlib import closing
from unittest.mock import patch

from fastapi.testclient import TestClient
from chat_practice import start, DiscussionProposal, DocumentProposal
from conversation_memory import ConversationMemory
from generation_budget import GenerationBudget
from learning_service import enroll_question, start_review, add_dimension, correct_grade
from practice_model import DiscussionGrade, DiscussionQuestion, LearningQuestionDraft
from practice_service import grade_pending_answer
from practice_store import public_session
from sources import Source
from outline_service import scope_fingerprint, public_outline
from structured_output import structured_protocols
from test_conversations import answer_data
from test_support import temporary_notes
from web_app import create_app


def discussion_question():
    return DiscussionQuestion.model_validate({'objective': 'Explain TCP ACK',
        'question': 'SEQ=100, LEN=30. What is ACK and why?',
        'rubric_points': ['Compute ACK=130', 'Explain next expected byte'], 'citations': [],
        'learning': {'knowledge': {'name': 'TCP cumulative acknowledgment',
            'statement': 'Discuss next expected byte; model explanation requires review.',
            'content_tags': ['TCP'], 'citations': []},
            'target': {'title': 'Explain ACK', 'capability': 'explain', 'description': 'Explain next expected byte',
                'scope': 'Payload without SYN/FIN', 'criteria': ['Compute the number', 'Explain the meaning']}},
        'criterion_indices': [1, 2]})


def discussion_grade():
    return DiscussionGrade.model_validate({'covered_indices': [1, 2], 'missing_indices': [],
        'answer_quotes': ['ACK=130'], 'feedback': 'Explained the calculation and next expected byte.',
        'next_action': 'finish', 'teaching_note': '', 'follow_up': None,
        'overall_assessment': 'meets_standard', 'suggested_rating': 'Good',
        'rating_reason': 'The actual answer explains both.', 'target_fit': 'within_scope', 'dimension_observations': []})


class ManagementAndChatPracticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.app = create_app(self.root / 'uploads')
        self.client = TestClient(self.app)
        self.store = self.app.state.practice_store
        self.chats = self.app.state.conversation_store
        self.library = self.app.state.library_store
        self.chat = self.chats.create('Synthetic TCP') ['id']
        self.complete(self.chat)
        self.payload = {'objective': '长度改成30呢？', 'response_language': 'en', 'collection_id': 'root', 'strict': False}

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.root / 'uploads', ignore_errors=True)
        self.temp.__exit__(None, None, None)

    def complete(self, chat):
        turn = self.chats.begin(chat, 'original', {'question': 'SEQ=100，LEN=20，ACK=120'})
        self.chats.finish(turn['id'], turn['attempt'], answer_data('ACK=120 表示下一期待字节'), {})

    def generator(self, messages, schema, *, before_request):
        before_request()
        for protocol in structured_protocols(schema):
            GenerationBudget.from_env().check(messages, **protocol)
        data = json.loads(messages[1]['content'])
        self.assertIn('SEQ=100', json.dumps(data, ensure_ascii=False))
        self.assertIn('ACK=120', json.dumps(data, ensure_ascii=False))
        self.assertIn('长度改成30', data['objective'])
        self.assertEqual(json.loads(messages[-1]['content'])['user_preferences']['response_language'], 'English')
        ref = next(r for r in data['discussion_reference_ranges'] if r['conversation_id'] == self.chat and r['role'] == 'user')
        return DiscussionProposal(**discussion_question().model_dump(), reason='A numerical follow-up to the actual discussion.',
            history_references=[{k: ref[k] for k in ('conversation_id', 'message_id', 'message_revision', 'start_offset', 'end_offset')} | {'block_index': 0}])

    def generate(self, request_id='exercise', generator=None):
        with patch.object(ConversationMemory, 'search', return_value=[]):
            return start(self.store, self.chats, self.chat, request_id, self.payload, generator=generator or self.generator)

    def imported(self):
        response = self.client.post('/api/documents', files=[('files', ('tcp.md', b'# TCP\nSEQ plus LEN gives ACK.', 'text/markdown'))])
        self.assertEqual(response.status_code, 201)
        return self.client.get('/api/documents/' + response.json()[0]['id']).json()

    def test_discussion_context_basis_and_opt_in_enrollment(self):
        job = self.generate()
        self.assertEqual(job['status'], 'completed', job)
        session = job['session']
        self.assertEqual(session['basis']['kind'], 'discussion_model')
        self.assertEqual(session['questions'][0]['citations'], [])
        self.assertIsNone(session['questions'][0]['rubric_points'])
        self.assertEqual(self.store.list_plan(), [])
        collection = self.library.create('TCP')
        qid = session['questions'][0]['id']
        item = enroll_question(self.store, session['id'], qid, collection_id=collection['id'])
        self.assertEqual(self.store.item(item)['collection_id'], collection['id'])
        self.assertEqual(enroll_question(self.store, session['id'], qid), item)

    def test_generation_failure_retry_and_duplicate_publish(self):
        def failure(*args, **kwargs):
            raise RuntimeError('controlled failure')
        failed = self.generate(generator=failure)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(self.store.list(), [])
        completed = self.generate()
        duplicate = self.generate(generator=failure)
        self.assertEqual(failed['id'], completed['id'])
        self.assertEqual(completed['session']['id'], duplicate['session']['id'])
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM practice_questions').fetchone()[0], 1)
        self.assertEqual(len(self.chats.history(self.chat)), 2)

    def test_request_identity_rejects_changed_input(self):
        self.generate()
        self.payload['objective'] = 'different request'
        with self.assertRaises(ValueError):
            self.generate()

    def test_cancelled_discussion_question_keeps_request_retryable(self):
        from agent_cancel import GenerationCancelled
        def cancel(*args, **kwargs):
            raise GenerationCancelled()
        with self.assertRaises(GenerationCancelled):
            self.generate(generator=cancel)
        with closing(self.store._connect()) as db:
            row = db.execute('SELECT status,attempt FROM chat_practice_jobs').fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['attempt'])
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.generate()['status'], 'completed')

    def test_delete_during_generation_revokes_late_result(self):
        def deleted(messages, schema, **kwargs):
            result = self.generator(messages, schema, **kwargs)
            self.chats.delete(self.chat)
            return result
        job = self.generate(generator=deleted)
        self.assertEqual(job['status'], 'revoked', job)
        self.assertIsNone(job['session'])
        self.assertEqual(self.store.list(), [])

    def test_deleted_source_blocks_all_saved_exercise_paths_preserves_fsrs(self):
        session = self.generate()['session']
        qid = session['questions'][0]['id']
        item = enroll_question(self.store, session['id'], qid)
        self.store.submit_answer(session['id'], 'ACK=130, next expected byte')
        with patch('chat_practice._request_structured', return_value=discussion_grade()):
            graded = grade_pending_answer(self.store, session['id'])
        self.assertTrue(graded['questions'][0]['review_event']['eligible'])
        before = self.store.item(item)
        self.chats.delete(self.chat)
        record = self.store.get(session['id'])
        self.assertTrue(record['basis']['revoked'])
        self.assertEqual(record['basis']['references'], [])
        self.assertEqual(record['questions'][0]['answer_text'], 'ACK=130, next expected byte')
        self.assertEqual(self.store.item(item)['card_json'], before['card_json'])
        self.assertEqual(self.store.item(item)['next_review_at'], before['next_review_at'])
        with patch('learning_service._request_structured') as model:
            for action in (lambda: start_review(self.store, item), lambda: enroll_question(self.store, session['id'], qid),
                           lambda: add_dimension(self.store, item, 'apply'),
                           lambda: self.store.reveal_reference(session['id'], qid)):
                with self.assertRaises(ValueError): action()
            model.assert_not_called()
        self.assertTrue(self.store.list_plan()[0]['items'][0]['source_revoked'])

    def test_delete_during_grading_does_not_save_feedback_or_event(self):
        session = self.generate()['session']
        item = enroll_question(self.store, session['id'], session['questions'][0]['id'])
        initial = self.store.item(item)['card_json']
        self.store.submit_answer(session['id'], 'ACK=130')
        def removed(*args, **kwargs):
            self.chats.delete(self.chat)
            return discussion_grade()
        with patch('chat_practice._request_structured', side_effect=removed):
            with self.assertRaises(ValueError): grade_pending_answer(self.store, session['id'])
        record = self.store.get(session['id'])
        self.assertEqual(record['questions'][0]['answer_text'], 'ACK=130')
        self.assertIsNone(record['questions'][0]['grade'])
        self.assertEqual(self.store.item(item)['card_json'], initial)

    def test_related_archived_chat_basis_revokes_transitively(self):
        old = self.chats.create('Archived source')['id']
        self.complete(old)
        self.chats.archive(old, True)
        memory = ConversationMemory(self.chats, self.chat)
        original = next(m for m in memory.messages() if m['conversation_id'] == old and m['role'] == 'user')
        span = memory.span(original, 0, len(original['content']))
        with patch.object(ConversationMemory, 'search', return_value=[span]):
            job = start(self.store, self.chats, self.chat, 'cross', self.payload, generator=self.generator)
        self.assertEqual(job['status'], 'completed', job)
        self.chats.delete(old)
        self.assertTrue(self.store.get(job['session']['id'])['basis']['revoked'])
        self.assertEqual(len(self.chats.history(self.chat)), 2)

    def test_original_review_variant_and_follow_up_keep_basis(self):
        session = self.generate()['session']
        item = enroll_question(self.store, session['id'], session['questions'][0]['id'])
        self.store.finish(session['id'])
        original = start_review(self.store, item, mode='original')
        self.assertEqual(original['basis']['references'], session['basis']['references'])
        self.store.finish(original['id'])
        with patch('chat_practice._request_structured', return_value=discussion_question()):
            variant = start_review(self.store, item)
        self.assertEqual(variant['review_item_id'], item)
        self.assertEqual(variant['questions'][0]['citations'], [])
        self.chats.delete(self.chat)
        self.assertTrue(self.store.get(variant['id'])['basis']['revoked'])

    def test_follow_up_preserves_discussion_contract_and_only_one_review(self):
        session = self.generate()['session']
        item = enroll_question(self.store, session['id'], session['questions'][0]['id'])
        self.store.submit_answer(session['id'], 'ACK=130')
        follow = discussion_grade().model_copy(update={'next_action': 'follow_up', 'follow_up': discussion_question()})
        with patch('chat_practice._request_structured', return_value=follow):
            grade_pending_answer(self.store, session['id'])
        self.store.continue_follow_up(session['id'])
        self.assertEqual(len(self.store.get(session['id'])['questions']), 2)
        self.store.submit_answer(session['id'], 'ACK=130')
        with patch('chat_practice._request_structured', return_value=discussion_grade()):
            grade_pending_answer(self.store, session['id'])
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_events WHERE item_id=?', (item,)).fetchone()[0], 1)

    def test_over_budget_preserves_job_no_question(self):
        small = GenerationBudget(10000, 4000, 2000)
        with patch('chat_practice.GenerationBudget.from_env', return_value=small), patch('chat_practice._request_structured') as model:
            job = self.generate(generator=lambda *a, **k: self.fail('Model must not run over budget'))
        self.assertEqual(job['status'], 'failed', job)
        model.assert_not_called()
        self.assertEqual(len(self.chats.history(self.chat)), 2)

    def test_collection_cycle_rejected_and_delete_moves_content(self):
        parent = self.library.create('Parent')
        child = self.library.create('Child', parent['id'])
        with self.assertRaises(ValueError): self.library.move(parent['id'], child['id'])
        with self.assertRaises(ValueError): self.library.move('root', child['id'])
        doc = self.imported()
        self.library.assign_documents([doc['id']], parent['id'])
        session = self.generate()['session']
        item = enroll_question(self.store, session['id'], session['questions'][0]['id'], collection_id=parent['id'])
        before = self.store.item(item)['card_json']
        self.library.remove_collection(parent['id'])
        self.assertEqual(self.library.get(child['id'])['parent_id'], 'root')
        self.assertEqual(self.library.document_memberships()[doc['id']], 'root')
        self.assertEqual(self.store.item(item)['collection_id'], 'root')
        self.assertEqual(self.store.item(item)['card_json'], before)

    def test_replace_has_stable_identity_new_version_and_restart_recovery(self):
        doc = self.imported()
        response = self.client.put('/api/documents/' + doc['id'], params={'expected_source_id': doc['source_id']},
            files={'file': ('new-name.md', b'# New\nDifferent contents.', 'text/markdown')})
        self.assertEqual(response.status_code, 200, response.text)
        current = self.client.get('/api/documents/' + doc['id']).json()
        self.assertEqual(current['id'], doc['id'])
        self.assertEqual(current['name'], doc['name'])
        self.assertNotEqual(current['source_id'], doc['source_id'])
        self.assertEqual(current['revision'], doc['revision'] + 1)
        reopened = TestClient(create_app(self.root / 'uploads'))
        try: self.assertEqual(reopened.get('/api/documents/' + doc['id']).json()['lines'], current['lines'])
        finally: reopened.close()
        stale = self.client.put('/api/documents/' + doc['id'], params={'expected_source_id': doc['source_id']},
            files={'file': ('again.md', b'Bad replacement', 'text/markdown')})
        self.assertEqual(stale.status_code, 409)

    def test_remove_revokes_catalog_scope_and_late_answer_publish(self):
        doc = self.imported()
        removed = self.client.delete('/api/documents/' + doc['id'], params={'expected_source_id': doc['source_id']})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get('/api/documents/' + doc['id']).status_code, 410)
        self.assertNotIn(doc['id'], [d['id'] for d in self.client.get('/api/documents').json()])
        turn = self.chats.begin(self.chat, 'late', {'question': 'Question'})
        with self.assertRaises(ValueError):
            self.chats.finish(turn['id'], turn['attempt'], answer_data('Late answer'),
                              {'document_dependencies': [{'id': doc['id'], 'revision': doc['revision']}]})
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute('SELECT text FROM document_versions WHERE document_id=?', (doc['id'],)).fetchone()[0], '# TCP\nSEQ plus LEN gives ACK.')

    def test_strict_chat_question_uses_only_actual_document_ranges(self):
        doc = self.imported()
        source = Source(doc['source_id'], self.root / 'uploads' / doc['id'].removeprefix('upload-') / doc['name'], '\n'.join(doc['lines']))
        payload = {**self.payload, 'strict': True}
        def generator(messages, schema, *, before_request):
            before_request()
            base = self.generator(messages, DiscussionProposal, before_request=before_request)
            data = discussion_question().model_dump(exclude={'basis_type'})
            citation = {'source_id': source.source_id, 'line_start': 2, 'line_end': 2}
            data['citations'] = [citation]; data['learning']['knowledge']['citations'] = [citation]
            return DocumentProposal(**LearningQuestionDraft.model_validate(data).model_dump(), history_references=base.history_references, reason=base.reason)
        with patch.object(ConversationMemory, 'search', return_value=[]):
            job = start(self.store, self.chats, self.chat, 'strict', payload, sources=[source], document_ids={source.source_id: doc['id']},
                document_dependencies=[{'id': doc['id'], 'revision': doc['revision']}], generator=generator)
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(job['session']['basis']['kind'], 'document_with_discussion')
        self.assertEqual(job['session']['questions'][0]['citations'][0]['line_start'], 2)

    def test_forged_history_range_and_document_claim_fail_without_saving(self):
        def outside(messages, schema, **kwargs):
            result = self.generator(messages, schema, **kwargs)
            ref = result.history_references[0].model_copy(update={'end_offset': 99999})
            return result.model_copy(update={'history_references': [ref]})
        job = self.generate(generator=outside)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.store.list(), [])
        data = discussion_question().model_dump()
        data['learning']['knowledge']['citations'] = [{'source_id': 'imaginary', 'line_start': 1, 'line_end': 1}]
        with self.assertRaises(ValueError): DiscussionQuestion.model_validate(data)

    def test_source_removal_during_question_generation_blocks_save(self):
        doc = self.imported()
        draft = discussion_question().model_dump(exclude={'basis_type'})
        citation = {'source_id': doc['source_id'], 'line_start': 2, 'line_end': 2}
        draft['citations'] = [citation]; draft['learning']['knowledge']['citations'] = [citation]
        def replaced(*args, **kwargs):
            self.client.delete('/api/documents/' + doc['id'], params={'expected_source_id': doc['source_id']})
            return LearningQuestionDraft.model_validate(draft)
        with patch('practice_service.generate_question', side_effect=replaced):
            response = self.client.post('/api/practice', json={'document_ids': [doc['id']], 'focus_document_id': doc['id'],
                'focus_source_id': doc['source_id']})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.list(), [])

    def test_outline_version_status_and_collection_removal_preserve_record(self):
        doc = self.imported()
        collection = self.library.create('Outline source')
        self.library.assign_documents([doc['id']], collection['id'])
        snapshot = {'document_id': doc['id'], 'source_id': doc['source_id'], 'name': doc['name'],
                    'path': str(self.root / 'uploads' / doc['id'][7:] / doc['name']), 'text': '\n'.join(doc['lines'])}
        outlines = self.app.state.outline_store
        identity = outlines.create(collection['id'], 'outline', '', 'en', scope_fingerprint([snapshot]), [snapshot], [], [])
        original = self.client.get('/api/outlines/' + identity).json()
        self.assertFalse(original['stale'])
        self.client.put('/api/documents/' + doc['id'], params={'expected_source_id': doc['source_id']}, files={'file': ('new.md', b'# Changed\nNew basis.', 'text/markdown')})
        self.assertTrue(self.client.get('/api/outlines/' + identity).json()['stale'])
        listing = self.client.get('/api/collections/' + collection['id'] + '/outlines').json()
        self.assertTrue(listing[0]['stale'])
        self.library.remove_collection(collection['id'])
        after = self.client.get('/api/outlines/' + identity).json()
        self.assertTrue(after['stale'])
        self.assertEqual(after['collection_id'], 'root')
        self.assertEqual(outlines.get(identity)['snapshots'], [snapshot])

    def test_chat_job_api_retry_restores_same_session_after_reopen(self):
        payload = {**self.payload, 'request_id': 'api-exercise'}
        with patch.object(ConversationMemory, 'search', return_value=[]), patch('chat_practice._request_structured', side_effect=self.generator):
            response = self.client.post('/api/conversations/' + self.chat + '/practice', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'completed', response.json())
        reopened = TestClient(create_app(self.root / 'uploads'))
        try:
            repeated = reopened.post('/api/conversations/' + self.chat + '/practice', json=payload).json()
            self.assertEqual(repeated['session']['id'], response.json()['session']['id'])
            self.assertEqual(reopened.get('/api/conversations/' + self.chat + '/practice').json()[0]['request_id'], 'api-exercise')
        finally: reopened.close()

    def test_collection_move_changes_ancestor_scopes_without_changing_file_identity(self):
        left, right = self.library.create('Left'), self.library.create('Right')
        nested = self.library.create('Nested', left['id'])
        doc = self.imported(); self.library.assign_documents([doc['id']], nested['id'])
        self.assertEqual(len(self.client.get('/api/documents', params={'collection_id': left['id']}).json()), 1)
        moved = self.client.post('/api/collections/' + nested['id'] + '/move', json={'collection_id': right['id']})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.client.get('/api/documents', params={'collection_id': left['id']}).json(), [])
        self.assertEqual(self.client.get('/api/documents', params={'collection_id': right['id']}).json()[0]['id'], doc['id'])

    def test_delete_doc_preserves_enrolled_source_snapshot_and_real_answer(self):
        from test_learning import learning_question
        doc = self.imported()
        draft = learning_question()
        data = draft.model_dump()
        citation = {'source_id': doc['source_id'], 'line_start': 2, 'line_end': 2}
        data['citations'] = [citation]; data['learning']['knowledge']['citations'] = [citation]
        draft = LearningQuestionDraft.model_validate(data)
        with patch('practice_service.generate_question', return_value=draft):
            response = self.client.post('/api/practice', json={'document_ids': [doc['id']], 'focus_document_id': doc['id'], 'focus_source_id': doc['source_id']})
        session = response.json(); qid = session['questions'][0]['id']
        item = enroll_question(self.store, session['id'], qid)
        self.store.submit_answer(session['id'], 'Real answer retained')
        before = self.store.item(item)
        self.client.delete('/api/documents/' + doc['id'], params={'expected_source_id': doc['source_id']})
        record = self.store.get(session['id'])
        self.assertEqual(record['questions'][0]['answer_text'], 'Real answer retained')
        self.assertEqual(record['snapshots'][0]['source_id'], doc['source_id'])
        self.assertEqual(self.store.item(item)['card_json'], before['card_json'])
        self.assertIsNone(record['basis'])


if __name__ == '__main__':
    unittest.main()
