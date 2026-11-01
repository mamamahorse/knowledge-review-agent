"""Soft counts preserve necessary content; request budgets remain explicit and finite."""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from generation_budget import BudgetError, GenerationBudget
from learning_types import CRITERIA_GUIDANCE, TargetDraft, criteria_organization_note
from outline_service import TargetProposal
from outline_v2 import DirectOutlineDraft, direct_budget


class SoftLimitTests(unittest.TestCase):
    def target(self, count):
        return dict(title='One coherent capability', capability='explain', description='Explain all conditions.',
                    scope='Provided material only.', criteria=[f'Necessary condition {i}' for i in range(count)])

    def test_more_than_four_criteria_are_valid_and_get_an_organization_hint(self):
        for schema in (TargetDraft, TargetProposal):
            result = schema.model_validate(self.target(5))
            self.assertEqual(len(result.criteria), 5)
            self.assertIn('不得删除必要条件', criteria_organization_note(result))
            self.assertIn('同一种能力', criteria_organization_note(result))
        self.assertEqual(criteria_organization_note(TargetDraft.model_validate(self.target(4))), '')
        self.assertIn('Never delete necessary conditions', CRITERIA_GUIDANCE)

    def test_empty_criteria_and_overlong_individual_proposal_criteria_still_fail(self):
        with self.assertRaises(ValidationError):
            TargetDraft.model_validate(self.target(0))
        data = self.target(5); data['criteria'][0] = 'x' * 241
        with self.assertRaises(ValidationError):
            TargetProposal.model_validate(data)

    def test_schemas_have_no_hard_count_limit_for_criteria_or_v2_proposals(self):
        for schema in (TargetDraft, TargetProposal):
            self.assertNotIn('maxItems', schema.model_json_schema()['properties']['criteria'])
        self.assertNotIn('maxItems', DirectOutlineDraft.model_json_schema()['properties']['proposals'])


class BudgetDefaultsTests(unittest.TestCase):
    def test_defaults_are_128k_context_and_12k_output_with_unchanged_safety_margin(self):
        with patch.dict('os.environ', {}, clear=True):
            for budget in (GenerationBudget(), GenerationBudget.from_env(), direct_budget()):
                self.assertEqual((budget.context_tokens, budget.output_tokens, budget.margin_tokens),
                                 (131072, 12288, 2048))
                self.assertEqual(budget.input_limit, 116736)

    def test_explicit_old_experiment_budget_is_not_rewritten(self):
        with patch.dict('os.environ', {'LLM_CONTEXT_TOKENS':'32768', 'LLM_OUTPUT_TOKENS':'4096',
                                      'LLM_SAFETY_TOKENS':'2048'}, clear=True):
            budget = GenerationBudget.from_env()
            self.assertEqual((budget.context_tokens, budget.output_tokens), (32768, 4096))
            self.assertEqual(direct_budget(32768).output_tokens, 4096)

    def test_output_reservation_still_cannot_exceed_context_capacity(self):
        with patch.dict('os.environ', {'LLM_CONTEXT_TOKENS':'8192'}, clear=True):
            with self.assertRaises(BudgetError):
                GenerationBudget.from_env()


if __name__ == '__main__':
    unittest.main()
