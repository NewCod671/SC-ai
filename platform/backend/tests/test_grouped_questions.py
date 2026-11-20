from copy import deepcopy
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app import ai, main
from app.reporting import excel
from app.models import Assessment
from app.questionnaire import normalize_questions, text_by_control, clean_question_text
from app.schemas import CreateAssessment, RunInput, ReplaceAnswers
from .fixtures import workbook
from .test_standard_skills import db, standard  # noqa: F401; socket-free database fixture

CONTROLS = [
    {'id': 'SC-A', 'requirement': 'Review access permissions quarterly', 'department': 'IT'},
    {'id': 'SC-B', 'requirement': 'Review access permissions quarterly', 'department': 'HR'},
]
CONTEXT = {'company': 'Example', 'business_type': 'Retail', 'departments': ['IT', 'HR'],
           'it_systems': ['ERP'], 'data_types': ['PII']}
QUESTION = {'control_ids': ['SC-A', 'SC-B'], 'departments': ['IT', 'HR'],
            'text': 'When someone changes roles, who checks their access? Tell us what each team does.',
            'mapping_reason': 'Both controls ask about the same access review process; team differences must be captured.'}


def test_one_human_question_covers_multiple_controls_and_departments():
    questions = normalize_questions([QUESTION], CONTROLS)
    assert len(questions) == 1
    assert questions[0]['control_ids'] == ['SC-A', 'SC-B']
    assert questions[0]['departments'] == ['IT', 'HR']
    assert text_by_control(questions) == {'SC-A': QUESTION['text'], 'SC-B': QUESTION['text']}
    assert normalize_questions(questions, CONTROLS) == questions
    assert normalize_questions([{**QUESTION, 'text': 'Reworded for people'}], CONTROLS)[0]['id'] == questions[0]['id']


@pytest.mark.parametrize('bad', [
    [{**QUESTION, 'control_ids': ['SC-A']}],
    [{**QUESTION, 'control_ids': ['SC-A', 'unknown']}],
    [{**QUESTION, 'control_ids': ['SC-A', 'SC-A', 'SC-B']}],
    [{**QUESTION, 'mapping_reason': ''}],
    [{**QUESTION, 'departments': []}],
    [{**QUESTION, 'text': ' '}],
    [QUESTION, {**QUESTION, 'control_ids': ['SC-A']}],
])
def test_missing_duplicate_or_unsupported_group_mappings_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_questions(bad, CONTROLS)


def test_legacy_questions_remain_valid_and_offline_groups_only_identical_requirements():
    legacy = [{'control_id': c['id'], 'department': c['department'], 'text': 'What happens in practice?'} for c in CONTROLS]
    assert len(normalize_questions(legacy, CONTROLS)) == 2
    assert len(ai.OfflineProvider().questions(CONTROLS, CONTEXT)) == 1
    different = [CONTROLS[0], {**CONTROLS[1], 'requirement': 'Approve access before granting it'}]
    assert len(ai.OfflineProvider().questions(different, CONTEXT)) == 2
    thai = ai.OfflineProvider().questions(CONTROLS, {**CONTEXT, 'question_language': 'th'})
    assert 'ในงานจริง' in thai[0]['text']


def proposals(source):
    return {'mappings': [
        {'control_id': 'SC-A', 'source_texts': [source], 'answer_text': 'IT reviews access.',
         'suggested_status': 'yes', 'confidence': .8, 'reason': 'IT explicitly reports review.'},
        {'control_id': 'SC-B', 'source_texts': [source], 'answer_text': 'HR plans access reviews.',
         'suggested_status': 'partial', 'confidence': .8, 'reason': 'HR reports planned work only.'}],
        'unmatched': []}


def test_shared_answer_can_map_to_multiple_controls_with_different_department_statuses():
    source = 'IT: review active; HR: planned'
    result = ai.validate_answer_mapping(proposals(source), source, CONTROLS, [QUESTION])
    assert [m['suggested_status'] for m in result['mappings']] == ['yes', 'partial']
    separate = [{'control_id': c['id'], 'department': c['department'], 'text': 'Separate question'} for c in CONTROLS]
    with pytest.raises(ValueError):
        ai.validate_answer_mapping(proposals(source), source, CONTROLS, separate)


def test_shared_answer_cannot_drop_invent_or_duplicate_source_occurrences():
    source = 'Shared process'
    for transform in ('invent', 'drop', 'duplicate', 'both'):
        result = proposals(source)
        if transform == 'invent':
            result['mappings'][0]['source_texts'] = ['Invented']
        elif transform == 'drop':
            source_text = source+'\nAnother answer'
        elif transform == 'duplicate':
            result['mappings'][0]['source_texts'].append(source)
        else:
            result['unmatched'] = [{'source_text': source, 'reason': 'Unclear'}]
        with pytest.raises(ValueError):
            ai.validate_answer_mapping(result, source_text if transform == 'drop' else source, CONTROLS, [QUESTION])
    repeated = proposals(source)
    for mapping in repeated['mappings']:
        mapping['source_texts'] *= 2
    assert ai.validate_answer_mapping(repeated, source+'\n'+source, CONTROLS, [QUESTION])


def test_excel_repeats_shared_question_but_preserves_individual_answers_and_layout():
    template = workbook([(c['id'], c['requirement'], c['department']) for c in CONTROLS])
    controls, mapping = excel.parse_standard(template, {'columns': {'question': 'Question'}})
    answers = {'SC-A': {'status': 'yes', 'text': 'IT implemented'}, 'SC-B': {'status': 'no', 'text': 'HR not implemented'}}
    manifest = {'assessment_id': 'a', 'standard_id': 's', 'version': 1}
    exported = excel.export(template, mapping, controls, answers, manifest, questions=[QUESTION])
    book = excel.read(exported)
    assert book['Checklist']['G2'].value == book['Checklist']['G3'].value == QUESTION['text']
    assert book['Reference']['A1'].value == '=SUM(1,2)'
    assert excel.import_answers(exported, mapping, controls, manifest) == answers


def test_grouped_questions_persist_and_each_control_is_still_assessed(db, monkeypatch):
    s = standard(db)
    monkeypatch.setattr(ai.OfflineProvider, 'questions', lambda *args: [deepcopy(QUESTION)])
    a = main.create_assessment(CreateAssessment(name='Interview', standard_id=s.id, context=CONTEXT), db)
    assert len(a['questions']) == 1
    assert set(a['answers']) == {'SC-A', 'SC-B'}
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        'SC-A': {'status': 'yes', 'text': 'IT done'}, 'SC-B': {'status': 'partial', 'text': 'HR planned'}}), db)
    captured = []
    original = ai.OfflineProvider.assess
    def assess(self, payload):
        captured.append(payload)
        return original(self, payload)
    monkeypatch.setattr(ai.OfflineProvider, 'assess', assess)
    run = main.run_assessment(a['id'], RunInput(), db)
    assert len(captured[0]['questions']) == 1
    assert len(run['results']) == 2
    assert main.get_assessment(a['id'], db)['answers']['SC-B']['status'] == 'partial'


def test_incomplete_grouped_generation_never_saves_assessment(db, monkeypatch):
    s = standard(db)
    monkeypatch.setattr(ai.OfflineProvider, 'questions', lambda *args: [{**QUESTION, 'control_ids': ['SC-A']}])
    with pytest.raises(HTTPException) as error:
        main.create_assessment(CreateAssessment(name='Bad', standard_id=s.id, context=CONTEXT), db)
    assert error.value.status_code == 502
    assert db.scalar(select(func.count()).select_from(Assessment)) == 0


def test_legacy_context_appendices_are_removed_without_removing_relevant_system_names():
    question = 'How do you remove Entra ID access when someone leaves?'
    clutter = (' Describe the owner, process, review frequency and evidence for IT.'
               ' Consider systems: AWS production, Microsoft 365 / Entra ID; data: Employee records.'
               ' Reference existing policies: Access policy v1.0.')
    assert clean_question_text(question + clutter) == question
    assert clean_question_text(question + ' Reference existing policies: Access v1.0.') == question
    assert clean_question_text(question) == question
    follow_up = question + ' Describe the owner, process, review frequency and evidence for IT. Who checks exceptions?'
    assert clean_question_text(follow_up) == follow_up


def test_new_generated_question_text_is_stored_without_inventory_appendices():
    result = normalize_questions([{**QUESTION, 'text': QUESTION['text'] + ' Consider systems: AWS; data: PII.'}], CONTROLS)
    assert result[0]['text'] == QUESTION['text']
    with pytest.raises(ValueError):
        normalize_questions([{**QUESTION, 'text': 'Consider systems: AWS; data: PII.'}], CONTROLS)


def test_existing_question_display_and_excel_clean_text_without_rewriting_saved_records(db):
    s = standard(db)
    a = main.create_assessment(CreateAssessment(name='Old review', standard_id=s.id, context=CONTEXT), db)
    record = db.get(Assessment, a['id'])
    original = [{'control_id': c['id'], 'department': c['department'],
                 'text': 'Who reviews access? Consider systems: AWS; data: PII. Reference existing policies: Access v1.'}
                for c in s.controls]
    record.questions = deepcopy(original)
    db.commit()
    shown = main.get_assessment(record.id, db)
    assert all(q['text'] == 'Who reviews access?' for q in shown['questions'])
    assert record.questions == original
    assert shown['context']['it_systems'] == ['ERP']
    mapping = {**s.mapping, 'indices': {**s.mapping['indices'], 'question': 7}}
    exported = excel.export(s.template, mapping, s.controls, shown['answers'],
                            {'assessment_id': record.id, 'standard_id': s.id, 'version': 1}, questions=record.questions)
    assert excel.read(exported)['Checklist']['G2'].value == 'Who reviews access?'
