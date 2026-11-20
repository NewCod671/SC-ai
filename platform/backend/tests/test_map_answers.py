from .test_api import client, assessment


def test_codex_note_review_repairs_unverified_topics_in_the_same_request(client, monkeypatch):
    from app import codex_provider

    value = assessment(client)
    calls = []
    note = 'IT manager approves access.'

    class Provider:
        name = 'fixture-codex'

        def assess_control(self, case):
            calls.append(case)
            retry = len(calls) > 1
            return {'coverage_topics': [
                {'topic': 'Approval owner', 'status': 'covered',
                 'quote': note if retry else 'Paraphrased: IT approves access',
                 'source_control_id': 'SC-A', 'missing_detail': ''},
                *([] if retry else [{'topic': 'Timing', 'status': 'missing',
                                    'missing_detail': 'Approval time is not recorded.'}]),
            ]}

    monkeypatch.setattr(codex_provider, 'health', lambda: {'available': True})
    monkeypatch.setattr(codex_provider, 'CodexProvider', Provider)
    response = client.post('/api/assessments/' + value['id'] + '/review-note', json={
        'expected_version': value['current_version'], 'control_id': 'SC-A',
        'text': note, 'processing_mode': 'codex', 'authorize_external': True,
    })
    assert response.status_code == 200, response.text
    assert calls[1]['context']['coverage_topics_to_recheck'] == ['Approval owner']
    assert len(calls) == 2
    assert [topic['status'] for topic in response.json()['question_coverage']] == ['covered', 'missing']


def test_note_review_uses_unsaved_clause_note_and_never_changes_answers(client, monkeypatch):
    from app import ai
    value = assessment(client)
    route = '/api/assessments/' + value['id']
    captured = []
    class Provider:
        name = 'fixture-ai'
        delegate = object()
        def assess(self, payload):
            captured.append(payload)
            return [{'control_id':'SC-A','rationale':'Quarterly review is recorded.',
                     'audit':{'requirement_gap':'Reviewer and last review date are missing.'},
                     'recommendation':'1. Who completed the last access review?\n2. When was it completed?'}]
    monkeypatch.setattr(ai, 'provider', lambda: Provider())
    body = {'expected_version':value['current_version'], 'control_id':'SC-A',
            'text':'Access review quarterly; sampled admin list.', 'authorize_external':False}
    assert client.post(route + '/review-note', json=body).status_code == 422
    assert not captured
    body['authorize_external'] = True
    response = client.post(route + '/review-note', json=body)
    assert response.status_code == 200, response.text
    assert 'Who completed' in response.json()['follow_up']
    assert [c['id'] for c in captured[0]['controls']] == ['SC-A']
    assert captured[0]['answers']['SC-A']['text'] == body['text']
    assert all(q['control_id'] == 'SC-A' or 'SC-A' in q.get('control_ids', []) for q in captured[0]['questions'])
    assert client.get(route).json()['answers'] == value['answers']
    assert client.get(route).json()['current_version'] == value['current_version']
    assert len(client.get(route + '/revisions').json()) == 1
    assert client.get(route + '/runs').json() == []
    assert client.post(route + '/review-note', json={**body,'control_id':'unknown'}).status_code == 422


def test_connection_checks_real_gateway_status_without_generating(client, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from app import ai
    value = assessment(client)
    provider = ai.HTTPProvider(url='https://inference.example', api_key='fixture-key', model='fixture-model')
    monkeypatch.setattr(ai, 'provider', lambda: SimpleNamespace(delegate=provider))
    calls = []
    status = [200]
    class Gateway:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers):
            calls.append((url, headers))
            return httpx.Response(status[0], json={'status':'ok','gateway_version':'fixture','model':'fixture-model'})
    monkeypatch.setattr(httpx, 'Client', Gateway)
    route = '/api/assessments/' + value['id'] + '/connection'
    response = client.post(route)
    assert response.status_code == 200
    assert response.json()['connected'] is True
    assert calls == [('https://inference.example/health', {'Authorization':'Bearer fixture-key'})]
    assert 'fixture-key' not in response.text
    status[0] = 404
    response = client.post(route)
    assert response.status_code == 502
    assert '404' in response.json()['detail']


def test_add_question_is_scoped_to_one_control_and_keeps_standard_and_answers(client):
    value = assessment(client)
    route = '/api/assessments/' + value['id']
    standard_before = client.get('/api/standards').json()
    response = client.post(route + '/questions/direct', json={
        'expected_version': value['current_version'], 'control_id': 'SC-A',
        'text': 'Who reviews access each quarter?', 'answer_text': 'The IT manager.'})
    assert response.status_code == 201, response.text
    updated = response.json()
    added = [q for q in updated['questions'] if q.get('source') == 'user']
    assert len(added) == 1
    assert added[0]['control_ids'] == ['SC-A']
    assert added[0]['text'] == 'Who reviews access each quarter?'
    assert 'The IT manager.' in updated['answers']['SC-A']['text']
    assert updated['answers']['SC-B'] == value['answers']['SC-B']
    assert updated['current_version'] == value['current_version'] + 1
    assert client.get('/api/standards').json() == standard_before
    assert client.post(route + '/questions/direct', json={
        'expected_version': value['current_version'], 'control_id': 'SC-A',
        'text': 'Stale question'}).status_code == 409


def test_short_notes_mapping_is_reviewable_and_does_not_save_answers(client):
    value = assessment(client)
    route = '/api/assessments/' + value['id']
    response = client.post(route + '/map-answers', json={'text': 'Review access permissions quarterly\nxyzunknown'})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['provider'].startswith('offline')
    assert preview['requires_review'] is True
    assert preview['current_version'] == value['current_version']
    assert any(item['control_id'] == 'SC-A' for item in preview['mappings'])
    assert any(item['source_text'] == 'xyzunknown' for item in preview['unmatched'])
    refreshed = client.get(route).json()
    assert refreshed['answers'] == value['answers']
    assert refreshed['current_version'] == value['current_version']
    assert len(client.get(route + '/revisions').json()) == 1
