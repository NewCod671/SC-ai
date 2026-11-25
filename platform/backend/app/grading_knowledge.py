"""Small, control-scoped synthetic grading examples shared by all AI adapters."""
from hashlib import sha256
import json
from pathlib import Path

PATH=Path(__file__).resolve().parents[1]/'agent_skills'/'finding-classifier'/'references'/'grading-cases.json'


def load():
    raw=PATH.read_bytes()
    value=json.loads(raw)
    if not isinstance(value.get('groups'),list) or not isinstance(value.get('version'),str):
        raise ValueError('Invalid grading knowledge')
    return value,{'version':value['version'],'sha256':sha256(raw).hexdigest()}


def examples(control, knowledge=None, *, enabled=True):
    if not enabled:
        return []
    knowledge=knowledge if knowledge is not None else load()[0]
    return [dict(case,synthetic=True) for group in knowledge['groups']
        if control['id'] in group['control_ids'] for case in group['cases']][:3]
