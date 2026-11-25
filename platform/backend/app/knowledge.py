"""Deterministic, exact-ID retrieval of pinned local standard reference text.

Documents are untrusted data. They never become system instructions or customer
evidence. No embeddings, network dependency, automatic learning or activation.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

KNOWLEDGE_ROOT = Path(__file__).resolve().parents[1] / 'knowledge'

ISO27001_2022_REF = {
    'id': 'iso27001-2022-48d25ea3d3bd1c95',
    'sha256': '48d25ea3d3bd1c95b96376ac0e8961b6a382f5980786ea02d684e3c1c1401a47',
    'source_sha256': '843a6728009540947b2c5531f94b4cf1fb698d5bf3b51b324c8801e3e01c5b78',
}


class KnowledgeError(ValueError):
    pass


def knowledge_reference(skill):
    try:
        value = json.loads((skill or {}).get('instructions', ''))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get('format') != 'compliance-review-skill/v1':
        return None
    return value.get('knowledge_ref')


def load_knowledge(reference):
    try:
        if (not isinstance(reference, dict)
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,40}-[a-f0-9]{16}', reference.get('id', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', reference.get('sha256', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', reference.get('source_sha256', ''))):
            raise ValueError('Invalid reference')
        raw = (KNOWLEDGE_ROOT / (reference['id'] + '.json')).read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ValueError('Bundle hash mismatch')
        value = json.loads(raw)
        if (value.get('format') != 'compliance-standard-knowledge/v1'
                or value['source']['sha256'] != reference['source_sha256']):
            raise ValueError('Source reference mismatch')
        source = (KNOWLEDGE_ROOT / 'sources' / (reference['source_sha256'] + '.pdf')).read_bytes()
        if hashlib.sha256(source).hexdigest() != reference['source_sha256']:
            raise ValueError('Source hash mismatch')
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise KnowledgeError('Standard knowledge is unavailable or failed integrity validation; restore the pinned bundle and source PDF') from None


def matched_references(control_id, entries):
    if control_id.startswith('A.'):
        return [control_id] if control_id in entries else []
    match = re.fullmatch(r'(?:clause[: ]+)?((?:[1-9]|10)(?:\.\d+){0,2})', control_id, flags=re.I)
    if not match:
        return []
    key = 'clause:' + match[1]
    # A parent clause must include its subclauses, not just its section heading.
    return [ref for ref in entries if ref == key or ref.startswith(key + '.')]


def prepare_knowledge(skill, controls, *, questions=False):
    reference = knowledge_reference(skill)
    if reference is None:
        return None
    bundle = load_knowledge(reference)
    source_entries = bundle['entries']
    matches = {c['id']: matched_references(c['id'], source_entries) for c in controls}
    required = {ref for refs in matches.values() for ref in refs}
    context = ['clause:1', 'clause:6.1.3'] if any(ref.startswith('A.') for ref in required) else []
    required.update(context)
    entries = {ref: deepcopy(source_entries[ref]) for ref in source_entries if ref in required}
    if questions:
        # Questions already receive full imported requirements. Titles give source
        # alignment without adding all 93 control statements to one large prompt.
        # Applicability context stays complete; assessment uses full statements.
        for ref, item in entries.items():
            if ref not in context:
                item.pop('text')
    return {'reference': deepcopy(reference), 'source': deepcopy(bundle['source']),
            'mode': 'question_reference_index' if questions else 'assessment_reference_text',
            'control_references': matches, 'context_references': context, 'entries': entries,
            'unmatched_control_ids': [cid for cid, refs in matches.items() if not refs]}


def prepare_iso27001_knowledge(controls, *, questions=False):
    synthetic_skill = {'instructions': json.dumps({
        'format': 'compliance-review-skill/v1',
        'knowledge_ref': ISO27001_2022_REF,
    })}
    return prepare_knowledge(synthetic_skill, controls, questions=questions)


def prepare_for_standard(standard, controls, *, questions=False):
    from . import standard_profiles

    profile = standard_profiles.resolve(standard)
    if profile.knowledge_ref is None:
        return None
    synthetic_skill = {'instructions': json.dumps({
        'format': 'compliance-review-skill/v1',
        'knowledge_ref': profile.knowledge_ref,
    })}
    return prepare_knowledge(synthetic_skill, controls, questions=questions)


def scope_knowledge(knowledge, control_ids):
    value = deepcopy(knowledge)
    matches = {cid: refs for cid, refs in value['control_references'].items() if cid in control_ids}
    required = {ref for refs in matches.values() for ref in refs}
    context = value['context_references'] if required else []
    required.update(context)
    value.update({'control_references': matches, 'context_references': context,
                  'entries': {ref: item for ref, item in value['entries'].items() if ref in required},
                  'unmatched_control_ids': [cid for cid, refs in matches.items() if not refs]})
    return value


def knowledge_summary(snapshot):
    if snapshot is None:
        return None
    return {'filename': snapshot['source']['filename'], 'reference': snapshot['reference'],
            'matched_control_count': sum(bool(refs) for refs in snapshot['control_references'].values()),
            'unmatched_control_ids': snapshot['unmatched_control_ids'],
            'references': [{'id': ref, 'title': entry['title'], 'pdf_pages': entry['pdf_pages'],
                            'printed_pages': entry['printed_pages']} for ref, entry in snapshot['entries'].items()]}
