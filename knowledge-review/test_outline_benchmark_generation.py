"""No network: current-code128k/12k planning, bounded execution and lossless blind export."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from openai import OpenAI
from pydantic import ValidationError

from generation_budget import BudgetError
from outline_benchmark import load_source
from outline_benchmark_export import export_comparison, assert_blind, ledger_records, usage_metrics
from outline_benchmark_generation import (BUDGET, ROOT, COMPACT_RUN, prepare, GenerationRunner, GenerationTransport, failure_info)
from outline_benchmark_guard import EvaluationStopped, require_approval
from outline_benchmark_prepare import write_json
from test_outlines import proposal, wire_proposal


class GenerationOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=ROOT/'.data'/('generation-controlled-'+uuid4().hex)
        with patch('httpx.Client.send', side_effect=AssertionError('Offline tests must not send HTTP')):
            prepare(cls.root)
        cls.plan=json.loads((cls.root/'planning.json').read_text(encoding='utf-8'))
        cls.baseline_ledger=(cls.root/'live/requests.jsonl').read_bytes()
        (cls.root/'live/jobs').mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.root.resolve().relative_to((ROOT/'.data').resolve())
        shutil.rmtree(cls.root)

    def tearDown(self):
        for path in (self.root/'live/jobs').glob('*.json'): path.unlink()
        (self.root/'live/requests.jsonl').write_bytes(self.baseline_ledger)

    def test_all_twelve_plans_use_latest_code_and_fixed_budget(self):
        self.assertEqual(len(self.plan['tasks']),12)
        require_approval(self.root)
        for task in self.plan['tasks']:
            self.assertEqual((task['context_tokens'],task['output_tokens']),(131072,12288))
            self.assertIn(task['algorithm'],('v1','v2'))
            if task['algorithm']=='v2': self.assertEqual(task['plan']['generation_budget']['output_tokens'],12288)
        self.assertIn('learning_types.py',self.plan['code_sha256'])
        self.assertIn('outline_benchmark_generation.py',self.plan['code_sha256'])
        self.assertEqual(BUDGET.margin_tokens,2048)

    def test_judge_is_rejected_before_any_transport(self):
        runner=object.__new__(GenerationRunner)
        with self.assertRaisesRegex(EvaluationStopped,'Judge requests are forbidden'):
            runner.before_http('judge','anything')

    def controlled_generation(self, task_index, invalid_count, invalid_kind='schema'):
        runner=GenerationRunner(self.root); task=self.plan['tasks'][task_index]; seen=[]
        def reply(request):
            body=json.loads(request.content); seen.append(body)
            p=proposal(task['source_id'],5).model_dump()
            p['target']['criteria']=[f'Necessary condition {i}' for i in range(5)]
            from outline_service import LearningProposal
            p=wire_proposal(LearningProposal.model_validate(p),json.loads(body['messages'][1]['content'])['available_evidence'])
            value=dict(proposals=[p],note='controlled',omission_risk=False,missing_context='')
            if len(seen)<=invalid_count and invalid_kind=='schema': value.pop('note')
            if len(seen)<=invalid_count and invalid_kind=='citation':
                p['knowledge']['evidence_ids']=['ref_not_provided']
            arguments=json.dumps(value)
            if len(seen)<=invalid_count and invalid_kind=='tail_brace': arguments+='}'
            return httpx.Response(200,json=dict(id='controlled',object='chat.completion',created=0,model='deepseek-flash',
                choices=[dict(index=0,finish_reason='tool_calls',message=dict(role='assistant',content=None,
                    tool_calls=[dict(id='result',type='function',function=dict(name='submit_result',arguments=arguments))]))],
                usage=dict(prompt_tokens=100,completion_tokens=200,total_tokens=300,prompt_cache_hit_tokens=64)))
        def client(kind,key,budget):
            return OpenAI(api_key='test',base_url=runner.protocol['generation']['endpoint'],max_retries=0,
                http_client=httpx.Client(transport=GenerationTransport(httpx.MockTransport(reply),runner,kind,key,budget)))
        with patch.object(runner,'client',side_effect=client), patch('outline_benchmark_generation.time.sleep'), \
             patch('outline_service.run_final_review_after_extraction'):
            result=runner.generate(task)
        return runner,result,seen

    def test_current_wire_uses128k12k_disabled_thinking_and_two_bounded_retries(self):
        runner,result,seen=self.controlled_generation(1,2)
        self.assertEqual(len(seen),3)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['job']['candidates']),1)
        self.assertEqual(len(result['job']['candidates'][0]['learning']['target']['criteria']),5)
        self.assertEqual(result['retry_resume_count'],0)
        for body in seen:
            self.assertEqual(body['max_tokens'],12288)
            self.assertEqual(body['thinking'],{'type':'disabled'})
            self.assertEqual(body['tool_choice']['function']['name'],'submit_result')
            self.assertNotIn('response_format',body)
            self.assertNotIn('reasoning_effort',body)
        self.assertEqual([e['category'] for e in result['failures']],['schema','schema'])
        self.assertIn('note',seen[1]['messages'][-1]['content'])
        self.assertEqual(sum(e['event']=='start' and e['task']==result['task_id'] for e in runner.ledger.events),3)

    def test_three_schema_invalid_outputs_stop_without_outer_retry_multiplication(self):
        _,result,seen=self.controlled_generation(2,99)
        self.assertEqual(len(seen),3)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['job']['candidates'],[])
        self.assertEqual(result['retry_resume_count'],0)

    def test_experiment_uses_project_local_json_repair_without_new_http(self):
        _,result,seen=self.controlled_generation(6,99,'tail_brace')
        self.assertEqual(len(seen),1)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['retry_resume_count'],0)
        self.assertTrue(list((self.root/'live/structured-output').glob('*.jsonl')))

    def test_experiment_citation_failure_is_fed_back_then_corrected(self):
        _,result,seen=self.controlled_generation(5,1,'citation')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(seen),2)
        self.assertIn('business_validation',seen[1]['messages'][-1]['content'])
        self.assertEqual(result['retry_resume_count'],0)

    def test_failure_categories_keep_schema_and_citation_validation(self):
        with self.assertRaises(ValidationError) as invalid:
            from outline_v2 import DirectOutlineDraft
            DirectOutlineDraft.model_validate_json('invalid json')
        self.assertEqual(failure_info(invalid.exception)['category'],'format')
        self.assertEqual(failure_info(ValueError('背景概述返回了未提供的证据ID。'))['category'],'citation')
        self.assertTrue(failure_info(httpx.ConnectError('offline'))['retryable'])
        self.assertFalse(failure_info(BudgetError('offline'))['retryable'])
        wrapped=RuntimeError('transport'); wrapped.__cause__=EvaluationStopped('Campaign request cap reached')
        self.assertTrue(failure_info(wrapped)['fatal'])

    def test_ledger_counts_unique_requests_not_diagnostic_copies(self):
        start=dict(event='start',request_id='a',task='x',reserved_usd=.05,kind='generation')
        end=dict(event='end',request_id='a',usage=dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=20))
        records=ledger_records([start,end,deepcopy(start),deepcopy(end)],dict(input_miss=.3,input_hit=.006,output=1.2))
        self.assertEqual(len(records),1)
        usage=usage_metrics(records)
        self.assertEqual(usage['input_tokens'],dict(known_sum=100,unknown_requests=0))
        self.assertEqual(usage['reasoning_tokens'],dict(known_sum=None,unknown_requests=1))
        self.assertAlmostEqual(usage['cost_estimate_usd_known'],.00003612)

    def test_export_contains_all34_gold_sources_rules_and_pdf_without_identity(self):
        with patch('httpx.Client.send',side_effect=AssertionError('Export cannot call models')):
            data=export_comparison(self.root)
        self.assertEqual(sum(len(d['gold']) for d in data['documents']),34)
        self.assertEqual(data['objective_totals']['current_generation_http_requests'],0)
        self.assertEqual(data['objective_totals']['current_judge_http_requests'],0)
        self.assertTrue(data['documents'][4]['raw_pdf']['content_base64'])
        self.assertEqual(len(data['documents'][4]['frozen_document_structure']['blocks'])>0,True)
        self.assertEqual(len(data['original_rubric_sha256']),64)
        assert_blind((self.root/'comparison_review.md').read_text(encoding='utf-8'))
        self.assertNotIn('INTEGRITY_REVIEW',(self.root/'comparison_review.md').read_text(encoding='utf-8'))

    def fixture_result(self, status='failed', batch_status='completed'):
        task=self.plan['tasks'][0]; doc=json.loads((self.root/'corpus.json').read_text(encoding='utf-8'))['documents'][0]
        source=load_source(COMPACT_RUN,doc)
        p=proposal(source.source_id,5).model_dump()
        p['target']['criteria']=[f'Complete necessary condition {i}' for i in range(5)]
        result=dict(task_id=task['task_id'],document_id='D01',status=status,
            elapsed_seconds=2,first_formal_candidate_seconds=1,completed_seconds=None,
            retry_resume_count=0,failures=[],job=dict(
                batches=[dict(id='b',status=batch_status,line_start=3,line_end=9,window_json=None,
                    visible_json=json.dumps([dict(source_id=source.source_id,line_start=3,line_end=9)]),request_json=None)],
                candidates=[dict(id='private-candidate',batch_id='b',learning=p)]))
        return task,result

    def test_partial_formal_candidates_are_lossless_and_no_semantic_scores_are_added(self):
        task,result=self.fixture_result()
        write_json(self.root/'live/jobs/D01-B.json',result)
        data=export_comparison(self.root)
        group=next(g for g in data['documents'][0]['groups'] if g['generation_status']!='not_run')
        self.assertEqual(group['generation_status'],'partial')
        self.assertEqual(group['formal_candidates'][0]['learning'],result['job']['candidates'][0]['learning'])
        self.assertEqual(len(group['formal_candidates'][0]['learning']['target']['criteria']),5)
        self.assertTrue(group['objective_metrics']['unprocessed_primary_ranges'])
        self.assertNotIn('coverage_score',group)
        text=(self.root/'comparison_review.md').read_text(encoding='utf-8')
        self.assertIn('Complete necessary condition 4',text)
        self.assertNotIn('private-candidate',text)
        self.assertIn('A [`Connection`]',group['formal_candidates'][0]['citation_evidence'][0]['text'])

    def test_superseded_parent_is_never_exported_as_formal_candidate(self):
        _,result=self.fixture_result(batch_status='superseded')
        write_json(self.root/'live/jobs/D01-B.json',result)
        with self.assertRaisesRegex(EvaluationStopped,'superseded/failed'):
            export_comparison(self.root)


if __name__=='__main__':unittest.main()
