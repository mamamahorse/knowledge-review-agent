"""Controlled routing and durable human waiting; no provider quality claims."""

import json
import unittest
from unittest.mock import patch

from agent_loop import AgentRun
from conversation_store import ConversationStore
from task_routing import TaskRouter
import test_agent_loop as runtime_tests
import test_conversations as conversation_tests


class TaskRuntimeTests(unittest.TestCase):
    setUp = runtime_tests.AgentLoopTests.setUp
    run_script = runtime_tests.AgentLoopTests.run_script
    completion = staticmethod(runtime_tests.AgentLoopTests.completion)
    call = staticmethod(runtime_tests.AgentLoopTests.call)

    def test_question_interrupts_before_answer_model(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            return self.completion(dict(role='assistant', content=None, tool_calls=[self.call('q', 'ask_question',
                dict(question='你希望得到什么？', reason_code='task_ambiguous', option_ids=['explain','extract_outline']))]))
        run = self.run_script(handler, task_router=TaskRouter())
        self.assertEqual(len(calls), 1)
        self.assertIsNone(run.answer)
        self.assertEqual(run.control['kind'], 'clarification')
        self.assertEqual(run.visible_chunks, ())

    def test_unavailable_and_duplicate_options_rejected(self):
        router = TaskRouter()
        for ids in (['explain','explain'], ['explain','summarize_materials']):
            with self.assertRaises(ValueError):
                router.execute('ask_question', dict(question='请选择', reason_code='task_ambiguous', option_ids=ids))
        with self.assertRaises(ValueError):
            router.execute('select_task', {'task_id':'delete_everything'})

    def test_conflicting_controls_execute_no_read(self):
        observed = []
        def handler(request):
            body = json.loads(request.content); observed.append(body)
            if len(observed) == 1:
                return self.completion(dict(role='assistant', content=None, tool_calls=[
                    self.call('a', 'select_task', {'task_id':'practice'}),
                    self.call('b', 'read_source', {'source_id':'doc-a','line_start':1,'line_end':3})]))
            if len(observed) == 2:
                self.assertIn('未执行', body['messages'][-1]['content'])
                return self.completion(dict(role='assistant', content=None,
                    tool_calls=[self.call('c','select_task',{'task_id':'practice'})]))
            self.fail('No final model expected')
        run = self.run_script(handler, task_router=TaskRouter())
        self.assertEqual(run.visible_chunks, ())
        self.assertEqual(run.control['task_id'], 'practice')


class ClarificationApiTests(unittest.TestCase):
    setUp = conversation_tests.ConversationTests.setUp
    tearDown = conversation_tests.ConversationTests.tearDown
    submit = conversation_tests.ConversationTests.submit

    def question(self, stream=False):
        control = TaskRouter().execute('ask_question', dict(question='想先做什么？',
            reason_code='task_ambiguous', option_ids=['explain','extract_outline','practice']))
        with patch('web_app.run_agent', return_value=AgentRun(None, (), 'clarification', (), {}, (), control)):
            if stream:
                response = self.client.post(f'/api/conversations/{self.identity}/turns/stream',
                    json={**self.request, 'request_id':'same'})
                self.assertIn('event: clarification', response.text)
                self.assertNotIn('event: error', response.text)
                return self.store.turns(self.identity)[0]
            return self.submit().json()

    def answer(self, turn, **updates):
        body = dict(card_id=turn['control']['id'], revision=turn['control']['revision'],
                    response_id='answer-1', option_id='extract_outline', text='')
        return self.client.post(f"/api/conversations/{self.identity}/turns/{turn['id']}/clarification",
                                json={**body, **updates})

    def test_wait_survives_restart_and_holds_no_slot(self):
        turn = self.question(stream=True)
        self.assertEqual(turn['status'], 'waiting_user')
        self.assertEqual(self.app.state.stream_runner.snapshot()['held_slots'], 0)
        reopened = ConversationStore(self.store.path)
        self.assertEqual(reopened.turn(turn['id'])['control'], turn['control'])
        self.assertEqual(self.store.history(self.identity), [])
        self.assertEqual(self.submit('different').status_code, 409)

    def test_answer_replay_conflict_and_no_model_for_selected_workflow(self):
        turn = self.question()
        self.assertEqual(self.answer(turn).status_code, 200)
        self.assertEqual(self.answer(turn).status_code, 200)
        self.assertEqual(self.answer(turn, option_id='practice').status_code, 409)
        with patch('web_app.run_agent', side_effect=AssertionError('selection needs no classification')):
            result = self.client.post(f"/api/conversations/{self.identity}/turns/{turn['id']}/retry", json={}).json()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['messages'][-1]['result']['task_action']['task_id'], 'extract_outline')
        self.assertEqual(self.store.turn(turn['id'])['request']['question'], self.request['question'])

    def test_foreign_turn_stale_revision_and_cancel(self):
        turn = self.question()
        self.assertEqual(self.answer(turn, revision=99).status_code, 409)
        self.assertEqual(self.answer(turn, option_id='not-real').status_code, 400)
        other = self.store.create('other')['id']
        response = self.client.post(f"/api/conversations/{other}/turns/{turn['id']}/clarification",
            json=dict(card_id=turn['control']['id'], revision=1, response_id='x', option_id='explain'))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post(f"/api/conversations/{self.identity}/turns/{turn['id']}/skip").status_code, 200)
        self.assertEqual(self.answer(turn).status_code, 409)

    def test_unanswered_duplicate_submission_returns_same_card(self):
        turn = self.question()
        with patch('web_app.run_agent', side_effect=AssertionError('must not classify again')):
            again = self.submit().json()
        self.assertEqual(again['control'], turn['control'])

    def test_free_text_saved_separately_and_scope_revalidated(self):
        turn = self.question()
        self.assertEqual(self.answer(turn, option_id=None, text='请解释确认号').status_code, 200)
        self.assertEqual(self.store.turn(turn['id'])['control']['answer']['text'], '请解释确认号')
        self.assertEqual(self.store.turn(turn['id'])['request']['question'], self.request['question'])
        self.assertFalse(next(x for x in self.client.get('/api/task-capabilities').json()
                              if x['id']=='summarize_materials')['available'])

    def test_existing_retry_route_can_save_without_running_model(self):
        turn = self.question()
        body = dict(card_id=turn['control']['id'], revision=1, response_id='saved', option_id='practice')
        with patch('web_app.run_agent', side_effect=AssertionError('save is not model execution')):
            response = self.client.post(f"/api/conversations/{self.identity}/turns/{turn['id']}/retry",
                                       json=dict(clarification=body, save_only=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'waiting_user')
        self.assertEqual(response.json()['control']['answer']['option_id'], 'practice')


if __name__ == '__main__':
    unittest.main()
