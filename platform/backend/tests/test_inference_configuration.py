import pytest
from fastapi import HTTPException
from app import ai, main


def test_missing_http_configuration_is_actionable_and_has_no_secret(monkeypatch):
    monkeypatch.setenv('AI_PROVIDER','http')
    monkeypatch.setenv('AI_BASE_URL','')
    monkeypatch.setenv('AI_API_KEY','')
    with pytest.raises(HTTPException) as error:
        main.ai_call(ai.provider)
    assert error.value.status_code == 503
    assert 'AI_BASE_URL, AI_API_KEY' in error.value.detail
    assert 'restart the backend' in error.value.detail


def test_missing_key_error_does_not_disclose_endpoint(monkeypatch):
    monkeypatch.setenv('AI_BASE_URL','https://private-company.example')
    monkeypatch.setenv('AI_API_KEY','')
    with pytest.raises(HTTPException) as error:
        main.ai_call(ai.HTTPProvider)
    assert 'AI configuration missing: AI_API_KEY.' in error.value.detail
    assert 'private-company' not in error.value.detail


def test_explicit_adapter_options_still_support_custom_unauthed_transport():
    p=ai.HTTPProvider(url='http://127.0.0.1:9000',api_key='',model='test')
    assert p.api_key == ''
