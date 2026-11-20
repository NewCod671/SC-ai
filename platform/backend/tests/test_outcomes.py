import hashlib
from types import SimpleNamespace
import pytest
from app import ai, main, outcomes
from app.models import Evidence, Run, RunSummary
from app.schemas import ReplaceAnswers, RunInput
from .test_standard_skills import db, standard, assessment  # noqa: F401


@pytest.mark.parametrize('states, expected', [
    (['met', 'met'], 'passed'),
    (['met', 'not_applicable'], 'passed'),
    (['met', 'partial'], 'partial'),
    (['partial', 'partial'], 'partial'),
    (['met', 'gap'], 'failed'),
    (['gap', 'needs_review'], 'failed'),
    (['partial', 'needs_review'], 'needs_review'),
    (['met', 'needs_review'], 'needs_review'),
    (['not_applicable', 'not_applicable'], 'not_applicable'),
])
def test_overall_outcome_uses_all_applicable_controls_not_an_average(states, expected):
    standard = SimpleNamespace(id='s', name='Example standard', version='1', controls=[{'id': 'A'}, {'id': 'B'}])
    findings = [{'control_id': cid, 'status': state} for cid, state in zip(('A', 'B'), states)]
    result = outcomes.summarize(standard, findings)
    assert result['status'] == expected
    assert sum(result['counts'].values()) == 2
    assert result['rule']['certification'] is False
    if not result['applicable_controls']:
        assert result['met_percent'] is None
    if 'gap' in states:
        assert result['status'] != 'passed'


@pytest.mark.parametrize('findings', [[], [{'control_id': 'A', 'status': 'met'}],
    [{'control_id': 'A', 'status': 'met'}, {'control_id': 'A', 'status': 'met'}],
    [{'control_id': 'A', 'status': 'met'}, {'control_id': 'unknown', 'status': 'met'}]])
def test_incomplete_or_duplicate_controls_cannot_produce_a_pass(findings):
    s = SimpleNamespace(id='s', name='Example', version='1', controls=[{'id': 'A'}, {'id': 'B'}])
    with pytest.raises(ValueError):
        outcomes.summarize(s, findings)


@pytest.mark.parametrize('answer, evidence, cited, expected', [
    ({'status': 'yes', 'text': 'Implemented'}, [{'id': 'e', 'control_id': 'A', 'text': 'Review records'}], ['e'], 'met'),
    ({'status': 'yes', 'text': 'Implemented'}, [], [], 'needs_review'),
    ({'status': 'yes', 'text': 'Implemented'}, [{'id': 'e', 'control_id': 'A', 'text': ''}], ['e'], 'needs_review'),
    ({'status': 'unanswered', 'text': 'Implemented'}, [{'id': 'e', 'control_id': 'A', 'text': 'Records'}], ['e'], 'met'),
    ({'status': 'yes', 'text': ''}, [{'id': 'e', 'control_id': 'A', 'text': 'Records'}], ['e'], 'needs_review'),
])
def test_passing_finding_requires_implementation_and_readable_linked_evidence(answer, evidence, cited, expected):
    findings = [{'control_id': 'A', 'status': 'met', 'rationale': 'Supported', 'recommendation': 'Review periodically', 'evidence_ids': cited}]
    result = ai.validate_findings(findings, [{'id': 'A'}], evidence, {'A': answer})
    assert result[0]['status'] == expected
    if expected == 'needs_review':
        assert 'Verification required' in result[0]['rationale']


def test_inapplicability_requires_an_explanation_and_evidence_cannot_cross_controls():
    finding = {'control_id': 'A', 'status': 'not_applicable', 'rationale': 'Not relevant', 'recommendation': '', 'evidence_ids': []}
    assert ai.validate_findings([finding], [{'id': 'A'}], [], {'A': {'status': 'na', 'text': ''}})[0]['status'] == 'needs_review'
    assert ai.validate_findings([finding], [{'id': 'A'}], [], {'A': {'status': 'na', 'text': 'This activity is outside this business scope'}})[0]['status'] == 'not_applicable'
    with pytest.raises(ValueError):
        ai.validate_findings([{**finding, 'status': 'met', 'evidence_ids': ['e']}], [{'id': 'A'}],
                             [{'id': 'e', 'control_id': 'B', 'text': 'Wrong control'}], {'A': {'status': 'yes', 'text': 'Done'}})


def implemented_assessment(db, s):
    a = assessment(db, s.id)
    answers = {c['id']: {'status': 'yes', 'text': 'Implementation described with named owner and review process.'} for c in s.controls}
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers=answers), db)
    for c in s.controls:
        content = b'Readable control-specific implementation records'
        db.add(Evidence(assessment_id=a['id'], control_id=c['id'], filename='record.txt', content=content,
                        text=content.decode(), sha256=hashlib.sha256(content).hexdigest()))
    db.commit()
    return a, answers


def met_findings(self, payload):
    return [{'control_id': c['id'], 'status': 'met', 'rationale': 'Implementation supported by cited records',
             'recommendation': 'Continue periodic reviews',
             'audit': {'category':'comply','reason':'Complete implementation supported by records',
                       'requirement_gap':'','system_impact':'does_not_affect'},
             'evidence_ids': [e['id'] for e in payload['evidence'] if e['control_id'] == c['id']]} for c in payload['controls']]


def test_summary_is_persisted_per_standard_and_becomes_stale_after_answer_change(db, monkeypatch):
    s = standard(db)
    a, answers = implemented_assessment(db, s)
    monkeypatch.setattr(ai.OfflineProvider, 'assess', met_findings)
    run = main.run_assessment(a['id'], RunInput(), db)
    assert run['summary']['status'] == 'passed'
    assert run['summary']['standard_id'] == s.id
    assert run['summary']['met_percent'] == 100
    snapshot = db.get(RunSummary, run['id']).data
    monkeypatch.setattr(outcomes, 'RULE_VERSION', 'future-rule')
    assert main.runs(a['id'], db)[0]['summary']['rule']['version'] == snapshot['rule']['version']
    assert main.get_assessment(a['id'], db)['evaluation']['status'] == 'passed'
    main.save_answers(a['id'], ReplaceAnswers(expected_version=2, answers={
        **answers, s.controls[0]['id']: {'status': 'no', 'text': 'Implementation removed'}}), db)
    evaluation = main.get_assessment(a['id'], db)['evaluation']
    assert evaluation['status'] == 'stale'
    assert evaluation['summary']['status'] == 'passed'  # explicitly historical, not the current status
    assert evaluation['stale']
    other = assessment(db, standard(db, '2').id)
    assert main.get_assessment(other['id'], db)['evaluation'] is None


def test_offline_does_not_promote_yes_answers_to_evidence_verified_pass(db):
    a, _ = implemented_assessment(db, standard(db))
    run = main.run_assessment(a['id'], RunInput(), db)
    assert run['summary']['status'] == 'needs_review'
    assert run['summary']['counts']['met'] == 0


def test_answer_change_during_inference_cannot_be_labeled_as_current_pass(db, monkeypatch):
    s = standard(db)
    a, answers = implemented_assessment(db, s)
    def changed_while_running(self, payload):
        main.save_answers(a['id'], ReplaceAnswers(expected_version=2, answers={
            cid: {'status': 'no', 'text': 'Removed during inference'} for cid in answers}), db)
        return met_findings(self, payload)
    monkeypatch.setattr(ai.OfflineProvider, 'assess', changed_while_running)
    run = main.run_assessment(a['id'], RunInput(), db)
    assert run['revision'] == 2
    assert run['stale']
    assert main.get_assessment(a['id'], db)['evaluation']['status'] == 'stale'


def test_legacy_runs_get_labeled_read_time_summaries_without_writing_history(db):
    a, _ = implemented_assessment(db, standard(db))
    run = main.run_assessment(a['id'], RunInput(), db)
    db.delete(db.get(RunSummary, run['id']))
    db.commit()
    historical = main.runs(a['id'], db)[0]
    assert historical['summary_source'] == 'derived_legacy'
    assert historical['summary']['status'] == 'needs_review'
    assert db.get(RunSummary, run['id']) is None


def test_late_stale_run_does_not_hide_an_available_current_result(db, monkeypatch):
    s = standard(db)
    a, answers = implemented_assessment(db, s)
    monkeypatch.setattr(ai.OfflineProvider, 'assess', met_findings)
    older = main.run_assessment(a['id'], RunInput(), db)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=2, answers=answers), db)
    current = main.run_assessment(a['id'], RunInput(), db)
    delayed = Run(assessment_id=a['id'], revision=2, provider='delayed-test',
                  skill=older['skill'], results=older['results'])
    db.add(delayed)
    db.commit()
    result = main.get_assessment(a['id'], db)['evaluation']
    assert result['run_id'] == current['id']
    assert result['status'] == 'passed'


@pytest.mark.parametrize('source_status, text, ai_status, overall', [
    ('unanswered', 'BKUP daily; restore never tested', 'partial', 'partial'),
    ('yes', 'No access review has ever been performed', 'gap', 'failed'),
])
def test_ai_interpretation_is_kept_and_result_is_not_copied_from_dropdown(db, monkeypatch, source_status, text, ai_status, overall):
    s = standard(db)
    a = assessment(db, s.id)
    answers = {c['id']: {'status': source_status, 'text': text} for c in s.controls}
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers=answers), db)
    captured = []
    def interpretation(self, payload):
        captured.append(payload)
        return [{'control_id': c['id'], 'status': ai_status, 'interpreted_answer': 'ความหมายจากคำตอบที่ผู้ใช้กรอก',
                 'rationale': 'เทียบเนื้อหาคำตอบกับข้อกำหนดแล้ว ยังมีส่วนที่ไม่ได้ดำเนินการ',
                 'recommendation': 'ดำเนินการส่วนที่ยังขาด', 'evidence_ids': []} for c in payload['controls']]
    monkeypatch.setattr(ai.OfflineProvider, 'assess', interpretation)
    run = main.run_assessment(a['id'], RunInput(), db)
    assert captured[0]['answers'][s.controls[0]['id']]['text'] == text
    # Requirements/IDs stay pinned to the standard; responsibility is scoped
    # to the company's entered departments rather than the template's labels.
    assert [{k:v for k,v in c.items() if k != 'department'} for c in captured[0]['controls']] == [
        {k:v for k,v in c.items() if k != 'department'} for c in s.controls]
    assert all(set(c['department'].split(' / ')) <= set(a['context']['departments']) for c in captured[0]['controls'])
    assert run['summary']['status'] == overall
    assert run['results'][0]['interpreted_answer'] == 'ความหมายจากคำตอบที่ผู้ใช้กรอก'
    assert main.get_assessment(a['id'], db)['answers'][s.controls[0]['id']]['status'] == source_status


def test_written_answer_with_evidence_can_pass_without_selecting_yes(db, monkeypatch):
    s = standard(db)
    a, answers = implemented_assessment(db, s)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=2, answers={
        cid: {**answer, 'status': 'unanswered'} for cid, answer in answers.items()}), db)
    monkeypatch.setattr(ai.OfflineProvider, 'assess', met_findings)
    assert main.run_assessment(a['id'], RunInput(), db)['summary']['status'] == 'passed'


def test_na_can_be_inferred_from_written_scope_and_missing_text_is_not_a_gap():
    finding = {'control_id': 'A', 'status': 'not_applicable', 'rationale': 'The described business scope excludes this activity',
               'recommendation': '', 'evidence_ids': []}
    answers = {'A': {'status': 'unanswered', 'text': 'Our company does not perform this activity; this process is outside our scope.'}}
    assert ai.validate_findings([finding], [{'id': 'A'}], [], answers)[0]['status'] == 'not_applicable'
    blank = {'A': {'status': 'no', 'text': ''}}
    assert ai.validate_findings([{**finding, 'status': 'gap'}], [{'id': 'A'}], [], blank)[0]['status'] == 'needs_review'


def test_result_provenance_reports_actual_run_provider_even_after_configuration_changes(db, monkeypatch):
    a = assessment(db, standard(db).id)
    run = main.run_assessment(a['id'], RunInput(), db)
    assert run['provider'].startswith('offline')
    monkeypatch.setenv('AI_PROVIDER', 'http')
    assert main.health()['ai_provider'] == 'http'
    # Merely configuring Colab cannot retroactively turn a fixed-rule run into an AI run.
    assert main.get_assessment(a['id'], db)['evaluation']['provider'] == run['provider']
