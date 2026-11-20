"""Generate a complete questionnaire with bounded, sequential inference calls."""
from copy import deepcopy
import logging
import time
import json
import os
import httpx
from .assessment_batches import failure_code
from .knowledge import prepare_knowledge, scope_knowledge
from .questionnaire import normalize_questions, QuestionValidationError, plain_question_output, text_question_batch_output
from .skill_templates import batch_instructions
from .question_jobs import check_cancelled, QuestionGenerationCancelled
from .question_skill import load_question_skill

logger = logging.getLogger('uvicorn.error')


class QuestionBatchError(ValueError):
    pass


def question_output(result, group):
    """Normalize complete containers and repeated follow-ups without new inference.

    Only identical explicit control sets can be combined. Overlapping different
    sets, invented IDs and missing coverage still fail the original validator.
    """
    if isinstance(result, dict) and set(result) == {'questions'}:
        result = result['questions']
    elif isinstance(result, dict) and 'text' in result and ('control_ids' in result or 'control_id' in result):
        result = [result]
    if isinstance(result, list) and len(group) < len(result) <= 4 * len(group):
        combined = {}
        by_id = {c['id']: c for c in group}
        for item in result:
            if not isinstance(item, dict):
                return result
            ids = item.get('control_ids', [item.get('control_id')])
            if not isinstance(ids, list) or not ids or any(not isinstance(cid, str) or cid not in by_id for cid in ids):
                return result
            # Validate each follow-up before combining any of its content.
            q = normalize_questions([item], [by_id[cid] for cid in ids])[0]
            key = tuple(sorted(ids))
            if key not in combined:
                combined[key] = q
            else:
                previous = combined[key]
                if q['text'] != previous['text']:
                    previous['text'] += '\n' + q['text']
                previous['departments'] = list(dict.fromkeys(previous['departments'] + q['departments']))
                if q['mapping_reason'] and q['mapping_reason'] != previous['mapping_reason']:
                    previous['mapping_reason'] += '\n' + q['mapping_reason']
        result = list(combined.values())
    return result


def generate_question_batches(request, controls, context, *, output_format_override=None, batch_size_override=None, text_fallback_override=None):
    output_format = (output_format_override or os.getenv('AI_QUESTION_OUTPUT_FORMAT', 'text')).lower()
    if output_format not in {'text', 'json'}:
        raise ValueError('AI_QUESTION_OUTPUT_FORMAT must be text or json')
    size = int(batch_size_override if batch_size_override is not None else os.getenv('AI_QUESTION_BATCH_SIZE', '8'))
    allow_text_fallback=(os.getenv('AI_QUESTION_TEXT_FALLBACK','true').lower()=='true'
                         if text_fallback_override is None else text_fallback_override)
    if not 1 <= size <= 16:
        raise ValueError('AI_QUESTION_BATCH_SIZE must be between 1 and 16')
    # Each clause now carries 3-4 prompts rather than a single question.
    size=min(size,4)
    agent_skill=load_question_skill()
    knowledge = prepare_knowledge(context.get('skill'), controls, questions=True)
    groups, pending, chars = [], [], 0
    for c in controls:
        count = len(c['requirement']) + len(c['id']) + len(c.get('department', ''))
        if pending and (len(pending) >= size or chars + count > 6000):
            groups.append(pending)
            pending, chars = [], 0
        pending.append(c)
        chars += count
    if pending:
        groups.append(pending)
    plan = {'batch_size': size, 'requests': 0, 'split_count': 0, 'format_retry_requests': 0,
            'successful_batches': 0, 'total_controls': len(controls), 'validation_failures': {}, 'text_fallback_requests': 0,
            'profile': 'sequential-questions-v3', 'output_format': output_format, 'text_batch_requests': 0,
            'single_text_requests': 0}
    plan['question_agent_skill']={k:agent_skill[k] for k in ('name','version','sha256')}
    positions = {c['id']: i + 1 for i, c in enumerate(controls)}
    ceiling = size

    def generate(group, format_retry=False, validation_code=None, text_only=False):
        nonlocal ceiling
        check_cancelled()
        ids = {c['id'] for c in group}
        scoped_context = deepcopy(context)
        if isinstance(scoped_context.get('skill'), dict):
            skill = scoped_context['skill']
            scoped_context['skill'] = {k: skill[k] for k in ('core_instructions', 'instructions') if k in skill}
            if 'instructions' in scoped_context['skill']:
                scoped_context['skill']['instructions'] = batch_instructions(skill['instructions'], ids)
        payload = {'controls': [{k: c[k] for k in ('id', 'requirement', 'department')} for c in group],
                   'context': scoped_context, 'required_control_ids': [c['id'] for c in group],
                   'question_agent_skill':agent_skill, 'clause_interview':True}
        if knowledge is not None:
            payload['standard_knowledge'] = scope_knowledge(knowledge, ids)
        if format_retry:
            payload['format_retry'] = True
            plan['format_retry_requests'] += 1
        if validation_code:
            payload['validation_retry_code'] = validation_code
        single_text = text_only or (output_format == 'text' and len(group) == 1)
        if single_text:
            payload['question_text_only'] = True
            if text_only:
                plan['text_fallback_requests'] += 1
            else:
                plan['single_text_requests'] += 1
        text_batch = output_format == 'text' and not single_text
        if text_batch:
            payload['question_text_batch'] = True
            for index, control in enumerate(payload['controls'], 1):
                control['question_index'] = index
            plan['text_batch_requests'] += 1
        start, end = positions[group[0]['id']], positions[group[-1]['id']]
        logger.info('questions.batch.start step=%s end_step=%s total=%s', start, end, len(controls))
        stage = 'request'
        started = time.perf_counter()
        try:
            plan['requests'] += 1
            result = request('questions', payload)
            check_cancelled()
            stage = 'validation'
            if (text_batch or single_text) and not isinstance(result, str):
                raise QuestionBatchError('AI gateway does not support text question batches; update inference/server.py to qwen35-v9 and restart the gateway; no assessment saved')
            # Unambiguous complete wrappers only. Never guess IDs, truncate an
            # overlong array or accept incomplete control coverage.
            result = (plain_question_output(result, group) if single_text else
                      text_question_batch_output(result, group) if text_batch else question_output(result, group))
            normalize_questions(result, group)
            plan['successful_batches'] += 1
            logger.info('questions.batch.done step=%s end_step=%s total=%s elapsed_seconds=%.3f',
                        start, end, len(controls), time.perf_counter()-started)
            return result
        except QuestionGenerationCancelled:
            raise
        except QuestionBatchError:
            logger.error('questions.batch.failed step=%s end_step=%s total=%s stage=%s code=gateway_contract_mismatch elapsed_seconds=%.3f',
                         start, end, len(controls), stage, time.perf_counter()-started)
            raise
        except Exception as exc:
            check_cancelled()
            code = failure_code(exc, stage)
            recoverable = False
            format_failure = False
            text_fallback = False
            validation_code = None
            provider_code=getattr(exc,'code',None)
            if isinstance(provider_code,str) and provider_code.replace('_','').isalnum():
                code=provider_code
                if provider_code in {'codex_json_invalid','codex_question_output_invalid'}:
                    recoverable=format_failure=True
            if isinstance(exc, QuestionValidationError):
                code = validation_code = exc.code
                recoverable = format_failure = True
                plan['validation_failures'][code] = plan['validation_failures'].get(code, 0) + 1
                logger.warning('questions.batch.validation_failed step=%s end_step=%s code=%s', start, end, code)
                if exc.reason:
                    logger.warning('questions.batch.validation_reason step=%s reason=%s', start, exc.reason)
                if code == 'question_array_invalid':
                    reason = ('wrong_root_type' if not isinstance(result, list)
                              else 'empty_array' if not result else 'too_many_questions')
                    logger.warning('questions.batch.array_invalid reason=%s count=%s limit=%s',
                                   reason, len(result) if isinstance(result, list) else None, len(group))
            if isinstance(exc, json.JSONDecodeError):
                code = 'invalid_json'
                recoverable = format_failure = True
            if isinstance(exc, httpx.HTTPStatusError):
                # Accept only known, content-free codes; never inspect raw output.
                header = exc.response.headers.get('X-Inference-Failure-Code')
                if header in {'gpu_out_of_memory', 'input_token_limit', 'output_token_limit', 'invalid_json', 'invalid_question_text'}:
                    code = header
                    recoverable = True
                    format_failure = header in {'invalid_json', 'invalid_question_text'}
                elif exc.response.status_code == 413:
                    recoverable = True
            logger.warning('questions.batch.request_failed step=%s end_step=%s total=%s stage=%s code=%s elapsed_seconds=%.3f',
                           start, end, len(controls), stage, code, time.perf_counter()-started)
            if text_only:
                raise QuestionBatchError(f'AI question generation failed at controls {start}–{end}/{len(controls)} ({code}); no assessment saved') from exc
            if recoverable and len(group) > 1:
                ceiling = min(ceiling, max(1, len(group) // 2))
                plan['split_count'] += 1
                logger.warning('questions.batch.split step=%s end_step=%s code=%s', start, end, code)
            elif format_failure and not format_retry:
                logger.warning('questions.batch.format_retry step=%s code=%s', start, code)
            elif (output_format == 'json' and len(group) == 1 and code == 'invalid_json' and format_retry
                  and allow_text_fallback):
                text_fallback = True
                logger.warning('questions.batch.text_fallback step=%s code=%s', start, code)
            else:
                raise QuestionBatchError(f'AI question generation failed at controls {start}–{end}/{len(controls)} ({code}); no assessment saved') from exc
        # Retry smaller groups after the failed request/traceback has unwound.
        if len(group) == 1:
            return generate(group, format_retry=True, validation_code=validation_code, text_only=text_fallback)
        middle = len(group) // 2
        return generate(group[:middle], validation_code=validation_code) + generate(group[middle:], validation_code=validation_code)

    questions = []
    for group in groups:
        offset = 0
        while offset < len(group):
            part = group[offset:offset + ceiling]
            questions.extend(generate(part))
            offset += len(part)
    plan['effective_batch_size'] = ceiling
    normalize_questions(questions, controls)
    return questions, plan
