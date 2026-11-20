import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace,ModuleType
import pytest


def configure(monkeypatch,memory=24_000_000_000,cuda=True):
    notebook=json.loads((Path(__file__).resolve().parents[2]/'inference/colab.ipynb').read_text(encoding='utf-8'))
    source=''.join(next(c['source'] for c in notebook['cells'] if c.get('id')=='configure'))
    monkeypatch.setattr(os,'environ',{})
    google=ModuleType('google');google.__path__=[]
    monkeypatch.setitem(sys.modules,'google',google)
    monkeypatch.setitem(sys.modules,'google.colab',SimpleNamespace(userdata=SimpleNamespace(get=lambda key:'synthetic-test-secret')))
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=SimpleNamespace(
        is_available=lambda:cuda,get_device_properties=lambda index:SimpleNamespace(name='Synthetic GPU',total_memory=memory))))
    exec(compile(source,'colab-configure','exec'),{})
    return dict(os.environ)


def test_l4_class_gpu_configures_exact_model_nf4_and_no_thinking(monkeypatch,capsys):
    settings=configure(monkeypatch)
    assert settings['AI_MODEL']=='Qwen/Qwen3.8-27B'
    assert settings['AI_LOAD_IN_4BIT']=='true'
    assert settings['AI_ENABLE_THINKING']=='false'
    assert settings['AI_ASSESS_MAX_INPUT_TOKENS']=='6144'
    assert settings['AI_LORA_ADAPTER']==''
    assert 'synthetic-test-secret' not in capsys.readouterr().out


def test_t4_class_gpu_rejected_before_loading_27b(monkeypatch):
    with pytest.raises(RuntimeError,match='Select L4'):
        configure(monkeypatch,memory=16_000_000_000)


def test_cpu_runtime_rejected_before_loading_27b(monkeypatch):
    with pytest.raises(RuntimeError,match='Select a GPU runtime'):
        configure(monkeypatch,cuda=False)
