"""V2 contract tests with controlled responses, isolated stores, and no cloud use."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import test_outline_context as context_tests
import test_outlines as outline_tests
import test_conversations as conversation_tests
from generation_budget import GenerationBudget
from outline_service import run_outline, public_outline, scope_fingerprint
from outline_store import OutlineBusy
from outline_v2 import (DirectOutlineDraft, PROPOSALS_SOFT_LIMIT, plan_direct, saved_budget,
                        chunks_for, validate_direct, covered_lines)
from chunking import with_table_context
from context import citation_is_visible
from sources import Source
from practice_model import _request_structured


class DirectOutlineTests(unittest.TestCase):
    setUp = context_tests.OutlineContextTests.setUp
    tearDown = context_tests.OutlineContextTests.tearDown
    current = context_tests.OutlineContextTests.current

    def create(self, text=None, capacity=131072, **overrides):
        if text is not None:
            self.source = Source('version', self.source.path, text)
            self.snapshots[0]['text'] = text
        sections, batches, plan = plan_direct([self.source], self.snapshots, '', 'en', capacity)
        plan.update(overrides)
        return self.store.create('root','v2-request','','en',scope_fingerprint(self.snapshots),
                                 self.snapshots,sections,batches,plan)

    def model(self, messages, schema, *, before_request=None, budget=None, telemetry=None):
        self.assertIs(schema, DirectOutlineDraft)
        self.assertIsNotNone(budget)
        before_request()
        if telemetry:
            telemetry(SimpleNamespace(usage=SimpleNamespace(model_dump=lambda:dict(prompt_tokens=99,completion_tokens=12)),
                                      model='controlled', id='controlled-response'), 'json_schema')
        data = json.loads(messages[1]['content'])
        ref = min(data['primary_evidence_ids'],key=lambda ref:data['available_evidence'][ref]['line_start'])
        return schema(proposals=[outline_tests.wire_proposal(outline_tests.proposal('version', data['available_evidence'][ref]['line_start']),data['available_evidence'])], note='controlled')

    def run_job(self, identity, model=None):
        with patch('outline_service._request_structured', side_effect=model or self.model) as called:
            run_outline(self.store,identity,self.current)
        return self.store.get(identity), called

    def test_direct_extraction_no_overviews_and_more_than_80_lines(self):
        identity = self.create('# TCP\n\n'+'\n\n'.join(f'Fact {i}: acknowledged bytes.' for i in range(100)))
        job, model = self.run_job(identity)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(model.call_count,1)
        self.assertEqual(job['context_steps'],[])
        self.assertGreater(job['batches'][0]['line_end'],80)
        public = public_outline(self.store,identity,self.current)
        self.assertTrue(all(s['analyzed_lines']==s['total_lines'] for s in public['coverage']))
        self.assertEqual(public['extraction_diagnostics'][0]['request']['provider_calls'][0]['usage']['prompt_tokens'],99)

    def test_128k_is_configurable_and_32k_produces_more_windows(self):
        text = '# TCP\n\n'+'\n\n'.join(f'{i}: '+('data '*90) for i in range(100))
        source = Source('version',self.source.path,text)
        snapshots = [{**self.snapshots[0],'text':text}]
        small = plan_direct([source],snapshots,'','en',32768)
        large = plan_direct([source],snapshots,'','en',131072)
        self.assertGreater(len(small[1]),len(large[1]))
        self.assertEqual(large[2]['generation_budget']['context_tokens'],131072)
        self.assertEqual(small[2]['generation_budget']['context_tokens'],32768)

    def test_saved_budget_survives_environment_changes(self):
        identity = self.create()
        def model(*args, **kwargs):
            self.assertEqual(kwargs['budget'].context_tokens,131072)
            self.assertEqual(kwargs['budget'].output_tokens,12288)
            return self.model(*args,**kwargs)
        with patch.dict('os.environ',{'LLM_CONTEXT_TOKENS':'8192','OUTLINE_V2_CONTEXT_TOKENS':'32768',
                                      'LLM_OUTPUT_TOKENS':'4096'}):
            job, _ = self.run_job(identity,model)
        self.assertEqual(job['status'],'completed')

    def test_eight_proposals_are_saved_without_count_based_splitting(self):
        identity = self.create()
        calls = 0
        def model(*args,**kwargs):
            nonlocal calls
            calls += 1
            result = self.model(*args,**kwargs)
            if calls == 1:
                result.proposals = [p.model_copy(deep=True) for p in result.proposals for _ in range(PROPOSALS_SOFT_LIMIT)]
            return result
        job, _ = self.run_job(identity,model)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(calls,1)
        self.assertEqual(job['batches'][0]['status'],'completed')
        self.assertEqual(len(job['batches']),1)
        self.assertEqual(len(job['candidates']),PROPOSALS_SOFT_LIMIT)
        diagnostic = json.loads(job['batches'][0]['request_json'])
        self.assertTrue(diagnostic['proposal_soft_limit_reached'])
        self.assertEqual(diagnostic['proposal_count'], PROPOSALS_SOFT_LIMIT)
        self.assertNotIn('coverage_risk', diagnostic)
        self.assertEqual(sum(b['calls'] for b in job['batches']),1)
        self.assertEqual(sum(covered_lines(job['batches'],s) for s in job['sections']),len(self.source.text.splitlines()))

    def test_output_truncation_splits_then_completes(self):
        identity = self.create()
        calls = 0
        def model(*args,**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs['before_request']()
                raise RuntimeError('the practice result was truncated.')
            return self.model(*args,**kwargs)
        job, _ = self.run_job(identity,model)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(calls,3)
        self.assertEqual(json.loads(job['batches'][0]['request_json'])['reason'],'output_truncated')

    def test_external_missing_context_is_saved_without_splitting(self):
        identity = self.create()
        def model(*args, **kwargs):
            result = self.model(*args, **kwargs)
            result.missing_context = 'The excerpt does not define an external prerequisite.'
            return result
        job, called = self.run_job(identity, model)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(called.call_count, 1)
        self.assertEqual(len(job['candidates']), 1)
        diagnostic = json.loads(job['batches'][0]['request_json'])
        self.assertFalse(diagnostic['omission_risk'])
        self.assertIn('external prerequisite', diagnostic['missing_context'])
        public = public_outline(self.store, identity, self.current)
        self.assertEqual(public['extraction_diagnostics'][0]['request']['missing_context'], diagnostic['missing_context'])

    def test_primary_omission_splits_and_does_not_publish_parent(self):
        identity = self.create()
        calls = 0
        def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = self.model(*args, **kwargs)
            result.omission_risk = calls == 1
            result.missing_context = 'External context is also absent.'
            return result
        job, _ = self.run_job(identity, model)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(calls, 3)
        parent = job['batches'][0]
        self.assertEqual(parent['status'], 'superseded')
        diagnostic = json.loads(parent['request_json'])
        self.assertTrue(diagnostic['omission_risk'])
        self.assertFalse(diagnostic['proposal_soft_limit_reached'])
        self.assertEqual(diagnostic['reason'], 'primary_omission_risk')
        self.assertTrue(diagnostic['superseded_result']['omission_risk'])
        self.assertTrue(all(c['batch_id'] != parent['id'] for c in job['candidates']))

    def test_omission_at_depth_limit_remains_visible(self):
        identity = self.create(max_split_depth=0)
        def model(*args, **kwargs):
            result = self.model(*args, **kwargs);result.omission_risk = True
            return result
        job, called = self.run_job(identity, model)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(job['status'], 'completed')
        diagnostic = json.loads(job['batches'][0]['request_json'])
        self.assertTrue(diagnostic['omission_risk'])
        self.assertTrue(diagnostic['coverage_risk'])

    def test_previous_schema_cannot_silently_resume_with_new_rules(self):
        identity = self.create(strategy='direct-structure-v1', schema_digest='old-schema')
        job, called = self.run_job(identity)
        self.assertEqual(called.call_count, 0)
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(job['candidates'], [])

    def test_more_than_eight_with_five_criteria_preserves_all_content(self):
        identity = self.create(max_split_depth=0)
        def model(*args,**kwargs):
            result = self.model(*args,**kwargs)
            original = result.proposals[0].model_dump()
            proposals = []
            for i in range(PROPOSALS_SOFT_LIMIT + 1):
                p = json.loads(json.dumps(original))
                p['target']['title'] = f'Distinct target {i}'
                p['target']['criteria'] = [f'Necessary condition {i}.{n}' for n in range(5)]
                proposals.append(p)
            # Real schema validation, rather than assigning an oversized list after construction.
            return DirectOutlineDraft.model_validate({**result.model_dump(), 'proposals':proposals})
        job, called = self.run_job(identity,model)
        self.assertEqual(called.call_count,1)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(len(job['candidates']),9)
        for i, c in enumerate(job['candidates']):
            self.assertEqual(c['learning']['target']['criteria'], [f'Necessary condition {i}.{n}' for n in range(5)])
        diagnostic = json.loads(job['batches'][0]['request_json'])
        self.assertTrue(diagnostic['proposal_soft_limit_reached'])
        self.assertNotIn('coverage_risk', diagnostic)
        self.assertEqual(len(diagnostic['criteria_organization']),9)
        self.assertIn('不得删除必要条件', diagnostic['criteria_organization'][0]['message'])

    def test_truncation_at_depth_limit_fails_without_accepting_partial_output(self):
        identity = self.create(max_split_depth=0)
        def model(*args, **kwargs):
            kwargs['before_request']()
            raise RuntimeError('the practice result was truncated.')
        job, called = self.run_job(identity, model)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['candidates'], [])

    def test_failure_and_resume_only_reprocess_remaining_leaf(self):
        identity = self.create('# TCP\n\n'+'\n\n'.join('fact '*100 for _ in range(100)),32768)
        calls = 0
        def model(*args,**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                kwargs['before_request']()
                raise RuntimeError('controlled failure')
            return self.model(*args,**kwargs)
        before, _ = self.run_job(identity,model)
        first = before['candidates'][0]['id']
        self.assertEqual(before['status'],'failed')
        self.store.resume(identity)
        after, _ = self.run_job(identity)
        self.assertEqual(after['status'],'completed')
        self.assertEqual(after['candidates'][0]['id'],first)
        retried = next(b for b in after['batches'] if b['calls']==2)
        attempts = json.loads(retried['request_json'])['attempts']
        self.assertEqual(len(attempts),2)
        self.assertIsNone(attempts[0]['provider_calls'][0]['usage'])
        self.assertEqual(attempts[1]['provider_calls'][0]['usage']['prompt_tokens'],99)

    def test_call_budget_persists_across_retries(self):
        identity = self.create('# TCP\n\n'+'\n\n'.join('fact '*100 for _ in range(100)),32768,max_calls=1)
        first, _ = self.run_job(identity)
        self.assertEqual(first['status'],'failed')
        self.store.resume(identity)
        after, _ = self.run_job(identity)
        self.assertEqual(sum(b['calls'] for b in after['batches']),1)
        self.assertEqual(after['status'],'failed')

    def test_oversize_single_line_does_not_hide_later_text(self):
        identity = self.create('# TCP\n\n'+'x'*150000+'\n\nLater fact.',32768)
        job, called = self.run_job(identity)
        self.assertEqual(job['status'],'failed')
        self.assertTrue(any(b['status']=='completed' and b['line_end']==5 for b in job['batches']))
        self.assertTrue(any(b['status']=='failed' and b['line_start']==3 for b in job['batches']))
        self.assertEqual(called.call_count,1)

    def test_table_rows_keep_true_header_and_support_cannot_be_only_anchor(self):
        text = '# Table\n\n| key | value |\n| --- | --- |\n'+'\n'.join(f'| {i} | '+('x'*700)+' |' for i in range(50))
        identity = self.create(text,32768)
        windows = [json.loads(b['window_json']) for b in self.store.get(identity)['batches'] if b['status']=='pending']
        window = next(w for w in windows if w['support_ranges'] and min(a for a,b in w['primary_ranges']) > 4)
        visible = with_table_context(chunks_for(self.source,window['units']),[self.source])
        self.assertTrue(citation_is_visible('version',3,4,visible))
        with self.assertRaises(ValueError) as caught:
            from outline_evidence import extraction_evidence
            validate_direct(DirectOutlineDraft(proposals=[outline_tests.wire_proposal(outline_tests.proposal('version',3),
                extraction_evidence(self.source,visible))],note='header only'),self.source,window)
        from structured_recovery import error_details
        detail=error_details(caught.exception)[0]
        self.assertEqual(detail['path'],['proposals',0,'knowledge','evidence_ids'])
        self.assertEqual(detail['type'],'missing_primary_evidence')
        self.assertTrue(detail['primary_evidence_ids'])
        self.assertLess(covered_lines([dict(status='completed',window_json=json.dumps(window))],
                                     dict(line_start=1,line_end=len(text.splitlines()))),len(text.splitlines()))

    def test_pause_during_request_rejects_late_publish_and_can_resume(self):
        identity = self.create()
        def model(*args,**kwargs):
            result = self.model(*args,**kwargs)
            self.store.pause(identity)
            return result
        job, _ = self.run_job(identity,model)
        self.assertEqual(job['status'],'paused')
        self.assertEqual(job['candidates'],[])
        self.store.resume(identity)
        after, _ = self.run_job(identity)
        self.assertEqual(after['status'],'completed')

    def test_source_change_rejects_late_result(self):
        identity = self.create()
        def model(*args,**kwargs):
            result = self.model(*args,**kwargs)
            self.snapshots[0]['source_id']='changed'
            return result
        job, _ = self.run_job(identity,model)
        self.assertEqual(job['candidates'],[])
        self.assertEqual(job['status'],'failed')

    def test_same_request_cannot_change_algorithm_or_budget(self):
        identity = self.create()
        job = self.store.get(identity)
        plan = dict(job['plan'],generation_budget={**job['plan']['generation_budget'],'context_tokens':32768})
        with self.assertRaises(OutlineBusy):
            self.store.create('root','v2-request','','en',job['scope_fingerprint'],self.snapshots,job['sections'],[],plan)

    def test_model_change_on_resume_does_not_reuse_old_semantic_identity(self):
        identity = self.create()
        with patch('outline_v2.model_identity',return_value=dict(model='changed',endpoint_hash='changed')):
            job, _ = self.run_job(identity)
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job['candidates'],[])

    def test_navigation_only_and_empty_documents_need_no_model(self):
        identity = self.create('# Navigation\n\n')
        job, called = self.run_job(identity)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(called.call_count,0)


class DirectOutlineApiTests(unittest.TestCase):
    setUp = conversation_tests.ConversationTests.setUp
    tearDown = conversation_tests.ConversationTests.tearDown

    def prepare(self):
        collection = self.client.post('/api/collections',json=dict(name='controlled-v2')).json()['id']
        response = self.client.post('/api/documents?collection_id='+collection,
            files=[('files',('controlled.md',b'# TCP\n\nACK points to the next expected byte.','text/markdown'))])
        self.assertEqual(response.status_code,201)
        return collection

    def test_api_keeps_v1_default(self):
        collection = self.prepare()
        with patch('web_app.run_outline'), patch('model_client.model_settings',return_value=('test',{})), \
             patch('outline_service.model_settings',return_value=('test',{})):
            response = self.client.post(f'/api/collections/{collection}/outlines',json=dict(request_id='v1-default'))
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.app.state.outline_store.get(response.json()['id'])['plan']['version'],1)

    def test_api_explicit_v2_128k_and_invalid_budget(self):
        collection = self.prepare()
        with patch('web_app.run_outline'), patch('model_client.model_settings',return_value=('test',{})), \
             patch('outline_service.model_settings',return_value=('test',{})):
            response = self.client.post(f'/api/collections/{collection}/outlines',
                json=dict(request_id='v2-opt-in',algorithm='v2',context_tokens=131072))
        self.assertEqual(response.status_code,202)
        plan = self.app.state.outline_store.get(response.json()['id'])['plan']
        self.assertEqual(plan['version'],2)
        self.assertEqual(saved_budget(plan).context_tokens,131072)
        self.assertEqual(self.client.post(f'/api/collections/{collection}/outlines',
            json=dict(request_id='bad',algorithm='v2',context_tokens=-1)).status_code,422)

    def test_128k_budget_reaches_actual_sdk_not_32k_fallback(self):
        seen=[]
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200,json=dict(id='controlled',object='chat.completion',created=0,model='test',
                choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',
                    content=json.dumps(dict(proposals=[],note='controlled',omission_risk=False,missing_context=''))))],
                usage=dict(prompt_tokens=100,completion_tokens=10,total_tokens=110)))
        client = httpx.Client(transport=httpx.MockTransport(respond))
        budget = GenerationBudget(131072,12288,2048)
        with patch('practice_model.model_settings',return_value=('test',dict(api_key='test',http_client=client))), \
             patch.dict('os.environ',{'LLM_CONTEXT_TOKENS':'32768'}):
            result = _request_structured([dict(role='user',content='a'*40000)],DirectOutlineDraft,budget=budget)
        self.assertEqual(result.note,'controlled')
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['max_tokens'],12288)
        self.assertEqual(len(seen[0]['messages'][0]['content']),40000)


if __name__ == '__main__':
    unittest.main()
