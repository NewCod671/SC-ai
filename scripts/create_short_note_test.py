"""Create a separate fictional short-note assessment; never alter SiamCloud3."""
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform/backend'))
from app.questionnaire import assessment_questions

bank = {}
for line in (ROOT / 'scripts/short-note-cases.txt').read_text(encoding='utf-8').splitlines():
    if line.strip():
        cid, notes = line.split(' :: ', 1)
        bank[cid] = [part.strip() for part in notes.split(' | ')]

db = sqlite3.connect(ROOT / 'platform/backend/compliance.db')
db.row_factory = sqlite3.Row
web = ROOT / 'SC-Project-main/data/sc-control-center.sqlite'
db.execute('PRAGMA foreign_keys=ON')
db.execute('ATTACH DATABASE ? AS web', (str(web),))
name = 'SiamCloud3 - Short-note Test'
if db.execute('SELECT 1 FROM assessments WHERE name=?', (name,)).fetchone():
    raise RuntimeError('Test assessment already exists; refusing to overwrite answers')
source = db.execute("SELECT * FROM assessments WHERE name='SiamCloud3'").fetchone()
standard = db.execute('SELECT * FROM standards WHERE id=?', (source['standard_id'],)).fetchone()
controls, questions = json.loads(standard['controls']), json.loads(source['questions'])
source_revision = source['current_version']
source_answers = db.execute('SELECT answers FROM revisions WHERE assessment_id=? AND version=?', (source['id'], source_revision)).fetchone()[0]
assert {control['id'] for control in controls} == set(bank), 'Note bank must cover exactly the imported controls'

answers, question_count = {}, 0
for control in controls:
    units = assessment_questions(control, questions, {'text': ''})
    notes = bank[control['id']]
    blocks = []
    used = set()
    for index, question in enumerate(units):
        prompt = question['question_text']
        if len(units) == len(notes):
            chosen = index
        else:
            # Prefer a relevant note, then distribute unused observations across questions.
            words = set(re.findall(r'[a-z]{4,}', prompt.lower())) - {'organization','information','security','there','have','does','what','with','that','this','been','which','your','ensure'}
            scores = [len(words & set(re.findall(r'[a-z]{4,}', note.lower()))) for note in notes]
            chosen = max(range(len(notes)), key=lambda i: (scores[i], i not in used, -abs(i-index*len(notes)/len(units))))
        used.add(chosen)
        note = notes[chosen]
        # Give a single question the complete control note rather than dropping details.
        if len(units) == 1:
            note = ' '.join(notes)
        blocks.append(f'[Q{index+1}] {prompt.replace(chr(10), " ")}\n{note}')
    text = '\n\n'.join(blocks)
    assert text and len(text) <= 20000
    answers[control['id']] = {'text': text, 'status': 'unanswered'}
    question_count += len(units)

timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
backup_dir = ROOT / '.runtime/short-note-backups' / timestamp
backup_dir.mkdir(parents=True)
for path in (ROOT / 'platform/backend/compliance.db', web):
    with sqlite3.connect(path) as connection, sqlite3.connect(backup_dir / path.name) as backup:
        connection.backup(backup)

aid = str(uuid4())
project_id = 'SC-SIAMCLOUD3-NOTES'
context = json.loads(source['context'])
context.update(test_dataset=True, test_dataset_type='fictional_interview_short_notes', source_assessment_id=source['id'])
try:
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT INTO assessments(id,name,standard_id,context,questions,current_version) VALUES(?,?,?,?,?,1)',
               (aid, name, source['standard_id'], json.dumps(context), source['questions']))
    db.execute('INSERT INTO revisions(id,assessment_id,version,answers,source,created_at) VALUES(?,?,1,?,?,?)',
               (str(uuid4()), aid, json.dumps(answers), 'short_note_test', datetime.now(timezone.utc).isoformat()))
    db.execute('INSERT INTO assessment_ai_settings(assessment_id,mode) VALUES(?,?)', (aid, 'colab'))
    admin = db.execute("SELECT id,username FROM web.users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    db.execute('''INSERT INTO web.projects(id,name,client,owner,initials,stage,progress,manload_locked,due,status,tone,created_by)
        VALUES(?,?,?,?,?,'Audit Checklist',0,1,'Not set','In Progress','mint',?)''',
        (project_id, name, context.get('company','SiamCloud Solutions'), admin['username'], 'SC', admin['id']))
    db.execute('INSERT INTO web.project_assessments(project_id,assessment_id,created_by) VALUES(?,?,?)', (project_id, aid, admin['id']))
    assert not list(db.execute('PRAGMA foreign_key_check'))
    assert db.execute('SELECT answers FROM revisions WHERE assessment_id=? AND version=?', (source['id'], source_revision)).fetchone()[0] == source_answers
    db.commit()
except Exception:
    db.rollback()
    raise
finally:
    db.close()
print(json.dumps({'project':name,'project_id':project_id,'assessment_id':aid,'controls':len(controls),
                  'answered_questions':question_count,'source_preserved':True,'backup':str(backup_dir)}, indent=2))
