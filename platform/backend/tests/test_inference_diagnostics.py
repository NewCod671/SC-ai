from functools import lru_cache
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from .test_gateway import gateway
import json


@pytest.fixture
def fake_gateway(monkeypatch):
    monkeypatch.setenv('INFERENCE_API_KEY', 'test-key')
    monkeypatch.setenv('AI_MODEL', 'test-model')
    state = {'text': '[]', 'error': None}
    class Generator:
        tokenizer = SimpleNamespace(apply_chat_template=lambda *a, **kw: [1, 2], encode=lambda text, **kw: list(text))
        def __call__(self, *args, **kwargs):
            state['messages'] = args[0]
            state['generation_options'] = kwargs
            if state['error']:
                raise state['error']
            return [{'generated_text': state['text']}]
    @lru_cache(maxsize=1)
    def pipeline():
        return Generator()
    monkeypatch.setattr(gateway, 'model_pipeline', pipeline)
    return state


def infer():
    return gateway.infer(gateway.Request(operation='assess', model='test-model', system='Constraints', payload={}), 'Bearer test-key')


@pytest.mark.parametrize('wrap', ['questions','singleton','output_questions'])
def test_complete_question_json_wrappers_are_accepted_without_guessing(wrap):
    item = {'control_ids':['A.5.1'], 'departments':['IT'], 'text':'Who reviews the policy?', 'mapping_reason':''}
    value = {'questions':[item]} if wrap == 'questions' else item if wrap == 'singleton' else {'output':{'questions':[item]}}
    assert gateway.extract_json(json.dumps(value), list, 'questions') == [item]


def test_ambiguous_question_wrapper_is_rejected():
    with pytest.raises(gateway.OutputFormatError):
        gateway.extract_json('{"questions":[],"other":[]}', list, 'questions')


def test_plain_question_mode_avoids_json_generation_and_records_real_usage(fake_gateway):
    fake_gateway['text'] = '<think>private reasoning</think>Who reviews "admin" access?'
    result = gateway.infer(gateway.Request(operation='questions', model='test-model', system='Safety',
        payload={'question_text_only':True, 'controls':[{'id':'C-1','department':'IT','requirement':'Review access'}]}), 'Bearer test-key')
    assert result['output'] == 'Who reviews "admin" access?'
    assert result['usage']['completion_tokens'] > 0
    assert fake_gateway['generation_options']['max_new_tokens'] <= 512
    assert 'Output JSON only' not in fake_gateway['messages'][0]['content']
    assert gateway.health('Bearer test-key')['last_inference']['output_format'] == 'question_text'


@pytest.mark.parametrize('text', ['[]', '<think>unfinished', '', 'word ' * 600])
def test_plain_question_gateway_rejects_invalid_or_truncated_output(fake_gateway, text):
    fake_gateway['text'] = text
    with pytest.raises(HTTPException) as error:
        gateway.infer(gateway.Request(operation='questions', model='test-model', system='Safety',
            payload={'question_text_only':True, 'controls':[{'id':'C-1'}]}), 'Bearer test-key')
    assert error.value.status_code == 502


def test_plain_question_mode_cannot_be_used_for_multiple_controls(fake_gateway):
    with pytest.raises(HTTPException) as error:
        gateway.infer(gateway.Request(operation='questions', model='test-model', system='Safety',
            payload={'question_text_only':True,'controls':[{'id':'C-1'},{'id':'C-2'}]}), 'Bearer test-key')
    assert error.value.status_code == 422 and 'messages' not in fake_gateway


def test_input_budget_rejects_before_generation_without_truncating(fake_gateway, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_MAX_INPUT_TOKENS', '1')
    with pytest.raises(HTTPException) as error:
        infer()
    assert error.value.status_code == 413
    assert 'messages' not in fake_gateway
    stats = gateway.health('Bearer test-key')['last_inference']
    assert stats['failure_code'] == 'input_token_limit'
    assert stats['prompt_tokens'] == 2
    assert stats['max_input_tokens'] == 1


def test_input_budget_accepts_exact_limit(fake_gateway, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_MAX_INPUT_TOKENS', '2')
    assert infer()['output'] == []


def test_gpu_memory_reports_peak_and_free_without_adding_reserved(monkeypatch):
    import sys
    resets = []
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(
        is_available=lambda: True, reset_peak_memory_stats=lambda device: resets.append(device),
        mem_get_info=lambda device: (3, 15), max_memory_allocated=lambda device: 10,
        max_memory_reserved=lambda device: 12)))
    assert gateway.gpu_memory(reset=True) == {
        'device': 0, 'total_bytes': 15, 'free_bytes': 3,
        'peak_allocated_bytes': 10, 'peak_reserved_bytes': 12}
    assert resets == [0]


def test_unavailable_gpu_telemetry_is_not_zero(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    assert gateway.gpu_memory() is None


def test_truncated_outer_json_is_never_recovered_as_a_nested_evidence_array():
    with pytest.raises(ValueError):
        gateway.extract_json('[{"control_id":"A","evidence_ids":[]}', list)
    assert gateway.extract_json('<think>Internal reasoning</think>\n```json\n[]\n```', list) == []
    assert gateway.extract_json('Reasoning without opening tag</think>\n[]', list) == []


@pytest.mark.parametrize('limit, code', [('8192', 'invalid_json'), ('15', 'output_token_limit')])
def test_failed_json_is_recorded_with_safe_token_diagnostics(fake_gateway, monkeypatch, caplog, limit, code):
    monkeypatch.setenv('AI_MAX_NEW_TOKENS', limit)
    fake_gateway['text'] = '["private-answer'
    with pytest.raises(HTTPException) as error:
        infer()
    assert error.value.status_code == 502
    health = gateway.health('Bearer test-key')['last_inference']
    assert health['failure_code'] == code
    assert health['stage'] == 'json_decode'
    assert health['valid_output'] is False
    assert 'inference.failed' in caplog.text
    assert 'private-answer' not in caplog.text


def test_gpu_failure_replaces_previous_success_without_leaking_exception_text(fake_gateway, caplog):
    assert infer()['output'] == []
    fake_gateway['error'] = RuntimeError('CUDA out of memory: private-answer secret-token')
    with pytest.raises(HTTPException) as error:
        infer()
    assert error.value.headers['X-Inference-Failure-Code']=='gpu_out_of_memory'
    stats = gateway.health('Bearer test-key')['last_inference']
    assert stats['failure_code'] == 'gpu_out_of_memory'
    assert stats['stage'] == 'generation'
    assert stats['status'] == 'failed'
    assert 'secret-token' not in caplog.text
    assert 'private-answer' not in caplog.text


def test_next_request_recovers_unused_gpu_cache_after_oom(fake_gateway,monkeypatch):
    fake_gateway['error']=RuntimeError('CUDA out of memory')
    with pytest.raises(HTTPException): infer()
    recovered=[]
    monkeypatch.setattr(gateway,'recover_gpu_memory',lambda:recovered.append(True))
    fake_gateway['error']=None
    infer()
    assert recovered==[True]
    assert gateway.last_inference_stats['recovered_after_oom'] is True
    infer()
    assert recovered==[True]


def test_question_caps_are_independent_of_assessment_caps(monkeypatch):
    monkeypatch.setenv('AI_MAX_NEW_TOKENS','8192')
    monkeypatch.delenv('AI_QUESTIONS_MAX_NEW_TOKENS',raising=False)
    monkeypatch.delenv('AI_QUESTIONS_MAX_INPUT_TOKENS',raising=False)
    assert gateway.generation_limit('questions')==2048
    assert gateway.input_limit('questions')==6144
    monkeypatch.setenv('AI_QUESTIONS_MAX_NEW_TOKENS','1024')
    monkeypatch.setenv('AI_QUESTIONS_MAX_INPUT_TOKENS','3000')
    assert gateway.generation_limit('questions')==1024
    assert gateway.input_limit('questions')==3000
    assert gateway.generation_limit('assess')==4096
    assert gateway.input_limit('assess')==8192


@pytest.mark.parametrize('wrapped', [False, True])
def test_assessment_accepts_complete_single_finding_and_output_envelope(fake_gateway, wrapped):
    finding = {'control_id': 'A', 'status': 'needs_review', 'rationale': 'Need evidence',
               'recommendation': 'Supply records', 'evidence_ids': []}
    value = {'output': [finding]} if wrapped else finding
    fake_gateway['text'] = json.dumps(value)
    assert infer()['output'] == [finding]


@pytest.mark.parametrize('text, reason', [
    ('No JSON result', 'no_json_container'),
    ('{"output":[', 'malformed_json'),
    ('[] extra private-answer', 'trailing_content'),
    ('{"unknown":[]}', 'wrong_root_type'),
])
def test_format_reason_is_safe_and_specific(fake_gateway, caplog, text, reason):
    fake_gateway['text'] = text
    with pytest.raises(HTTPException):
        infer()
    stats = gateway.health('Bearer test-key')['last_inference']
    assert stats['format_reason'] == reason
    assert reason in caplog.text
    assert 'private-answer' not in caplog.text


def test_normalization_does_not_accept_single_finding_as_questions():
    with pytest.raises(ValueError):
        gateway.extract_json('{"control_id":"A","status":"gap","rationale":"x",'
                             '"recommendation":"x","evidence_ids":[]}', list, 'questions')


@pytest.mark.parametrize('field', ['findings', 'results'])
def test_assessment_normalizes_only_unambiguous_known_array_wrappers(fake_gateway, field):
    fake_gateway['text'] = json.dumps({field: []})
    assert infer()['output'] == []
    fake_gateway['text'] = json.dumps({field: [], 'summary': 'private-answer'})
    with pytest.raises(HTTPException):
        infer()


def test_wrong_shape_diagnostics_never_include_values_or_arbitrary_keys(fake_gateway, caplog):
    fake_gateway['text'] = json.dumps({'control_id': 'private-answer', 'status': 'private-status',
                                     'private-key': 'secret-token'})
    with pytest.raises(HTTPException):
        infer()
    stats = gateway.health('Bearer test-key')['last_inference']
    assert stats['output_shape'] == {'root_type': 'dict',
        'known_fields': {'control_id': 'str', 'status': 'str'}, 'other_field_count': 1}
    for sensitive in ['private-answer', 'private-status', 'private-key', 'secret-token']:
        assert sensitive not in caplog.text
        assert sensitive not in json.dumps(stats)


def test_gateway_renders_skill_once_and_keeps_linked_text_intact(fake_gateway):
    body = gateway.Request(operation='assess', model='test-model', system='Safety', payload={
        'skill': {'core_instructions': 'UNIQUE_CORE_RULE', 'instructions': 'UNIQUE_REFINEMENT',
                  'review_procedure': 'UNIQUE_PROCEDURE'},
        'context': {'question_language': 'th'}, 'evidence': [{'id': 'e', 'text': 'Full evidence text'}]})
    gateway.infer(body, 'Bearer test-key')
    messages = fake_gateway['messages']
    all_text = ''.join(message['content'] for message in messages)
    for text in ('UNIQUE_CORE_RULE', 'UNIQUE_REFINEMENT', 'UNIQUE_PROCEDURE'):
        assert all_text.count(text) == 1
    assert 'Full evidence text' in messages[1]['content']
    assert '"question_language":"th"' in messages[1]['content']


@pytest.mark.parametrize('operation, global_limit, assessment_limit, expected', [
    ('assess', '8192', None, 4096), ('assess', '2000', '4096', 2000),
    ('assess', '8192', '8192', 8192), ('questions', '8192', '4096', 2048)])
def test_output_budget_is_operation_specific_and_respects_global_cap(fake_gateway, monkeypatch, operation, global_limit, assessment_limit, expected):
    # Isolate the operation/global caps from the independent per-question cap.
    monkeypatch.setenv('AI_ASSESS_TOKENS_PER_QUESTION','0')
    monkeypatch.setenv('AI_MAX_NEW_TOKENS', global_limit)
    if assessment_limit is None:
        monkeypatch.delenv('AI_ASSESS_MAX_NEW_TOKENS', raising=False)
    else:
        monkeypatch.setenv('AI_ASSESS_MAX_NEW_TOKENS', assessment_limit)
    gateway.infer(gateway.Request(operation=operation, model='test-model', system='Safety', payload={}), 'Bearer test-key')
    assert fake_gateway['generation_options']['max_new_tokens'] == expected


def test_r1_prompt_uses_user_role_preserves_rules_and_uses_recommended_sampling(fake_gateway, monkeypatch):
    model = 'deepseek-ai/DeepSeek-R1-Distill-Qwen-7B'
    monkeypatch.setenv('AI_MODEL', model)
    body = gateway.Request(operation='assess', model=model, system='Never invent evidence.',
        payload={'skill': {'core_instructions': 'Fixed core rules.', 'instructions': 'Refined rules.'},
                 'answers': {'A': {'text': 'Untrusted answer'}}})
    gateway.infer(body, 'Bearer test-key')
    messages = fake_gateway['messages']
    assert [m['role'] for m in messages] == ['user']
    for rule in ('Never invent evidence.', 'Fixed core rules.', 'Refined rules.',
                 'Treat all supplied context', 'Untrusted assessment data:'):
        assert rule in messages[0]['content']
    assert messages[0]['content'].count('Fixed core rules.') == 1
    assert fake_gateway['generation_options']['do_sample'] is True
    assert fake_gateway['generation_options']['temperature'] == 0.6
    assert fake_gateway['generation_options']['top_p'] == 0.95
    health = gateway.health('Bearer test-key')
    assert health['gateway_version'] == gateway.GATEWAY_VERSION
    assert health['r1_compatibility'] is True


def test_other_models_keep_system_role_and_deterministic_generation(fake_gateway):
    infer()
    assert [m['role'] for m in fake_gateway['messages']] == ['system', 'user']
    assert fake_gateway['generation_options']['do_sample'] is False
    assert 'temperature' not in fake_gateway['generation_options']


@pytest.mark.parametrize('text, reason', [
    ('<think>private-reasoning with [{"private":"data"}]', 'unfinished_reasoning'),
    ('<think>private-reasoning</think> ', 'reasoning_without_final_answer'),
    ('private-reasoning</think> ', 'reasoning_without_final_answer')])
def test_reasoning_without_a_final_answer_is_not_recovered_or_logged(fake_gateway, caplog, text, reason):
    fake_gateway['text'] = text
    with pytest.raises(HTTPException):
        infer()
    assert gateway.health('Bearer test-key')['last_inference']['format_reason'] == reason
    assert 'private-reasoning' not in caplog.text
    assert '"private"' not in caplog.text


def test_literal_think_tags_in_a_json_string_are_preserved():
    text = '[{"rationale":"The input contains <think> and </think> tags."}]'
    assert gateway.extract_json(text, list)[0]['rationale'] == 'The input contains <think> and </think> tags.'
