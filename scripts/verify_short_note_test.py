"""Exercise short-note processing on a disposable copy, with no remote inference."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform/backend'))
from app.main import app
from app.db import get_db
from app.questionnaire import assessment_questions
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

os.environ.update(AI_PROVIDER='offline', AUTO_CREATE_DB='false')
os.environ.pop('APP_API_KEY', None)
with tempfile.TemporaryDirectory(prefix='sc-short-note-check-') as folder:
    path = Path(folder) / 'test.db'
    source = sqlite3.connect((ROOT / 'platform/backend/compliance.db').as_uri() + '?mode=ro', uri=True)
    target = sqlite3.connect(path)
    source.backup(target)
    source.close()
    target.close()
    engine = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread':False})
    Session = sessionmaker(engine, expire_on_commit=False)
    def override():
        with Session() as db:
            yield db
    app.dependency_overrides[get_db] = override
    try:
        lookup = sqlite3.connect(path)
        aid = lookup.execute("SELECT id FROM assessments WHERE name='SiamCloud3 - Short-note Test'").fetchone()[0]
        lookup.close()
        with TestClient(app) as client:
            route = '/api/assessments/' + aid
            assessment = client.get(route).json()
            units = [item for control in assessment['controls'] for item in assessment_questions(control, assessment['questions'], assessment['answers'][control['id']])]
            assert len(units) == 516 and all(item['answer_text'].strip() for item in units)
            response = client.post(route + '/runs', json={'processing_mode':'colab', 'authorize_external':True})
            assert response.status_code == 201, response.text[:200]
            results = response.json()['results']
            assert len(results) == 118 and {item['control_id'] for item in results} == {item['id'] for item in assessment['controls']}
            exported = client.get(route + '/export')
            assert exported.status_code == 200
            print(json.dumps({'answered_questions':len(units),'control_results':len(results),'export_bytes':len(exported.content),'provider':'offline','original_database_unchanged':True}))
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
