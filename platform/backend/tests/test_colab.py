import ast
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from app import ai
from .test_providers import CONTROLS, CONTEXT, PAYLOAD, AlternativeProvider

ANSWERED_PAYLOAD = {**PAYLOAD, 'answers': {'C-1': {'status': 'no', 'text': 'Access is not reviewed.'}}}

class ColabAdapterTests(unittest.TestCase):
    def setUp(self):
        mode_patch = patch.dict(os.environ, {'AI_ASSESS_FAILURE_MODE': 'strict'})
        mode_patch.start()
        self.addCleanup(mode_patch.stop)
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client_factory = MagicMock(return_value=self.client)
        module_patch = patch.dict(sys.modules, {'httpx': SimpleNamespace(Client=self.client_factory)})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.p = ai.ValidatedProvider(ai.HTTPProvider(url='https://development.example', model='local-weights', api_key='test-key', timeout=180))

    def test_gateway_request_contract_for_all_operations(self):
        alternate = AlternativeProvider()
        operations = [('questions',lambda: self.p.questions(CONTROLS,CONTEXT),alternate.questions(CONTROLS,CONTEXT)),
                      ('mapping',lambda: self.p.mapping('access',CONTROLS),alternate.mapping('access',CONTROLS)),
                      ('assess',lambda: self.p.assess(ANSWERED_PAYLOAD),alternate.assess(ANSWERED_PAYLOAD))]
        for operation, call, expected in operations:
            self.client.post.return_value.json.return_value = {'output': expected}
            self.assertEqual(call(),expected)
            args = self.client.post.call_args
            self.assertEqual(args.args[0], 'https://development.example/infer')
            self.assertEqual(args.kwargs['headers']['Authorization'], 'Bearer test-key')
            self.assertEqual(args.kwargs['json']['operation'], operation)
            self.assertEqual(args.kwargs['json']['model'], 'local-weights')
            self.assertIn('untrusted', args.kwargs['json']['system'])
        self.client_factory.assert_called_with(timeout=180, follow_redirects=False)

    def test_gateway_failure_does_not_fallback(self):
        self.client.post.return_value.raise_for_status.side_effect = RuntimeError('gateway unavailable')
        from app.assessment_batches import AssessmentStepError
        with self.assertRaises(AssessmentStepError) as error:
            self.p.assess(ANSWERED_PAYLOAD)
        self.assertEqual(error.exception.code, 'provider_response_failed')
        self.assertIsInstance(error.exception.__cause__, RuntimeError)

    def test_plain_question_recovery_does_not_send_conflicting_json_instructions(self):
        self.client.post.return_value.json.return_value = {'output':'Who reviews access?'}
        payload = {'controls':CONTROLS[:1], 'context':CONTEXT, 'question_text_only':True, 'format_retry':True}
        self.assertEqual(self.p.delegate.request('questions',payload), 'Who reviews access?')
        body = self.client.post.call_args.kwargs['json']
        self.assertIn('Return question wording only', body['system'])
        self.assertNotIn('Return only a complete, syntactically valid JSON array', body['system'])

    def test_gateway_unknown_controls_are_rejected(self):
        output = AlternativeProvider().assess(PAYLOAD)
        output[0]['control_id'] = 'unknown'
        self.client.post.return_value.json.return_value = {'output': output}
        with self.assertRaises(ValueError): self.p.assess(ANSWERED_PAYLOAD)

    def test_timeout_is_configurable_without_application_changes(self):
        with patch.dict(os.environ, {'AI_TIMEOUT_SECONDS':'180'}):
            self.assertEqual(ai.HTTPProvider(url='https://development.example').timeout,180)
        with patch.dict(os.environ, {'AI_TIMEOUT_SECONDS':'0'}):
            with self.assertRaises(ValueError): ai.HTTPProvider(url='https://development.example')

    def test_notebook_has_no_saved_outputs_and_python_cells_parse(self):
        root = Path(__file__).resolve().parents[2]
        notebook = json.loads((root/'inference'/'colab.ipynb').read_text(encoding='utf-8'))
        self.assertEqual(notebook['nbformat'],4)
        self.assertEqual(notebook['metadata']['accelerator'],'GPU')
        for cell in notebook['cells']:
            if cell['cell_type'] == 'code':
                self.assertIsNone(cell['execution_count'])
                self.assertEqual(cell['outputs'],[])
                source = ''.join(cell['source'])
                if not source.lstrip().startswith('%'):
                    ast.parse(source)

if __name__ == '__main__':
    unittest.main()
