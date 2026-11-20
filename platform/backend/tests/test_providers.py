import os
import unittest
from unittest.mock import patch
from app import ai

CONTROLS = [{'id': 'C-1', 'requirement': 'Review access', 'department': 'IT'}]
CONTEXT = {'company': 'Example', 'business_type': 'Retail', 'departments': ['IT'],
           'it_systems': [], 'data_types': []}
PAYLOAD = {'controls': CONTROLS, 'context': CONTEXT,
           'answers': {'C-1': {'status': 'no', 'text': ''}}, 'evidence': []}

class AlternativeProvider:
    """A stand-in for any SDK/model; has no dependency on built-in adapters."""
    name = 'alternative:model-v2'

    def questions(self, controls, context):
        return [{'control_id': c['id'], 'department': c['department'], 'text': 'Who reviews access?'} for c in controls]

    def mapping(self, text, controls):
        return [{'control_id': controls[0]['id'], 'score': 0.8, 'reason': 'Access review process'}]

    def map_question(self, text, controls, context, skill, departments, answer_text=''):
        return self.mapping(text, controls)

    def map_answers(self, text, controls, questions):
        lines = [line for line in text.splitlines() if line.strip()]
        return {'mappings': [{'control_id': controls[0]['id'], 'source_texts': lines, 'answer_text': '\n'.join(lines),
                              'suggested_status': 'unanswered', 'confidence': 0.5, 'reason': 'Confirm the match.'}],
                'unmatched': []} if lines else {'mappings': [], 'unmatched': []}

    def assess(self, payload):
        return [{'control_id': c['id'], 'status': 'needs_review', 'rationale': 'Confirm evidence.',
                 'recommendation': 'Obtain dated review records.', 'evidence_ids': []} for c in payload['controls']]

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.registry = patch.dict(ai._PROVIDERS)
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_builtins_satisfy_explicit_contract(self):
        self.assertIsInstance(ai.OfflineProvider(), ai.AIProvider)
        self.assertIsInstance(ai.HTTPProvider(url='http://localhost:9000', model='any-model'), ai.AIProvider)

    def test_swap_adapters_for_all_operations(self):
        ai.register_provider('alternative', AlternativeProvider)
        for name in ('offline', 'alternative'):
            p = ai.provider(name)
            self.assertIsInstance(p, ai.AIProvider)
            self.assertEqual(p.questions(CONTROLS, CONTEXT)[0]['control_id'], 'C-1')
            self.assertEqual(p.mapping('Review access', CONTROLS)[0]['control_id'], 'C-1')
            self.assertEqual(p.assess(PAYLOAD)[0]['control_id'], 'C-1')
        with patch.dict(os.environ, {'AI_PROVIDER': 'alternative'}):
            self.assertEqual(ai.provider().name, AlternativeProvider.name)

    def test_registration_rejects_duplicates_and_invalid_factories(self):
        with self.assertRaises(ValueError): ai.register_provider('offline', AlternativeProvider)
        with self.assertRaises(ValueError): ai.register_provider('../module', AlternativeProvider)
        with self.assertRaises(TypeError): ai.register_provider('invalid', None)
        with self.assertRaises(ValueError): ai.provider('missing')
        ai.register_provider('incomplete', lambda: object())
        with self.assertRaises(TypeError): ai.provider('incomplete')

    def test_invalid_custom_outputs_are_rejected(self):
        ai.register_provider('alternative', AlternativeProvider)
        p = ai.provider('alternative')
        with patch.object(AlternativeProvider, 'questions', return_value=[{'control_id': 'wrong', 'text': 'x', 'department': 'IT'}]):
            with self.assertRaises(ValueError): p.questions(CONTROLS, CONTEXT)
        with patch.object(AlternativeProvider, 'mapping', return_value=[{'control_id': 'C-1', 'score': float('nan'), 'reason': 'x'}]):
            with self.assertRaises(ValueError): p.mapping('x', CONTROLS)
        with patch.object(AlternativeProvider, 'assess', return_value=[{'control_id': 'C-1', 'status': 'gap', 'rationale': 'x', 'recommendation': 'x', 'evidence_ids': ['fabricated']}]):
            with self.assertRaises(ValueError): p.assess(PAYLOAD)

    def test_provider_exceptions_propagate_without_fallback(self):
        ai.register_provider('alternative', AlternativeProvider)
        p = ai.provider('alternative')
        with patch.object(AlternativeProvider, 'assess', side_effect=TimeoutError('Unavailable')):
            with self.assertRaises(TimeoutError): p.assess(PAYLOAD)

    def test_http_model_configuration_is_captured_at_construction(self):
        with patch.dict(os.environ, {'AI_MODEL': 'first', 'AI_API_KEY': 'first-key'}):
            p = ai.HTTPProvider(url='http://localhost:9000')
        with patch.dict(os.environ, {'AI_MODEL': 'second', 'AI_API_KEY': 'second-key'}):
            self.assertEqual(p.model, 'first')
            self.assertEqual(p.api_key, 'first-key')
        explicit = ai.HTTPProvider(url='https://inference.example', model='another-model', api_key='key', timeout=30)
        self.assertEqual(explicit.name, 'http:another-model')
        self.assertEqual(explicit.timeout, 30)

if __name__ == '__main__':
    unittest.main()
