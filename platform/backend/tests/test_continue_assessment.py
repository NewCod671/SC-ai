import json
import pytest
import httpx
from sqlalchemy import select, func
from app import ai, main
from app.assessment_batches import assess_batches
from app.models import Run, RunSummary
from app.schemas import ReplaceAnswers, RunInput
from app.usage import safe_usage
from .test_standard_skills import db, standard, assessment  # noqa: F401


def payload(n=93):
    return {'controls': [{'id': f'C-{i}', 'requirement': 'Review access', 'department': 'IT'} for i in range(n)],
            'context': {'question_language': 'th'}, 'answers': {f'C-{i}': {'text': 'Written answer', 'status': 'unanswered'}
                                                           for i in range(n)}, 'evidence': []}


def findings(batch):
    return [{'control_id': c['id'], 'status': 'gap', 'rationale': 'Not implemented',
             'recommendation': 'Implement process', 'evidence_ids': []} for c in batch['controls']]


@pytest.fixture(autouse=True)
def continue_defaults(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_COVERAGE_REPAIR_ATTEMPTS','0')
    monkeypatch.setenv('AI_ASSESS_TRANSIENT_RETRIES','0')
    for name in ('AI_ASSESS_BATCH_SIZE', 'AI_ASSESS_FAILURE_MODE', 'AI_ASSESS_REPAIR_ATTEMPTS'):
        monkeypatch.delenv(name, raising=False)


def test_93_controls_finish_in_19_calls_even_after_late_gateway_failure():
    data = payload()
    calls = []
    def request(operation, batch):
        calls.append(batch)
        if len(calls) == 10:
            raise httpx.ReadTimeout('private-url secret-token')
        return findings(batch)
    result, plan = assess_batches(request, data, ai.validate_findings)
    assert len(calls) == plan['requests'] == 19
    assert plan['batch_size'] == 5
    assert len(calls[-1]['controls']) == 3
    assert len(result) == 93
    assert len(plan['ai_control_ids']) == 88
    assert plan['failed_control_ids'] == [f'C-{i}' for i in range(45, 50)]
    assert all(f['status'] == 'needs_review' for f in result[45:50])
    assert result[-1]['status'] == 'gap'
    assert plan['completion_status'] == 'completed_with_warnings'
    assert 'secret-token' not in json.dumps(plan)


@pytest.mark.parametrize('kind', ['schema', 'reference', 'duplicate', 'missing'])
def test_bad_item_does_not_discard_valid_peers_or_repeat_calls(kind):
    calls = []
    def request(operation, batch):
        calls.append(batch)
        result = findings(batch)
        if kind == 'schema': result[2]['status'] = 'invalid'
        elif kind == 'reference': result[2]['evidence_ids'] = ['invented']
        elif kind == 'duplicate': result.append(dict(result[2]))
        else: result.pop(2)
        return result
    result, plan = assess_batches(request, payload(5), ai.validate_findings)
    assert len(calls) == 1
    assert plan['failed_control_ids'] == ['C-2']
    assert len(plan['ai_control_ids']) == 4
    assert result[2]['status'] == 'needs_review'
    assert result[2]['evidence_ids'] == []
    assert all(f['status'] == 'gap' for i, f in enumerate(result) if i != 2)


def test_all_gateway_failures_finish_without_claiming_any_pass_or_leaking_details():
    def request(*args):
        raise httpx.HTTPStatusError('secret-response', request=httpx.Request('POST', 'https://private-url'),
                                    response=httpx.Response(401))
    result, plan = assess_batches(request, payload(11), ai.validate_findings)
    assert plan['requests'] == 3
    assert len(plan['failed_control_ids']) == 11
    assert plan['ai_control_ids'] == []
    assert all(f['status'] == 'needs_review' for f in result)
    assert 'private-url' not in json.dumps(plan)
    assert 'secret-response' not in json.dumps(plan)


def test_extra_unknown_findings_are_warned_and_never_saved():
    def request(operation, batch):
        return findings(batch) + [{'control_id': 'unknown', 'status': 'met', 'rationale': 'Invented'}]
    result, plan = assess_batches(request, payload(5), ai.validate_findings)
    assert len(result) == 5
    assert all(f['control_id'] != 'unknown' for f in result)
    assert plan['failed_control_ids'] == []
    assert plan['warnings'][0]['code'] == 'unexpected_findings_ignored'


def test_partial_run_is_saved_and_excel_marks_failure_unchecked_with_warning(db, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '1')
    s = standard(db)
    a = assessment(db, s.id)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        c['id']: {'status': 'unanswered', 'text': 'Written answer'} for c in s.controls}), db)
    delegate = ai.HTTPProvider(url='https://synthetic.example', model='test-model')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        if len(calls) == 2:
            raise httpx.ReadTimeout('private-error')
        delegate.usage_calls.append(safe_usage({'prompt_tokens': 100, 'completion_tokens': 20, 'generation_seconds': 1}))
        return findings(batch)
    monkeypatch.setattr(delegate, 'request', request)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))
    result = main.run_assessment(a['id'], RunInput(), db)
    plan = result['skill']['assessment_plan']
    assert plan['completion_status'] == 'completed_with_warnings'
    assert plan['failed_control_ids'] == [s.controls[1]['id']]
    assert db.scalar(select(func.count()).select_from(Run)) == 1
    assert db.scalar(select(func.count()).select_from(RunSummary)) == 1
    assert result['summary']['status'] != 'passed'
    assert result['usage']['requests'] == len(calls)
    assert result['usage']['unmeasured_requests'] == 1
    assert result['usage']['total_tokens'] is None
    assert result['usage']['measured_total_tokens'] == 120
    preview = main.preview_assessment(a['id'], None, 1, db)
    row = preview['rows'][s.controls[1]['row'] - 1]
    assert row[s.mapping['indices']['status'] - 1] == 'ยังไม่ตรวจ'
    assert 'inference_timeout' in row[s.mapping['indices']['result'] - 1]
    evaluation = main.get_assessment(a['id'], db)['evaluation']
    assert evaluation['analysis_plan']['warnings'] == plan['warnings']
