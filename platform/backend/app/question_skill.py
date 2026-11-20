"""Load the versioned, model-independent question-writing agent skill."""
from hashlib import sha256
import json
from pathlib import Path
import re

SKILL_ROOT=Path(__file__).resolve().parents[1]/'agent_skills'
SKILL_PATH=SKILL_ROOT/'question-generator'/'SKILL.md'


def load_question_skill():
    manifest=json.loads((SKILL_ROOT/'manifest.json').read_text(encoding='utf-8'))
    source=SKILL_PATH.read_text(encoding='utf-8')
    version=manifest.get('question-generator','')
    if (not source.startswith('---\n') or len(source)>20000
            or not re.search(r'^name: question-generator$',source,re.M)
            or not re.fullmatch(r'\d+\.\d+\.\d+',version)):
        raise ValueError('question_skill_invalid')
    sections=source.split('---',2)
    if len(sections)!=3 or not sections[2].strip():
        raise ValueError('question_skill_invalid')
    return {'name':'question-generator','version':version,
            'sha256':sha256(source.encode()).hexdigest(),
            'instructions':sections[2].strip(),'source':source}
