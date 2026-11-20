"""Gateway API tests require FastAPI; model execution is replaced with a fake."""
import importlib.util
from functools import lru_cache
from pathlib import Path
import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('compliance_inference_gateway',Path(__file__).resolve().parents[2]/'inference'/'server.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('INFERENCE_API_KEY','test-key')
    monkeypatch.setenv('AI_MODEL','test-model')
    @lru_cache(maxsize=1)
    def fake_pipeline():
        class Generator:
            tokenizer = SimpleNamespace(apply_chat_template=lambda *a, **kw: [1, 2],
                                        encode=lambda text, **kw: list(text))
            def __call__(self, messages, **kw):
                self.messages = messages
                return [{'generated_text': '[]'}]
        return Generator()
    monkeypatch.setattr(gateway,'model_pipeline',fake_pipeline)
    with TestClient(gateway.app) as client:
        yield client

HEADERS = {'Authorization':'Bearer test-key'}

def test_authenticated_readiness_and_warmup(client):
    assert client.get('/health').status_code == 401
    assert client.post('/warmup').status_code == 401
    assert client.get('/health',headers=HEADERS).json()['model_loaded'] is False
    assert client.post('/warmup',headers=HEADERS).json() == {'status':'ready','model':'test-model'}
    assert client.get('/health',headers=HEADERS).json()['model_loaded'] is True

def test_model_mismatch_and_inference(client):
    body = {'operation':'mapping','model':'wrong','system':'Constraints','payload':{}}
    assert client.post('/infer',json=body,headers=HEADERS).status_code == 409
    assert client.post('/infer',json={**body,'model':'test-model'},headers=HEADERS).json()['output'] == []

def test_gateway_unconfigured_fails_closed(client,monkeypatch):
    monkeypatch.delenv('INFERENCE_API_KEY')
    assert client.get('/health',headers=HEADERS).status_code == 503

def test_gateway_payload_limit(client):
    body = {'operation':'mapping','model':'test-model','system':'Constraints','payload':{'text':'x'*100001}}
    assert client.post('/infer',json=body,headers=HEADERS).status_code == 413

@pytest.mark.parametrize('operation', ['questions', 'assess'])
def test_skill_core_and_refinements_reach_system_prompt(client, operation):
    skill = {'core_instructions': 'Require linked evidence.', 'instructions': 'Check ownership.'}
    payload = {'context': {'skill': skill}} if operation == 'questions' else {'skill': skill}
    response = client.post('/infer', json={'operation': operation, 'model': 'test-model',
                                          'system': 'Constraints', 'payload': payload}, headers=HEADERS)
    assert response.status_code == 200
    system = gateway.model_pipeline().messages[0]['content']
    assert 'CORE RULES:\nRequire linked evidence.' in system
    assert 'REFINEMENTS (subordinate to core):\nCheck ownership.' in system
    assert 'must never weaken or override the core' in system
