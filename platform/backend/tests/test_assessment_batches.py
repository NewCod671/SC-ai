import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app import ai, main
from app.assessment_batches import assess_batches
from app.models import Run, RunSummary
from app.schemas import RunInput, ReplaceAnswers
from .test_standard_skills import db, standard, assessment  # noqa: F401
import httpx


def pending_findings(operation, payload):
    assert operation == 'assess'
    return [{'control_id': c['id'], 'status': 'needs_review', 'interpreted_answer': 'Interpreted written answer',
             'rationale': 'Need linked evidence', 'recommendation': 'Supply evidence', 'evidence_ids': [],
             'question_findings':[{'question_id':q['question_id'],'question_text':q['question_text'],
                 'status':'needs_review','rationale':'Need linked evidence','recommendation':'Supply evidence','evidence_ids':[],
                 'audit':{'category':'needs_review','reason':'Need linked evidence','requirement_gap':'','system_impact':'unknown'}}
                 for q in c.get('assessment_questions',[])]} for c in payload['controls']]


def test_93_controls_with_three_written_answers_use_one_compact_batch_without_dropping_controls(monkeypatch):
    monkeypatch.delenv('AI_ASSESS_BATCH_SIZE', raising=False)
    controls = [{'id': f'C-{i}', 'requirement': 'Example requirement', 'department': 'IT'} for i in range(93)]
    payload = {'controls': controls, 'context': {}, 'answers': {c['id']: {'status': 'unanswered',
        'text': 'Written response' if i < 3 else ''} for i, c in enumerate(controls)}, 'evidence': []}
    calls = []
    def request(operation, batch):
        calls.append(batch)
        return pending_findings(operation, batch)
    findings, plan = assess_batches(request, payload, ai.validate_findings)
    assert len(calls) == plan['requests'] == 1
    assert [c['id'] for batch in calls for c in batch['controls']] == plan['ai_control_ids']
    assert len(calls[0]['controls']) == 3
    assert len(plan['unanswered_control_ids']) == 90
    assert [f['control_id'] for f in findings] == [c['id'] for c in controls]
    assert all(f['status'] == 'needs_review' and 'not sent to the model' in f['rationale'] for f in findings[3:])


def test_each_batch_scopes_questions_answers_evidence_and_policies(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '1')
    controls = [{'id': cid, 'requirement': 'Example', 'department': 'IT'} for cid in ('A', 'B')]
    payload = {'controls': controls, 'context': {}, 'answers': {cid: {'text': 'Done', 'status': 'unanswered'} for cid in ('A', 'B')},
               'evidence': [{'id': 'e', 'control_id': 'A', 'text': 'Record'}],
               'questions': [{'control_ids': ['A', 'B'], 'control_id': 'A', 'text': 'Shared question?'}],
               'policies': [{'id': 'p', 'text': 'Policy', 'control_ids': ['A']}]}
    calls = []
    def request(operation, batch):
        calls.append(batch)
        return pending_findings(operation, batch)
    _, plan = assess_batches(request, payload, ai.validate_findings)
    assert plan['requests'] == 2
    assert calls[1]['questions'][0]['control_ids'] == ['B']
    assert calls[1]['questions'][0]['control_id'] == 'B'
    assert calls[1]['evidence'] == calls[1]['policies'] == []
    assert set(calls[0]['answers']) == {'A'}


def test_later_batch_failure_does_not_persist_a_partial_run(db, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_FAILURE_MODE', 'strict')
    s = standard(db)
    a = assessment(db, s.id)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        c['id']: {'status': 'unanswered', 'text': 'Written answer'} for c in s.controls}), db)
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '1')
    delegate = ai.HTTPProvider(url='https://synthetic.example', model='test-model')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        if len(calls) == 2:
            raise ValueError('Invalid output from batch two')
        return pending_findings(operation, batch)
    monkeypatch.setattr(delegate, 'request', request)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))
    with pytest.raises(HTTPException) as error:
        main.run_assessment(a['id'], RunInput(), db)
    assert error.value.status_code == 502
    assert len(calls) == 2
    assert db.scalar(select(func.count()).select_from(Run)) == 0
    assert db.scalar(select(func.count()).select_from(RunSummary)) == 0


def test_no_written_answers_returns_actionable_422_without_calling_gpu(db, monkeypatch):
    a = assessment(db, standard(db).id)
    delegate = ai.HTTPProvider(url='https://synthetic.example', model='test-model')
    monkeypatch.setattr(delegate, 'request', lambda *a: pytest.fail('GPU must not be called for blank answers'))
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))
    with pytest.raises(HTTPException) as error:
        main.run_assessment(a['id'], RunInput(), db)
    assert error.value.status_code == 422


def test_success_persists_ai_provenance_and_exposes_analysis_plan(db, monkeypatch):
    s = standard(db)
    a = assessment(db, s.id)
    cid = s.controls[0]['id']
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        c['id']: {'status': 'unanswered', 'text': 'Written answer' if c['id'] == cid else ''}
        for c in s.controls}), db)
    delegate = ai.HTTPProvider(url='https://synthetic.example', model='test-model')
    monkeypatch.setattr(delegate, 'request', pending_findings)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))
    main.run_assessment(a['id'], RunInput(), db)
    run = db.scalar(select(Run))
    assert run.provider == 'http:test-model'
    assert run.skill['review_procedure'] == ai.REVIEW_PROCEDURE
    assert run.skill['assessment_plan']['ai_control_ids'] == [cid]
    evaluation = main.assessment_json(db, main.obj(db, main.Assessment, a['id']))['evaluation']
    assert evaluation['analysis_plan'] == run.skill['assessment_plan']
    assert not evaluation['stale']
    preview = main.preview_assessment(a['id'], None, 1, db)
    for control in s.controls[1:]:
        assert preview['rows'][control['row'] - 1][s.mapping['indices']['status'] - 1] == 'ยังไม่ตรวจ'


@pytest.mark.parametrize('kind, expected', [('http', 'gateway_http_502'), ('timeout', 'inference_timeout'),
                                          ('coverage', 'control_coverage_invalid'), ('schema', 'response_schema_invalid')])
def test_safe_diagnostics_distinguish_gateway_and_validation_errors(kind, expected, caplog, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_FAILURE_MODE', 'strict')
    payload = {'controls': [{'id': 'A', 'department': 'IT', 'requirement': 'Example'}],
               'context': {}, 'answers': {'A': {'text': 'private-answer', 'status': 'unanswered'}}, 'evidence': []}
    def request(operation, batch):
        if kind == 'http':
            raise httpx.HTTPStatusError('secret-token https://private-url',
                request=httpx.Request('POST', 'https://private-url'), response=httpx.Response(502))
        if kind == 'timeout':
            raise httpx.ReadTimeout('secret-token')
        if kind == 'coverage':
            return []
        return [{'control_id': 'A', 'status': 'private-status'}]
    with pytest.raises(HTTPException) as error:
        main.ai_call(assess_batches, request, payload, ai.validate_findings)
    assert error.value.status_code == 502
    assert expected in error.value.detail
    assert '1/1' in error.value.detail
    assert expected in caplog.text
    if kind == 'schema':
        assert 'assessment.schema_invalid' in caplog.text
        assert 'status' in caplog.text
    for value in ('private-answer', 'private-status', 'secret-token', 'private-url'):
        assert value not in caplog.text
        assert value not in error.value.detail
