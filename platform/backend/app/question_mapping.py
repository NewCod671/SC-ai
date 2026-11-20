"""Complete, bounded AI mapping of a user question across an imported standard."""
import os
from pydantic import TypeAdapter
from .schemas import QuestionControlMapping
from .knowledge import prepare_knowledge, scope_knowledge
from .skill_templates import batch_instructions
from .assessment_batches import failure_code


class QuestionMappingError(ValueError):
    pass


def validate_question_mapping(result, controls):
    if not isinstance(result, list) or len(result) > len(controls):
        raise ValueError('Invalid question mapping response')
    parsed = TypeAdapter(list[QuestionControlMapping]).validate_python(result)
    allowed = {c['id'] for c in controls}
    ids = [item.control_id for item in parsed]
    if len(ids) != len(set(ids)) or not set(ids) <= allowed or any(not item.reason.strip() for item in parsed):
        raise ValueError('Question mapping references unknown or duplicate controls, or an empty reason')
    return [item.model_dump() for item in parsed]


def question_mapping(request, text, controls, context, skill, departments, answer_text=''):
    size = int(os.getenv('AI_QUESTION_MAPPING_BATCH_SIZE', '24'))
    if size < 1 or size > 32:
        raise ValueError('AI_QUESTION_MAPPING_BATCH_SIZE must be between 1 and 32')
    knowledge = prepare_knowledge(skill, controls, questions=True)
    # Bound each group by both count and characters; never truncate a requirement.
    batches, pending, chars = [], [], 0
    for control in controls:
        count = len(control['requirement']) + len(control['id']) + len(control.get('department', ''))
        if pending and (len(pending) >= size or chars + count > 12000):
            batches.append(pending)
            pending, chars = [], 0
        pending.append(control)
        chars += count
    if pending:
        batches.append(pending)
    candidates = []
    for step, group in enumerate(batches, 1):
        ids = {c['id'] for c in group}
        payload = {'question': {'text': text, 'departments': departments}, 'context': {k:v for k,v in context.items() if k in ('company','business_type','departments','it_systems','data_types','existing_policies','question_language')},
                   'controls': [{k: c[k] for k in ('id', 'requirement', 'department')} for c in group]}
        if answer_text:
            payload['question']['answer_text'] = answer_text
        if skill:
            payload['skill'] = {k: skill[k] for k in ('core_instructions', 'instructions') if k in skill}
            if 'instructions' in payload['skill']:
                payload['skill']['instructions'] = batch_instructions(payload['skill']['instructions'], ids)
        if knowledge is not None:
            payload['standard_knowledge'] = scope_knowledge(knowledge, ids)
        # Any failed/invalid batch aborts the preview; do not silently omit matches.
        stage = 'request'
        try:
            result = request('map_question', payload)
            stage = 'validation'
            candidates.extend(validate_question_mapping(result, group))
        except Exception as exc:
            code = getattr(exc,'code',None)
            if not isinstance(code,str) or not code.startswith('codex_'):
                code = failure_code(exc, stage)
            if code == 'gateway_http_404':
                hint = ' Check AI_BASE_URL is the ngrok tunnel root (no /infer suffix), the tunnel forwards to Colab port 9000, and /health is served by that gateway.'
            elif code == 'gateway_http_422':
                hint = ' Upload/restart the latest inference/server.py; /health supported_operations must include map_question.'
            else:
                hint = ''
            raise QuestionMappingError(f'AI question mapping failed at batch {step}/{len(batches)} ({code}); no question added.' + hint) from exc
    return sorted(candidates, key=lambda item: -item['score'])
