"""Copy SiamCloud3 and its linked records from a read-only source database."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'ai-project' / 'backend' / 'compliance.db'
TARGET = ROOT / 'platform/backend/compliance.db'
WEB = ROOT / 'SC-Project-main/data/sc-control-center.sqlite'
BACKUP = ROOT / '.runtime/import-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
BACKUP.mkdir(parents=True)

source = sqlite3.connect(SOURCE.resolve().as_uri() + '?mode=ro', uri=True)
source.row_factory = sqlite3.Row
matches = source.execute('SELECT * FROM assessments WHERE lower(name)=?', ('siamcloud3',)).fetchall()
if len(matches) != 1:
    raise RuntimeError('Expected exactly one SiamCloud3 assessment')
assessment = matches[0]
aid, sid = assessment['id'], assessment['standard_id']
for file in (TARGET, WEB):
    with sqlite3.connect(file) as original, sqlite3.connect(BACKUP / file.name) as backup:
        original.backup(backup)

db = sqlite3.connect(TARGET)
db.row_factory = sqlite3.Row
db.execute('PRAGMA foreign_keys=ON')
db.execute('ATTACH DATABASE ? AS web', (str(WEB),))
counts = {}
ids = {aid, sid}

def copy_rows(table, rows):
    rows = list(rows)
    counts[table] = len(rows)
    if not rows:
        return
    keys = rows[0].keys()
    columns = ','.join('"' + key + '"' for key in keys)
    placeholders = ','.join('?' for _ in keys)
    for row in rows:
        db.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(row))
        if 'id' in keys:
            ids.add(row['id'])

try:
    db.execute('BEGIN IMMEDIATE')
    if db.execute('SELECT 1 FROM assessments WHERE id=?', (aid,)).fetchone():
        raise RuntimeError('SiamCloud3 is already imported; existing data was not overwritten')
    copy_rows('standards', source.execute('SELECT * FROM standards WHERE id=?', (sid,)))
    families = source.execute('SELECT * FROM skill_families WHERE standard_id=?', (sid,)).fetchall()
    copy_rows('skill_families', families)
    for family in families:
        versions = source.execute('SELECT * FROM standard_skill_versions WHERE family_id=? ORDER BY created_at', (family['id'],)).fetchall()
        # Parent versions may have equal timestamps: defer constraints until commit.
        db.execute('PRAGMA defer_foreign_keys=ON')
        copy_rows('standard_skill_versions', versions)
    copy_rows('assessments', [assessment])
    payloads = [assessment['context']]
    for table in ('revisions', 'evidence', 'policies', 'runs', 'run_checkpoints',
                  'assessment_ai_settings', 'agent_runs', 'auditor_feedback'):
        rows = source.execute(f'SELECT * FROM "{table}" WHERE assessment_id=?', (aid,)).fetchall()
        copy_rows(table, rows)
        for row in rows:
            payloads.extend(value for value in row if isinstance(value, str))
    for skill in source.execute('SELECT * FROM skills'):
        if any(skill['id'] in value for value in payloads):
            copy_rows('skills', [skill])
    run_ids = [row[0] for row in db.execute('SELECT id FROM runs WHERE assessment_id=?', (aid,))]
    summaries = [row for row in source.execute('SELECT * FROM run_summaries') if row['run_id'] in run_ids]
    copy_rows('run_summaries', summaries)
    copy_rows('audit', [row for row in source.execute('SELECT * FROM audit') if row['entity_id'] in ids])
    admin = db.execute("SELECT id,username FROM web.users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    if not admin:
        raise RuntimeError('No SC administrator available to own the imported project')
    project_id = 'SC-SIAMCLOUD3'
    company = json.loads(assessment['context']).get('company') or 'SiamCloud Solutions'
    db.execute('''INSERT INTO web.projects
        (id,name,client,owner,initials,stage,progress,manload_locked,due,status,tone,created_by)
        VALUES(?,?,?,?,?,'Audit Checklist',0,1,'Not set','In Progress','mint',?)''',
        (project_id, assessment['name'], company, admin['username'], 'SC', admin['id']))
    db.execute('INSERT INTO web.project_assessments(project_id,assessment_id,created_by) VALUES(?,?,?)',
               (project_id, aid, admin['id']))
    db.execute("INSERT INTO web.audit_log(user_id,action,entity,entity_id) VALUES(?,'IMPORT','compliance_assessment',?)", (admin['id'], aid))
    if list(db.execute('PRAGMA foreign_key_check')) or list(db.execute('PRAGMA web.foreign_key_check')):
        raise RuntimeError('Imported records failed referential integrity validation')
    # Verify the latest answers match the source byte for byte.
    original = source.execute('SELECT answers FROM revisions WHERE assessment_id=? AND version=?', (aid, assessment['current_version'])).fetchone()
    imported = db.execute('SELECT answers FROM revisions WHERE assessment_id=? AND version=?', (aid, assessment['current_version'])).fetchone()
    if not original or not imported or original[0] != imported[0]:
        raise RuntimeError('Latest answer revision did not match')
    db.commit()
    report = {'assessment': assessment['name'], 'project_id': project_id,
              'assessment_id': aid, 'current_version': assessment['current_version'],
              'controls': len(json.loads(db.execute('SELECT controls FROM standards WHERE id=?', (sid,)).fetchone()[0])),
              'answered_controls': sum(bool(answer.get('text', '').strip()) for answer in json.loads(imported[0]).values()),
              'copied_records': counts, 'backup': str(BACKUP), 'source_read_only': True}
    (BACKUP / 'import-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
except Exception:
    db.rollback()
    raise
finally:
    db.close()
    source.close()
