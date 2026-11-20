from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from app import ai, main
from app.usage import process_usage, safe_usage
from app.schemas import ReplaceAnswers, RunInput
from .test_standard_skills import db, standard, assessment  # noqa: F401
from .test_inference_diagnostics import fake_gateway, gateway  # noqa: F401


def test_missing_or_invalid_usage_is_never_counted_as_zero():
    p = SimpleNamespace(name='http:model', usage_calls=[safe_usage({'prompt_tokens': 10, 'completion_tokens': 5}), safe_usage(None)])
    value = process_usage(p, 4.125)
    assert value['total_tokens'] is None
    assert value['requests'] == 2
    assert value['wall_seconds'] == 4.125
    assert safe_usage({'prompt_tokens': True, 'completion_tokens': -2, 'generation_seconds': float('nan')}) == {
        'prompt_tokens': None, 'completion_tokens': None, 'generation_seconds': None}
    assert process_usage(ai.OfflineProvider(), 0.1)['total_tokens'] == 0


def test_gateway_returns_usage_from_this_request(fake_gateway):
    result = gateway.infer(gateway.Request(operation='assess', model='test-model', system='Test', payload={}), 'Bearer test-key')
    assert result['usage']['prompt_tokens'] == 2
    assert result['usage']['completion_tokens'] == 2
    assert result['usage']['total_tokens'] == 4
    assert result['usage']['generation_seconds'] >= 0


def test_multibatch_counts_are_added_and_saved_in_run(db, monkeypatch):
    s = standard(db)
    a = assessment(db, s.id)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        c['id']: {'status': 'unanswered', 'text': 'Written answer'} for c in s.controls}), db)
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '1')
    delegate = ai.HTTPProvider(url='https://test.example', model='model')
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))
    client = MagicMock()
    client.__enter__.return_value = client
    def post(*args, **kwargs):
        control = kwargs['json']['payload']['controls'][0]
        response = MagicMock()
        response.json.return_value = {'output': [{'control_id': control['id'], 'status': 'needs_review',
            'rationale': 'Need records', 'recommendation': 'Supply records', 'evidence_ids': []}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'generation_seconds': 1.5}}
        return response
    client.post.side_effect = post
    with patch('httpx.Client', return_value=client):
        result = main.run_assessment(a['id'], RunInput(), db)
    usage = result['usage']
    batches = len(s.controls)
    assert usage['requests'] == batches
    assert usage['prompt_tokens'] == batches * 100
    assert usage['completion_tokens'] == batches * 20
    assert usage['total_tokens'] == batches * 120
    assert usage['generation_seconds'] == batches * 1.5
    reloaded = main.get_assessment(a['id'], db)
    assert reloaded['evaluation']['usage'] == usage
    assert reloaded['question_usage']['total_tokens'] == 0
