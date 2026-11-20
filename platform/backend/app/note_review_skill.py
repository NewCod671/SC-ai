"""Load the versioned, model-independent note-coverage review skill."""
from hashlib import sha256
import json
from pathlib import Path
import re

SKILL_ROOT = Path(__file__).resolve().parents[1] / 'agent_skills'
SKILL_PATH = SKILL_ROOT / 'note-reviewer' / 'SKILL.md'


def load_note_review_skill():
    manifest = json.loads((SKILL_ROOT / 'manifest.json').read_text(encoding='utf-8'))
    source = SKILL_PATH.read_text(encoding='utf-8')
    version = manifest.get('note-reviewer', '')
    if (not source.startswith('---\n') or len(source) > 20000
            or not re.search(r'^name: note-reviewer$', source, re.M)
            or not re.fullmatch(r'\d+\.\d+\.\d+', version)):
        raise ValueError('note_review_skill_invalid')
    sections = source.split('---', 2)
    if len(sections) != 3 or not sections[2].strip():
        raise ValueError('note_review_skill_invalid')
    return {
        'name': 'note-reviewer',
        'version': version,
        'sha256': sha256(source.encode()).hexdigest(),
        'instructions': sections[2].strip(),
        'source': source,
    }
