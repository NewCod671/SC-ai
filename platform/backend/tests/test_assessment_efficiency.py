import json
import pytest
from app import ai
from app.assessment_batches import assess_batches, compact_batch


def payload():
    controls = [{'id': f'C-{i}', 'requirement': f'Requirement {i}', 'department': 'IT', 'row': i+2} for i in range(93)]
    return {'controls': controls, 'context': {'company': 'Example', 'business_type': 'SaaS',
            'it_systems': ['Production system'], 'question_language': 'th'},
            'answers': {c['id']: {'text': 'A substantive written response.', 'status': 'unanswered'} for c in controls},
            'evidence': [], 'policies': [], 'questions': [],
            'skill': {'id': 'skill-id', 'diff': 'historical changes '*100, 'core_instructions': 'Preserve scope and require evidence.',
                      'instructions': 'Review the answer carefully.', 'review_procedure': ai.REVIEW_PROCEDURE}}


def run(data, monkeypatch, size):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', str(size))
    calls = []
    def request(operation, batch):
        calls.append(batch)
        return [{'control_id': c['id'], 'status': 'needs_review', 'rationale': 'Need records',
                 'recommendation': 'Supply records', 'evidence_ids': []} for c in batch['controls']]
    findings, plan = assess_batches(request, data, ai.validate_findings)
    return findings, plan, calls


def test_93_control_efficiency_uses_31_calls_with_identical_validated_coverage(monkeypatch):
    data = payload()
    findings1, plan1, calls1 = run(data, monkeypatch, 1)
    findings3, plan3, calls3 = run(data, monkeypatch, 3)
    assert plan1['requests'] == 93
    assert plan3['requests'] == 31
    assert findings1 == findings3
    assert [c['id'] for b in calls3 for c in b['controls']] == [c['id'] for c in data['controls']]
    chars = lambda calls: sum(len(json.dumps(call, ensure_ascii=False, separators=(',', ':'))) for call in calls)
    assert chars(calls3) < chars(calls1)


def test_compaction_removes_only_bookkeeping_and_preserves_full_inputs():
    data = payload()
    data['controls'][0]['requirement'] = 'Full requirement '*2000
    data['answers']['C-0']['text'] = 'Full answer '*1500
    data['evidence'] = [{'id': 'e', 'control_id': 'C-0', 'text': 'Full evidence '*2000, 'sha256': 'hash', 'bytes': 30}]
    data['policies'] = [{'id': 'p', 'name': 'Policy', 'version': '1', 'text': 'Full policy '*2000,
                         'control_ids': ['C-0', 'C-92'], 'changes': {'diff': 'bookkeeping'}}]
    before = json.dumps(data, sort_keys=True)
    batch = compact_batch(data, data['controls'][:3])
    assert batch['controls'][0]['requirement'] == data['controls'][0]['requirement']
    assert batch['answers']['C-0'] == data['answers']['C-0']
    assert batch['evidence'][0]['text'] == data['evidence'][0]['text']
    assert batch['policies'][0]['text'] == data['policies'][0]['text']
    assert batch['policies'][0]['control_ids'] == ['C-0']
    assert 'row' not in batch['controls'][0]
    assert 'sha256' not in batch['evidence'][0]
    assert 'diff' not in batch['skill']
    assert batch['skill']['core_instructions'] == data['skill']['core_instructions']
    assert json.dumps(data, sort_keys=True) == before


def test_compaction_keeps_all_codex_skills_and_only_examples_for_batch_controls():
    data = payload()
    data['assessment_basis'] = 'sc_recorded_answers_v1'
    data['codex_agent_skills'] = {
        name: {'version': '1.0.0', 'sha256': name, 'instructions': f'{name} rules'}
        for name in ('iso27001-auditor', 'evidence-validator', 'finding-classifier',
                     'auditor-feedback', 'excel-report')}
    data['approved_examples'] = {'C-0': [{'approved_category': 'ofi'}],
                                 'C-1': [{'approved_category': 'nc_minor'}],
                                 'C-2': [{'approved_category': 'nc_major'}]}
    batch = compact_batch(data, data['controls'][:2])
    assert batch['codex_agent_skills'] == data['codex_agent_skills']
    assert batch['approved_examples'] == {'C-0': data['approved_examples']['C-0'],
                                          'C-1': data['approved_examples']['C-1']}
    assert 'C-2' not in batch['approved_examples']


def test_assessment_batch_preserves_user_question_source_for_answer_interpretation():
    data=payload()
    data['questions']=[{'id':'UQ-1','source':'user','control_ids':['C-0'],'text':'ทีมจัดการบัญชีอย่างไร?'}]
    batch=compact_batch(data,data['controls'][:1])
    assert batch['questions']==[{'id':'UQ-1','source':'user','control_ids':['C-0'],
                                 'control_id':'C-0','text':'ทีมจัดการบัญชีอย่างไร?','mapping_reason':''}]


@pytest.mark.parametrize('size', ['0', '9', 'invalid'])
def test_invalid_batch_size_does_not_call_model(monkeypatch, size):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', size)
    with pytest.raises(ValueError):
        assess_batches(lambda *args: pytest.fail('Unexpected inference'), payload(), ai.validate_findings)
