"""Versioned application review skills, independent of Codex skills and MCP.

Templates are explicitly installed against an imported standard. Existing skill
versions are never rewritten; only selected-batch guidance is sent to inference.
"""
import json
from copy import deepcopy
from pathlib import Path

TEMPLATES = ('standard-review', 'iso27001-2022-review', 'iso27001-2022-knowledge-review', 'sc-iso27001-audit-review')
# Internal bases remain available for versioned inheritance; only SC is offered in the app.
CATALOG = ('sc-iso27001-audit-review',)
FORMAT = 'compliance-review-skill/v1'


def load_template(template_id):
    # Explicit allowlist: never resolve user-provided paths.
    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    value = json.loads((Path(__file__).with_name('skill_templates') / (template_id + '.json')).read_text(encoding='utf-8'))
    if 'extends' in value:
        base = load_template(value['extends'])
        base.update({k: value[k] for k in ('id', 'name', 'version', 'description')})
        base['instructions'].update({k: value[k] for k in ('scope_note', 'control_rules', 'knowledge_ref') if k in value})
        value = base
    value['instructions']['template_id'] = value['id']
    value['instructions']['template_version'] = value['version']
    return value


def template_catalog():
    return [{k: template[k] for k in ('id', 'name', 'version', 'description')}
            for template in map(load_template, CATALOG)]


def template_skill(template_id, standard):
    template = load_template(template_id)
    instructions = template['instructions']
    if 'knowledge_ref' in instructions:
        from .knowledge import load_knowledge
        load_knowledge(instructions['knowledge_ref'])
    ids = {c['id'] for c in standard.controls}
    instructions['control_rules'] = {cid: rule for cid, rule in instructions['control_rules'].items() if cid in ids}
    instructions['standard_scope'] = {'id': standard.id, 'name': standard.name, 'version': standard.version}
    # Verdict meanings are part of the immutable family core, not refinements
    # that an operator can replace when creating the next version.
    decisions = instructions.pop('decision_rules')
    core = template['core_instructions'] + '\nFIXED DECISION RULES:\n' + json.dumps(decisions, ensure_ascii=False, separators=(',', ':'))
    return {'standard_id': standard.id, 'name': template['name'], 'version': template['version'],
            'core_instructions': core,
            'instructions': json.dumps(instructions, ensure_ascii=False, indent=2)}


def batch_instructions(instructions, control_ids):
    """Scope our structured guidance only; preserve arbitrary operator prose.

    All shared rules remain unchanged. The complete source stays in the immutable
    skill/run snapshot; this copy prevents sending unrelated per-control guidance.
    """
    try:
        value = json.loads(instructions)
    except (ValueError, TypeError):
        return instructions
    if not isinstance(value, dict) or value.get('format') != FORMAT or not isinstance(value.get('control_rules'), dict):
        return instructions
    value = deepcopy(value)
    value['control_rules'] = {cid: rule for cid, rule in value['control_rules'].items() if cid in control_ids}
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
