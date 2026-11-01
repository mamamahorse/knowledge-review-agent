"""Real PDF coordinates/rendering, controlled vision and Agent responses; no paid API."""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import pymupdf

from answers import NoteAnswer, validate_answer
from chunking import block_chunk
from document_model import citation_metadata
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from pdf_parser import parse_pdf, render_asset, MAX_RENDER_PIXELS
from pdf_regions import checked_box, select_region, resolve_asset, region_asset
from pdf_vision import VisualReading, SuggestedRegion, inspect_visual, VisionBudget, vision_request
from sources import source_from_document
from test_pdf_documents import text_pdf
from test_support import temporary_notes


class PDFNavigationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict('os.environ', {'PDF_PARSER': 'pymupdf', 'PDF_VLM_ENABLED': '0'})
        environment.start()
        self.addCleanup(environment.stop)
        dotenv = patch('pdf_parser.load_dotenv')
        dotenv.start()
        self.addCleanup(dotenv.stop)
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.root / 'test.pdf'
        self.source = self.make_source(text_pdf(table=True))

    def make_source(self, data):
        self.path.write_bytes(data)
        return source_from_document(self.path, parse_pdf(data, raw_file=self.path.name,
                                    config={'engine': 'pymupdf', 'vlm': False}))

    def answer(self, receipt, asset_id=None):
        return NoteAnswer.model_validate(dict(coverage='sufficient', coverage_note='', blocks=[dict(
            kind='grounded', text='Observed detail.', citations=[dict(source_id=self.source.source_id,
                line_start=receipt['line_start'], line_end=receipt['line_end'],
                asset_id=asset_id or receipt['asset_id'])])]))

    def test_crop_identity_roundtrips_without_mutating_source_and_rejects_other_version(self):
        before = self.source.document.to_dict()
        crop = select_region(self.source, 'page-1', [.1, .2, .4, .5])
        self.assertEqual(crop.bbox, (42., 120., 168., 300.))
        self.assertEqual(resolve_asset(self.source, crop.asset_id), crop)
        self.assertEqual(before, self.source.document.to_dict())
        restored = source_from_document(self.path, self.source.document.from_dict(json.loads(json.dumps(before))))
        self.assertEqual(resolve_asset(restored, crop.asset_id), crop)
        with self.assertRaises(ValueError):
            resolve_asset(replace(self.source, source_id='different-version'), crop.asset_id)
        self.assertEqual(select_region(self.source, crop.asset_id, [0, 0, .5, .5]).bbox,
                         (42., 120., 105., 210.))

    def test_malformed_and_outside_regions_are_rejected_before_model_call(self):
        bad = [[0, 0, 1, float('nan')], [False, 0, 1, 1], [-.1, 0, 1, 1],
               [0, 0, 1.1, 1], [1, 0, 0, 1], [0, 0, 0, 1], [0, 1]]
        with patch('pdf_vision.vision_request') as request:
            for box in bad:
                with self.subTest(box=box), self.assertRaises(ValueError):
                    inspect_visual(self.source, 'page-1', 'Read.', bbox=box)
            forged = region_asset(self.source, 1, (-50, 0, 100, 100))
            with self.assertRaises(ValueError):
                inspect_visual(self.source, forged.asset_id, 'Read.')
            request.assert_not_called()

    def test_rotated_and_cropped_pages_use_display_coordinates(self):
        for angle in (0, 90, 180, 270):
            with self.subTest(angle=angle), pymupdf.open() as doc:
                page = doc.new_page(width=400, height=600)
                for rect, color in [((0,0,200,300),(1,0,0)), ((200,0,400,300),(0,1,0)),
                                    ((0,300,200,600),(0,0,1)), ((200,300,400,600),(1,1,0))]:
                    page.draw_rect(rect, color=color, fill=color)
                page.set_cropbox(pymupdf.Rect(20, 30, 380, 570))
                page.set_rotation(angle)
                source = self.make_source(doc.tobytes())
                full = pymupdf.Pixmap(render_asset(source, 'page-1'))
                crop = select_region(source, 'page-1', [.1, .1, .4, .4])
                zoom = pymupdf.Pixmap(render_asset(source, crop.asset_id, detail=True))
                self.assertEqual(zoom.pixel(zoom.width // 2, zoom.height // 2),
                                 full.pixel(full.width // 4, full.height // 4))
                self.assertLessEqual(zoom.width * zoom.height, MAX_RENDER_PIXELS)
                self.assertGreater(zoom.width, full.width * .3)

    def test_extremely_thin_crop_still_has_bounded_raster(self):
        crop = select_region(self.source, 'page-1', [0, 0, .00001, 1])
        raster = pymupdf.Pixmap(render_asset(self.source, crop.asset_id, detail=True))
        self.assertLessEqual(raster.width * raster.height, MAX_RENDER_PIXELS)
        self.assertLessEqual(max(raster.width, raster.height), 8192)

    def test_new_suggested_region_is_not_citable_until_successfully_read(self):
        reading = VisualReading(observation='Small print needs zoom.', limitations='Not readable.',
            suggested_regions=[SuggestedRegion(label='Footnote', bbox=[.1,.3,.7,.6], reason='Read the condition.')],
            next_read='Read the footnote before concluding.')
        with patch('pdf_vision.vision_request', return_value=reading):
            receipt, anchor = inspect_visual(self.source, 'page-1', 'Find conditions.')
        crop_id = receipt['suggested_regions'][0]['asset_id']
        visible = [block_chunk(self.source, anchor)]
        with self.assertRaises(RuntimeError):
            validate_answer(self.answer(receipt, crop_id), [self.source], chunks=visible, visual_evidence=[receipt])
        with patch('pdf_vision.vision_request', return_value=VisualReading(observation='Condition visible.', limitations='')) as call:
            cropped, _ = inspect_visual(self.source, crop_id, 'Read the condition.')
        self.assertEqual(len(call.call_args.args[0]), 1)
        validate_answer(self.answer(cropped), [self.source], chunks=visible, visual_evidence=[cropped])
        metadata = citation_metadata(self.source, self.answer(cropped).blocks[0].citations[0])
        self.assertEqual(metadata['locations'][0]['bbox'], resolve_asset(self.source, crop_id).bbox)
        with self.assertRaises(RuntimeError):
            validate_answer(self.answer(cropped, 'page-1'), [self.source], chunks=visible, visual_evidence=[cropped])

    def test_page_context_is_optional_and_only_requested_images_get_receipts(self):
        crop = select_region(self.source, 'page-1', [.1,.1,.5,.5])
        with patch('pdf_vision.vision_request', return_value=VisualReading(observation='Visible.', limitations='')) as call:
            receipt, anchor = inspect_visual(self.source, crop.asset_id, 'Read.', include_page=True)
        self.assertEqual(len(call.call_args.args[0]), 2)
        self.assertEqual([v['asset_id'] for v in receipt['viewed_assets']], [crop.asset_id, 'page-1'])
        self.assertEqual(receipt['navigation']['next_page'], 'page-2')
        validate_answer(self.answer(receipt, 'page-1'), [self.source], chunks=[block_chunk(self.source, anchor)], visual_evidence=[receipt])
        with self.assertRaises(RuntimeError):
            validate_answer(self.answer(receipt, 'page-2'), [self.source], visual_evidence=[receipt])

    def test_bad_model_region_coordinates_fail_schema_validation(self):
        for box in ([0,0,2,1], [0,0,0,1], [True,0,1,1], [0,0,1,float('inf')]):
            with self.subTest(box=box), self.assertRaises(ValueError):
                SuggestedRegion(label='bad', bbox=box, reason='test')

    def test_raw_file_change_blocks_rendering(self):
        crop = select_region(self.source, 'page-1', [.1,.1,.5,.5])
        self.path.write_bytes(text_pdf('Changed.'))
        with self.assertRaises(ValueError):
            render_asset(self.source, crop.asset_id)

    def test_web_asset_route_reopens_crop_and_rejects_stale_version(self):
        from test_pdf_documents import PDFStorageAndWebTests
        fixture = PDFStorageAndWebTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        identity, document = fixture.upload()
        # Use the exact catalog snapshot, including its version identity.
        from web_app import _source_for, DocumentCatalog
        catalog = DocumentCatalog(fixture.root / 'uploads', fixture.client.app.state.library_store)
        source = _source_for(catalog.get(identity))
        crop = select_region(source, 'page-1', [.1,.1,.4,.4])
        url = f'/api/documents/{identity}/assets/{crop.asset_id}?source_id={source.source_id}'
        response = fixture.client.get(url)
        self.assertEqual(response.status_code, 200, response.text[:200] if response.status_code != 200 else '')
        self.assertTrue(response.content.startswith(b'\x89PNG'))
        self.assertEqual(fixture.client.get(url.replace(source.source_id, 'old-version')).status_code, 409)
        bad = region_asset(source, 1, (-10, 0, 100, 100))
        self.assertEqual(fixture.client.get(url.replace(crop.asset_id, bad.asset_id)).status_code, 404)

    def test_agent_can_read_crop_directly_then_other_page_and_persist_evidence(self):
        from agent_loop import run_agent
        from test_agent_loop import AgentLoopTests
        from structured_output import FUNCTION_OUTPUT_PROVIDERS
        FUNCTION_OUTPUT_PROVIDERS.clear()
        self.addCleanup(FUNCTION_OUTPUT_PROVIDERS.clear)
        tools = KnowledgeTools([self.source], select_embedding_config())
        requests = []
        crop_id = select_region(self.source, 'page-1', [.1,.1,.5,.5]).asset_id
        def respond(request):
            body = json.loads(request.content); requests.append(body)
            if len(requests) <= 2:
                args = dict(source_id=self.source.source_id, asset_id='page-1' if len(requests)==1 else 'page-2', question='Read conditions.')
                if len(requests)==1:
                    args['bbox'] = [.1,.1,.5,.5]
                else:
                    self.assertIn(crop_id, str(body['messages']))
                return AgentLoopTests.completion(dict(role='assistant', content=None,
                    tool_calls=[AgentLoopTests.call(str(len(requests)), 'inspect_visual', args)]), 'tool_calls')
            if len(requests)==3:
                return AgentLoopTests.completion(dict(role='assistant', content='Enough evidence.'))
            answer = self.answer(dict(asset_id=crop_id, line_start=1, line_end=1))
            return AgentLoopTests.completion(dict(role='assistant', content=answer.model_dump_json()))
        def client(**kwargs):
            return OpenAI(api_key='test', http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        with patch('agent_loop.OpenAI', side_effect=client), patch('model_client.OpenAI', side_effect=client), \
             patch('agent_loop.model_settings', return_value=('test', {})), patch('model_client.model_settings', return_value=('test', {})), \
             patch('pdf_vision.enabled', return_value=True), patch('pdf_vision.vision_request',
                return_value=VisualReading(observation='Controlled observation.', limitations='Synthetic.')) as vision:
            result = run_agent([self.source], 'Explain conditions.', tools, strict=True)
        self.assertEqual(vision.call_count, 2)
        self.assertEqual([len(c.args[0]) for c in vision.call_args_list], [1,1])
        observations = result.context_snapshot['visual_observations']
        self.assertEqual([v['asset_id'] for v in observations], [crop_id, 'page-2'])
        self.assertEqual(result.answer.blocks[0].citations[0].asset_id, crop_id)


class VisionResourceTests(unittest.TestCase):
    def test_image_count_configurable_and_still_limited_by_full_budget(self):
        with patch.dict('os.environ', {'VLM_MAX_IMAGES':'3', 'VLM_IMAGE_TOKEN_ALLOWANCE':'9000',
            'LLM_CONTEXT_TOKENS':'131072', 'LLM_OUTPUT_TOKENS':'12288', 'LLM_SAFETY_TOKENS':'2048'}):
            budget = VisionBudget.from_env()
        image = dict(type='image_url', image_url=dict(url='data:image/png;base64,AA=='))
        messages = [dict(role='user',content=[image]*3)]
        self.assertGreater(budget.usage(messages), 27000)
        budget.check(messages)
        with self.assertRaises(ValueError):
            budget.check([dict(role='user',content=[image]*4)])
        with self.assertRaises(ValueError):
            replace(budget, context_tokens=20000).check(messages)

    def test_three_images_can_reach_structured_transport_without_live_request(self):
        with patch('pdf_vision.vision_settings', return_value=('test',dict(api_key='test'))), \
             patch.dict('os.environ', {'VLM_MAX_IMAGES':'3'}), \
             patch('pdf_vision.request_structured', return_value=VisualReading(observation='Seen.',limitations='')) as request:
            vision_request([b'image']*3, 'Read.', VisualReading)
        self.assertEqual(len(request.call_args.args[2][1]['content']), 4)


if __name__ == '__main__':
    unittest.main()
