"""No network: revised evaluation keeps original source IDs and bounded scope."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from outline_v2 import DirectOutlineDraft,DIRECT_STRATEGY
from outline_benchmark_omission import RevisedPairRunner,COMPACT_RUN
from outline_benchmark_guard import EvaluationStopped


class RevisionTest(unittest.TestCase):
    def test_model_contract_has_distinct_risks_and_no_old_output_field(self):
        result=DirectOutlineDraft(proposals=[],note='controlled',missing_context='external')
        self.assertFalse(result.omission_risk)
        self.assertEqual(result.missing_context,'external')
        self.assertNotIn('coverage_risk',DirectOutlineDraft.model_json_schema()['properties'])
        with self.assertRaises(ValidationError):DirectOutlineDraft(proposals=[],note='n',omission_risk='false')

    def test_generation_preserves_frozen_original_paths(self):
        runner=object.__new__(RevisedPairRunner);doc={'id':'D01'}
        def invoke(self,task):
            from outline_benchmark_live import load_source
            return load_source('new directory',doc)
        with patch('outline_benchmark_pair.PairRunner.generate_once',invoke),patch('outline_benchmark_omission.load_source',return_value='original') as loader:
            self.assertEqual(runner.generate_once('D01-C'),'original')
        loader.assert_called_once_with(COMPACT_RUN,doc)

    def test_original_small_scope_is_retained(self):
        runner=object.__new__(RevisedPairRunner);runner.work='fixture';runner.fatal=None
        with patch('outline_benchmark_omission.verify_revision'):
            runner.before_http('generation','D01-C')
            runner.before_http('judge','D01-A:coverage:01')
            with self.assertRaises(EvaluationStopped):runner.before_http('generation','D06-A')
            with self.assertRaises(EvaluationStopped):runner.before_http('judge','D05-B:quality:01')


if __name__=='__main__':unittest.main()
