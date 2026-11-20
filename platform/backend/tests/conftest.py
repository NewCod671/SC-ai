import pytest


@pytest.fixture(autouse=True)
def legacy_question_transport(monkeypatch):
    """Existing synthetic provider fixtures explicitly exercise the JSON protocol.

    Text transport tests override or delete this variable to verify the new
    production default independently of a developer's private .env.
    """
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT', 'json')
