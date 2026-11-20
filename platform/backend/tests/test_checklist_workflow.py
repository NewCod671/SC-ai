"""Checklist creation/setup must work without a running inference provider."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from app import main
from app.models import Revision
from app.schemas import CreateAssessment, ChecklistSetupInput
from app.checklist_questions import from_standard
from app.reporting import native_sc
from .test_standard_skills import db, standard  # noqa: F401


def test_create_checklist_does_not_call_inference_and_persists_metadata(db, monkeypatch):
    s = standard(db)
    monkeypatch.setattr(main.ai, 'provider', lambda: pytest.fail('Checklist creation must not call inference'))
    result = main.create_assessment(CreateAssessment(name='Checklist review', standard_id=s.id,
        context={'company':'Fictional Co', 'business_type':'Hosting', 'departments':['IT'],
                 'auditor':'Test Auditor', 'observer':'Test Observer'}), db)
    assert result['context']['auditor'] == 'Test Auditor'
    assert result['context']['observer'] == 'Test Observer'
    assert {q['control_id'] for q in result['questions']} == {c['id'] for c in s.controls}
    assert all(q['source']=='checklist' for q in result['questions'])
    assert result['question_usage']['requests']==0
    assert result['question_usage']['total_tokens']==0


def test_checklist_can_be_created_before_first_page_is_filled(db, monkeypatch):
    s=standard(db)
    monkeypatch.setattr(main.ai, 'provider', lambda: pytest.fail('No inference before first-page setup'))
    a=main.create_assessment(CreateAssessment(name='First page pending',standard_id=s.id,
        context={'company':'Fictional','business_type':'Hosting'}),db)
    assert a['context']['departments']==[]
    assert a['context']['auditor']==''
    from app.schemas import RunInput
    with pytest.raises(HTTPException) as error: main.perform_assessment(a['id'],RunInput(),db)
    assert error.value.status_code==422
    ready=main.checklist_setup(a['id'],ChecklistSetupInput(expected_version=1,departments=['IT'],auditor='Reviewer'),db)
    assert ready['context']['departments']==['IT']


def test_legacy_ai_generation_still_requires_departments():
    with pytest.raises(ValidationError): CreateAssessment(name='Missing department',standard_id='demo',
        question_provider='inference',context={'company':'Fictional','business_type':'Hosting'})


def test_setup_preserves_answers_and_rejects_stale_revision(db, monkeypatch):
    s = standard(db)
    a = main.create_assessment(CreateAssessment(name='Original answers',standard_id=s.id,
        context={'company':'Fictional Co','business_type':'Hosting','departments':['IT']}),db)
    revision = db.scalar(select(Revision).where(Revision.assessment_id==a['id'],Revision.version==1))
    answers = deepcopy(revision.answers)
    answers[s.controls[0]['id']]['text']='Customer described the actual practice.'
    revision.answers=answers; db.commit()
    monkeypatch.setattr(main.ai, 'provider', lambda: pytest.fail('Setup must not call inference'))
    body=ChecklistSetupInput(expected_version=1,departments=['IT','HR'],auditor='Auditor',observer='Observer')
    updated=main.checklist_setup(a['id'],body,db)
    assert updated['current_version']==2
    assert updated['answers']==answers
    assert updated['context']['departments']==['IT','HR']
    with pytest.raises(HTTPException) as error: main.checklist_setup(a['id'],body,db)
    assert error.value.status_code==409


def test_native_checklist_uses_exact_original_question_text():
    _,refs=native_sc.source()
    controls=[{'id':cid,'requirement':r['requirement'],'department':'IT','row':r['row']} for cid,r in refs.items()]
    questions=from_standard(SimpleNamespace(controls=controls,mapping={'export_profile':native_sc.PROFILE}))
    assert len(questions)==118
    assert all(q['text']==refs[q['control_id']]['requirement'].partition('\n\n')[2] for q in questions)


def test_export_retains_auditor_and_observer_as_separate_roles():
    from app.reporting import excel
    _,refs=native_sc.source()
    controls=[{'id':cid,'requirement':r['requirement'],'department':'IT','row':r['row']} for cid,r in refs.items()]
    data=native_sc.export(controls,{c['id']:{'status':'unanswered','text':''} for c in controls},
        {'version':1},context={'company':'Fictional','departments':['IT'],'auditor':'Reviewer','observer':'Witness'})
    workbook=excel.read(data)
    try:
        assert workbook['Scope']['B8'].value=='Auditor: Reviewer\nObserver: Witness'
        assert workbook['Clause 4-10']['AG6'].value=='Auditor: Reviewer\nObserver: Witness'
        assert workbook['Scope']['A9'].value=='Auditee'
    finally: workbook.close()


@pytest.mark.parametrize('names',[[''],['IT','IT'],['IT',' IT ']])
def test_invalid_department_setup(names):
    with pytest.raises(ValidationError): ChecklistSetupInput(expected_version=1,departments=names)
