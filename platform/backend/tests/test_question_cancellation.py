from threading import Event, Thread

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app import ai, main, question_jobs as jobs
from app.models import Assessment, Revision, Audit
from app.schemas import CreateAssessment
from app.question_batches import generate_question_batches
from .test_standard_skills import db, standard  # noqa: F401
from .test_question_batches import controls, generated, status_error


def body(s, job):
    return CreateAssessment(name='Cancelled interview', standard_id=s.id, generation_id=job['id'],
                            context={'company':'Example','business_type':'Retail','departments':['IT']})


def test_cancel_before_start_is_not_lost_and_job_cannot_be_reused(db, monkeypatch):
    s = standard(db)
    job = main.reserve_question_generation()
    assert main.cancel_question_generation(job['id'])['status'] == 'cancelled'
    monkeypatch.setattr(ai, 'provider', lambda: pytest.fail('No inference should start'))
    with pytest.raises(HTTPException) as error:
        main.create_assessment(body(s, job), db)
    assert error.value.status_code == 409
    assert db.scalar(select(func.count()).select_from(Assessment)) == 0


@pytest.mark.parametrize('failed_request', [False, True])
def test_cancel_during_inference_prevents_retry_next_batch_and_all_persistence(db, monkeypatch, failed_request):
    s = standard(db); s.controls = controls(16); db.commit()
    job = main.reserve_question_generation()
    provider = ai.HTTPProvider(url='https://synthetic.example', model='test')
    calls = []
    def request(operation, payload):
        calls.append(payload)
        assert main.cancel_question_generation(job['id'])['status'] == 'cancelled'
        if failed_request:
            raise status_error(502, 'invalid_json')
        return generated(payload['controls'])
    monkeypatch.setattr(provider, 'request', request)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(provider))
    with pytest.raises(HTTPException) as error:
        main.create_assessment(body(s, job), db)
    assert error.value.status_code == 409
    assert len(calls) == 1
    for model in (Assessment, Revision, Audit):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_cancel_can_be_requested_from_another_thread_while_inference_waits():
    job = jobs.reserve()
    entered, release = Event(), Event()
    outcomes = []
    def request(*args):
        entered.set()
        assert release.wait(5)
        return generated(controls(1))
    def worker():
        try:
            with jobs.generation(job['id']):
                generate_question_batches(request, controls(1), {})
        except jobs.QuestionGenerationCancelled:
            outcomes.append('cancelled')
    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        assert jobs.cancel(job['id'])['status'] == 'cancelled'
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert outcomes == ['cancelled']


def test_success_is_completed_and_late_cancel_does_not_claim_it_was_cancelled(db):
    s = standard(db)
    job = main.reserve_question_generation()
    created = main.create_assessment(body(s, job), db)
    assert created['id']
    assert main.cancel_question_generation(job['id'])['status'] == 'completed'
    with pytest.raises(HTTPException) as error:
        main.create_assessment(body(s, job), db)
    assert error.value.status_code == 409


def test_commit_guard_rechecks_cancellation_and_does_not_execute_save():
    job = jobs.reserve()
    with jobs.generation(job['id']):
        jobs.cancel(job['id'])
        with pytest.raises(jobs.QuestionGenerationCancelled):
            with jobs.commit_guard():
                pytest.fail('Cancelled job must not commit')


def test_unknown_job_is_not_silently_accepted():
    with pytest.raises(HTTPException) as error:
        main.cancel_question_generation('unknown')
    assert error.value.status_code == 404


def test_cancellation_during_plain_text_recovery_does_not_save(db, monkeypatch):
    s = standard(db); s.controls = controls(1); db.commit()
    job = main.reserve_question_generation()
    provider = ai.HTTPProvider(url='https://synthetic.example', model='test')
    calls = []
    def request(operation, payload):
        calls.append(payload)
        if not payload.get('question_text_only'):
            raise status_error(502, 'invalid_json')
        main.cancel_question_generation(job['id'])
        return 'Who reviews access?'
    monkeypatch.setattr(provider, 'request', request)
    monkeypatch.setattr(ai, 'provider', lambda:ai.ValidatedProvider(provider))
    with pytest.raises(HTTPException) as error:
        main.create_assessment(body(s, job), db)
    assert error.value.status_code == 409 and len(calls) == 3
    assert db.scalar(select(func.count()).select_from(Assessment)) == 0
