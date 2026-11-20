"""Answer-based department mapping with exact-input caching and bounded AI calls."""
from copy import deepcopy
from collections import deque
from . import assessment_jobs
from hashlib import sha256
import json
import os
import logging
from pydantic import TypeAdapter
from .departments import company_departments
from .schemas import AnswerDepartmentMapping
from .assessment_batches import failure_code
from .usage import process_usage, safe_usage

logger=logging.getLogger('uvicorn.error')
VERSION='answer-departments-v2'
CONTEXT_FIELDS=('company','business_type','departments','it_systems','data_types','existing_policies','question_language')
MAPPING_CONTEXT_FIELDS=('business_type','departments','question_language')
INSTRUCTION=(
 'Map each supplied written answer to the company departments whose people actually perform, own, approve, or collaborate in the work described. '
 'Return a JSON array with one object per supplied control: control_id, departments (array of exact allowed_departments names), reason (one short sentence). '
 'Use the written answer as the primary source; use the question and requirement to understand the work, not to assume the standard owner. Interpret abbreviations in context. '
 'Map every participating department supported by the answer, including collaborating teams; do not infer a department from a control topic alone. '
 'A department mentioned only as excluded, uninvolved, or a recipient is not a work owner. Do not copy all departments or invent names. '
 'If the answer says only “we” or is too vague to identify the teams, use departments: [] and explain the ambiguity instead of guessing. '
 'Do not produce compliance findings. Preserve exact control IDs. Write reasons in context.question_language.'
)


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def public_context(context):
    return {k:context[k] for k in CONTEXT_FIELDS if k in context}


def mapping_context(context):
    # Keep department mapping focused on actual answer ownership; large policy,
    # data and system inventories consume tokens without identifying responders.
    return {k:context[k] for k in MAPPING_CONTEXT_FIELDS if k in context}


def input_hash(control,answer,context,questions):
    return digest({'version':VERSION,'control':{k:control.get(k) for k in ('id','requirement')},
        'text':answer.get('text',''),'context':mapping_context(context),
        'questions':[{'ids':q.get('control_ids',[q.get('control_id')]),'text':q.get('text','')}
                     for q in questions if control['id'] in q.get('control_ids',[q.get('control_id')])]})


def current_entry(control,answer,context,questions):
    entry=context.get('answer_department_map',{}).get(control['id'])
    if not isinstance(entry,dict) or entry.get('input_hash')!=input_hash(control,answer,context,questions):return None
    names=entry.get('departments')
    if not isinstance(names,list) or len(names)!=len(set(names)) or not set(names)<=set(company_departments(context)):return None
    return entry


def validate(result,controls,names):
    parsed=TypeAdapter(list[AnswerDepartmentMapping]).validate_python(result)
    ids=[x.control_id for x in parsed]
    if len(ids)!=len(controls) or len(set(ids))!=len(ids) or set(ids)!={c['id'] for c in controls}:
        raise ValueError('Department mapping coverage invalid')
    if any(len(x.departments)!=len(set(x.departments)) or not set(x.departments)<=set(names) or not x.reason.strip() for x in parsed):
        raise ValueError('Department mapping names or reasons invalid')
    return [x.model_dump() for x in parsed]


def refresh(controls,answers,context,questions,provider_factory,force=False):
    assessment_jobs.check_cancelled()
    context=deepcopy(context)
    names=company_departments(context)
    valid={c['id']:current_entry(c,answers.get(c['id'],{}),context,questions) for c in controls}
    context['answer_department_map']={cid:entry for cid,entry in valid.items() if entry is not None}
    targets=[c for c in controls if answers.get(c['id'],{}).get('text','').strip()
             and c['id'] not in context.get('department_overrides',{}) and (force or valid[c['id']] is None)]
    plan={'version':VERSION,'mapped_control_ids':[],'reused_control_ids':[c['id'] for c in controls if valid[c['id']] is not None and not force],
          'pending_control_ids':[],'warnings':[],'requests':0}
    if not targets or not names:return context,plan,None
    if len(names)==1:
        for c in targets:
            context['answer_department_map'][c['id']]={'control_id':c['id'],'departments':names[:],
                'reason':'บริษัทระบุแผนกเดียวสำหรับ assessment นี้','input_hash':input_hash(c,answers[c['id']],context,questions),
                'provider':'company_single_department','version':VERSION}
            plan['mapped_control_ids'].append(c['id'])
        return context,plan,None
    provider=None
    try:
        provider=provider_factory()
    except Exception as exc:
        code='inference_configuration_missing' if hasattr(exc,'missing') else 'department_provider_unavailable'
        plan['pending_control_ids']=[c['id'] for c in targets]
        plan['warnings']=[{'control_ids':plan['pending_control_ids'],'code':code}]
        return context,plan,provider
    delegate=getattr(provider,'delegate',provider)
    if provider.name.startswith('offline'):
        plan['pending_control_ids']=[c['id'] for c in targets]
        return context,plan,provider
    method=getattr(delegate,'map_departments',None)
    if not callable(method):
        plan['pending_control_ids']=[c['id'] for c in targets]
        plan['warnings']=[{'control_ids':plan['pending_control_ids'],'code':'department_mapping_unsupported'}]
        return context,plan,provider
    size=int(os.getenv('AI_DEPARTMENT_BATCH_SIZE','4'))
    if not 1<=size<=16:raise ValueError('AI_DEPARTMENT_BATCH_SIZE must be 1..16')
    batches=[];batch=[];chars=0
    for c in targets:
        length=len(c['requirement'])+len(answers[c['id']]['text'])
        if batch and (len(batch)>=size or chars+length>10000):batches.append(batch);batch=[];chars=0
        batch.append(c);chars+=length
    if batch:batches.append(batch)
    queue=deque((batch,0,False) for batch in batches)
    i=-1
    while queue:
        assessment_jobs.check_cancelled()
        i+=1
        batch,depth,format_retry=queue.popleft()
        ids={c['id'] for c in batch}
        payload={'controls':[{k:c[k] for k in ('id','requirement')} for c in batch],
                 'answers':{cid:answers[cid]['text'] for cid in ids},'allowed_departments':names,
                 'context':mapping_context(context),
                 'questions':[{'control_ids':[cid for cid in q.get('control_ids',[q.get('control_id')]) if cid in ids],'text':q['text']}
                              for q in questions if ids.intersection(q.get('control_ids',[q.get('control_id')]))]}
        if format_retry:
            payload['format_retry']=True
            payload['validation_retry_code']='department_mapping_invalid'
        before=len(getattr(delegate,'usage_calls',[]));plan['requests']+=1
        stage='request'
        logger.info('departments.batch.start step=%s controls=%s',i+1,len(batch))
        try:
            raw=method(payload)
            assessment_jobs.check_cancelled()
            stage='validation'
            rows=validate(raw,batch,names)
        except Exception as exc:
            assessment_jobs.check_cancelled()
            code=failure_code(exc,stage)
            logger.warning('departments.batch.failed step=%s stage=%s code=%s',i+1,stage,code)
            transient=code in {'gateway_http_502','gateway_http_503','gateway_http_504','inference_timeout','inference_connection_failed'}
            recoverable=transient or code in {'model_invalid_json','model_output_token_limit','model_input_token_limit','model_gpu_out_of_memory','response_schema_invalid','response_validation_failed','provider_response_failed'}
            # A smaller prompt often fixes malformed/incomplete model output; one
            # constrained retry gives a singleton a chance to recover as well.
            if recoverable and len(batch)>1 and depth<4:
                middle=len(batch)//2
                queue.appendleft((batch[middle:],depth+1,False))
                queue.appendleft((batch[:middle],depth+1,False))
                logger.info('departments.batch.split step=%s controls=%s',i+1,len(batch))
                continue
            retry_singleton=transient or code in {'model_invalid_json','model_output_token_limit','response_schema_invalid','response_validation_failed','provider_response_failed'}
            if retry_singleton and len(batch)==1 and not format_retry:
                queue.appendleft((batch,depth,True))
                logger.info('departments.batch.format_retry step=%s code=%s',i+1,code)
                continue
            pending=[c['id'] for c in batch]
            plan['pending_control_ids'].extend(pending)
            plan['warnings'].append({'control_ids':pending,'code':code})
            continue
        else:
            for row in rows:
                c=next(c for c in batch if c['id']==row['control_id'])
                entry={**row,'input_hash':input_hash(c,answers[c['id']],context,questions),'provider':provider.name,'version':VERSION}
                context['answer_department_map'][c['id']]=entry
                plan['mapped_control_ids'].append(c['id'])
                if not row['departments']:plan['pending_control_ids'].append(c['id'])
        finally:
            if hasattr(delegate,'usage_calls') and len(delegate.usage_calls)==before:delegate.usage_calls.append(safe_usage(None))
    assessment_jobs.check_cancelled()
    return context,plan,provider
