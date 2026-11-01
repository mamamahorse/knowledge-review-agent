"""No external requests: verify the exact wire controls before live smoke."""
from types import SimpleNamespace
import json
import unittest
import httpx
from generation_budget import GenerationBudget,BudgetError
from outline_benchmark_guard import EvaluationStopped
from outline_benchmark_compact import compact_protocol
from outline_benchmark_live import ControlledAuditTransport
import test_outline_benchmark as previous


class LiveAdapterTest(unittest.TestCase):
    setUp=previous.LedgerTest.setUp
    tearDown=previous.LedgerTest.tearDown
    approve_fixture=previous.LedgerTest.approve_fixture

    def runner(self):
        self.approve_fixture();live=self.directory/'live';(live/'calls').mkdir(parents=True)
        return SimpleNamespace(protocol=compact_protocol(),directory=self.directory,live=live,
            ledger=self.ledger,returned_models={},fatal=None)

    def test_controls_are_applied_and_hidden_reasoning_is_not_persisted(self):
        runner=self.runner();seen=[]
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200,json=dict(id='fake',model='same-model',usage={'prompt_tokens':8},
                choices=[dict(finish_reason='stop',message=dict(content='public result',reasoning_content='DO_NOT_SAVE_PRIVATE_REASONING'))]))
        t=ControlledAuditTransport(httpx.MockTransport(respond),runner,'generation','D01-A',GenerationBudget())
        with httpx.Client(transport=t) as client:
            client.post('https://api.deepseek.com/chat/completions',json=dict(model='deepseek-flash',messages=[],max_tokens=4096))
        self.assertEqual(seen[0]['thinking'],{'type':'enabled'});self.assertEqual(seen[0]['reasoning_effort'],'high')
        artifact=next((runner.live/'calls').glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('DO_NOT_SAVE_PRIVATE_REASONING',artifact);self.assertIn('public result',artifact)

    def test_full_wire_payload_rejected_before_network_when_over_budget(self):
        runner=self.runner();seen=[]
        t=ControlledAuditTransport(httpx.MockTransport(lambda req:seen.append(req)),runner,'generation','D01-A',GenerationBudget(7000,4096,2048))
        with httpx.Client(transport=t) as client:
            with self.assertRaises(BudgetError):client.post('https://api.deepseek.com/chat/completions',
                json=dict(model='deepseek-flash',messages=[{'role':'user','content':'x'*2000}],max_tokens=4096))
        self.assertEqual(seen,[]);self.assertEqual(runner.ledger.events,[])

    def test_output_limit_mismatch_is_rejected_without_a_request(self):
        runner=self.runner()
        t=ControlledAuditTransport(httpx.MockTransport(lambda req:None),runner,'generation','D01-A',GenerationBudget())
        with httpx.Client(transport=t) as client:
            with self.assertRaises(EvaluationStopped):client.post('https://api.deepseek.com/chat/completions',
                json=dict(model='deepseek-flash',messages=[],max_tokens=8000))
        self.assertEqual(runner.ledger.events,[])


if __name__=='__main__':unittest.main()
