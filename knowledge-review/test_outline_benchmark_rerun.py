"""Offline proof of inherited caps and separate per-request result stages."""

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from answers import StrictModel
from outline_benchmark_generation import ROOT, BUDGET
from outline_benchmark_guard import require_approval
from outline_benchmark_prepare import write_json
from outline_benchmark_rerun import prepare, PREVIOUS, RerunRunner, export_stages
from structured_output import request_structured


class Result(StrictModel):
    count: int
    evidence_ids: list[str]


class RerunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=ROOT/'.data'/('rerun-controlled-'+uuid4().hex)
        with patch('httpx.Client.send',side_effect=AssertionError('Preparation must be offline')):
            prepare(cls.root)
        cls.plan=json.loads((cls.root/'planning.json').read_text(encoding='utf-8'))
        cls.baseline=(cls.root/'live/requests.jsonl').read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.root.resolve().relative_to((ROOT/'.data').resolve())
        shutil.rmtree(cls.root)

    def test_latest_checkpoint_limits_and_only_failed_tasks(self):
        require_approval(self.root)
        self.assertEqual([t['document_id'] for t in self.plan['tasks']],['D03','D04','D06'])
        self.assertTrue(all(t['algorithm']=='v1' for t in self.plan['tasks']))
        ref=json.loads((self.root/'RUN_REFERENCE.json').read_text(encoding='utf-8'))
        self.assertEqual(ref['inherited_http_requests'],123)
        self.assertEqual(ref['inherited_generation_requests'],93)
        self.assertEqual(ref['remaining_generation_requests'],35)
        self.assertEqual(ref['logical_lower_bound'],36)
        self.assertTrue(ref['limits_not_expanded'])
        for name in ('GOLD.json','corpus.json'):
            self.assertEqual((self.root/name).read_bytes(),(PREVIOUS/name).read_bytes())

    def test_actual_response_schema_evidence_and_final_candidates_are_separate(self):
        runner=RerunRunner(self.root); task=self.plan['tasks'][0]; seen=[]
        replies=['{"count":"1","evidence_ids":["ref1"]}}',
                 '{"count":1,"evidence_ids":["ref99"]}',
                 '{"count":1,"evidence_ids":["ref1"]}']
        def respond(request):
            seen.append(json.loads(request.content)); raw=replies[len(seen)-1]
            return httpx.Response(200,json=dict(id='same-provider-id',model='deepseek-flash',created=0,
                object='chat.completion',choices=[dict(index=0,finish_reason='tool_calls',message=dict(
                    role='assistant',content=None,reasoning_content='HIDDEN_REASONING',tool_calls=[dict(
                        id='call',type='function',function=dict(name='submit_result',arguments=raw))]))],
                usage=dict(prompt_tokens=100,completion_tokens=20,total_tokens=120)))
        def validate(value):
            if value.evidence_ids!=['ref1']: raise ValueError('引用必须为 ref1')
        with patch('outline_benchmark_rerun.ForwardTransport',return_value=httpx.MockTransport(respond)):
            with runner.model_scope('generation',task['task_id'],BUDGET):
                with runner.client('generation',task['task_id'],BUDGET) as client:
                    result=request_structured(client,'deepseek-flash',[dict(role='user',content='Use ref1')],
                        Result,budget=BUDGET,validator=validate,protocol_policy='forced_tools')
        self.assertEqual(result.count,1); self.assertEqual(len(seen),3)
        for body in seen:
            self.assertEqual(body['thinking'],{'type':'disabled'})
            self.assertEqual(body['max_tokens'],12288)
        # Only persisted formal candidates are exported. Rejected raw drafts above
        # remain in per-request diagnostics and cannot be promoted by the exporter.
        candidate=dict(id='formal',learning={'target':'persisted fixture'})
        write_json(self.root/'live/jobs/D03-B.json',dict(status='completed',
            structured_output_trace_ids=runner.output_traces[task['task_id']],source_sha256='frozen',
            job=dict(candidates=[candidate])))
        index=export_stages(self.root)
        self.assertEqual(index[0]['request_count'],3)
        directory=self.root/'live/artifacts/D03-B'
        attempts=json.loads((directory/'model_attempts.json').read_text(encoding='utf-8'))
        self.assertEqual([a['schema_status'] for a in attempts],['failed','passed','passed'])
        self.assertEqual([a['evidence_status'] for a in attempts],['not_run','failed','passed'])
        self.assertEqual(len({a['request_id'] for a in attempts}),3)
        for i,a in enumerate(attempts):
            folder=self.root/a['artifact_directory']
            raw=(folder/'original_response.json').read_text(encoding='utf-8')
            self.assertNotIn('HIDDEN_REASONING',raw); self.assertNotIn('authorization',raw.lower())
            body=json.loads(raw)['public_response']
            self.assertEqual(body['choices'][0]['message']['tool_calls'][0]['function']['arguments'],replies[i])
            parsed=json.loads((folder/'parse_result.json').read_text(encoding='utf-8'))
            self.assertEqual(parsed['parsed_result']['count'],'1' if i==0 else 1)
            self.assertEqual(parsed['repaired'],i==0)
        formal=json.loads((directory/'final_candidates.json').read_text(encoding='utf-8'))
        self.assertEqual(formal['candidates'],[candidate])
        self.assertEqual(index[1]['status'],'not_run')
        self.assertEqual((PREVIOUS/'live/requests.jsonl').read_bytes(),self.baseline)


if __name__=='__main__': unittest.main()
