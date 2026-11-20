import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base, get_db
from app.main import app
from app.models import Revision, Audit
from .fixtures import workbook, patch_cell

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('AUTO_CREATE_DB', 'false')
    monkeypatch.setenv('AI_PROVIDER', 'offline')
    monkeypatch.delenv('APP_API_KEY', raising=False)
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    def override():
        with Session() as db:
            yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        c.session_factory = Session
        yield c
    app.dependency_overrides.clear()
    engine.dispose()

def standard(client, version='1', previous=None, data=None):
    form = {'name':'Configurable SC','version':version}
    if previous: form['previous_id'] = previous
    response = client.post('/api/standards/import', data=form, files={'file':('checklist.xlsx',data or workbook())})
    assert response.status_code == 201, response.text
    return response.json()

def assessment(client):
    s = standard(client)
    r = client.post('/api/assessments',json={'name':'Review','standard_id':s['id'],'context':{'company':'Example','business_type':'Retail','departments':['IT','HR'],'it_systems':['ERP'],'data_types':['PII']}})
    assert r.status_code == 201, r.text
    return r.json()

def test_end_to_end_excel_evidence_policy_and_run(client):
    a = assessment(client); base = '/api/assessments/'+a['id']
    exported = client.get(base+'/export'); assert exported.status_code == 200
    edited = patch_cell(exported.content,2,4,'Access is reviewed quarterly')
    edited = patch_cell(edited,2,5,'yes')
    r = client.post(base+'/import',files={'file':('answers.xlsx',edited)})
    assert r.status_code == 200, r.text
    assert r.json()['current_version'] == 2
    assert client.post(base+'/import',files={'file':('stale.xlsx',edited)}).status_code == 422
    ev = client.post(base+'/evidence',data={'control_id':'SC-A'},files={'file':('review.txt',b'Quarterly access permissions review completed')})
    assert ev.status_code == 201, ev.text
    policy = {'name':'Access','version':'1','text':'Review access quarterly','control_ids':['SC-A']}
    assert client.post(base+'/policies',json=policy).status_code == 201
    p2 = client.post(base+'/policies',json={**policy,'version':'2','text':'Review access monthly'}).json()
    assert p2['changes']['impacted_controls'] == ['SC-A']
    assert '-Review access quarterly' in p2['changes']['diff']
    skill = client.post('/api/skills',json={'name':'Review','version':'1','instructions':'Require dated evidence'}).json()
    run = client.post(base+'/runs',json={'skill_id':skill['id']})
    assert run.status_code == 201, run.text
    assert run.json()['revision'] == 2
    assert run.json()['skill']['policy_ids'] == [p2['id']]
    assert run.json()['results'][0]['evidence_ids'] == [ev.json()['id']]
    assert client.get(base+'/export').status_code == 200
    assert len(client.get(base+'/revisions').json()) == 2
    assert client.get(base+'/evidence/'+ev.json()['id']+'/download').content == b'Quarterly access permissions review completed'
    assert client.get('/api/audit').json()

def test_web_replacement_is_atomic_and_stale_writes_rejected(client):
    a = assessment(client); base = '/api/assessments/'+a['id']
    invalid = {'expected_version':1,'answers':{'SC-A':{'status':'no','text':'Missing'}}}
    assert client.put(base+'/answers',json=invalid).status_code == 422
    assert client.get(base).json()['current_version'] == 1
    answers = {'expected_version':1,'answers':{c:{'status':'no','text':'Missing'} for c in a['answers']}}
    assert client.put(base+'/answers',json=answers).status_code == 200
    assert client.put(base+'/answers',json=answers).status_code == 409
    with client.session_factory() as db:
        assert len(list(db.scalars(select(Revision)))) == 2
        assert len(list(db.scalars(select(Audit).where(Audit.action == 'assessment.replace')))) == 1

def test_standard_changes_and_immutability(client):
    a = assessment(client)
    s2 = standard(client,'2',a['standard_id'],workbook([('SC-A','Review access monthly','IT'),('SC-C','Monitor systems','IT')]))
    assert s2['changes']['modified'] == ['SC-A']
    assert s2['changes']['removed'] == ['SC-B']
    assert s2['changes']['added'] == ['SC-C']
    assert a['id'] in s2['changes']['impacted_assessments']
    assert client.get('/api/assessments/'+a['id']).json()['standard_id'] == a['standard_id']

def test_duplicate_versions_return_conflict(client):
    s = standard(client)
    assert client.post('/api/standards/import',data={'name':s['name'],'version':'1'},files={'file':('same.xlsx',workbook())}).status_code == 409
    assert len(client.get('/api/standards').json()) == 1

def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setenv('APP_API_KEY','test-secret')
    assert client.get('/api/standards').status_code == 401
    assert client.get('/api/standards',headers={'Authorization':'Bearer test-secret'}).status_code == 200

def test_invalid_evidence_control_and_policy_links(client):
    a = assessment(client); base = '/api/assessments/'+a['id']
    assert client.post(base+'/evidence',data={'control_id':'invalid'},files={'file':('x.txt',b'evidence')}).status_code == 422
    assert client.post(base+'/policies',json={'name':'x','version':'1','text':'x','control_ids':['unknown']}).status_code == 422
    assert client.get(base+'/evidence').json() == []

def test_invalid_ai_response_never_saves_run(client, monkeypatch):
    from app.ai import OfflineProvider
    a = assessment(client)
    monkeypatch.setattr(OfflineProvider,'assess',lambda *_: [{'control_id':'unknown'}])
    base = '/api/assessments/'+a['id']
    assert client.post(base+'/runs',json={}).status_code == 502
    assert client.get(base+'/runs').json() == []

def test_tampered_excel_keeps_current_revision(client):
    a = assessment(client); base = '/api/assessments/'+a['id']
    data = patch_cell(client.get(base+'/export').content,2,1,'unknown')
    assert client.post(base+'/import',files={'file':('bad.xlsx',data)}).status_code == 422
    assert client.get(base).json()['current_version'] == 1

def test_new_evidence_and_policy_version_make_findings_stale(client):
    a = assessment(client); base = '/api/assessments/'+a['id']
    assert client.post(base+'/runs',json={}).status_code == 201
    assert client.get(base+'/runs').json()[0]['stale'] is False
    client.post(base+'/evidence',data={'control_id':'SC-A'},files={'file':('access.txt',b'access evidence')})
    assert client.get(base+'/runs').json()[0]['stale'] is True
    assert client.post(base+'/runs',json={}).status_code == 201
    client.post(base+'/policies',json={'name':'Access','version':'1','text':'Quarterly review','control_ids':['SC-A']})
    assert all(r['stale'] for r in client.get(base+'/runs').json())
