import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app import ai, main
from app.assessment_batches import assess_batches, AssessmentStepError
from app.usage import safe_usage
from app.models import Run
from app.schemas import ReplaceAnswers, RunInput
from .test_standard_skills import db, standard, assessment  # noqa: F401



@pytest.fixture(autouse=True)
def strict_mode(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_FAILURE_MODE', 'strict')

def payload():
    controls = [{'id': cid, 'requirement': 'Review access', 'department': 'IT'} for cid in ('A', 'B', 'C')]
    return {'controls': controls, 'context': {},
            'answers': {c['id']: {'text': 'Access reviewed', 'status': 'unanswered'} for c in controls},
            'evidence': [{'id': 'e-a', 'control_id': 'A', 'text': 'Review record A'},
                         {'id': 'e-b', 'control_id': 'B', 'text': 'Review record B'}]}


def finding(cid, evidence_ids=(), status='met'):
    return {'control_id': cid, 'status': status, 'rationale': 'Review finding',
            'recommendation': 'Next action', 'evidence_ids': list(evidence_ids)}


def test_only_wrong_reference_is_reassessed_valid_peers_are_preserved(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '3')
    monkeypatch.setenv('AI_ASSESS_REPAIR_ATTEMPTS', '1')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        if len(calls) == 1:
            assert batch['allowed_evidence_ids'] == {'A': ['e-a'], 'B': ['e-b'], 'C': []}
            return [finding('A', ['e-a']), finding('B', ['e-a']), finding('C', [], 'gap')]
        assert [c['id'] for c in batch['controls']] == ['B']
        assert batch['allowed_evidence_ids'] == {'B': ['e-b']}
        assert batch['validation_feedback']['code'] == 'evidence_reference_invalid'
        return [finding('B', ['e-b'])]
    findings, plan = assess_batches(request, payload(), ai.validate_findings)
    assert [f['control_id'] for f in findings] == ['A', 'B', 'C']
    assert findings[0] == finding('A', ['e-a'])
    assert findings[1] == finding('B', ['e-b'])
    assert plan['requests'] == 2
    assert plan['batch_requests'] == plan['repair_requests'] == 1


def test_no_evidence_requires_empty_references_and_unsupported_pass_is_downgraded(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_REPAIR_ATTEMPTS', '1')
    data = payload()
    data['evidence'] = []
    calls = []
    def request(operation, batch):
        calls.append(batch)
        if len(calls) == 1:
            return [finding('A', ['invented']), finding('B', [], 'needs_review'), finding('C', [], 'gap')]
        assert batch['allowed_evidence_ids'] == {'A': []}
        return [finding('A', [])]
    findings, plan = assess_batches(request, data, ai.validate_findings)
    assert findings[0]['status'] == 'needs_review'
    assert findings[0]['evidence_ids'] == []
    assert plan['repair_requests'] == 1


def test_repair_is_bounded_and_does_not_silently_strip_invented_references(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_REPAIR_ATTEMPTS', '1')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        return [finding(c['id'], ['still-invented']) for c in batch['controls']]
    with pytest.raises(AssessmentStepError) as error:
        assess_batches(request, payload(), ai.validate_findings)
    assert error.value.code == 'evidence_reference_invalid'
    assert len(calls) == 2


def test_repairs_can_be_disabled_for_exact_benchmark_comparison(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_REPAIR_ATTEMPTS', '0')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        return [finding(c['id'], ['invented']) for c in batch['controls']]
    with pytest.raises(AssessmentStepError):
        assess_batches(request, payload(), ai.validate_findings)
    assert len(calls) == 1


def test_unknown_control_is_not_repaired_or_accepted(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_REPAIR_ATTEMPTS', '1')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        return [finding('unknown', ['invented'])]
    with pytest.raises(AssessmentStepError) as error:
        assess_batches(request, payload(), ai.validate_findings)
    assert error.value.code == 'control_coverage_invalid'
    assert len(calls) == 1


def test_repair_tokens_are_included_in_usage_total_and_saved_plan(db, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '3')
    monkeypatch.setenv('AI_ASSESS_REPAIR_ATTEMPTS', '1')
    s = standard(db)
    a = assessment(db, s.id)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        c['id']: {'status': 'unanswered', 'text': 'Access reviewed'} for c in s.controls}), db)
    delegate = ai.HTTPProvider(url='https://synthetic.example', model='test-model')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        delegate.usage_calls.append(safe_usage({'prompt_tokens': 100, 'completion_tokens': 20, 'generation_seconds': 2}))
        return [finding(c['id'], ['invented'] if len(calls) == 1 and c == batch['controls'][0] else [],
                        'needs_review') for c in batch['controls']]
    monkeypatch.setattr(delegate, 'request', request)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))
    result = main.run_assessment(a['id'], RunInput(), db)
    assert result['usage']['requests'] == 2
    assert result['usage']['total_tokens'] == 240
    assert result['skill']['assessment_plan']['repair_requests'] == 1
    assert db.scalar(select(func.count()).select_from(Run)) == 1
