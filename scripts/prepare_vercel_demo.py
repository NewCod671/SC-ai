"""Seed a dedicated trial database from the synthetic assessment only."""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / '.runtime' / 'vercel-demo'
DEMO.mkdir(parents=True, exist_ok=True)
source = sqlite3.connect((ROOT / 'platform/backend/compliance.db').as_uri() + '?mode=ro', uri=True)
source.row_factory = sqlite3.Row
assessment = source.execute("SELECT * FROM assessments WHERE name='SiamCloud3 - Mixed Grades Test'").fetchone()
assert assessment is not None and json.loads(assessment['context']).get('test_dataset') is True
aid = assessment['id']
sid = assessment['standard_id']
revision = source.execute('SELECT * FROM revisions WHERE assessment_id=? AND version=?',
                          (aid, assessment['current_version'])).fetchone()
target = sqlite3.connect(DEMO / 'compliance.db')
target.execute('PRAGMA foreign_keys=ON')

def insert(table, row):
    columns = ','.join('"' + name + '"' for name in row.keys())
    values = ','.join('?' for _ in row)
    target.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({values})', tuple(row))

with target:
    target.execute('BEGIN IMMEDIATE')
    if not target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assessments'").fetchone():
        for entry in source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            target.execute(entry['sql'])
        insert('standards', source.execute('SELECT * FROM standards WHERE id=?', (sid,)).fetchone())
        insert('assessments', assessment)
        insert('revisions', revision)
        target.execute("INSERT INTO assessment_ai_settings(assessment_id,mode) VALUES(?,'codex')", (aid,))
    assert target.execute('SELECT id FROM assessments').fetchall() == [(aid,)]
    assert target.execute('SELECT id FROM standards').fetchall() == [(sid,)]
    assert not target.execute('PRAGMA foreign_key_check').fetchall()
(DEMO / 'assessment-id.txt').write_text(aid, encoding='utf-8')
print('Isolated demo assessment ready.')
