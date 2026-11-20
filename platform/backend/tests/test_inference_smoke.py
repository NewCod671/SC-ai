import importlib.util
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest


@pytest.mark.parametrize('invalid', [False, True])
def test_assessment_smoke_validates_three_synthetic_answers_without_database(monkeypatch, capsys, invalid):
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'check_inference.py'
    spec = importlib.util.spec_from_file_location('inference_smoke_script', path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    for key, value in {'AI_BASE_URL': 'https://synthetic.example', 'AI_API_KEY': 'test-key',
                       'AI_MODEL': 'test-model', 'AI_ASSESS_BATCH_SIZE': '3', 'AI_ASSESS_FAILURE_MODE': 'continue'}.items():
        monkeypatch.setenv(key, value)
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {'model': 'test-model', 'model_loaded': True,
                              'supported_operations': ['questions','assess','map_question']})
        def post(self, *args, **kwargs):
            body = kwargs['json']
            calls.append(body)
            findings = [{'control_id': c['id'], 'status': 'needs_review', 'rationale': 'Need records',
                         'recommendation': 'Supply records', 'evidence_ids': []} for c in body['payload']['controls']]
            if invalid:
                findings[0]['evidence_ids'] = ['invented']
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'output': findings})
    monkeypatch.setattr(httpx, 'Client', Client)
    if invalid:
        with pytest.raises(script.CheckFailed, match='synthetic assessment produced warnings'):
            script.main(assess=True)
        assert 'synthetic findings validated' not in capsys.readouterr().out
        return
    script.main(assess=True)
    assert len(calls) == 1
    assert calls[0]['operation'] == 'assess'
    assert len(calls[0]['payload']['answers']) == 3
    assert '3 synthetic findings validated' in capsys.readouterr().out
