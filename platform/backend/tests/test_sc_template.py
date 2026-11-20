import importlib.util
import json
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.reporting import excel
from app.db import Base
from app.models import Standard, StandardSkillVersion
from app.skill_templates import template_skill

ROOT = Path(__file__).resolve().parents[2]


def prepared():
    folder = ROOT / 'outputs/sc-audit-template'
    data = (folder / 'sc-iso27001-audit-template.xlsx').read_bytes()
    controls, mapping = excel.parse_standard(data, json.loads((folder / 'mapping.json').read_text()))
    answers = {c['id']: {'status':'unanswered','text':''} for c in controls}
    return data, controls, mapping, answers, {'assessment_id':'test','version':1}


def test_sc_coverage_and_no_sample_answers():
    data, controls, mapping, _, _ = prepared()
    ids = {c['id'] for c in controls}
    assert len(ids) == 118
    assert {'6.1','7.5.1','7.5.2','7.5.3','10.1','10.2','A.5.1','A.8.34'} <= ids
    assert len([c for c in ids if c.startswith('A.')]) == 93
    wb = excel.read(data)
    assert wb.sheetnames == ['Checklist','Audit Note']
    assert mapping['report_profile'] == 'sc-audit-note-v1'
    for row in range(7,125):
        assert wb['Checklist'].cell(row,4).value in (None,'')
        assert wb['Checklist'].cell(row,5).value == 'ยังไม่ตรวจ'
        assert wb['Checklist'].cell(row,6).value in (None,'')
        assert wb['Audit Note'].cell(row,6).value in (None,'')
    wb.close()


@pytest.mark.parametrize('mode', ['current','stale','failed','unreviewed','empty'])
def test_audit_note_export_matches_actual_run_and_answers_roundtrip(mode):
    template, controls, mapping, answers, manifest = prepared()
    cid = controls[0]['id']
    answers[cid] = {'status':'yes','text':'=literal respondent text'}
    finding = {'control_id':cid,'status':'gap','interpreted_answer':'Interpreted',
               'rationale':'Actual gap','recommendation':'Actual action','evidence_ids':['ev-1']}
    kwargs = {'results':[finding]} if mode != 'empty' else {}
    if mode == 'stale': kwargs['stale'] = True
    if mode == 'failed': kwargs['failed_ids'] = [cid]
    if mode == 'unreviewed': kwargs['unreviewed_ids'] = [cid]
    routing = {cid:'Risk / IT'}
    result = excel.export(template,mapping,controls,answers,manifest,department_overrides=routing,**kwargs)
    wb = excel.read(result)
    ws = wb['Audit Note']
    assert ws['B7'].value == 'Risk / IT'
    assert ws['C7'].value == '=literal respondent text' and ws['C7'].data_type != 'f'
    assert ws['E7'].value == wb['Checklist']['E7'].value
    assert ws['E7'].value == ('ไม่ผ่าน' if mode=='current' else 'ต้องประเมินใหม่' if mode=='stale' else 'ยังไม่ตรวจ')
    assert (ws['G7'].value or '') == ('ev-1' if mode=='current' else '')
    assert (ws['D7'].value or '') == ('Interpreted' if mode=='current' else '')
    assert (ws['F7'].value or '') == ('Actual gap' if mode in ('current','failed') else '')
    wb.close()
    assert excel.import_answers(result,mapping,controls,manifest,answers,routing) == answers


def test_unknown_profile_is_rejected():
    data, _, mapping, _, _ = prepared()
    with pytest.raises(ValueError,match='Unknown report profile'):
        excel.parse_standard(data, {**mapping,'report_profile':'unknown'})


def test_install_idempotent_and_skill_is_scoped():
    spec=importlib.util.spec_from_file_location('install_sc_template',ROOT/'scripts/install_sc_template.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        sid, created=module.install(db)
        db.commit()
        assert created
        assert module.install(db) == (sid,False)
        standard=db.get(Standard,sid)
        instructions=json.loads(template_skill('sc-iso27001-audit-review',standard)['instructions'])
        assert instructions['control_rules'] == {}
        assert instructions['assessment_basis']=='sc_recorded_answers_v1'
        assert instructions['evidence_role']=='archive_only'
        assert len(db.scalars(select(StandardSkillVersion)).all()) == 1
    engine.dispose()
def test_skill_upgrade_preserves_previous_version():
    from app.models import SkillFamily
    spec=importlib.util.spec_from_file_location('upgrade_sc_audit_skill',ROOT/'scripts/upgrade_sc_audit_skill.py')
    upgrade=importlib.util.module_from_spec(spec);spec.loader.exec_module(upgrade)
    spec=importlib.util.spec_from_file_location('install_sc_template_for_upgrade',ROOT/'scripts/install_sc_template.py')
    install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        sid,_=install.install(db)
        old=db.scalar(select(StandardSkillVersion))
        old.version='1.0';old.instructions='Original immutable version'
        family=db.scalar(select(SkillFamily))
        core=family.core_instructions
        db.commit()
        upgraded=upgrade.upgrade(db);db.commit()
        assert db.get(StandardSkillVersion,upgraded).parent_id==old.id
        assert db.get(StandardSkillVersion,old.id).instructions=='Original immutable version'
        assert db.get(SkillFamily,family.id).core_instructions==core
        assert upgrade.upgrade(db)==upgraded
    engine.dispose()
