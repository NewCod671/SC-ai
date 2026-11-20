from copy import deepcopy
from io import BytesIO

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from openpyxl import load_workbook

from app import departments, main
from app.reporting import excel
from app.models import Revision, Assessment
from app.schemas import CreateAssessment, DepartmentAssignments
from .test_standard_skills import db, standard  # noqa: F401
from .fixtures import workbook

CONTEXT = {'company':'Example','business_type':'Retail','departments':['IT','HR','Finance']}
ROWS = [('C-1','Review network access permissions','All'),
        ('C-2','Train employees during recruitment','All'),
        ('C-3','Approve supplier invoices and payments','All')]


def setup_assessment(db):
    s = standard(db)
    s.template = workbook(ROWS)
    s.controls,s.mapping = excel.parse_standard(s.template,{})
    db.commit()
    a = main.create_assessment(CreateAssessment(name='Department scope',standard_id=s.id,context=CONTEXT),db)
    return s,a


def test_generic_template_routes_to_real_company_names_without_mutating_source():
    controls=[{'id':cid,'requirement':req,'department':dept} for cid,req,dept in ROWS]
    original=deepcopy(controls)
    routed=departments.assignments(controls,CONTEXT)
    assert routed['C-1']['departments']==['IT']
    assert routed['C-2']['departments']==['HR']
    assert routed['C-3']['departments']==['Finance']
    assert controls==original


def test_unrecognized_roles_are_not_assigned_to_every_department_or_arbitrary_first_team():
    result=departments.assignments([{'id':'X','requirement':'Review the strategic scope','department':'All'}],
                                  {**CONTEXT,'departments':['Team Blue','Team Green']})
    assert result['X']=={'departments':[],'source':'unassigned'}


def test_short_it_alias_does_not_match_audit_department():
    result=departments.assignments([{'id':'X','requirement':'Review network access','department':'All'}],
                                  {**CONTEXT,'departments':['Audit','HR']})
    assert result['X']['departments']==[]


def test_aliases_preserve_company_spelling_and_multiple_responsibilities():
    result=departments.assignments([{'id':'X','requirement':'Review employee account termination','department':'All'}],
                                  {**CONTEXT,'departments':['ฝ่ายไอที','ฝ่ายบุคคล']})
    assert result['X']['departments']==['ฝ่ายไอที','ฝ่ายบุคคล']


def test_shared_interview_does_not_copy_its_department_union_to_each_control():
    controls=[{'id':cid,'requirement':req,'department':dept} for cid,req,dept in ROWS[:2]]
    question={'control_ids':['C-1','C-2'],'departments':['IT','HR'],'text':'Shared interview'}
    routed=departments.assignments(controls,CONTEXT,[question])
    assert routed['C-1']['departments']==['IT'] and routed['C-2']['departments']==['HR']


def test_assessment_questions_controls_and_excel_share_routing_and_source_template_remains_generic(db):
    s,a=setup_assessment(db)
    assert [c['department'] for c in a['controls']]==['IT','HR','Finance']
    assert [q['departments'] for q in a['questions']]==[['IT'],['HR'],['Finance']]
    assert all(c['department']=='All' for c in s.controls)
    _,data=main.assessment_workbook(a['id'],db)
    ws=load_workbook(BytesIO(data))[s.mapping['sheet']]
    assert [ws.cell(c['row'],s.mapping['indices']['department']).value for c in s.controls]==['IT','HR','Finance']
    assert excel.import_answers(data,s.mapping,s.controls,{'assessment_id':a['id'],'standard_id':s.id,'version':1},
        a['answers'],department_overrides={c['id']:c['department'] for c in a['controls']})==a['answers']
    original=load_workbook(BytesIO(s.template))[s.mapping['sheet']]
    assert all(original.cell(c['row'],s.mapping['indices']['department']).value=='All' for c in s.controls)


def test_manual_assignment_is_versioned_keeps_answers_and_updates_shared_views(db):
    s,a=setup_assessment(db)
    original_answers=deepcopy(a['answers'])
    result=main.save_departments(a['id'],DepartmentAssignments(expected_version=1,assignments={'C-1':['IT','Finance']}),db)
    assert result['current_version']==2 and result['answers']==original_answers
    assert result['controls'][0]['department']=='IT / Finance'
    assert result['questions'][0]['departments']==['IT','Finance']
    assert result['department_assignments']['C-1']['source']=='manual'
    assert db.scalar(select(func.count()).select_from(Revision))==2
    assert s.controls[0]['department']=='All'
    _,data=main.assessment_workbook(a['id'],db)
    ws=load_workbook(BytesIO(data))[s.mapping['sheet']]
    assert ws.cell(s.controls[0]['row'],s.mapping['indices']['department']).value=='IT / Finance'


@pytest.mark.parametrize('values',[{'C-1':['Unknown']},{'unknown':['IT']},{'C-1':['IT','IT']}])
def test_invalid_assignments_are_rejected_without_revisions(db,values):
    _,a=setup_assessment(db)
    with pytest.raises(HTTPException) as error:
        main.save_departments(a['id'],DepartmentAssignments(expected_version=1,assignments=values),db)
    assert error.value.status_code==422
    assert db.scalar(select(func.count()).select_from(Revision))==1


def test_stale_manual_change_cannot_overwrite_current_assignment(db):
    _,a=setup_assessment(db)
    main.save_departments(a['id'],DepartmentAssignments(expected_version=1,assignments={'C-1':['HR']}),db)
    with pytest.raises(HTTPException) as error:
        main.save_departments(a['id'],DepartmentAssignments(expected_version=1,assignments={'C-1':['Finance']}),db)
    assert error.value.status_code==409
    assert main.get_assessment(a['id'],db)['controls'][0]['department']=='HR'


def test_manual_unassigned_does_not_get_silently_replaced_by_topic_suggestion(db):
    _,a=setup_assessment(db)
    result=main.save_departments(a['id'],DepartmentAssignments(expected_version=1,assignments={'C-1':[]}),db)
    assert result['controls'][0]['departments']==[]
    assert result['controls'][0]['department']==departments.UNASSIGNED


def test_existing_all_questions_gain_company_projection_without_regeneration_or_answer_changes(db,monkeypatch):
    from app import ai
    _,a=setup_assessment(db)
    record=db.get(Assessment,a['id'])
    record.questions=[{**q,'departments':['All'],'department':'All'} for q in record.questions]
    db.commit()
    monkeypatch.setattr(ai,'provider',lambda:pytest.fail('Viewing old departments must not call AI'))
    view=main.get_assessment(a['id'],db)
    assert [c['department'] for c in view['controls']]==['IT','HR','Finance']
    assert view['answers']==a['answers'] and view['current_version']==1
    assert all(q['departments']==['All'] for q in record.questions)
