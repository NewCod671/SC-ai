import json
from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app import ai, main
from app.models import Assessment, Audit
from app.schemas import CreateAssessment
from app.questionnaire import text_question_batch_output, QuestionValidationError
from app.question_batches import generate_question_batches, QuestionBatchError
from .test_question_batches import controls
from .test_standard_skills import db, standard  # noqa: F401
from .test_inference_diagnostics import fake_gateway  # noqa: F401
from .test_gateway import gateway


def labeled(group):
    return '\n'.join(f'Q{i}: Who approves "admin" access for team {i}?' for i, _ in enumerate(group, 1))


def test_default_transport_avoids_json_in_every_request_and_keeps_batch_size(monkeypatch):
    monkeypatch.delenv('AI_QUESTION_OUTPUT_FORMAT')
    monkeypatch.setenv('AI_QUESTION_BATCH_SIZE', '5')
    seen = []
    def request(op, payload):
        seen.append(payload)
        if len(payload['controls']) == 1:
            assert payload['question_text_only'] is True and 'question_text_batch' not in payload
            return 'Who approves "admin" access?'
        assert payload['question_text_batch'] is True
        assert [c['question_index'] for c in payload['controls']] == list(range(1, len(payload['controls'])+1))
        return labeled(payload['controls'])
    result, plan = generate_question_batches(request, controls(123), {'company':'Example'})
    assert len(seen) == 25 and len(result) == 123
    assert plan['split_count'] == plan['format_retry_requests'] == 0
    assert plan['text_batch_requests'] == 25 and plan['output_format'] == 'text'
    assert [q['control_ids'][0] for q in result] == [c['id'] for c in controls(123)]
    assert '"admin"' in result[0]['text']
    assert main.health()['question_output_format'] == 'text'


def test_shared_questions_keep_explicit_control_mapping_departments_and_ai_reason():
    group = controls(3); group[1]['department'] = 'HR'
    result = text_question_batch_output('Q1,2: How do IT and HR review access?\nWHY: Shared joiner and leaver workflow\nQ3: Who approves exceptions?', group)
    assert len(result) == 2
    assert result[0]['control_ids'] == ['C-0','C-1']
    assert result[0]['departments'] == ['IT','HR']
    assert result[0]['mapping_reason'] == 'Shared joiner and leaver workflow'


@pytest.mark.parametrize('text', [
    'Q1: One?', 'Q1: One?\nQ1: Duplicate?\nQ2: Two?',
    'Q1,2: Shared without reason?', 'Q1: One?\nQ9: Unknown?',
    'Q1,1: Duplicate?\nWHY: Invalid\nQ2: Two?',
    '{"Q1":"One?","Q2":"Two?"}', '<think>private unfinished',
    'Introduction\nQ1: One?\nQ2: Two?', 'Q1: One?\nQ2:',
    'Q1: One?\nQ2: Two?\nSummary: private summary',
])
def test_invalid_text_never_silently_reassigns_omits_or_duplicates_controls(text):
    with pytest.raises(QuestionValidationError):
        text_question_batch_output(text, controls(2))


def test_text_generation_recovers_missing_coverage_with_smaller_text_batches(monkeypatch):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    seen=[]
    def request(op, payload):
        seen.append(payload)
        return labeled(payload['controls'][:1])
    result, plan = generate_question_batches(request, controls(4), {})
    assert len(result) == 4 and plan['split_count'] == 3
    assert all(p.get('question_text_batch') if len(p['controls']) > 1 else p.get('question_text_only') for p in seen)
    assert plan['single_text_requests'] == 4


def test_legacy_gateway_cannot_be_mistaken_for_success_or_trigger_a_retry_storm(monkeypatch):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    calls=[]
    def request(*args):
        calls.append(1)
        return []
    with pytest.raises(QuestionBatchError, match='qwen35-v9'):
        generate_question_batches(request, controls(5), {})
    assert len(calls) == 1


def test_gateway_text_batch_does_not_call_json_decoder_or_require_json_quotes(fake_gateway, monkeypatch):
    fake_gateway['text'] = 'Q1: Who approves "admin" access?\nQ2: Who reviews exceptions?'
    monkeypatch.setattr(gateway, 'extract_json', lambda *args:pytest.fail('Text questions must bypass JSON decode'))
    result = gateway.infer(gateway.Request(operation='questions', model='test-model', system=ai.TEXT_QUESTION_BATCH_INSTRUCTION,
        payload={'question_text_batch':True,'controls':controls(2)}), 'Bearer test-key')
    assert result['output'] == fake_gateway['text']
    assert gateway.health('Bearer test-key')['last_inference']['output_format'] == 'question_text_batch'
    assert 'Output JSON only' not in fake_gateway['messages'][0]['content']
    assert gateway.health('Bearer test-key')['question_text_batch_supported'] is True


def test_ollama_text_batch_disables_json_generation_constraints(monkeypatch):
    p = ai.OllamaProvider(model='synthetic-test')
    envelope = {'done':True,'message':{'content':labeled(controls(2))}}
    skill={'name':'question-generator','version':'1.0.0','sha256':'a'*64,
           'instructions':'Ask people what they actually do.'}
    with patch('app.ai.build_opener') as opener:
        opener.return_value.open.return_value = BytesIO(json.dumps(envelope).encode())
        result = p.request('questions', {'question_text_batch':True,'controls':controls(2),'context':{},'question_agent_skill':skill})
        sent = json.loads(opener.return_value.open.call_args.args[0].data)
    assert result == envelope['message']['content']
    assert 'format' not in sent and 'Return only JSON matching' not in sent['messages'][0]['content']
    assert 'Ask people what they actually do.' in sent['messages'][0]['content']
    assert 'question_agent_skill' not in sent['messages'][1]['content']


def test_complete_assessment_persists_text_generated_questions_and_audit(db, monkeypatch):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    s = standard(db); s.controls = controls(7); db.commit()
    p = ai.HTTPProvider(url='https://synthetic.example',model='test')
    monkeypatch.setattr(p,'request',lambda op,payload:labeled(payload['controls']))
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(p))
    result = main.create_assessment(CreateAssessment(name='Text questionnaire',standard_id=s.id,
        context={'company':'Example','business_type':'Retail','departments':['IT']}), db)
    assert len(result['questions']) == len(result['answers']) == 7
    saved = db.scalar(select(Audit).where(Audit.entity_id == result['id'],Audit.action == 'assessment.create'))
    assert saved.details['question_plan']['output_format'] == 'text'


def test_persistent_text_failure_is_bounded_and_saves_nothing(db, monkeypatch):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    s = standard(db); s.controls = controls(1); db.commit()
    p = ai.HTTPProvider(url='https://synthetic.example',model='test')
    seen=[]
    def request(*args):
        seen.append(1)
        return '<think>unfinished'
    monkeypatch.setattr(p,'request',request)
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(p))
    with pytest.raises(HTTPException):
        main.create_assessment(CreateAssessment(name='Invalid text',standard_id=s.id,
            context={'company':'Example','business_type':'Retail','departments':['IT']}), db)
    assert len(seen) == 2
    assert db.scalar(select(func.count()).select_from(Assessment)) == 0


def test_cancellation_in_default_text_mode_stops_before_another_batch_or_save(db, monkeypatch):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    monkeypatch.setenv('AI_QUESTION_BATCH_SIZE','5')
    s = standard(db); s.controls = controls(12); db.commit()
    job = main.reserve_question_generation()
    p = ai.HTTPProvider(url='https://synthetic.example',model='test')
    seen=[]
    def request(operation,payload):
        seen.append(payload)
        main.cancel_question_generation(job['id'])
        return labeled(payload['controls'])
    monkeypatch.setattr(p,'request',request)
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(p))
    with pytest.raises(HTTPException) as error:
        main.create_assessment(CreateAssessment(name='Cancelled text',standard_id=s.id,generation_id=job['id'],
            context={'company':'Example','business_type':'Retail','departments':['IT']}), db)
    assert error.value.status_code == 409 and len(seen) == 1
    assert db.scalar(select(func.count()).select_from(Assessment)) == 0


def test_quotes_backslashes_and_unicode_are_encoded_by_python_not_the_model():
    wording = 'Q1: Who approves "admin" access to C:\\files and ข้อมูลลูกค้า?'
    result = text_question_batch_output(wording, controls(1))
    envelope = json.dumps({'questions':result}, ensure_ascii=False)
    assert json.loads(envelope)['questions'][0]['text'] == wording[4:]


@pytest.mark.parametrize('prefix', ['', 'Q8: ', 'Q1: ', '1. ', '8) '])
def test_single_control_never_needs_a_local_index_and_strips_cosmetic_numbering(monkeypatch, prefix):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    group = [{'id':'8.1','requirement':'Plan operational controls','department':'IT'}]
    seen=[]
    def request(operation,payload):
        seen.append(payload)
        assert payload['question_text_only'] is True
        assert 'question_text_batch' not in payload
        assert 'question_index' not in payload['controls'][0]
        return prefix+'How do you approve operational changes?'
    result,plan=generate_question_batches(request,group,{})
    assert len(seen)==1 and plan['single_text_requests']==1
    assert plan['format_retry_requests']==0 and plan['text_fallback_requests']==0
    assert result[0]['control_ids']==['8.1']
    assert result[0]['text']=='How do you approve operational changes?'


def test_single_text_retries_once_after_empty_model_response_then_recovers(monkeypatch):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    seen=[]
    def request(op,payload):
        seen.append(payload)
        return '' if len(seen)==1 else 'Who reviews changes?'
    result,plan=generate_question_batches(request,controls(1),{})
    assert result[0]['control_ids']==['C-0']
    assert len(seen)==2 and all(p['question_text_only'] for p in seen)
    assert plan['format_retry_requests']==1


def test_invalid_single_text_reason_does_not_log_model_text(monkeypatch,caplog):
    monkeypatch.setenv('AI_QUESTION_OUTPUT_FORMAT','text')
    with pytest.raises(QuestionBatchError):
        generate_question_batches(lambda *args:'<think>private-company-reasoning',controls(1),{})
    assert 'reason=unfinished_reasoning' in caplog.text
    assert 'private-company-reasoning' not in caplog.text


@pytest.mark.parametrize('text', ['Q1: One?\nQ2: Two?', 'Q1,2: Shared?'])
def test_single_control_does_not_absorb_multiple_mapping_labels(text):
    from app.questionnaire import plain_question_output
    with pytest.raises(QuestionValidationError):
        plain_question_output(text,controls(1))
