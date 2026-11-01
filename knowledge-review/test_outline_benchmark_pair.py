"""No paid requests: acceptance, blind judging, evidence checks and bounded scope."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

from sources import Source
from outline_benchmark_guard import EvaluationStopped,RequestLedger,Limits
from outline_benchmark_pair import PairRunner,ALLOWED_TASKS,run
from outline_benchmark_judge import (CoverageResult,QualityResult,anonymize,packets,check_coverage,check_quality,metrics)
import test_outline_benchmark as previous


class PairTest(unittest.TestCase):
    setUp=previous.LedgerTest.setUp
    tearDown=previous.LedgerTest.tearDown

    def fixture(self):
        source=Source('s',Path('public.md'),'A transaction is atomic.\nNo work is partially committed.')
        learning=dict(knowledge=dict(name='Atomicity',statement='A transaction is atomic.',content_tags=[],
            citations=[dict(source_id='s',line_start=1,line_end=1)]),
            target=dict(title='Explain atomicity',capability='explain',description='Explain atomicity.',
                scope='Transactions.',criteria=['A transaction is atomic.']))
        candidates,mapping=anonymize([dict(id='PRIVATE-ID',learning=learning)],{'s':source})
        gold=dict(id='G1',topic='atomicity',ability='explain',source_id='s',
            required_conditions=[dict(id='C01',text='State atomicity')],
            evidence_ranges=[dict(line_start=1,line_end=1,text='A transaction is atomic.')])
        e=dict(source_id='s',line_start=1,line_end=1,quote='A transaction is atomic.')
        condition=dict(condition_id='C01',status='supported',supports=[dict(candidate_id='C001',
            candidate_quote='A transaction is atomic.',evidence=[e])],reason='explicit')
        rating=dict(gold_id='G1',label='complete',condition_results=[condition],missing_conditions=[],
            critical_contradiction=False,reason='All conditions explicit.')
        quality=dict(candidate_id='C001',source_support='supported',conditions_accurate='yes',ability_clear='yes',
            gradable='yes',duplicate_of=None,duplicate_uncertain=False,serious_errors=[],evidence=[e],reason='supported')
        return source,candidates,gold,rating,quality

    def test_missing_reasoning_usage_no_longer_blocks_acceptance(self):
        runner=SimpleNamespace(fatal=None)
        PairRunner.on_provider_result(runner,200,dict(usage={'prompt_tokens':12},choices=[]),None)
        self.assertIsNone(runner.fatal)
        PairRunner.on_provider_result(runner,400,{},None)
        self.assertEqual(runner.fatal,'provider_HTTP_400')

    def test_fixed_scope_does_not_expand_to_all_tasks(self):
        runner=object.__new__(PairRunner);runner.work=self.directory;runner.fatal=None
        with patch('outline_benchmark_pair.require_retry_authorization'),patch('outline_benchmark_pair.verify_pair'):
            for t in ALLOWED_TASKS:runner.before_http('generation',t)
            with self.assertRaises(EvaluationStopped):runner.before_http('generation','D06-A')
            with self.assertRaises(EvaluationStopped):runner.before_http('judge','D06-A:coverage:01')

    def test_anonymous_inputs_have_exact_source_and_no_private_identity(self):
        source,candidates,gold,_,_=self.fixture()
        encoded=json.dumps(candidates)
        self.assertNotIn('PRIVATE-ID',encoded);self.assertEqual(candidates[0]['candidate_id'],'C001')
        self.assertEqual(candidates[0]['cited_evidence'][0]['text'],source.text.splitlines()[0])
        planned=list(packets([gold]*3,candidates))
        self.assertEqual([p[0] for p in planned],['coverage','coverage','quality'])
        self.assertEqual(len(planned[0][1]['gold']),2)
        self.assertEqual(planned[0][1]['candidates'],candidates)

    def test_complete_requires_all_conditions_and_exact_proof(self):
        source,candidates,gold,rating,_=self.fixture();packet=dict(gold=[gold],candidates=candidates)
        result=check_coverage(CoverageResult(ratings=[rating]),packet,{'s':source})
        self.assertEqual(result[0]['label'],'complete')
        rating['condition_results'][0]['supports'][0]['candidate_quote']='invented quotation'
        result=check_coverage(CoverageResult(ratings=[rating]),packet,{'s':source})
        self.assertEqual(result[0]['label'],'uncertain')

    def test_source_existing_but_not_cited_cannot_support_judgement(self):
        source,candidates,gold,rating,_=self.fixture()
        e=rating['condition_results'][0]['supports'][0]['evidence'][0]
        e.update(line_start=2,line_end=2,quote=source.text.splitlines()[1])
        checked=check_coverage(CoverageResult(ratings=[rating]),dict(gold=[gold],candidates=candidates),{'s':source})
        self.assertIn('evidence_quote_or_range_invalid',checked[0]['invalid_judge'])

    def test_missing_condition_and_unknown_gold_ids_are_uncertain(self):
        source,candidates,gold,rating,_=self.fixture()
        gold['required_conditions'].append(dict(id='C02',text='Another necessary condition'))
        rows=check_coverage(CoverageResult(ratings=[rating]),dict(gold=[gold],candidates=candidates),{'s':source})
        self.assertEqual(rows[0]['label'],'uncertain')
        rating['gold_id']='G-UNKNOWN'
        rows=check_coverage(CoverageResult(ratings=[rating]),dict(gold=[gold],candidates=candidates),{'s':source})
        self.assertEqual(rows[0]['label'],'uncertain')

    def test_valid_target_and_duplicate_validation(self):
        source,candidates,_,_,quality=self.fixture()
        packet=dict(candidates=candidates,all_candidate_cards=[dict(candidate_id='C001',learning=candidates[0]['learning'])])
        checked=check_quality(QualityResult(ratings=[quality]),packet,{'s':source})
        self.assertTrue(checked[0]['valid_target'])
        quality['duplicate_of']='C001'
        checked=check_quality(QualityResult(ratings=[quality]),packet,{'s':source})
        self.assertFalse(checked[0]['valid_target']);self.assertIn('invalid_duplicate_representative',checked[0]['invalid_judge'])

    def test_uncertain_and_unscored_not_counted_as_correct(self):
        source,candidates,gold,rating,quality=self.fixture()
        rating['label']='uncertain';quality['conditions_accurate']='uncertain'
        coverage=check_coverage(CoverageResult(ratings=[rating]),dict(gold=[gold],candidates=candidates),{'s':source})
        q=check_quality(QualityResult(ratings=[quality]),dict(candidates=candidates,all_candidate_cards=candidates),{'s':source})
        score=metrics([gold],candidates,coverage,q)
        self.assertEqual(score['key_goal_complete_recall'],0);self.assertEqual(score['valid_target_ratio'],0)
        unscored=metrics([gold],candidates,[],[])
        self.assertEqual(unscored['gold_uncertain'],1);self.assertEqual(unscored['quality_unscored_or_invalid'],1)

    def test_budget_keeps_existing_usd10_global_limits(self):
        runner=SimpleNamespace(ledger=RequestLedger(self.directory/'shared.jsonl',Limits()),work=self.directory)
        for n in range(5):runner.ledger.reserve('generation',str(n),.05,{})
        value=PairRunner.assess_remaining_budget(runner)
        self.assertAlmostEqual(value['remaining_reserved_usd'],9.75)
        self.assertTrue(value['sufficient_for_pair_with_full_remaining_judge_reserve'])

    def test_failed_D01_C_stops_before_judge_or_complex_generation(self):
        live=self.directory/'live';(live/'jobs').mkdir(parents=True)
        (live/'jobs/D01-A.json').write_text('{}',encoding='utf-8')
        fake=SimpleNamespace(live=live,generated={},generate_once=Mock(side_effect=EvaluationStopped('failed C')),
            score=Mock(),assess_remaining_budget=Mock(),checkpoint=Mock())
        with (patch('outline_benchmark_pair.FORCED_RUN',self.directory),
                patch('outline_benchmark_pair.PairRunner',return_value=fake),patch('outline_benchmark_pair.validate_job')):
            run(self.directory)
        fake.generate_once.assert_called_once_with('D01-C');fake.score.assert_not_called();fake.assess_remaining_budget.assert_not_called()


if __name__=='__main__':unittest.main()
