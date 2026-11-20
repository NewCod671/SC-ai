import hashlib
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func, text, update
from app import main
from app.models import Assessment, Audit, Evidence, Policy, Revision, Run, RunSummary, Standard, SkillFamily, StandardSkillVersion
from app.schemas import DeleteAssessmentInput, ReplaceAnswers, RunInput
from .test_standard_skills import db, standard, assessment, root_skill  # noqa: F401


@pytest.fixture
def populated(db):
    # Exercise real FK constraints, including summary -> run -> assessment.
    db.execute(text('PRAGMA foreign_keys=ON'))
    assert db.scalar(text('PRAGMA foreign_keys')) == 1
    s=standard(db)
    skill=root_skill(db,s.id)
    created=[]
    for name in ['Delete this assessment','Keep this assessment']:
        a=assessment(db,s.id)
        model=db.get(Assessment,a['id'])
        model.name=name
        db.commit()
        saved=main.save_answers(a['id'],ReplaceAnswers(expected_version=1,
            answers={c['id']:{'text':f'{name} private answer','status':'partial'} for c in s.controls}),db)
        e=Evidence(assessment_id=a['id'],control_id=s.controls[0]['id'],filename='evidence.txt',
                   content=name.encode(),sha256=hashlib.sha256(name.encode()).hexdigest(),text=name)
        p=Policy(assessment_id=a['id'],name='Policy',version='1',text=name,control_ids=[s.controls[0]['id']],changes={})
        db.add_all([e,p]);db.flush()
        main.audit(db,'evidence.upload',e.id,{'assessment_id':a['id']})
        main.audit(db,'policy.version',p.id,{'assessment_id':a['id']})
        main.audit(db,'question.mapping.preview',a['id'],{'text':'Private interview','answer_text':name})
        main.audit(db,'question.add',a['id'],{'question_id':'user-question'})
        db.commit()
        run=main.run_assessment(a['id'],RunInput(skill_id=skill['id']),db)
        created.append({'assessment':main.get_assessment(a['id'],db),'evidence':e.id,'policy':p.id,'run':run['id']})
    return s,skill,created[0],created[1]


def request_body(target, **overrides):
    a=target['assessment']
    return DeleteAssessmentInput(**({'expected_version':a['current_version'],'confirm_name':a['name']}|overrides))


def table_counts(db):
    return {m.__tablename__:db.scalar(select(func.count()).select_from(m)) for m in
            [Assessment,Revision,Evidence,Policy,Run,RunSummary,Audit,Standard,SkillFamily,StandardSkillVersion]}


def test_deletion_removes_all_owned_data_with_foreign_keys_and_preserves_other_assessment(db,populated):
    s,skill,target,other=populated
    before=table_counts(db)
    aid=target['assessment']['id']
    result=main.delete_assessment(aid,request_body(target),db)
    assert result['deleted'] is True and result['id']==aid
    assert result['deleted_counts']=={'audit_records':7,'run_summaries':1,'runs':1,'evidence':1,'policies':1,'revisions':2}
    assert db.get(Assessment,aid) is None
    for model in [Revision,Evidence,Policy,Run]:
        assert not db.scalars(select(model).where(model.assessment_id==aid)).all()
    assert db.get(RunSummary,target['run']) is None
    assert db.get(Standard,s.id) is not None
    assert db.get(StandardSkillVersion,skill['id']) is not None
    assert main.get_assessment(other['assessment']['id'],db)==other['assessment']
    assert db.get(Evidence,other['evidence']) is not None
    assert db.get(Policy,other['policy']) is not None
    assert db.get(RunSummary,other['run']) is not None
    after=table_counts(db)
    for label in ['standards','skill_families','standard_skill_versions']:
        assert before[label]==after[label]
    deletion=db.scalar(select(Audit).where(Audit.entity_id==aid,Audit.action=='assessment.delete'))
    assert deletion is not None
    assert set(deletion.details)=={'standard_id','revision','deleted_counts'}
    assert db.scalars(select(Audit).where(Audit.entity_id==aid)).all()==[deletion]
    with pytest.raises(HTTPException) as error: main.get_assessment(aid,db)
    assert error.value.status_code==404


@pytest.mark.parametrize('overrides, status', [({'confirm_name':'Wrong name'},422),({'expected_version':1},409)])
def test_wrong_confirmation_or_stale_revision_never_deletes_anything(db,populated,overrides,status):
    s,skill,target,other=populated
    before=table_counts(db)
    with pytest.raises(HTTPException) as error:
        main.delete_assessment(target['assessment']['id'],request_body(target,**overrides),db)
    assert error.value.status_code==status
    assert table_counts(db)==before


def test_deletion_rolls_back_every_child_and_audit_on_failure(db,populated,monkeypatch):
    s,skill,target,other=populated
    before=table_counts(db)
    monkeypatch.setattr(main,'commit',lambda db: (_ for _ in ()).throw(RuntimeError('Simulated write failure')))
    with pytest.raises(RuntimeError):
        main.delete_assessment(target['assessment']['id'],request_body(target),db)
    assert table_counts(db)==before
    assert main.get_assessment(target['assessment']['id'],db)==target['assessment']


def test_atomic_revision_check_handles_change_after_cached_read(db,populated):
    s,skill,target,other=populated
    a=db.get(Assessment,target['assessment']['id'])
    old=a.current_version
    db.execute(update(Assessment).where(Assessment.id==a.id).values(current_version=old+1)
               .execution_options(synchronize_session=False))
    db.commit()
    assert a.current_version==old  # Simulates a stale request's identity map.
    before=table_counts(db)
    with pytest.raises(HTTPException) as error:
        main.delete_assessment(a.id,request_body(target),db)
    assert error.value.status_code==409
    assert table_counts(db)==before


def test_missing_assessment_returns_404_without_touching_shared_data(db,populated):
    before=table_counts(db)
    with pytest.raises(HTTPException) as error:
        main.delete_assessment('missing',DeleteAssessmentInput(expected_version=1,confirm_name='Missing'),db)
    assert error.value.status_code==404
    assert table_counts(db)==before


def test_delete_route_uses_common_authentication_and_cors_supports_delete():
    route=next(r for r in main.app.routes if getattr(r,'path',None)=='/api/assessments/{id}' and 'DELETE' in getattr(r,'methods',set()))
    assert any(dependency.call is main.authenticate for dependency in route.dependant.dependencies)
    assert any('DELETE' in middleware.kwargs.get('allow_methods',[]) for middleware in main.app.user_middleware)
    assert main.app.openapi()['paths']['/api/assessments/{id}']['delete']['requestBody']['required'] is True
