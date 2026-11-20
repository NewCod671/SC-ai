import json
import os
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from app import ai
from .test_providers import CONTROLS, CONTEXT, PAYLOAD, AlternativeProvider

def envelope(output):
    return {'done': True, 'done_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps({'output': output})}}

class OllamaTests(unittest.TestCase):
    def setUp(self):
        mode_patch = patch.dict(os.environ, {'AI_ASSESS_FAILURE_MODE': 'strict'})
        mode_patch.start()
        self.addCleanup(mode_patch.stop)
        self.p = ai.OllamaProvider(model='local-test', api_key='')
        self.opener_patch = patch('app.ai.build_opener')
        self.opener = self.opener_patch.start().return_value
        self.addCleanup(self.opener_patch.stop)

    def respond(self, value):
        self.opener.open.return_value = BytesIO(json.dumps(value).encode())

    def test_native_chat_request_and_question_validation(self):
        output = AlternativeProvider().questions(CONTROLS, CONTEXT)
        self.respond(envelope(output))
        self.assertEqual(self.p.questions(CONTROLS, CONTEXT), output)
        req = self.opener.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, 'http://127.0.0.1:11434/api/chat')
        self.assertEqual(req.method, 'POST')
        self.assertEqual(body['model'], 'local-test')
        self.assertFalse(body['stream'])
        self.assertEqual(body['options']['temperature'], 0)
        self.assertEqual(body['format']['required'], ['output'])
        self.assertEqual([m['role'] for m in body['messages']], ['system','user'])
        self.assertIn('Example', body['messages'][1]['content'])
        self.assertIsNone(req.get_header('Authorization'))
        self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 120)
        handlers = ai.build_opener.call_args.args
        self.assertTrue(any(isinstance(h, ai.NoInferenceRedirects) for h in handlers))
        self.assertTrue(any(isinstance(h, ai.ProxyHandler) and h.proxies == {} for h in handlers))

    def test_plain_question_recovery_omits_json_schema_and_returns_only_model_wording(self):
        self.respond({'done':True, 'message':{'content':'Who reviews user access?'}})
        result = self.p.request('questions', {'question_text_only':True, 'controls':CONTROLS[:1], 'context':CONTEXT})
        self.assertEqual(result, 'Who reviews user access?')
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertNotIn('format', body)
        self.assertEqual(body['options']['num_predict'],512)
        self.assertNotIn('Return only JSON matching', body['messages'][0]['content'])

    def test_all_operations_use_shared_validation(self):
        p = ai.ValidatedProvider(self.p)
        alternate = AlternativeProvider()
        payload = {**PAYLOAD, 'answers': {'C-1': {'status': 'no', 'text': 'Access is not reviewed.'}}}
        for call, output in [(lambda: p.mapping('access', CONTROLS), alternate.mapping('access', CONTROLS)),
                             (lambda: p.assess(payload), alternate.assess(payload))]:
            self.respond(envelope(output))
            self.assertEqual(call(), output)
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertIn('Finding', body['format']['$defs'])
        self.respond(envelope([dict(alternate.assess(PAYLOAD)[0], evidence_ids=['fabricated'])]))
        with self.assertRaises(ValueError): p.assess(payload)
        self.respond(envelope([{'control_id':'unknown','department':'IT','text':'Question'}]))
        with self.assertRaises(ValueError): p.questions(CONTROLS, CONTEXT)

    def test_scoped_skill_is_sent_for_questions_and_assessment(self):
        skill = {'core_instructions': 'Require linked evidence.', 'instructions': 'Check evidence dates.'}
        for operation, payload in [('questions', {'controls': CONTROLS, 'context': {**CONTEXT, 'skill': skill}}),
                                   ('assess', {**PAYLOAD, 'skill': skill})]:
            self.respond(envelope([]))
            self.p.request(operation, payload)
            system = json.loads(self.opener.open.call_args.args[0].data)['messages'][0]['content']
            self.assertIn('CORE RULES:\nRequire linked evidence.', system)
            self.assertIn('REFINEMENTS (subordinate to core):\nCheck evidence dates.', system)

    def test_context_remains_available_but_prompt_requires_question_only_text(self):
        context = {**CONTEXT, 'it_systems': ['AWS production'], 'data_types': ['Employee records'],
                   'existing_policies': ['Access policy v1.0']}
        self.respond(envelope(AlternativeProvider().questions(CONTROLS, context)))
        self.p.questions(CONTROLS, context)
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertIn('Use company systems, data types and policies silently as background', body['messages'][0]['content'])
        for value in ('AWS production', 'Employee records', 'Access policy v1.0'):
            self.assertIn(value, body['messages'][1]['content'])

    def test_malformed_or_incomplete_generations_fail(self):
        for response in [{'error':'model not found'}, {'done':False},
                         dict(envelope([]), done_reason='length'),
                         {'done':True,'message':{'content':'not JSON'}},
                         {'done':True,'message':{'content':'[]'}}]:
            with self.subTest(response=response):
                self.respond(response)
                with self.assertRaises(ValueError): self.p.mapping('access', CONTROLS)

    def test_connection_timeout_and_http_errors_propagate(self):
        for error in [URLError('refused'), TimeoutError(), HTTPError(self.p.url,404,'model missing',{},None)]:
            self.opener.open.side_effect = error
            with self.assertRaises(type(error)): self.p.mapping('access', CONTROLS)

    def test_env_selection_and_explicit_configuration(self):
        with patch.dict(os.environ, {'AI_PROVIDER':'ollama','OLLAMA_MODEL':'another-local-model', 'OLLAMA_BASE_URL':'http://localhost:11434', 'OLLAMA_TIMEOUT_SECONDS':'45'}):
            p = ai.provider()
            self.assertEqual(p.name, 'ollama:another-local-model')
            self.assertIsInstance(p, ai.AIProvider)
        self.p = ai.OllamaProvider(url='https://gpu.example',model='selected',api_key='secret',timeout=30)
        self.respond(envelope([])); self.p.mapping('x',CONTROLS)
        self.assertEqual(self.opener.open.call_args.args[0].get_header('Authorization'), 'Bearer secret')

    def test_invalid_configuration_and_payload_size(self):
        for url in ['http://untrusted.example', 'https://user:password@gpu.example', 'https://gpu.example?key=x']:
            with self.assertRaises(ValueError): ai.OllamaProvider(url=url)
        with self.assertRaises(ValueError): ai.OllamaProvider(model='')
        with self.assertRaises(ValueError): ai.OllamaProvider(timeout=0)
        with self.assertRaises(ValueError): self.p.mapping('x'*100001, CONTROLS)
        self.opener.open.assert_not_called()

if __name__ == '__main__':
    unittest.main()
