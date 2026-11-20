from copy import deepcopy
import json
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app import ai, main
from app.models import Assessment, Revision, Audit
from app.schemas import CreateAssessment
from app.question_batches import generate_question_batches, QuestionBatchError
from app.skill_templates import template_skill
from app.questionnaire import normalize_questions, QuestionValidationError
from .test_standard_skills import db, standard  # noqa: F401


def controls(n=93):
    return [{'id':f'C-{i}','requirement':'Review access','department':'IT'} for i in range(n)]


def generated(group):
    return [{'control_ids':[c['id']],'departments':['IT'],'text':'Who reviews access?','mapping_reason':''} for c in group]


@pytest.mark.parametrize('wrapper', ['questions', 'singleton'])
def test_complete_question_containers_are_normalized_without_another_model_call(wrapper):
    calls = []
    def request(op, payload):
        calls.append(1)
        output = generated(payload['controls'])
        return {'questions':output} if wrapper == 'questions' else output[0]
    result, plan = generate_question_batches(request, controls(1), {})
    assert len(result) == 1 and len(calls) == 1
    assert plan['split_count'] == 0


def test_extra_followups_for_identical_controls_preserve_all_text_without_retry():
    def request(op, payload):
        first = generated(payload['controls'])
        return first + [{**q, 'text':'Can you give a recent example?'} for q in first]
    result, plan = generate_question_batches(request, controls(2), {})
    normalized = normalize_questions(result, controls(2))
    assert len(normalized) == 2 and plan['requests'] == 1
    assert all('Who reviews access?' in q['text'] and 'recent example?' in q['text'] for q in normalized)


def test_overlapping_different_control_sets_are_never_merged_into_false_coverage():
    from app.question_batches import question_output
    group = controls(2)
    output = generated(group) + [{'control_ids':['C-0','C-1'], 'departments':['IT'],
                                  'text':'Who approves changes?', 'mapping_reason':'Shared process'}]
    with pytest.raises(QuestionValidationError):
        normalize_questions(question_output(output, group), group)


def test_learned_smaller_batch_size_is_used_for_later_batches(monkeypatch):
    monkeypatch.setenv('AI_QUESTION_BATCH_SIZE', '8')
    seen = []
    def request(op, payload):
        size = len(payload['controls']); seen.append(size)
        if size > 2:
            raise status_error(502, 'output_token_limit')
        return generated(payload['controls'])
    result, plan = generate_question_batches(request, controls(16), {})
    assert len(result) == 16
    assert seen == [8,4,2,2,4,2,2,2,2,2,2]
    assert plan['effective_batch_size'] == 2


def status_error(status, code=None):
    response=httpx.Response(status,headers={'X-Inference-Failure-Code':code} if code else {},
                            request=httpx.Request('POST','https://private.example/infer'))
    return httpx.HTTPStatusError('private response secret-key',request=response.request,response=response)


@pytest.mark.parametrize('n', [93,123])
def test_complete_questionnaire_is_sequential_and_every_control_is_validated_once(n,monkeypatch):
    monkeypatch.delenv('AI_QUESTION_BATCH_SIZE',raising=False)
    seen=[]
    def request(operation,payload):
        assert operation=='questions'
        seen.append(payload)
        return generated(payload['controls'])
    result,plan=generate_question_batches(request,controls(n),{'company':'Example'})
    assert len(seen)==(n+7)//8
    assert max(len(item['controls']) for item in seen)<=8
    assert [cid for question in result for cid in question['control_ids']]==[c['id'] for c in controls(n)]
    assert plan['requests']==len(seen) and plan['split_count']==0


@pytest.mark.parametrize('status,code',[(502,'gpu_out_of_memory'),(413,None),(502,'output_token_limit'),(502,'invalid_json')])
def test_known_resource_failure_retries_smaller_groups_without_dropping_or_duplicating_controls(status,code):
    seen=[]
    def request(operation,payload):
        group=payload['controls'];seen.append(len(group))
        if len(group)>2: raise status_error(status,code)
        return generated(group)
    result,plan=generate_question_batches(request,controls(8),{})
    assert seen==[8,4,2,2,4,2,2]
    assert len(result)==8
    assert plan['requests']==7 and plan['split_count']==3 and plan['successful_batches']==4


def test_opaque_gateway_error_is_not_blindly_retried_and_does_not_leak_details():
    seen=[]
    def request(*args):
        seen.append(1);raise status_error(502)
    with pytest.raises(QuestionBatchError) as error:
        generate_question_batches(request,controls(),{})
    assert len(seen)==1
    assert 'gateway_http_502' in str(error.value)
    assert 'secret-key' not in str(error.value) and 'private.example' not in str(error.value)


def test_persistent_oom_stops_at_one_control_with_bounded_attempts():
    seen=[]
    def request(operation,payload):
        seen.append(len(payload['controls']));raise status_error(502,'gpu_out_of_memory')
    with pytest.raises(QuestionBatchError): generate_question_batches(request,controls(8),{})
    assert seen==[8,4,2,1]


def test_single_question_retries_invalid_json_once_with_new_format_instructions():
    seen=[]
    def request(operation,payload):
        seen.append(payload)
        if not payload.get('format_retry'): raise status_error(502,'invalid_json')
        return generated(payload['controls'])
    result,plan=generate_question_batches(request,controls(1),{})
    assert len(result)==1 and len(seen)==2
    assert seen[1]['format_retry'] is True
    assert plan['format_retry_requests']==1
    assert 'private response' not in json.dumps(seen)


def test_persistent_malformed_json_has_bounded_retries_and_saves_nothing():
    seen=[]
    def request(operation,payload):
        seen.append(len(payload['controls']));raise status_error(502,'invalid_json')
    with pytest.raises(QuestionBatchError) as error:
        generate_question_batches(request,controls(8),{})
    assert seen==[8,4,2,1,1,1]
    assert 'invalid_json' in str(error.value)
    assert 'secret-key' not in str(error.value)


def test_single_control_plain_text_recovery_keeps_ai_wording_and_binds_only_the_requested_control():
    seen = []
    def request(operation, payload):
        seen.append(payload)
        if not payload.get('question_text_only'):
            raise status_error(502, 'invalid_json')
        return 'Who approves access changes in the finance system?'
    result, plan = generate_question_batches(request, controls(1), {'company':'Example'})
    assert len(seen) == 3 and plan['text_fallback_requests'] == 1
    assert result[0]['control_ids'] == ['C-0']
    assert result[0]['departments'] == ['IT']
    assert result[0]['text'] == 'Who approves access changes in the finance system?'
    assert seen[-1]['context']['company'] == 'Example'


def test_plain_text_recovery_can_be_disabled(monkeypatch):
    monkeypatch.setenv('AI_QUESTION_TEXT_FALLBACK', 'false')
    seen = []
    def request(*args):
        seen.append(1)
        raise status_error(502, 'invalid_json')
    with pytest.raises(QuestionBatchError):
        generate_question_batches(request, controls(1), {})
    assert len(seen) == 2


def test_native_json_decode_failure_has_the_same_bounded_recovery():
    seen = []
    def request(operation, payload):
        seen.append(payload)
        if not payload.get('question_text_only'):
            return json.loads('not json')
        return 'How do you review user access?'
    result, plan = generate_question_batches(request, controls(1), {})
    assert len(seen) == 3 and plan['text_fallback_requests'] == 1
    assert result[0]['control_ids'] == ['C-0']


def test_creation_with_text_recovery_saves_complete_coverage_and_counts_all_attempts(db, monkeypatch):
    monkeypatch.setenv('AI_QUESTION_BATCH_SIZE', '1')
    s = standard(db); s.controls = controls(3); db.commit()
    provider = ai.HTTPProvider(url='https://synthetic.example', model='test')
    def request(operation, payload):
        if payload['controls'][0]['id'] == 'C-1':
            if not payload.get('question_text_only'):
                raise status_error(502, 'invalid_json')
            return 'Who reviews access exceptions?'
        return generated(payload['controls'])
    monkeypatch.setattr(provider, 'request', request)
    monkeypatch.setattr(ai, 'provider', lambda:ai.ValidatedProvider(provider))
    result = main.create_assessment(CreateAssessment(name='Recovered', standard_id=s.id,
        context={'company':'Example','business_type':'Retail','departments':['IT']}), db)
    assert {cid for q in result['questions'] for cid in q['control_ids']} == {'C-0','C-1','C-2'}
    saved = db.scalar(select(Audit).where(Audit.entity_id == result['id'], Audit.action == 'assessment.create'))
    assert saved.details['question_plan']['text_fallback_requests'] == 1
    assert saved.details['usage']['requests'] == 5


@pytest.mark.parametrize('bad', ['', '[]', '{"text":"invented"}', '<think>private reasoning', 'word ' * 500])
def test_failed_text_recovery_stops_without_accepting_private_or_incomplete_output(bad, caplog):
    seen = []
    def request(operation, payload):
        seen.append(payload)
        if not payload.get('question_text_only'):
            raise status_error(502, 'invalid_json')
        return bad
    with pytest.raises(QuestionBatchError):
        generate_question_batches(request, controls(1), {})
    assert len(seen) == 3
    assert 'private reasoning' not in caplog.text


def test_missing_control_coverage_is_split_until_complete_and_reason_is_recorded(caplog):
    seen=[]
    def request(operation,payload):
        seen.append(payload)
        # Well-formed JSON, but only the first supplied control is covered.
        return generated(payload['controls'][:1])
    result,plan=generate_question_batches(request,controls(8),{})
    assert len(result)==8
    assert [cid for q in result for cid in q['control_ids']]==[c['id'] for c in controls(8)]
    assert plan['validation_failures']=={'question_coverage_invalid':7}
    assert plan['split_count']==7 and plan['requests']==15
    assert seen[1]['validation_retry_code']=='question_coverage_invalid'
    assert 'questions.batch.validation_failed' in caplog.text
    assert 'question_coverage_invalid' in caplog.text


def test_invalid_department_field_retries_with_specific_safe_hint_once():
    seen=[]
    def request(operation,payload):
        seen.append(payload)
        result=generated(payload['controls'])
        if not payload.get('format_retry'): result[0]['departments']='IT'
        return result
    result,plan=generate_question_batches(request,controls(1),{})
    assert len(result)==1 and len(seen)==2
    assert seen[1]['validation_retry_code']=='question_departments_invalid'
    assert plan['format_retry_requests']==1


def test_persistent_invalid_question_fields_are_bounded_and_private_values_are_not_logged(caplog):
    seen=[]
    def request(operation,payload):
        seen.append(payload)
        return [{'control_ids':['private-invented-id'],'text':'private-answer','departments':['IT']}]
    with pytest.raises(QuestionBatchError) as error:
        generate_question_batches(request,controls(2),{})
    assert len(seen)==3
    assert 'question_control_reference_invalid' in str(error.value)
    assert 'private-invented-id' not in caplog.text
    assert 'private-answer' not in caplog.text
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('field,value,code',[
    ('departments',[], 'question_departments_invalid'),
    ('text','', 'question_text_invalid'),
    ('control_ids',['unknown'], 'question_control_reference_invalid'),
    ('control_id','different', 'question_primary_control_invalid'),
])
def test_question_validator_emits_specific_codes_without_loosening_contract(field,value,code):
    question=generated(controls(1))[0]
    question[field]=value
    with pytest.raises(QuestionValidationError) as error:
        normalize_questions([question],controls(1))
    assert error.value.code==code


def test_invalid_or_missing_questions_cannot_create_incomplete_assessment(db,monkeypatch):
    s=standard(db)
    provider=ai.HTTPProvider(url='https://synthetic.example',model='test')
    monkeypatch.setattr(provider,'request',lambda *args:[])
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(provider))
    with pytest.raises(HTTPException) as error:
        main.create_assessment(CreateAssessment(name='Incomplete',standard_id=s.id,
            context={'company':'Example','business_type':'Retail','departments':['IT']}),db)
    assert error.value.status_code==502
    assert db.scalar(select(func.count()).select_from(Assessment))==0
    assert db.scalar(select(func.count()).select_from(Revision))==0


def test_creation_persists_all_questions_and_batch_usage_only_after_complete_success(db,monkeypatch):
    monkeypatch.setenv('AI_QUESTION_BATCH_SIZE','8')
    s=standard(db);s.controls=controls();db.commit()
    provider=ai.HTTPProvider(url='https://synthetic.example',model='test')
    def request(operation,payload):
        from app.usage import safe_usage
        provider.usage_calls.append(safe_usage({'prompt_tokens':10,'completion_tokens':5,'generation_seconds':.1}))
        return generated(payload['controls'])
    monkeypatch.setattr(provider,'request',request)
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(provider))
    result=main.create_assessment(CreateAssessment(name='Complete',standard_id=s.id,
        context={'company':'Example','business_type':'Retail','departments':['IT']}),db)
    assert len(result['questions'])==len(result['answers'])==93
    assert result['question_usage']['requests']==12
    assert result['question_usage']['total_tokens']==180
    saved=db.scalar(select(Audit).where(Audit.entity_id==result['id'],Audit.action=='assessment.create'))
    assert saved.details['question_plan']['successful_batches']==12
    assert saved.details['question_plan']['question_agent_skill']['name']=='question-generator'
    assert saved.details['question_plan']['question_agent_skill']['version']=='1.0.0'


def test_skill_context_and_knowledge_are_scoped_without_modifying_inputs(db,monkeypatch):
    monkeypatch.setenv('AI_QUESTION_BATCH_SIZE','1')
    s=standard(db)
    s.controls=[{'id':cid,'requirement':'Full imported requirement','department':'IT'} for cid in ['A.8.5','A.8.13']]
    skill=template_skill('iso27001-2022-knowledge-review',s)
    context={'company':'Example','it_systems':['ERP'],'skill':skill}
    before=deepcopy(context);seen=[]
    def request(operation,payload):
        seen.append(payload);return generated(payload['controls'])
    generate_question_batches(request,s.controls,context)
    assert context==before
    for payload in seen:
        cid=payload['controls'][0]['id']
        assert payload['context']['it_systems']==['ERP']
        assert payload['context']['skill']['core_instructions']==skill['core_instructions']
        assert set(json.loads(payload['context']['skill']['instructions'])['control_rules'])=={cid}
        assert set(payload['standard_knowledge']['control_references'])=={cid}


def test_long_requirements_are_split_by_length_and_never_truncated():
    cs=controls(3)
    for c in cs:c['requirement']='Full requirement '*300
    seen=[]
    def request(operation,payload):
        seen.append(payload);return generated(payload['controls'])
    generate_question_batches(request,cs,{})
    assert len(seen)==3
    assert all(payload['controls'][0]['requirement']==cs[0]['requirement'] for payload in seen)


def test_project_question_skill_is_shared_with_every_model_batch():
    seen=[]
    result,plan=generate_question_batches(lambda operation,payload:(seen.append(payload) or generated(payload['controls'])),
        controls(3),{},batch_size_override=2,output_format_override='json')
    assert len(result)==3 and len(seen)==2
    assert all(p['question_agent_skill']['name']=='question-generator' for p in seen)
    assert all('Cover every required control exactly once' in p['question_agent_skill']['instructions'] for p in seen)
    assert plan['question_agent_skill']['version']=='1.0.0'
    assert len(plan['question_agent_skill']['sha256'])==64


def test_colab_http_provider_sends_project_skill_as_instruction_not_company_data(monkeypatch):
    provider=ai.HTTPProvider(url='https://synthetic.example',model='test')
    skill={'name':'question-generator','version':'1.0.0','sha256':'a'*64,
           'instructions':'Ask short questions about actual work.'}
    sent={}
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'output':'Q1: Who checks the access?','usage':{}}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):sent.update(kwargs['json']);return Response()
    monkeypatch.setattr('httpx.Client',Client)
    provider.request('questions',{'controls':controls(1),'context':{},'required_control_ids':['C-0'],'question_text_batch':True,
                                  'question_agent_skill':skill})
    assert 'Ask short questions about actual work.' in sent['system']
    assert 'question_agent_skill' not in sent['payload']


def test_colab_assessment_provider_uses_all_codex_skills_as_system_rules(monkeypatch):
    from app.codex_provider import SKILLS
    provider=ai.HTTPProvider(url='https://synthetic.example',model='test')
    skills={name:{'version':'1.0.0','sha256':'a'*64,'instructions':f'RULE FROM {name}'} for name in SKILLS}
    payload={'assessment_basis':'sc_recorded_answers_v1','controls':controls(1),
             'answers':{'C-0':{'text':'The customer answer','status':'unanswered'}},
             'codex_agent_skills':skills,'approved_examples':{'C-0':[{'approved_category':'ofi'}]}}
    sent={}
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'output':[],'usage':{}}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):sent.update(kwargs['json']);return Response()
    monkeypatch.setattr('httpx.Client',Client)
    provider.request('assess',payload)
    for name in SKILLS:
        assert f'RULE FROM {name}' in sent['system']
    assert 'codex_agent_skills' not in sent['payload']
    assert sent['payload']['approved_examples']==payload['approved_examples']


def test_colab_assessment_does_not_apply_sc_codex_skills_to_other_assessment_basis():
    from app.ai import colab_codex_skill_instructions
    payload={'assessment_basis':'generic_standard_v1','codex_agent_skills':{
        'finding-classifier':{'instructions':'SC grading rule'}}}
    assert colab_codex_skill_instructions(payload,'assess')==''
    assert colab_codex_skill_instructions(payload,'questions')==''


@pytest.mark.parametrize('provider_type',[ai.HTTPProvider,ai.OllamaProvider])
def test_usage_counts_failed_attempts_as_unknown_instead_of_underreporting(provider_type,monkeypatch):
    provider=provider_type(url='https://synthetic.example',model='test')
    def request(operation,payload):
        if len(payload['controls'])>1: raise status_error(502,'gpu_out_of_memory')
        from app.usage import safe_usage
        provider.usage_calls.append(safe_usage({'prompt_tokens':10,'completion_tokens':5,'generation_seconds':.1}))
        return generated(payload['controls'])
    monkeypatch.setattr(provider,'request',request)
    result=provider.questions(controls(2),{})
    assert len(result)==2 and provider.question_plan['requests']==3
    from app.usage import process_usage
    usage=process_usage(ai.ValidatedProvider(provider),1)
    assert usage['requests']==3 and usage['unmeasured_requests']==1
    assert usage['total_tokens'] is None and usage['measured_total_tokens']==30

@pytest.mark.parametrize('kind,expected', [('timeout','inference_timeout'), ('http','gateway_http_502')])
def test_request_failure_log_includes_batch_code_without_secrets(kind,expected,caplog):
    import logging
    seen=[]
    def request(op,payload):
        seen.append(1)
        if len(seen)==1:
            return generated(payload['controls'])
        if kind=='timeout':
            raise httpx.ReadTimeout('secret-url-and-key')
        raise status_error(502)
    with caplog.at_level(logging.INFO,logger='uvicorn.error'):
        with pytest.raises(QuestionBatchError):
            generate_question_batches(request,controls(16),{})
    assert 'questions.batch.done step=1 end_step=8' in caplog.text
    assert 'questions.batch.request_failed step=9 end_step=16' in caplog.text
    assert 'code='+expected in caplog.text
    assert 'elapsed_seconds=' in caplog.text
    assert 'secret-url-and-key' not in caplog.text and 'private response' not in caplog.text
