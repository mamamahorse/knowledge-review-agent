"""Vision routing and explicit unsupported-image responses; no live API calls."""

import unittest
from unittest.mock import patch

import httpx
from openai import APIStatusError

from pdf_vision import (VISION_CAPABILITIES, VisionUnsupported, VisualReading, capability_key,
                        enabled, image_input_is_unsupported, vision_request, vision_settings, vision_status)


def service_error(status, message, code=None):
    body = {'error': {'message': message, 'code': code}}
    response = httpx.Response(status, json=body, request=httpx.Request('POST', 'https://example.invalid/v1/chat/completions'))
    return APIStatusError(message, response=response, body=body)


class VisionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'LLM_MODEL': 'chat-model', 'LLM_API_KEY': 'test-chat-key',
                                           'LLM_BASE_URL': 'https://example.invalid/v1'}, clear=True)
        self.dotenv = patch('pdf_vision.load_dotenv')
        self.env.start(); self.dotenv.start(); VISION_CAPABILITIES.clear()

    def tearDown(self):
        VISION_CAPABILITIES.clear(); self.dotenv.stop(); self.env.stop()

    def test_unset_vision_configuration_uses_all_chat_settings(self):
        model, options = vision_settings()
        self.assertEqual(model, 'chat-model')
        self.assertEqual(options['api_key'], 'test-chat-key')
        self.assertEqual(options['base_url'], 'https://example.invalid/v1')
        self.assertTrue(enabled())
        self.assertEqual(vision_status()['model_source'], 'chat_default')
        self.assertEqual(vision_status()['state'], 'unknown')

    def test_vision_fields_override_independently_and_empty_values_fall_back(self):
        with patch.dict('os.environ', {'VLM_MODEL': 'vision-model', 'VLM_API_KEY': '  ', 'VLM_BASE_URL': ''}):
            model, options = vision_settings()
            self.assertEqual(model, 'vision-model')
            self.assertEqual(options['api_key'], 'test-chat-key')
            self.assertEqual(options['base_url'], 'https://example.invalid/v1')
        with patch.dict('os.environ', {'VLM_MODEL': 'vision-model', 'VLM_API_KEY': 'vision-key', 'VLM_BASE_URL': 'https://vision.invalid/v1'}):
            model, options = vision_settings()
            self.assertEqual((model, options['api_key'], options['base_url']), ('vision-model', 'vision-key', 'https://vision.invalid/v1'))

    def test_explicit_unsupported_is_cached_without_repeated_requests(self):
        error = service_error(400, 'This model does not support image input.')
        with patch('pdf_vision.request_structured', side_effect=error) as request:
            for _ in range(2):
                with self.assertRaises(VisionUnsupported):
                    vision_request([b'synthetic-image'], 'Read the number.', VisualReading)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(enabled())
        self.assertEqual(vision_status()['state'], 'unsupported')
        self.assertNotIn('test-chat-key', str(vision_status()))
        with patch.dict('os.environ', {'VLM_MODEL': 'another-model'}):
            self.assertTrue(enabled())
            self.assertEqual(vision_status()['state'], 'unknown')

    def test_api_failures_and_invalid_images_do_not_poison_capability(self):
        errors = [service_error(400, 'Invalid image data.'),
                  service_error(400, 'Unsupported image format.'),
                  service_error(401, 'Unauthorized'), service_error(429, 'Rate limit'),
                  service_error(500, 'Unsupported images due to internal error'),
                  service_error(400, 'response_format is unsupported'),
                  service_error(400, 'response_format is unsupported with vision inputs')]
        for error in errors:
            with self.subTest(status=error.status_code, message=error.message):
                with patch('pdf_vision.request_structured', side_effect=error):
                    with self.assertRaises(APIStatusError):
                        vision_request([b'synthetic-image'], 'Read.', VisualReading)
                self.assertTrue(enabled())
                self.assertEqual(vision_status()['state'], 'unknown')

    def test_success_is_recorded_for_the_exact_connection(self):
        reading = VisualReading(observation='17 is visible.', limitations='')
        with patch('pdf_vision.request_structured', return_value=reading) as request:
            self.assertEqual(vision_request([b'image'], 'Read.', VisualReading), reading)
        self.assertEqual(request.call_args.args[1], 'chat-model')
        self.assertEqual(vision_status()['state'], 'supported')
        with patch.dict('os.environ', {'LLM_API_KEY': 'rotated-key'}):
            self.assertEqual(vision_status()['state'], 'unknown')

    def test_explicit_service_code_is_recognized_but_auth_is_not(self):
        self.assertTrue(image_input_is_unsupported(service_error(422, 'Rejected', 'image_input_not_supported')))
        self.assertFalse(image_input_is_unsupported(service_error(401, 'Unsupported image input', 'image_input_not_supported')))

    def test_import_stops_after_first_explicit_unsupported_response(self):
        from pdf_parser import parse_pdf
        from test_pdf_documents import scan_pdf
        with patch('pdf_vision.transcribe_page', side_effect=VisionUnsupported('No image support')) as request:
            doc = parse_pdf(scan_pdf(4), raw_file='scan.pdf', config={'engine': 'pymupdf', 'vlm': True})
        self.assertEqual(request.call_count, 1)
        self.assertEqual({i['page'] for i in doc.issues if i['code'] == 'vlm_unsupported'}, {1, 2, 3, 4})
        self.assertFalse(any(i['code'] == 'vlm_page_budget_exhausted' for i in doc.issues))
        self.assertEqual(doc.attempts[0]['status'], 'unsupported')
        self.assertEqual(doc.status, 'partial')


if __name__ == '__main__':
    unittest.main()
