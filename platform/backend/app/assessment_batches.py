"""Bound inference output size while retaining every control in the final run."""
import logging
import os
import time
import random
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import copy_context
from . import assessment_runtime
from pydantic import ValidationError
import httpx
from . import assessment_jobs
from .questionnaire import control_ids
from .skill_templates import batch_instructions
from .knowledge import scope_knowledge

logger = logging.getLogger('uvicorn.error')


def compact_batch(payload, controls):
    """Drop bookkeeping, never truncate requirements, answers or linked text."""
    from . import assessment_policy
    payload=assessment_policy.prepare(payload)
    ids = {c['id'] for c in controls}
    batch = {'controls': [{k: c[k] for k in ('id', 'requirement', 'department', 'assessment_questions', 'grading_examples') if k in c} for c in controls],
             'context': payload.get('context', {}),
             'answers': {c['id']: payload['answers'][c['id']] for c in controls},
             'evidence': [{k: e[k] for k in ('id', 'control_id', 'filename', 'text') if k in e}
                          for e in payload.get('evidence', []) if e['control_id'] in ids]}
    if payload.get('assessment_basis'):batch['assessment_basis']=payload['assessment_basis']
    if payload.get('note_review_mode') is True:batch['note_review_mode']=True
    batch['allowed_evidence_ids'] = {c['id']: [e['id'] for e in batch['evidence'] if e['control_id'] == c['id']]
                                     for c in controls}
    if payload.get('standard_knowledge') is not None:
        batch['standard_knowledge'] = scope_knowledge(payload['standard_knowledge'], ids)
    if payload.get('codex_agent_skills'):
        batch['codex_agent_skills'] = payload['codex_agent_skills']
    if payload.get('approved_examples'):
        batch['approved_examples'] = {c['id']:payload['approved_examples'].get(c['id'],[]) for c in controls}
    if 'questions' in payload:
        batch['questions'] = []
        for q in payload['questions']:
            targets = [cid for cid in control_ids(q) if cid in ids]
            if targets:
                batch['questions'].append({k:v for k,v in {
                    'id':q.get('id'),'source':q.get('source'),'control_ids':targets,
                    'control_id':targets[0],'text':q['text'],'mapping_reason':q.get('mapping_reason','')
                }.items() if v is not None})
    if 'policies' in payload:
        batch['policies'] = [{**{k: p[k] for k in ('id', 'name', 'version', 'text') if k in p},
                              'control_ids': [cid for cid in p['control_ids'] if cid in ids]}
                             for p in payload['policies'] if ids.intersection(p['control_ids'])]
    if 'skill' in payload:
        batch['skill'] = {k: payload['skill'][k] for k in ('core_instructions', 'instructions', 'review_procedure','assessment_basis')
                          if k in payload['skill']}
        if 'instructions' in batch['skill']:
            batch['skill']['instructions'] = batch_instructions(batch['skill']['instructions'], ids)
            if os.getenv('AI_ASSESS_PROMPT_PROFILE','compact-v1')=='compact-v1':
                import json
                try:
                    guidance=json.loads(batch['skill']['instructions'])
                    if isinstance(guidance,dict) and guidance.get('format')=='compliance-review-skill/v1':
                        guidance.pop('question_guidance',None)
                        batch['skill']['instructions']=json.dumps(guidance,ensure_ascii=False,separators=(',',':'))
                except (ValueError,TypeError):pass
    return batch


class AssessmentStepError(ValueError):
    def __init__(self, step, total, code):
        self.step, self.total, self.code = step, total, code
        super().__init__(f'AI assessment failed at control {step}/{total} ({code}); no changes saved')


def failure_code(exc, stage):
    if isinstance(exc, httpx.HTTPStatusError):
        detail=exc.response.headers.get('X-Inference-Failure-Code','')
        model_codes={'invalid_json','invalid_question_text','output_token_limit','gpu_out_of_memory','input_token_limit','inference_cancelled'}
        if detail in model_codes:return 'model_'+detail
        return f'gateway_http_{exc.response.status_code}'
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return 'inference_timeout'
    if isinstance(exc, httpx.RequestError):
        return 'inference_connection_failed'
    if isinstance(exc, ValidationError):
        return 'response_schema_invalid'
    if stage == 'validation':
        known = {'Provider must return exactly one finding per control': 'control_coverage_invalid',
                 'Provider must return exactly one finding per assessment question': 'question_coverage_invalid',
                 'Provider cited unknown or incorrectly linked evidence': 'evidence_reference_invalid'}
        return known.get(str(exc), 'response_validation_failed')
    return 'provider_response_failed'


def _assess_sequential(request, payload, validate, notify=True, position_offset=0, total_count=None):
    answer_based = payload.get('assessment_basis', payload.get('skill', {}).get('assessment_basis')) == 'sc_recorded_answers_v1'
    size_setting = 'SC_ASSESS_BATCH_SIZE' if answer_based else 'AI_ASSESS_BATCH_SIZE'
    size = int(os.getenv(size_setting, '1' if answer_based else '5'))
    if not 1 <= size <= 8:
        raise ValueError(f'{size_setting} must be between 1 and 8')
    repair_enabled = int(os.getenv('AI_ASSESS_REPAIR_ATTEMPTS', '0'))
    if repair_enabled not in (0, 1):
        raise ValueError('AI_ASSESS_REPAIR_ATTEMPTS must be 0 or 1')
    schema_retry_limit=int(os.getenv('AI_ASSESS_SCHEMA_RETRIES','1'))
    if schema_retry_limit not in (0,1):raise ValueError('AI_ASSESS_SCHEMA_RETRIES must be 0 or 1')
    coverage_enabled=int(os.getenv('AI_ASSESS_COVERAGE_REPAIR_ATTEMPTS','1'))
    if coverage_enabled not in (0,1):raise ValueError('AI_ASSESS_COVERAGE_REPAIR_ATTEMPTS must be 0 or 1')
    format_retry_limit=int(os.getenv('AI_ASSESS_FORMAT_RETRIES','1'))
    if format_retry_limit not in (0,1):raise ValueError('AI_ASSESS_FORMAT_RETRIES must be 0 or 1')
    mode = os.getenv('AI_ASSESS_FAILURE_MODE', 'continue')
    if mode not in {'continue', 'strict'}:
        raise ValueError('AI_ASSESS_FAILURE_MODE must be continue or strict')
    controls = payload['controls']
    question_items=[item for control in controls for item in control.get('assessment_questions',[])]
    answered = [c for c in controls if payload['answers'].get(c['id'], {}).get('text', '').strip()]
    if not answered:
        raise ValueError('Enter written answers before requesting AI assessment')
    total_count=total_count or len(answered)
    pending = [c for c in controls if c not in answered]
    thai = payload.get('context', {}).get('question_language') == 'th' and not answer_based
    findings = {c['id']: {'control_id': c['id'], 'status': 'needs_review',
        'rationale': 'ยังไม่มีคำตอบเป็นข้อความ ข้อนี้ยังไม่ได้ส่งให้ AI วิเคราะห์' if thai else 'No written answer yet. This control was not sent to the model.',
        'recommendation': 'กรอกคำตอบแล้วประเมินอีกครั้ง' if thai else 'Enter a written answer and reassess.',
        'evidence_ids': [],'question_findings':[{'question_id':item['question_id'],'question_text':item['question_text'],
            'status':'needs_review','rationale':'No written answer was recorded for this subquestion.',
            'recommendation':'Record an answer and reassess this question.','evidence_ids':[],
            'audit':{'category':'needs_review','reason':'No written answer was recorded for this subquestion.',
                     'requirement_gap':'','system_impact':'unknown'}}
            for item in c.get('assessment_questions',[])]} for c in pending}
    attempts = repairs = batches = coverage_repairs = schema_repairs = 0
    split_count = format_retry_requests = 0
    warnings, failed_ids = [], []
    request_metrics=[];batch_metrics=[];validation_seconds=0.0;transport_retries=0
    retry_limit=int(os.getenv('AI_ASSESS_TRANSIENT_RETRIES','1'))
    if not 0<=retry_limit<=2:raise ValueError('AI_ASSESS_TRANSIENT_RETRIES must be 0..2')
    backoff=float(os.getenv('AI_ASSESS_RETRY_BACKOFF_SECONDS','0.5'))
    if not 0<=backoff<=10:raise ValueError('AI_ASSESS_RETRY_BACKOFF_SECONDS must be 0..10')
    original_validate=validate
    def validate(*args):
        nonlocal validation_seconds
        began=time.perf_counter()
        try:return original_validate(*args)
        finally:validation_seconds+=time.perf_counter()-began

    def call_model(batch):
        nonlocal attempts,transport_retries
        for retry in range(retry_limit+1):
            assessment_jobs.check_cancelled()
            attempts+=1
            began=time.perf_counter()
            metric={'control_ids':[c['id'] for c in batch['controls']],'attempt':retry+1,
                    'format_retry':batch.get('format_retry') is True}
            try:
                result=request('assess',batch)
                metric['status']='completed'
                return result
            except assessment_jobs.AssessmentCancelled:
                metric['status']='cancelled'
                raise
            except Exception as exc:
                assessment_jobs.check_cancelled()
                code=failure_code(exc,'request')
                metric.update(status='failed',code=code)
                if isinstance(exc,httpx.HTTPStatusError):metric['http_status']=exc.response.status_code
                transient=code in {'gateway_http_502','gateway_http_503','gateway_http_504','inference_timeout','inference_connection_failed'}
                if not transient or retry==retry_limit:raise
                transport_retries+=1
                delay=min(10.0,backoff*(2**retry))+random.uniform(0,backoff*0.25)
                metric['backoff_seconds']=round(delay,6)
                logger.info('assessment.request.retry attempt=%s code=%s controls=%s',retry+1,code,len(batch['controls']))
                event=assessment_jobs.cancel_event()
                if event is not None:event.wait(delay)
                else:time.sleep(delay)
                assessment_jobs.check_cancelled()
            finally:
                metric['request_and_backoff_seconds']=round(time.perf_counter()-began,6)
                request_metrics.append(metric)
                assessment_jobs.check_cancelled()

    def recover_questions(control, batch, step, original=None):
        """Keep validated question outcomes and retry only unresolved questions."""
        nonlocal repairs, coverage_repairs
        units=control.get('assessment_questions',[])
        if not units:raise ValueError('No question units to recover')
        original=original if isinstance(original,dict) else {}
        recovered=[];parent=None
        for unit in units:
            assessment_jobs.check_cancelled()
            scoped_control={**control,'assessment_questions':[unit]}
            scoped=compact_batch(batch,[scoped_control])
            scoped['answers']={control['id']:{'status':'unanswered','text':unit.get('answer_text','')}}
            scoped['questions']=[{'id':unit['question_id'],'control_ids':[control['id']],
                'text':unit['question_text']}]
            matches=[row for row in original.get('question_findings',[]) if isinstance(row,dict)
                and row.get('question_id')==unit['question_id']]
            candidate={**original,'question_findings':matches}
            try:
                if len(matches)!=1:raise ValueError('Question missing or duplicated')
                checked=validate([candidate],[scoped_control],scoped['evidence'],scoped['answers'])[0]
            except (ValueError,TypeError,KeyError):
                repairs+=1;coverage_repairs+=1
                scoped['format_retry']=True
                logger.info('assessment.question.recovery control=%s question=%s',control['id'],unit['question_id'])
                try:
                    response=call_model(scoped)
                    # The request contains one exact question and its answer.
                    # A parent-only response can therefore be bound unambiguously.
                    if isinstance(response,list) and len(response)==1 and isinstance(response[0],dict):
                        row=response[0]
                        if row.get('control_id')==control['id'] and not row.get('question_findings') and row.get('audit'):
                            response=[{**row,'question_findings':[{
                                **{key:row[key] for key in ('status','rationale','recommendation','audit','evidence_ids') if key in row},
                                'question_id':unit['question_id'],'question_text':unit['question_text']}]}]
                    checked=validate(response,[scoped_control],scoped['evidence'],scoped['answers'])[0]
                except assessment_jobs.AssessmentCancelled:raise
                except Exception as exc:
                    code=failure_code(exc,'validation' if isinstance(exc,ValueError) else 'request')
                    warnings.append({'control_ids':[control['id']],'question_id':unit['question_id'],
                        'code':code,'stage':'question_recovery','step':step})
                    if control['id'] not in failed_ids:failed_ids.append(control['id'])
                    reason='AI could not complete this question; review or reassess it.'
                    recovered.append({'question_id':unit['question_id'],'question_text':unit['question_text'],
                        'status':'needs_review','rationale':reason,'recommendation':reason,'evidence_ids':[],
                        'audit':{'category':'needs_review','reason':reason,'requirement_gap':'','system_impact':'unknown'}})
                    continue
            parent=parent or checked
            recovered.extend(checked['question_findings'])
        parent=parent or {'control_id':control['id'],'status':'needs_review','rationale':'Question assessments require review.',
            'recommendation':'Review unresolved questions.','evidence_ids':[]}
        parent={**parent,'question_findings':recovered,
            'rationale':'\n'.join(row['rationale'] for row in recovered if row.get('rationale'))[:20000]}
        return validate([parent],[control],batch['evidence'],batch['answers'])[0]

    def request_with_recovery(control_list, batch, step):
        """Retry model-format/resource failures at smaller scopes, preserving good peers."""
        nonlocal split_count, format_retry_requests
        try:
            return call_model(batch), []
        except assessment_jobs.AssessmentCancelled:
            raise
        except Exception as exc:
            assessment_jobs.check_cancelled()
            code = failure_code(exc, 'request')
            recoverable = code in {
                'model_invalid_json', 'model_output_token_limit', 'model_input_token_limit',
                'model_gpu_out_of_memory', 'gateway_http_502', 'inference_timeout',
            }
            if recoverable and len(control_list) > 1:
                split_count += 1
                midpoint = len(control_list) // 2
                left, right = control_list[:midpoint], control_list[midpoint:]
                # Splitting is an automatic recovery step; only controls that
                # remain unresolved below should be reported as warnings.
                logger.info('assessment.control.split.recovery step=%s end_step=%s code=%s',
                            step, step + len(control_list) - 1, code)
                combined, failures = [], []
                for group in (left, right):
                    child_batch = compact_batch(payload, group)
                    child_step = step + (0 if group is left else len(left))
                    child_raw, child_failures = request_with_recovery(group, child_batch, child_step)
                    if isinstance(child_raw, list):
                        combined.extend(child_raw)
                    elif child_raw is not None:
                        combined.append(child_raw)
                    failures.extend(child_failures)
                return combined, failures
            if code == 'model_invalid_json' and len(control_list) == 1 and format_retry_limit:
                format_retry_requests += 1
                retry_batch = {**batch, 'format_retry': True}
                reason=exc.response.headers.get('X-Inference-Format-Reason','unspecified') if isinstance(exc,httpx.HTTPStatusError) else 'unspecified'
                if not reason.replace('_','').isalpha() or len(reason)>80:reason='unspecified'
                logger.info('assessment.control.format_retry step=%s code=%s reason=%s', step, code, reason)
                try:
                    return call_model(retry_batch), []
                except assessment_jobs.AssessmentCancelled:
                    raise
                except Exception as retry_error:
                    assessment_jobs.check_cancelled()
                    if control_list[0].get('assessment_questions'):
                        return [recover_questions(control_list[0],batch,step)], []
                    return None, [(control_list[0], retry_error, 'request')]
            if recoverable and len(control_list)==1 and control_list[0].get('assessment_questions'):
                return [recover_questions(control_list[0],batch,step)], []
            return None, [(control, exc, 'request') for control in control_list]

    def failed(control_list, exc, stage, step):
        assessment_jobs.check_cancelled()
        code = failure_code(exc, stage)
        logger.warning('assessment.control.warning step=%s count=%s total=%s stage=%s code=%s',
                       step, len(control_list), total_count, stage, code)
        if isinstance(exc, ValidationError):
            fields = {'control_id', 'status', 'rationale', 'recommendation', 'evidence_ids', 'interpreted_answer',
                      'audit', 'category', 'reason', 'requirement_gap', 'system_impact'}
            invalid_fields = sorted({part for error in exc.errors(include_input=False, include_context=False)
                                     for part in error['loc'] if isinstance(part, str) and part in fields})
            logger.warning('assessment.schema_invalid step=%s fields=%s', step, ','.join(invalid_fields) or 'root')
        if mode == 'strict':
            raise AssessmentStepError(step, total_count, code) from exc
        ids = [c['id'] for c in control_list]
        warnings.append({'control_ids': ids, 'code': code, 'stage': stage, 'step': step})
        for cid in ids:
            failed_ids.append(cid)
            findings[cid] = {'control_id': cid, 'status': 'needs_review', 'evidence_ids': [],
                'rationale': ('ระบบยังตรวจข้อนี้ไม่สำเร็จ รอตรวจเพิ่มเติม' if thai else
                              'System warning: AI assessment did not complete for this control.') + ' (' + code + ')',
                'recommendation': 'ตรวจด้วยผู้ประเมินหรือประเมินข้อนี้อีกครั้ง' if thai else
                                  'Review manually or reassess this control.',
                'question_findings':[{'question_id':item['question_id'],'question_text':item['question_text'],
                    'status':'needs_review','rationale':('ระบบประเมินคำถามนี้ไม่สำเร็จ' if thai else
                        'AI assessment did not complete for this question.')+' ('+code+')',
                    'recommendation':'Review manually or reassess this question.', 'evidence_ids':[],
                    'audit':{'category':'needs_review','reason':'AI assessment did not complete for this question.',
                        'requirement_gap':'','system_impact':'unknown'}} for control in control_list if control['id']==cid
                    for item in control.get('assessment_questions',[])]}

    for start in range(0, len(answered), size):
        batch_controls = answered[start:start+size]
        batch = compact_batch(payload, batch_controls)
        batch_questions=[item for control in batch_controls for item in control.get('assessment_questions',[])]
        step, end_step = position_offset + start + 1, position_offset + start + len(batch_controls)
        started = time.perf_counter()
        validation_before=validation_seconds
        batches += 1
        logger.info('assessment.control.start step=%s end_step=%s total=%s', step, end_step, total_count)
        if batch_questions:logger.info('assessment.questions.start controls=%s count=%s answered=%s',
                    ','.join(c['id'] for c in batch_controls),len(batch_questions),
                    sum(bool(item.get('answer_text','').strip()) for item in batch_questions))
        try:
            raw, request_failures = request_with_recovery(batch_controls, batch, step)
            for control, error, stage in request_failures:
                failed([control], error, stage, step + batch_controls.index(control))
            valid_controls = [control for control in batch_controls
                              if all(control['id'] != failed_control['id'] for failed_control, _, _ in request_failures)]
            if not valid_controls:
                elapsed=time.perf_counter()-started
                batch_metrics.append({'control_ids':[c['id'] for c in batch_controls],'seconds':round(elapsed,6),'validation_seconds':0.0,'amortized_control_seconds':round(elapsed/len(batch_controls),6)})
                if notify:assessment_runtime.completed(batch_controls,[],[c['id'] for c in batch_controls])
                logger.info('assessment.control.done step=%s end_step=%s total=%s elapsed_seconds=%.3f',
                            step, end_step, total_count, time.perf_counter()-started)
                continue
        except (AssessmentStepError, assessment_jobs.AssessmentCancelled):
            raise
        except Exception as exc:
            failed(batch_controls, exc, 'request', step)
            elapsed=time.perf_counter()-started
            batch_metrics.append({'control_ids':[c['id'] for c in batch_controls],'seconds':round(elapsed,6),'validation_seconds':0.0,'amortized_control_seconds':round(elapsed/len(batch_controls),6)})
            if notify:assessment_runtime.completed(batch_controls,[],[c['id'] for c in batch_controls])
            continue
        try:
            output = validate(raw, valid_controls, batch['evidence'], batch['answers'])
        except Exception as batch_error:
            # Recover only uniquely identified, individually valid peer findings.
            # Unknown IDs, duplicates, malformed items and invented citations are
            # never accepted or converted into a pass.
            code = failure_code(batch_error, 'validation')
            if mode == 'strict' and not (repair_enabled and code == 'evidence_reference_invalid'):
                failed(batch_controls, batch_error, 'validation', step)
            by_id = {}
            extras = False
            expected_ids = {c['id'] for c in valid_controls}
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, dict) and isinstance(item.get('control_id'), str) and item['control_id'] in expected_ids:
                    by_id.setdefault(item['control_id'], []).append(item)
                else:
                    extras = True
            for offset, control in enumerate(valid_controls):
                single = compact_batch(payload, [control])
                items = by_id.get(control['id'], [])
                stage = 'validation'
                try:
                    if len(items) != 1:
                        if mode!='continue' or not coverage_enabled:raise batch_error
                        if control.get('assessment_questions'):
                            checked=[recover_questions(control,single,step+offset)]
                            findings[control['id']]=checked[0]
                            continue
                        repairs+=1;coverage_repairs+=1
                        single['required_control_ids']=[control['id']]
                        single['validation_feedback']={'code':'control_coverage_invalid',
                            'message':'Return a JSON array with exactly one finding for the single supplied control. Copy its exact control_id; do not omit, renumber or duplicate it.'}
                        logger.info('assessment.control.coverage_repair step=%s total=%s attempt=1',step+batch_controls.index(control),total_count)
                        stage='request';repaired=call_model(single);stage='validation'
                        checked=validate(repaired,[control],single['evidence'],single['answers'])
                    else:
                        try:
                            checked = validate(items, [control], single['evidence'], single['answers'])
                        except ValidationError as schema_error:
                            if control.get('assessment_questions'):
                                checked=[recover_questions(control,single,step+offset,items[0])]
                                findings[control['id']]=checked[0]
                                continue
                            if not schema_retry_limit:raise
                            repairs+=1;schema_repairs+=1
                            single['format_retry']=True
                            single['validation_feedback']={'code':'response_schema_invalid',
                            'message':'Return exactly one compact finding with c=exact control ID, s=status, i=brief interpretation, r=concise rationale, n=brief action, and a={c:category,r:reason,g:requirement_gap,i:system_impact}. Use only allowed values; keep every text field concise and in English; do not output evidence IDs.'}
                            logger.info('assessment.control.schema_retry step=%s total=%s attempt=1',step+batch_controls.index(control),total_count)
                            stage='request';repaired=call_model(single);stage='validation'
                            checked=validate(repaired,[control],single['evidence'],single['answers'])
                        except ValueError as item_error:
                            if failure_code(item_error,'validation') == 'question_coverage_invalid':
                                checked=[recover_questions(control,single,step+offset,items[0])]
                                findings[control['id']]=checked[0]
                                continue
                            if not repair_enabled or failure_code(item_error, 'validation') != 'evidence_reference_invalid':
                                raise
                            repairs += 1
                            logger.info('assessment.control.repair step=%s total=%s attempt=1', step+batch_controls.index(control), total_count)
                            single['validation_feedback'] = {'code': 'evidence_reference_invalid',
                                'message': 'Use only this control allowed_evidence_ids; if empty return evidence_ids: []. Never invent IDs.'}
                            stage = 'request'
                            repaired = call_model(single)
                            stage = 'validation'
                            checked = validate(repaired, [control], single['evidence'], single['answers'])
                    findings[control['id']] = checked[0]
                except Exception as exc:
                    failed([control], exc, stage, step+batch_controls.index(control))
            if extras:
                warnings.append({'control_ids': [], 'code': 'unexpected_findings_ignored', 'stage': 'validation', 'step': step})
        else:
            findings.update({f['control_id']: f for f in output})
        elapsed=time.perf_counter()-started
        batch_metrics.append({'control_ids':[c['id'] for c in batch_controls],'seconds':round(elapsed,6),'validation_seconds':round(validation_seconds-validation_before,6),'amortized_control_seconds':round(elapsed/len(batch_controls),6)})
        if notify:assessment_runtime.completed(batch_controls,[findings[c['id']] for c in batch_controls if c['id'] not in failed_ids],[c['id'] for c in batch_controls if c['id'] in failed_ids])
        logger.info('assessment.control.done step=%s end_step=%s total=%s elapsed_seconds=%.3f',
                    step, end_step, total_count, time.perf_counter()-started)
    assessment_jobs.check_cancelled()
    failed_set = set(failed_ids)
    plan = {'batch_size': size, 'requests': attempts, 'batch_requests': batches, 'repair_requests': repairs,'coverage_repair_requests':coverage_repairs,
            'split_count':split_count,'format_retry_requests':format_retry_requests,'schema_repair_requests':schema_repairs,
            'attempted_control_ids': [c['id'] for c in answered],
            'ai_control_ids': [c['id'] for c in answered if c['id'] not in failed_set],
            'failed_control_ids': failed_ids, 'unanswered_control_ids': [c['id'] for c in pending],
            'assessment_unit':'question' if question_items else 'control','assessment_control_count':len(controls),'assessment_question_count':len(question_items),
            'answered_question_count':sum(bool(item.get('answer_text','').strip()) for item in question_items),
            'unanswered_question_count':sum(not item.get('answer_text','').strip() for item in question_items),
            'warnings': warnings, 'completion_status': 'completed_with_warnings' if warnings else 'completed',
            'profile': 'compact-v2', 'failure_mode': mode, 'concurrency':1, 'transport_retry_requests':transport_retries,
            'batch_metrics':batch_metrics,'request_metrics':request_metrics,'validation_seconds':round(validation_seconds,6)}
    if notify:logger.info('assessment.finished controls=%s validated=%s failed=%s warnings=%s requests=%s',
                len(controls), len(plan['ai_control_ids']), len(failed_ids), len(warnings), attempts)
    return [findings[c['id']] for c in controls], plan


def assess_batches(request,payload,validate):
    concurrency=int(os.getenv('AI_ASSESS_CONCURRENCY','1'))
    if concurrency not in (1,2,3):raise ValueError('AI_ASSESS_CONCURRENCY must be 1, 2 or 3')
    if concurrency==1:return _assess_sequential(request,payload,validate)
    answer_based=payload.get('assessment_basis',payload.get('skill',{}).get('assessment_basis'))=='sc_recorded_answers_v1'
    size_setting='SC_ASSESS_BATCH_SIZE' if answer_based else 'AI_ASSESS_BATCH_SIZE'
    size=int(os.getenv(size_setting,'1' if answer_based else '5'))
    if not 1<=size<=8:raise ValueError(f'{size_setting} must be between 1 and 8')
    controls=payload['controls']
    answered=[c for c in controls if payload['answers'].get(c['id'],{}).get('text','').strip()]
    if not answered:raise ValueError('Enter written answers before requesting AI assessment')
    groups=[answered[i:i+size] for i in range(0,len(answered),size)]
    findings={};plans=[];index=0;active={}
    executor=ThreadPoolExecutor(max_workers=concurrency,thread_name_prefix='assessment')
    try:
        while active or index<len(groups):
            assessment_jobs.check_cancelled()
            while len(active)<concurrency and index<len(groups):
                group=groups[index];index+=1
                scoped=compact_batch(payload,group)
                context=copy_context()
                future=executor.submit(context.run,_assess_sequential,request,scoped,validate,False,(index-1)*size,len(answered))
                active[future]=group
            done,_=wait(active,timeout=0.1,return_when=FIRST_COMPLETED)
            for future in done:
                group=active.pop(future)
                rows,plan=future.result()
                findings.update({r['control_id']:r for r in rows})
                plans.append(plan)
                failed=set(plan['failed_control_ids'])
                assessment_runtime.completed(group,[r for r in rows if r['control_id'] not in failed],failed)
    finally:
        for future in active:future.cancel()
        # Cancellable HTTP workers observe the shared job event; legacy requests
        # retain their configured timeout and no result can commit after cancel.
        executor.shutdown(wait=True,cancel_futures=True)
    thai=payload.get('context',{}).get('question_language')=='th' and not answer_based
    for c in controls:
        if c['id'] not in findings:
            reason='ยังไม่มีคำตอบเป็นข้อความ ข้อนี้ยังไม่ได้ส่งให้ AI วิเคราะห์' if thai else 'No written answer yet. This control was not sent to the model.'
            findings[c['id']]={'control_id':c['id'],'status':'needs_review','rationale':reason,'recommendation':'กรอกคำตอบแล้วประเมินอีกครั้ง' if thai else 'Enter a written answer and reassess.','evidence_ids':[],
                'question_findings':[{'question_id':item['question_id'],'question_text':item['question_text'],'status':'needs_review',
                    'rationale':'No written answer was recorded for this subquestion.','recommendation':'Record an answer and reassess this question.',
                    'evidence_ids':[],'audit':{'category':'needs_review','reason':'No written answer was recorded for this subquestion.',
                        'requirement_gap':'','system_impact':'unknown'}} for item in c.get('assessment_questions',[])]}
    failed={cid for plan in plans for cid in plan['failed_control_ids']}
    combined={key:sum(plan.get(key,0) for plan in plans) for key in ('requests','batch_requests','repair_requests','coverage_repair_requests','transport_retry_requests','validation_seconds','split_count','format_retry_requests','schema_repair_requests')}
    combined.update(batch_size=size,concurrency=concurrency,profile='compact-v2',failure_mode=os.getenv('AI_ASSESS_FAILURE_MODE','continue'),
        assessment_unit='question' if any(c.get('assessment_questions') for c in controls) else 'control',assessment_control_count=len(controls),assessment_question_count=sum(len(c.get('assessment_questions',[])) for c in controls),
        answered_question_count=sum(bool(q.get('answer_text','').strip()) for c in controls for q in c.get('assessment_questions',[])),
        unanswered_question_count=sum(not q.get('answer_text','').strip() for c in controls for q in c.get('assessment_questions',[])),
        attempted_control_ids=[c['id'] for c in answered],ai_control_ids=[c['id'] for c in answered if c['id'] not in failed],
        failed_control_ids=[c['id'] for c in answered if c['id'] in failed],unanswered_control_ids=[c['id'] for c in controls if c not in answered],
        warnings=[w for plan in plans for w in plan['warnings']],batch_metrics=[m for plan in plans for m in plan['batch_metrics']],
        request_metrics=[m for plan in plans for m in plan['request_metrics']])
    combined['completion_status']='completed_with_warnings' if combined['warnings'] else 'completed'
    logger.info('assessment.finished controls=%s validated=%s failed=%s warnings=%s requests=%s concurrency=%s',len(controls),len(combined['ai_control_ids']),len(failed),len(combined['warnings']),combined['requests'],concurrency)
    return [findings[c['id']] for c in controls],combined
