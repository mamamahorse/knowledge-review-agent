"""Infrastructure tests with caller-owned state; no learning state machine."""
import unittest
from tool_execution import ToolRecovery, ToolArgumentsError, parse_arguments, validate_arguments


class ToolExecutionTests(unittest.TestCase):
    def test_contract_parser_and_field_error_are_business_independent(self):
        with self.assertRaises(ToolArgumentsError) as result: parse_arguments('{')
        self.assertEqual(result.exception.issues[0]['code'],'invalid_json')
        with self.assertRaises(ToolArgumentsError) as result:
            validate_arguments({},dict(type='object',properties={'source_id':dict(type='string')},required=['source_id']))
        self.assertEqual(result.exception.issues[0]['path'],['source_id'])

    def test_legacy_network_slot_migrates_without_resetting_observed_count(self):
        state=dict(correction=dict(error=dict(code='MODEL_TRANSPORT'),attempts=2),transport=None)
        recovery=ToolRecovery(state)
        self.assertIsNone(state['correction'])
        self.assertEqual(recovery.exhausted(),'transport')
        recovery.reset()
        self.assertIsNone(recovery.exhausted())
