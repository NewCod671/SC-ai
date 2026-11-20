import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app import ai, main, assessment_jobs as jobs
from app.models import Run, RunSummary, Audit
from app.schemas import RunInput, ReplaceAnswers
from app.assessment_batches import assess_batches
from .test_standard_skills import db, standard, assessment
from .test_continue_assessment import payload, findings


def test_cancel_before_start_prevents_inference(db, monkeypatch):
    a=assessment(db,standard(db).id)
    job=main.reserve_assessment_run(a['id'],db)
    main.cancel_assessment_run(job['id'])
    monkeypatch.setattr(ai,'provider',lambda:pytest.fail('No inference'))
    with pytest.raises(HTTPException) as exc:
        main.run_assessment(a['id'],RunInput(job_id=job['id']),db)
    assert exc.value.status_code==409
    assert db.scalar(select(func.count()).select_from(Run))==0


@pytest.mark.parametrize('timeout',[False,True])
def test_cancel_in_request_prevents_next_batch_and_persistence(db,monkeypatch,timeout):
    a=assessment(db,standard(db).id)
    main.save_answers(a['id'],ReplaceAnswers(expected_version=a['current_version'],answers={cid:{'status':'unanswered','text':'Implemented by IT'} for cid in a['answers']}),db)
    job=main.reserve_assessment_run(a['id'],db)
    p=ai.HTTPProvider(url='https://synthetic.example',model='test')
    calls=[]
    def request(operation,batch):
        calls.append(operation)
        jobs.cancel(job['id'])
        if timeout:raise httpx.ReadTimeout('secret must not leak')
        return findings(batch)
    monkeypatch.setattr(p,'request',request)
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(p))
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE','1')
    before=db.scalar(select(func.count()).select_from(Audit))
    with pytest.raises(HTTPException) as exc:
        main.run_assessment(a['id'],RunInput(job_id=job['id']),db)
    assert exc.value.status_code==409 and len(calls)==1
    assert db.scalar(select(func.count()).select_from(Run))==0
    assert db.scalar(select(func.count()).select_from(RunSummary))==0
    assert db.scalar(select(func.count()).select_from(Audit))==before


def test_completed_job_late_cancel_is_honest_and_cannot_reuse(db):
    a=assessment(db,standard(db).id)
    job=main.reserve_assessment_run(a['id'],db)
    result=main.run_assessment(a['id'],RunInput(job_id=job['id']),db)
    assert result['id']
    assert main.cancel_assessment_run(job['id'])['status']=='completed'
    with pytest.raises(HTTPException) as exc:
        main.run_assessment(a['id'],RunInput(job_id=job['id']),db)
    assert exc.value.status_code==409


def test_cancellation_before_commit_rolls_back_run(db,monkeypatch):
    a=assessment(db,standard(db).id)
    job=main.reserve_assessment_run(a['id'],db)
    original=main.summarize
    def cancel_then_summarize(*args):
        jobs.cancel(job['id'])
        return original(*args)
    monkeypatch.setattr(main,'summarize',cancel_then_summarize)
    with pytest.raises(HTTPException) as exc:
        main.run_assessment(a['id'],RunInput(job_id=job['id']),db)
    assert exc.value.status_code==409
    assert db.scalar(select(func.count()).select_from(Run))==0


def test_job_cannot_run_for_other_assessment():
    job=jobs.reserve('one')
    with pytest.raises(HTTPException) as exc:
        with jobs.generation(job['id'],'two'):pytest.fail('Wrong assessment')
    assert exc.value.status_code==409


def test_default_assessment_batch_is_five(monkeypatch):
    monkeypatch.delenv('AI_ASSESS_BATCH_SIZE',raising=False)
    calls=[]
    def request(operation,batch):
        calls.append(len(batch['controls']))
        return findings(batch)
    result,plan=assess_batches(request,payload(12),ai.validate_findings)
    assert calls==[5,5,2] and len(result)==12 and plan['batch_size']==5


def test_stop_from_another_thread_while_model_waits():
    from threading import Event, Thread
    entered,release=Event(),Event()
    job=jobs.reserve('thread-cancel-test')
    calls=[];outcomes=[]
    def request(operation,batch):
        calls.append(batch)
        entered.set()
        assert release.wait(5)
        return findings(batch)
    def worker():
        try:
            with jobs.generation(job['id'],'thread-cancel-test'):
                assess_batches(request,payload(12),ai.validate_findings)
        except jobs.AssessmentCancelled:
            outcomes.append('cancelled')
    thread=Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        assert jobs.cancel(job['id'])['status']=='cancelled'
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive() and len(calls)==1 and outcomes==['cancelled']
