import unittest
from pathlib import Path

from outline_benchmark_continue import allowance,validate_saved_candidates
from outline_benchmark_guard import EvaluationStopped


class ContinuationTest(unittest.TestCase):
    def test_retries_count_per_failed_unit_without_reset(self):
        job=dict(batches=[dict(id='done',status='completed',calls=2),dict(id='failed',status='failed',calls=1)],context_steps=[])
        self.assertTrue(allowance(job,{'done':1,'failed':1}))
        job['batches'][1]['calls']=3
        self.assertFalse(allowance(job,{'failed':1}))

    def fixture(self):
        return dict(job=dict(status='failed',snapshots=[dict(source_id='s',path='fixture.md',text='Evidence supports a claim.')],
            batches=[dict(id='b',status='completed',source_id='s',line_start=1,line_end=1,visible_json=None)],
            candidates=[dict(batch_id='b',learning=dict(
                knowledge=dict(name='Evidence',statement='Evidence supports a claim.',content_tags=[],
                    citations=[dict(source_id='s',line_start=1,line_end=1)]),
                target=dict(title='Explain evidence',capability='explain',description='Explain evidence.',scope='A claim.',criteria=['Explain support.'])))]))

    def test_partial_job_retains_only_completed_leaf_candidates(self):
        self.assertEqual(set(validate_saved_candidates(self.fixture())),{'s'})

    def test_superseded_parent_cannot_be_promoted_for_scoring(self):
        value=self.fixture();value['job']['batches'][0]['status']='superseded'
        with self.assertRaises(EvaluationStopped):validate_saved_candidates(value)


if __name__=='__main__':unittest.main()
