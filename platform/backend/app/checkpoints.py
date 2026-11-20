"""Write only validated successful rows, using an independent short transaction."""
from copy import deepcopy
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Assessment,RunCheckpoint
from . import incremental


def writer(bind,assessment_id,revision,provider,hashes):
    def save(findings):
        if not findings:return
        ids={f['control_id'] for f in findings}
        # The coordinator alone calls this; model worker threads never share a Session.
        with Session(bind=bind) as db, db.begin():
            current=db.scalar(select(Assessment.current_version).where(Assessment.id==assessment_id))
            if current!=revision:return
            db.add(RunCheckpoint(assessment_id=assessment_id,revision=revision,provider=provider,
                skill={'input_fingerprint_version':incremental.VERSION,
                       'input_fingerprints':{cid:hashes[cid] for cid in ids},
                       'assessment_plan':{'failed_control_ids':[],'unanswered_control_ids':[]}},
                results=deepcopy(findings)))
    return save


def save_departments(bind,assessment_id,revision,entries):
    if not entries:return
    with Session(bind=bind) as db, db.begin():
        if db.scalar(select(Assessment.current_version).where(Assessment.id==assessment_id))!=revision:return
        db.add(RunCheckpoint(assessment_id=assessment_id,revision=revision,provider='local:department-checkpoint',
            skill={'department_map':deepcopy(entries)},results=[]))


def restore_departments(db,assessment,controls,answers):
    from . import answer_departments
    context=deepcopy(assessment.context)
    entries=context.setdefault('answer_department_map',{})
    history=db.scalars(select(RunCheckpoint).where(RunCheckpoint.assessment_id==assessment.id,
        RunCheckpoint.provider=='local:department-checkpoint').order_by(RunCheckpoint.created_at.desc(),RunCheckpoint.id.desc())).all()
    for row in history:
        for control in controls:
            cid=control['id']
            if answer_departments.current_entry(control,answers.get(cid,{}),context,assessment.questions) is not None:continue
            candidate=row.skill.get('department_map',{}).get(cid)
            if not candidate:continue
            proposed={**context,'answer_department_map':{**entries,cid:candidate}}
            if answer_departments.current_entry(control,answers.get(cid,{}),proposed,assessment.questions) is not None:
                entries[cid]=deepcopy(candidate)
    return context
