"""Scope/limit/cache checks using frozen public data and fake HTTP only."""

from hashlib import sha256
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch
import unittest

import httpx
from openai import OpenAI
from pydantic import BaseModel,ValidationError

from generation_budget import GenerationBudget
from outline_benchmark import load_source,validate_artifacts
from outline_benchmark_compact import COMPACT_RUN,PARENT_RUN,ROOT,OVERRIDES,verify_parent,compact_protocol,check_pending
from outline_benchmark_guard import limits_from_protocol,RequestLedger,AuditTransport,EvaluationStopped
from outline_benchmark_prepare import digest
from structured_output import request_structured,FUNCTION_OUTPUT_PROVIDERS
import test_outline_benchmark as previous_tests


@unittest.skipUnless((COMPACT_RUN/'GOLD.json').exists(),'prepare compact data first')
class CompactArtifactsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus=validate_artifacts(COMPACT_RUN)
        cls.plan=json.loads((COMPACT_RUN/'planning.json').read_text(encoding='utf-8'))
        cls.gold=json.loads((COMPACT_RUN/'GOLD.json').read_text(encoding='utf-8'))

    def test_parent_and_all_core_files_preserved(self):
        self.assertEqual(verify_parent(COMPACT_RUN),41)
        for name,expected in self.plan['code_sha256'].items():self.assertEqual(digest(ROOT/name),expected)

    def test_original_directory_rejected_as_a_write_target(self):
        with self.assertRaises(ValueError):check_pending(PARENT_RUN)
        with self.assertRaises(ValueError):check_pending(PARENT_RUN/'new-folder')

    def test_matrix_and_true_planner_totals(self):
        tasks=self.plan['tasks'];self.assertEqual(len(tasks),16)
        self.assertEqual(sum(t['successful_logical_calls_lower_bound'] for t in tasks),99)
        for doc in self.corpus['documents']:
            self.assertEqual({t['arm'] for t in tasks if t['document_id']==doc['id']},
                set('ABCD') if doc['id'] in ('D01','D06') else set('BD'))
        for t in tasks:self.assertEqual(t['actual_request_limit'],OVERRIDES.get(t['task_id'],16))
        self.assertEqual(self.plan['live_model_requests'],0)

    def test_map_every_markdown_line_to_exact_original(self):
        for doc in self.corpus['documents']:
            if doc['id']=='D05':continue
            mapping=json.loads((COMPACT_RUN/doc['mapping_file']).read_text(encoding='utf-8'))
            new=(COMPACT_RUN/doc['source_file']).read_text(encoding='utf-8').splitlines()
            old=(COMPACT_RUN/mapping['original_canonical']).read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(mapping['lines']),len(new))
            for row in mapping['lines']:
                actual=new[row['derived_line']-1]
                if row['original_line'] is None:self.assertEqual(actual,'')
                else:self.assertEqual(actual,old[row['original_line']-1])

    def test_gold_evidence_and_ids_are_new_and_in_scope(self):
        rows=self.gold['goals'];self.assertEqual(len(rows),34);self.assertFalse(self.gold['exhaustive'])
        self.assertEqual(len({r['id'] for r in rows}),34)
        for doc in self.corpus['documents']:
            own=[r for r in rows if r['document_id']==doc['id']];self.assertTrue(5<=len(own)<=10)
            source=load_source(COMPACT_RUN,doc);lines=source.text.splitlines()
            for r in own:
                self.assertIn('-SG',r['id']);self.assertEqual(r['source_id'],source.source_id)
                self.assertEqual(r['source_sha256'],doc['sha256']);self.assertTrue(r['required_conditions'])
                for e in r['evidence_ranges']:
                    self.assertEqual(e['text'],'\n'.join(lines[e['line_start']-1:e['line_end']]))
                    self.assertTrue(e['original_line_ranges'])

    def test_real_pdf_is_derived_two_pages_not_original_seven(self):
        doc=next(d for d in self.corpus['documents'] if d['id']=='D05');source=load_source(COMPACT_RUN,doc)
        self.assertEqual(source.document.page_count,2)
        self.assertEqual(source.path.name,'D05-pages5-6.pdf')
        self.assertEqual(source.document.raw_hash,digest(source.path))
        mapping=json.loads((COMPACT_RUN/doc['mapping_file']).read_text(encoding='utf-8'))
        self.assertEqual([r['original_page'] for r in mapping['page_map']],[5,6])
        self.assertTrue(all(r['pixel_identical'] for r in mapping['visual_checks']))
        self.assertIn('k1 = k2',source.text)
        self.assertEqual(sum(r['parsed_evidence_status']=='contradictory_symbol' for r in self.gold['goals']),1)

    def test_long_scope_still_exceeds_128k_input_and_keeps_rules(self):
        tasks=[t for t in self.plan['tasks'] if t['document_id']=='D06']
        self.assertTrue(all(t['complete_request']['estimated_input_tokens']>131072 for t in tasks if t['algorithm']=='v2'))
        self.assertEqual({t['arm']:t['initial_pending'] for t in tasks if t['algorithm']=='v2'},{'C':7,'D':2})
        doc=next(d for d in self.corpus['documents'] if d['id']=='D06')
        text=load_source(COMPACT_RUN,doc).text
        self.assertEqual(sum(l.startswith('R') and '：' in l[:5] for l in text.splitlines()),24)
        self.assertEqual(sum(l.startswith('**案例') for l in text.splitlines()),36)

    def test_offline_jobs_all_paused_no_candidates_no_approval(self):
        with closing(sqlite3.connect(COMPACT_RUN/'offline-plans.sqlite3')) as db:
            self.assertEqual(db.execute('select status,count(*) from learning_outlines group by status').fetchall(),[('paused',16)])
            self.assertEqual(db.execute('select count(*) from outline_candidates').fetchone()[0],0)
        self.assertFalse((COMPACT_RUN/'gold-approved.json').exists())

    def test_v2_budget_and_endpoint_identity_persisted(self):
        expected=sha256(b'https://api.deepseek.com').hexdigest()
        for t in self.plan['tasks']:
            if t['algorithm']=='v2':
                self.assertEqual(t['plan']['model_identity']['endpoint_hash'],expected)
                b=t['plan']['generation_budget'];self.assertEqual(b['context_tokens'],t['context_tokens'])
                self.assertEqual((b['output_tokens'],b['margin_tokens']),(4096,2048))
                self.assertEqual((t['plan']['max_split_depth'],t['plan']['max_calls']),(4,200))


class CompactLimitsAndCacheTest(unittest.TestCase):
    setUp=previous_tests.LedgerTest.setUp
    tearDown=previous_tests.LedgerTest.tearDown
    approve_fixture=previous_tests.LedgerTest.approve_fixture

    def configured_ledger(self):
        return RequestLedger(self.directory/'compact-requests.jsonl',limits_from_protocol(compact_protocol()))

    def test_exactly_three_task_exceptions(self):
        limits=limits_from_protocol(compact_protocol());self.assertEqual(dict(limits.generation_task_overrides),OVERRIDES)
        ledger=self.configured_ledger()
        for task,n in [('D01-A',16),('D06-A',24),('D06-B',24),('D05-B',24)]:
            for _ in range(n):ledger.reserve('generation',task,.001,{})
            with self.assertRaises(EvaluationStopped):ledger.reserve('generation',task,.001,{})
        self.assertEqual((limits.generation_total,limits.judge_total,limits.all_total,limits.reserved_usd),(128,96,224,10))

    def test_overrides_do_not_expand_generation_global_cap(self):
        ledger=self.configured_ledger()
        for task,n in [('D06-A',24),('D06-B',24),('D05-B',24),('D01-A',16),('D01-B',16),('D03-B',16),('D04-B',8)]:
            for _ in range(n):ledger.reserve('generation',task,.001,{})
        with self.assertRaises(EvaluationStopped):ledger.reserve('generation','D01-C',.001,{})
        ledger.reserve('judge','independent-judge',.001,{})

    def test_no_40_dollar_alternative_in_compact_configuration(self):
        p=compact_protocol();self.assertNotIn('alternative_for_user_review',p)
        self.assertEqual(p['soft_conservative_reservation_usd'],10)
        self.assertIn('same model+endpoint',p['generation']['protocol_cache'])

    def controlled_response(self,valid=True):
        seen=[]
        def respond(request):
            payload=json.loads(request.content);seen.append(payload)
            if payload.get('response_format'):
                return httpx.Response(400,json={'error':{'message':'response_format unsupported','type':'invalid_request_error'}})
            return httpx.Response(200,json={'id':'mock-only','object':'chat.completion','created':0,'model':payload['model'],
                'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
                    'tool_calls':[{'id':'call1','type':'function','function':{'name':'submit_result',
                        'arguments':json.dumps({'x':1 if valid else 'not-an-int'})}}]}}],
                'usage':{'prompt_tokens':12,'completion_tokens':3,'prompt_cache_hit_tokens':0}})
        return seen,respond

    def execute_fake(self,respond,ledger,endpoint,model,task):
        class Result(BaseModel):x:int
        transport=AuditTransport(httpx.MockTransport(respond),ledger,'generation',task,.05,self.directory)
        with httpx.Client(transport=transport) as http:
            with OpenAI(api_key='test-not-real',base_url=endpoint,http_client=http,max_retries=0) as client:
                return request_structured(client,model,[{'role':'user','content':'controlled fixture'}],Result,
                    budget=GenerationBudget(32768,4096,2048))

    def test_successful_protocol_cache_shared_only_same_model_and_endpoint(self):
        self.approve_fixture();ledger=self.configured_ledger();seen,respond=self.controlled_response()
        with patch.dict(FUNCTION_OUTPUT_PROVIDERS,{},clear=True):
            self.execute_fake(respond,ledger,'https://unit.test/v1','model-a','D01-A')
            self.assertEqual(len(seen),2)
            self.execute_fake(respond,ledger,'https://unit.test/v1','model-a','D01-C')
            self.assertEqual(len(seen),3);self.assertIn('tools',seen[-1])
            self.execute_fake(respond,ledger,'https://unit.test/v1','model-b','other-model')
            self.assertEqual(len(seen),5)
            self.execute_fake(respond,ledger,'https://other.unit.test/v1','model-a','other-endpoint')
            self.assertEqual(len(seen),7)
        ends=[e for e in ledger.events if e['event']=='end'];self.assertEqual(len(ends),7)
        self.assertEqual(ends[0]['http_status'],400);self.assertIsNone(ends[0]['usage'])
        self.assertEqual(ends[1]['usage']['prompt_tokens'],12)

    def test_failed_result_does_not_confirm_protocol_capability(self):
        self.approve_fixture();ledger=self.configured_ledger();seen,respond=self.controlled_response(valid=False)
        with patch.dict(FUNCTION_OUTPUT_PROVIDERS,{},clear=True):
            with self.assertRaises(ValidationError):self.execute_fake(respond,ledger,'https://unit.test/v1','model-a','D01-A')
            self.assertEqual(FUNCTION_OUTPUT_PROVIDERS,{})
        self.assertEqual(len(seen),2)


if __name__=='__main__':unittest.main()
