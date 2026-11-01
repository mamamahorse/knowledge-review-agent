"""Evidence IDs across extraction: sparse ranges, schema, bounded corrective feedback."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

import httpx
from openai import OpenAI
from pydantic import ValidationError

from outline_evidence import (extraction_evidence, available_evidence, evidence_id,
                              EvidenceReferenceError)
from outline_service import (OutlineDraft, excerpt,
                             resolve_proposals, validate_learning)
from outline_context import FullOutlineDraft
from outline_v2 import DirectOutlineDraft
from sources import Source
from structured_output import request_structured
from structured_recovery import audit_scope, error_details
from test_outlines import proposal
from test_support import temporary_notes


class ExtractionEvidenceTests(unittest.TestCase):
    def setUp(self):
        lines=[f'Original evidence line {n}.' for n in range(1,55)]
        for n in (10,26,33,41): lines[n-1]=''
        self.source=Source('version',Path('s.md'),'\n'.join(lines))
        self.ranges=[(1,9),(11,25),(27,32),(34,40),(42,54)]
        self.visible=[excerpt(self.source,a,b) for a,b in self.ranges]
        self.registry=extraction_evidence(self.source,self.visible)

    def draft(self, ids=None, schema=OutlineDraft):
        value=proposal('version',1).model_dump()
        value['knowledge'].pop('citations')
        value['knowledge']['evidence_ids']=list(self.registry) if ids is None else ids
        return schema(proposals=[value],note='Controlled only')

    def test_five_disjoint_fragments_remain_five_exact_citations(self):
        for schema in (OutlineDraft,FullOutlineDraft,DirectOutlineDraft):
            result=resolve_proposals(self.draft(schema=schema),self.registry,self.visible)[0]
            self.assertEqual(sorted((c.line_start,c.line_end) for c in result.knowledge.citations),self.ranges)
            self.assertNotIn('evidence_ids',result.knowledge.model_dump())

    def test_wire_schema_forbids_all_free_coordinates(self):
        for schema in (OutlineDraft,FullOutlineDraft,DirectOutlineDraft):
            with self.assertRaises(ValidationError): schema(proposals=[proposal('version',1)],note='bad')
            definitions=schema.model_json_schema()['$defs']
            self.assertNotIn('Citation',definitions)
            knowledge=definitions['EvidenceKnowledgeProposal']['properties']
            self.assertNotIn('citations',knowledge)
            self.assertNotIn('maxItems',knowledge['evidence_ids'])

    def test_ids_stable_across_order_but_bound_to_source_version_and_clip(self):
        self.assertEqual(extraction_evidence(self.source,list(reversed(self.visible))),self.registry)
        old=available_evidence([dict(source_id='other-version',line_start=1,line_end=9)])
        self.assertFalse(set(old)&set(self.registry))
        clipped=extraction_evidence(self.source,[excerpt(self.source,5,8)])
        self.assertEqual([(v['line_start'],v['line_end']) for v in clipped.values()],[(5,8)])
        with self.assertRaises(EvidenceReferenceError): resolve_proposals(self.draft(list(old)),self.registry,self.visible)

    def test_block_registry_has_no_whole_window_shortcut_or_phantom_gap(self):
        full=extraction_evidence(self.source,[excerpt(self.source,1,54)])
        self.assertEqual(sorted((c['line_start'],c['line_end']) for c in full.values()),self.ranges)
        self.assertNotIn(evidence_id(dict(source_id='version',line_start=1,line_end=54)),full)

    def test_duplicate_ids_deduplicate_without_merging_coordinates(self):
        ids=list(self.registry)
        result=resolve_proposals(self.draft(ids+ids),self.registry,self.visible)[0]
        self.assertEqual(len(result.knowledge.citations),5)

    def test_unknown_id_feedback_identifies_target_and_reference(self):
        draft=self.draft(); other=deepcopy(draft.proposals[0]); other.knowledge.evidence_ids=['ref_missing']
        draft.proposals.append(other)
        with self.assertRaises(EvidenceReferenceError) as caught: resolve_proposals(draft,self.registry,self.visible)
        detail=error_details(caught.exception)[0]
        self.assertEqual(detail['path'],['proposals',1,'knowledge','evidence_ids',0])
        self.assertIn('目标2',detail['message'])
        self.assertEqual(detail['available_evidence_ids'],list(self.registry))

    def test_duplicate_ids_do_not_shift_the_reported_unknown_reference_position(self):
        known=next(iter(self.registry))
        with self.assertRaises(EvidenceReferenceError) as caught:
            resolve_proposals(self.draft([known,known,'ref_missing']),self.registry,self.visible)
        self.assertEqual(error_details(caught.exception)[0]['path'][-1],2)

    def test_historical_gap_failure_reports_exact_missing_lines(self):
        learning=proposal('version',1)
        learning.knowledge.citations[0].line_end=32
        with self.assertRaises(EvidenceReferenceError) as caught:
            validate_learning(learning,self.visible,proposal_index=0)
        detail=error_details(caught.exception)[0]
        self.assertEqual(detail['path'],['proposals',0,'knowledge','citations',0])
        self.assertEqual(detail['requested_range'],[1,32])
        self.assertEqual(detail['missing_ranges'],[[10,10],[26,26]])
        self.assertEqual(detail['available_ranges'],[list(r) for r in self.ranges])
        self.assertIn('引用1',detail['message'])

    def test_even_bad_registry_entries_cannot_bypass_visibility(self):
        registry={'bad-ref':dict(source_id='version',line_start=1,line_end=32)}
        with self.assertRaises(EvidenceReferenceError) as caught:
            resolve_proposals(self.draft(['bad-ref']),registry,self.visible)
        detail=error_details(caught.exception)[0]
        self.assertEqual(detail['evidence_id'],'bad-ref')
        self.assertEqual(detail['missing_ranges'],[[10,10],[26,26]])

    def controlled_requests(self, always_invalid=False):
        seen=[]
        def reply(request):
            seen.append(json.loads(request.content))
            draft=self.draft(['ref_missing'] if always_invalid or len(seen)==1 else None)
            return httpx.Response(200,json=dict(id='controlled',created=0,model='controlled',object='chat.completion',
                choices=[dict(index=0,finish_reason='tool_calls',message=dict(role='assistant',content=None,
                    tool_calls=[dict(id='call',type='function',function=dict(name='submit_result',arguments=draft.model_dump_json()))]))]))
        with temporary_notes() as root, audit_scope(root):
            with OpenAI(api_key='test',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(reply))) as client:
                def invoke():
                    return request_structured(client,'controlled',[dict(role='user',content='Use provided evidence IDs')],OutlineDraft,
                        protocol_policy='forced_tools',validator=lambda draft:resolve_proposals(draft,self.registry,self.visible))
                if always_invalid:
                    with self.assertRaises(EvidenceReferenceError) as caught: invoke()
                    self.assertTrue(caught.exception.structured_recovery_exhausted)
                else:
                    result=invoke()
                    self.assertEqual(len(resolve_proposals(result,self.registry,self.visible)[0].knowledge.citations),5)
        return seen

    def test_actual_correction_message_has_structured_target_reference_and_allowed_ids(self):
        seen=self.controlled_requests()
        self.assertEqual(len(seen),2)
        feedback=json.loads(seen[1]['messages'][-1]['content'])
        detail=feedback['validation_errors'][0]
        self.assertEqual(detail['type'],'unknown_evidence_id')
        self.assertEqual(detail['path'],['proposals',0,'knowledge','evidence_ids',0])
        self.assertEqual(detail['available_evidence_ids'],list(self.registry))

    def test_invalid_ids_still_stop_after_two_correction_retries(self):
        self.assertEqual(len(self.controlled_requests(always_invalid=True)),3)


if __name__=='__main__': unittest.main()
