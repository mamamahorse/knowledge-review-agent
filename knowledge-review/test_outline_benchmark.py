"""Controlled/offline checks; never constructs an external model client."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid
import unittest

import httpx

from generation_budget import GenerationBudget
from outline_benchmark import ARMS, offline_scope, plan_one, validate_artifacts
from outline_benchmark_prepare import DEFAULT_RUN, write_json, synthetic
from outline_benchmark_guard import (RequestLedger,Limits,EvaluationStopped,AuditTransport,
    require_approval,review_digest,dollar_cost)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        # Normal workspace directory inherits the sandbox ACL on Windows;
        # Python 3.13's private TemporaryDirectory ACL can deny this test token.
        self.base=Path('.data/benchmark-controlled-tests').resolve()
        self.directory=self.base/uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.ledger=RequestLedger(self.directory/'requests.jsonl',Limits(generation_per_task=2,
            generation_total=3,judge_total=1,all_total=4,reserved_usd=1))
        for n in ['corpus.json','GOLD.json','PROTOCOL.json','RUBRIC.md','planning.json','environment.json']:
            (self.directory/n).write_text('{}',encoding='utf-8')

    def tearDown(self):
        self.directory.resolve().relative_to(self.base)
        shutil.rmtree(self.directory)

    def approve_fixture(self):
        # A fake fixture approval, never the actual campaign marker.
        write_json(self.directory/'gold-approved.json',dict(approved_by='user',review_digest=review_digest(self.directory)))

    def test_missing_approval_blocks_transport_before_send(self):
        with self.assertRaises(EvaluationStopped):
            AuditTransport(httpx.MockTransport(lambda _:None),self.ledger,'generation','A',.05,self.directory)
        self.assertEqual(self.ledger.events,[])

    def test_changed_review_invalidates_approval(self):
        self.approve_fixture();require_approval(self.directory)
        (self.directory/'GOLD.json').write_text('{"changed":true}',encoding='utf-8')
        with self.assertRaises(EvaluationStopped):require_approval(self.directory)

    def test_frozen_source_bytes_checked_even_when_manifest_unchanged(self):
        source=self.directory/'source.md';source.write_text('original',encoding='utf-8')
        write_json(self.directory/'corpus.json',dict(documents=[dict(id='D',source_file='source.md',
            sha256=sha256(source.read_bytes()).hexdigest(),raw_files=[])]))
        self.approve_fixture();require_approval(self.directory)
        source.write_text('changed',encoding='utf-8')
        with self.assertRaises(EvaluationStopped):require_approval(self.directory)

    def test_failed_native_and_fallback_both_count(self):
        self.approve_fixture();calls=[]
        def reply(request):
            calls.append(json.loads(request.content))
            return httpx.Response(400 if len(calls)==1 else 200,json=dict(model='test-model',
                usage=None if len(calls)==1 else dict(prompt_tokens=12,completion_tokens=3,prompt_cache_hit_tokens=2)))
        transport=AuditTransport(httpx.MockTransport(reply),self.ledger,'generation','A',.05,self.directory)
        with httpx.Client(transport=transport) as client:
            client.post('https://unit.test/chat/completions',json=dict(model='test',response_format={'type':'json_schema'}),
                headers={'Authorization':'secret-must-not-be-saved'})
            client.post('https://unit.test/chat/completions',json=dict(model='test',tools=[{}]))
            with self.assertRaises(EvaluationStopped):client.post('https://unit.test/chat/completions',json={})
        starts=[e for e in self.ledger.events if e['event']=='start']
        ends=[e for e in self.ledger.events if e['event']=='end']
        self.assertEqual(len(calls),2);self.assertEqual([e['protocol'] for e in starts],['response_format','tools'])
        self.assertEqual(ends[0]['usage_status'],'unknown');self.assertEqual(ends[1]['usage']['prompt_tokens'],12)
        self.assertNotIn('secret-must-not-be-saved',self.ledger.path.read_text())
        self.assertAlmostEqual(sum(e['reserved_usd'] for e in starts),.1)

    def test_transport_failure_and_restart_preserve_consumption(self):
        self.approve_fixture()
        def fail(_):raise httpx.ReadTimeout('simulated; no actual network')
        t=AuditTransport(httpx.MockTransport(fail),self.ledger,'generation','A',.2,self.directory)
        with httpx.Client(transport=t) as c:
            with self.assertRaises(httpx.ReadTimeout):c.post('https://unit.test/chat/completions',json={})
        recovered=RequestLedger(self.ledger.path,self.ledger.limits)
        self.assertEqual(len([e for e in recovered.events if e['event']=='start']),1)
        self.assertIsNone(recovered.events[-1]['usage'])

    def test_separate_judge_and_global_caps(self):
        self.ledger.reserve('judge','J',.01,{})
        with self.assertRaises(EvaluationStopped):self.ledger.reserve('judge','J2',.01,{})
        for task in ('A','A','B'):self.ledger.reserve('generation',task,.01,{})
        with self.assertRaises(EvaluationStopped):self.ledger.reserve('generation','C',.01,{})

    def test_cost_cap_holds_failed_unknown_reservation(self):
        first=self.ledger.reserve('generation','A',.9,{})
        self.ledger.finish(first,usage=None)
        with self.assertRaises(EvaluationStopped):self.ledger.reserve('generation','B',.2,{})

    def test_no_rebilling_diagnostic_copy(self):
        identity=self.ledger.reserve('generation','superseded-parent',.05,{})
        self.ledger.finish(identity,usage={})
        with self.assertRaises(ValueError):self.ledger.finish(identity,usage={})

    def test_cost_does_not_add_cache_to_prompt_twice(self):
        price=dict(input_miss=.3,input_hit=.006,output=1.2)
        usage=dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=20)
        self.assertAlmostEqual(dollar_cost(usage,price),(80*.3+20*.006+10*1.2)/1e6)
        self.assertIsNone(dollar_cost(None,price))
        self.assertIsNone(dollar_cost(dict(prompt_tokens=1,completion_tokens=1),price))
        self.assertIsNone(dollar_cost({**usage,'prompt_cache_hit_tokens':101},price))


@unittest.skipUnless((DEFAULT_RUN/'planning.json').exists(),'run public preparation first')
class PreparationTest(unittest.TestCase):
    def setUp(self):
        self.manifest=validate_artifacts(DEFAULT_RUN)
        self.plan=json.loads((DEFAULT_RUN/'planning.json').read_text(encoding='utf-8'))

    def test_matrix_exact_and_no_live_calls(self):
        self.assertEqual(len(self.plan['tasks']),18)
        self.assertEqual(self.plan['live_model_requests'],0)
        self.assertEqual(self.plan['full_matrix_documents'],['D01','D03','D06'])
        for doc in self.manifest['documents']:
            arms={t['arm'] for t in self.plan['tasks'] if t['document_id']==doc['id']}
            self.assertEqual(arms,set(ARMS) if doc['id'] in self.plan['full_matrix_documents'] else {'B','D'})

    def test_gold_all_ranges_and_conditions_trace_exactly(self):
        gold=json.loads((DEFAULT_RUN/'GOLD.json').read_text(encoding='utf-8'))
        self.assertFalse(gold['exhaustive']);self.assertEqual(len(gold['goals']),43)
        self.assertEqual(len({g['id'] for g in gold['goals']}),43)
        for d in self.manifest['documents']:
            own=[g for g in gold['goals'] if g['document_id']==d['id']]
            self.assertTrue(5<=len(own)<=10)
            lines=(DEFAULT_RUN/d['source_file']).read_text(encoding='utf-8').splitlines()
            for g in own:
                self.assertEqual(g['source_sha256'],d['sha256'])
                self.assertTrue(g['required_conditions'])
                for e in g['evidence_ranges']:
                    self.assertEqual(e['text'],'\n'.join(lines[e['line_start']-1:e['line_end']]))

    def test_small_full_route_and_scoped_budget_restore(self):
        old=GenerationBudget.from_env()
        doc=self.manifest['documents'][0]
        for arm in ('A','B'):
            task=plan_one(DEFAULT_RUN,doc,arm)
            self.assertEqual(task['mode'],'full');self.assertEqual(task['planned_output_limit'],6)
        with offline_scope(131072):self.assertEqual(GenerationBudget.from_env().context_tokens,131072)
        self.assertEqual(GenerationBudget.from_env(),old)

    def test_v2_persisted_budget_and_core_fingerprints(self):
        for task in self.plan['tasks']:
            if task['algorithm']=='v2':
                b=task['plan']['generation_budget']
                self.assertEqual(b['context_tokens'],task['context_tokens'])
                self.assertEqual((b['output_tokens'],b['margin_tokens']),(4096,2048))
                self.assertEqual(task['plan']['max_split_depth'],4)
                self.assertEqual(task['plan']['max_calls'],200)
        for name,expected in self.plan['code_sha256'].items():
            self.assertEqual(sha256(Path(name).read_bytes()).hexdigest(),expected,name)

    def test_synthetic_tables_are_real_structure(self):
        from markdown_it import MarkdownIt
        tokens=MarkdownIt('commonmark').enable('table').parse(synthetic())
        self.assertEqual(sum(t.type=='table_open' for t in tokens),2)
        self.assertNotIn('-G01',synthetic())


if __name__=='__main__':unittest.main()
