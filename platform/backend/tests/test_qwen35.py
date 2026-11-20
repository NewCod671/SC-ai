from contextlib import nullcontext
from types import SimpleNamespace
import sys
import pytest
from .test_gateway import gateway


@pytest.fixture
def qwen_runtime(monkeypatch):
    monkeypatch.setenv('INFERENCE_API_KEY', 'test-key')
    monkeypatch.setenv('AI_MODEL', 'Qwen/Qwen3.5-9B')
    monkeypatch.delenv('AI_ENABLE_THINKING', raising=False)
    monkeypatch.delenv('AI_LOAD_IN_4BIT', raising=False)
    state = {'templates': []}
    class Batch(dict):
        def to(self, device):
            state['input_device'] = device
            return self
    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            state['templates'].append(kwargs)
            return [1, 2, 3] if kwargs['tokenize'] else 'formatted prompt'
        def __call__(self, text, **kwargs):
            state['tokenizer_options'] = kwargs
            return Batch(input_ids=SimpleNamespace(shape=(1, 3)), attention_mask='mask')
        def decode(self, tokens, **kwargs):
            state['decode_options'] = kwargs
            state['decoded_tokens'] = tokens
            return '[]'
    class Model:
        device = 'cuda:0'
        def eval(self): state['eval'] = True
        def generate(self, **kwargs):
            state['generation'] = kwargs
            return [[1, 2, 3, 7, 8]]
    def load_model(model_id, **kwargs):
        state['model_id'] = model_id
        state['model_options'] = kwargs
        return Model()
    def quantization(**kwargs):
        state['quantization'] = kwargs
        return 'quant-config'
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        float16='fp16', bfloat16='bf16', float32='fp32', inference_mode=nullcontext,
        cuda=SimpleNamespace(is_available=lambda: True, is_bf16_supported=lambda: False)))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=lambda model_id: Tokenizer()),
        Qwen3_5ForConditionalGeneration=SimpleNamespace(from_pretrained=load_model),
        BitsAndBytesConfig=quantization))
    gateway.model_pipeline.cache_clear()
    yield state
    gateway.model_pipeline.cache_clear()


def test_qwen35_loads_conditional_model_in_4bit_and_disables_thinking(qwen_runtime):
    result = gateway.infer(gateway.Request(operation='assess', model='Qwen/Qwen3.5-9B', system='Safety', payload={}), 'Bearer test-key')
    state = qwen_runtime
    assert state['model_id'] == 'Qwen/Qwen3.5-9B'
    assert state['model_options']['quantization_config'] == 'quant-config'
    assert state['model_options']['dtype'] == 'fp16'
    assert state['model_options']['attn_implementation'] == 'sdpa'
    assert state['quantization']['load_in_4bit'] is True
    assert all(options['enable_thinking'] is False for options in state['templates'])
    assert state['input_device'] == 'cuda:0'
    assert state['generation']['do_sample'] is False
    assert 'temperature' not in state['generation']
    assert 'top_p' not in state['generation']
    assert state['generation']['logits_to_keep'] == 1
    assert state['tokenizer_options']['add_special_tokens'] is False
    assert state['decoded_tokens'] == [7, 8]
    assert state['decode_options']['skip_special_tokens'] is True
    assert result['usage']['prompt_tokens'] == 3
    assert result['usage']['completion_tokens'] == 2
    assert result['usage']['total_tokens'] == 5
    health = gateway.health('Bearer test-key')
    assert health['gateway_version'] == gateway.GATEWAY_VERSION
    assert health['thinking_enabled'] is False
    assert health['load_in_4bit'] is True
    assert health['r1_compatibility'] is False


def test_thinking_can_be_explicitly_enabled(qwen_runtime, monkeypatch):
    monkeypatch.setenv('AI_ENABLE_THINKING', 'true')
    gateway.infer(gateway.Request(operation='assess', model='Qwen/Qwen3.5-9B', system='Safety', payload={}), 'Bearer test-key')
    assert all(options['enable_thinking'] is True for options in qwen_runtime['templates'])
    assert qwen_runtime['generation']['temperature'] == 1.0
    assert qwen_runtime['generation']['top_p'] == 0.95


def test_model_mismatch_fails_before_model_loading(qwen_runtime):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        gateway.infer(gateway.Request(operation='assess', model='old-model', system='Safety', payload={}), 'Bearer test-key')
    assert error.value.status_code == 409
    assert 'model_id' not in qwen_runtime


def test_qwen35_forwards_cancel_criterion_to_generate(qwen_runtime):
    from uuid import uuid4
    gateway.infer(gateway.Request(operation='assess',model='Qwen/Qwen3.5-9B',system='Safety',payload={},cancellation_id=uuid4().hex),'Bearer test-key')
    criteria=qwen_runtime['generation']['stopping_criteria']
    assert len(criteria)==1 and isinstance(criteria[0],gateway.StopOnCancel)
    assert criteria[0](None,None) is False


def test_qwen38_27b_uses_hybrid_loader_and_nf4_bf16_on_supported_gpu(qwen_runtime,monkeypatch):
    monkeypatch.setenv('AI_MODEL','Qwen/Qwen3.8-27B')
    sys.modules['torch'].cuda.is_bf16_supported=lambda:True
    gateway.infer(gateway.Request(operation='assess',model='Qwen/Qwen3.8-27B',system='Safety',payload={}), 'Bearer test-key')
    assert qwen_runtime['model_id']=='Qwen/Qwen3.8-27B'
    assert qwen_runtime['model_options']['dtype']=='bf16'
    assert qwen_runtime['quantization']['bnb_4bit_compute_dtype']=='bf16'
    assert qwen_runtime['quantization']['bnb_4bit_quant_type']=='nf4'
    assert qwen_runtime['quantization']['bnb_4bit_use_double_quant'] is True
    assert qwen_runtime['quantization']['load_in_4bit'] is True
    assert all(options['enable_thinking'] is False for options in qwen_runtime['templates'])
    assert qwen_runtime['generation']['do_sample'] is False
    health=gateway.health('Bearer test-key')
    assert health['model']=='Qwen/Qwen3.8-27B'
    assert 'qwen3.8' in health['qwen_hybrid_models']
