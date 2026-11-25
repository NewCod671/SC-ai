"""Isolated experimental assessment workflow and explicit human-reviewed examples."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import copy,deepcopy
from itertools import islice
from datetime import datetime,timezone
import json
import os
import time
from fastapi import HTTPException
from sqlalchemy import select,update
from .models import Assessment,Standard,Revision,Evidence,Policy,AgentRun,AuditorFeedback,AssessmentAISettings
from . import assessment_jobs, audit_classification, codex_provider, assessment_policy, grading_knowledge, knowledge, standard_profiles
from .reporting import excel
from .answer_departments import digest,public_context
from .outcomes import summarize
from .usage import process_usage,safe_usage


def get(db,model,key):
    row=db.get(model,key)
    if row is None:raise HTTPException(404,'Record not found')
    return row


def mode(db,assessment_id):
    settings=db.get(AssessmentAISettings,assessment_id)
    return settings.mode if settings else 'colab'


def client_inputs(db,a):
    s=get(db,Standard,a.standard_id)
    revision=db.scalar(select(Revision).where(Revision.assessment_id==a.id,Revision.version==a.current_version))
    evidence=[{'id':e.id,'control_id':e.control_id,'filename':e.filename,'sha256':e.sha256,'text':e.text} for e in db.scalars(select(Evidence).where(Evidence.assessment_id==a.id).order_by(Evidence.id))]
    policies={}
    for p in db.scalars(select(Policy).where(Policy.assessment_id==a.id).order_by(Policy.created_at,Policy.id)):
        policies[p.name]={'id':p.id,'name':p.name,'version':p.version,'text':p.text,'control_ids':p.control_ids}
    return {'standard_id':s.id,'controls':deepcopy(s.controls),'answers':deepcopy(revision.answers),
        'context':public_context(a.context),'audit_participants':{k:a.context.get(k,'') for k in ('auditor','observer')},
        'questions':deepcopy(a.questions),'evidence':evidence,'policies':list(policies.values())}


def approved_examples(db,assessment_id,control,bundle=None):
    current_rules={name:value['sha256'] for name,value in (bundle or codex_provider.skill_bundle()).items() if name in {'evidence-validator','finding-classifier'}}
    rows=db.scalars(select(AuditorFeedback).where(AuditorFeedback.assessment_id==assessment_id,
        AuditorFeedback.control_id==control['id'],AuditorFeedback.decision=='approved').order_by(AuditorFeedback.created_at.desc(),AuditorFeedback.id.desc())).all()
    seen=set();result=[]
    for row in rows:
        if row.snapshot.get('requirement_hash')!=digest(control['requirement']):continue
        if row.snapshot.get('review',{}).get('rule_hashes')!=current_rules:continue
        key=row.snapshot['case_hash']
        if key in seen:continue
        seen.add(key)
        result.append({'historical_case_id':row.id,'historical_answer':row.snapshot['answer'],
            'approved_category':row.category,
            'auditor_explanation':row.explanation,'previous_ai_category':(row.prediction.get('audit') or {}).get('category','unclassified'),'skill_versions':row.snapshot.get('skill_versions',{})})
        if len(result)==3:break
    return result


def make_case(inputs,control,examples,grading_cases=None,standard_knowledge=None,standard_profile=None,standard_meta=None):
    cid=control['id']
    context={k:v for k,v in inputs['context'].items() if k!='departments'}
    questions=[{k:v for k,v in q.items() if k not in {'department','departments'}}
        for q in inputs['questions'] if cid in q.get('control_ids',[q.get('control_id')])]
    case_control={k:v for k,v in control.items() if k in {'id','requirement'}}
    grading_enabled=True if standard_profile is None else standard_profile.grading_knowledge_enabled
    case_control['grading_examples']=grading_knowledge.examples(control,grading_cases,enabled=grading_enabled)
    if standard_profile is not None:
        guidance=standard_profiles.control_rules(standard_profile,cid)
        if guidance:case_control['standard_guidance']=guidance
    if standard_knowledge is not None:
        scoped_kb=knowledge.scope_knowledge(standard_knowledge,{cid})
        if scoped_kb['entries']:case_control['standard_knowledge']=scoped_kb
    case={'control_id':cid,'control':case_control,
        'context':context,'answer':inputs['answers'][cid],
        'questions':questions,
        'evidence':[], 'policies':[],
        'approved_examples':examples}
    if standard_meta is not None:
        case['standard']=standard_meta
    return case


def pending(cid,reason='Not assessed by Codex or insufficient recorded information.',questions=None):
    return {'control_id':cid,'status':'needs_review','interpreted_answer':'','rationale':reason,'recommendation':'Review the note or reassess.',
        'evidence_ids':[],'audit':audit_classification.pending(reason),
        'question_findings':[{'question_id':item['question_id'],'question_text':item['question_text'],
            'status':'needs_review','rationale':reason,'recommendation':'Record an answer and reassess this question.',
            'evidence_ids':[],'audit':audit_classification.pending(reason)} for item in (questions or [])]}


def assessed_inputs(inputs):
    values={k:v for k,v in inputs.items() if k not in {'evidence','policies'}}
    if isinstance(values.get('context'),dict):
        values['context']={k:v for k,v in values['context'].items() if k!='departments'}
    values['controls']=[{k:v for k,v in c.items() if k not in {'department','departments'}}
                        for c in values.get('controls',[])]
    values['questions']=[{k:v for k,v in q.items() if k not in {'department','departments'}}
                         for q in values.get('questions',[])]
    return values


def run_json(db,a,run):
    s=get(db,Standard,a.standard_id)
    profile=standard_profiles.resolve(s)
    snapshot=run.snapshot
    stale=a.current_version!=run.revision or digest(assessed_inputs(client_inputs(db,a)))!=snapshot['client_hash'] or snapshot.get('context_protocol')!=codex_provider.CONTEXT_PROTOCOL
    try:stale=stale or digest(codex_provider.skill_bundle())!=digest(snapshot['skills'])
    except (ValueError,OSError,KeyError):stale=True
    expected_grading=grading_knowledge.load()[1] if profile.grading_knowledge_enabled else None
    stale=stale or snapshot.get('grading_knowledge')!=expected_grading
    stale=stale or snapshot.get('standard_knowledge')!=profile.knowledge_ref
    stale=stale or snapshot.get('standard_profile')!=standard_profiles.profile_snapshot(s,profile)
    result=deepcopy(run.results)
    summary=summarize(s,result)
    if run.status not in {'completed','completed_with_warnings'}:
        summary.update(status='needs_review',reason='Experimental run is partial or interrupted; resume or review it.')
    return {'id':run.id,'revision':run.revision,'provider':run.provider,'experimental':True,'processing_mode':'codex',
        'status':run.status,'review_required':True,'completion_status':'completed' if run.status=='completed' else 'completed_with_warnings',
        'skill':snapshot,'usage':snapshot.get('usage'),'analysis_plan':snapshot['assessment_plan'],
        'results':result,'summary':summary,'summary_source':'experimental_snapshot','stale':stale}



def batch_limits():
    try:
        size=int(os.getenv('CODEX_BATCH_SIZE','1'))
        chars=int(os.getenv('CODEX_BATCH_MAX_CHARS','48000'))
        if not 1<=size<=20 or not 1000<=chars<=500000:raise ValueError()
        return size,chars
    except ValueError:raise HTTPException(503,'Invalid Codex batch size/context budget configuration')


def control_batches(controls,cases,size,char_limit):
    batch=[];chars=0
    for control in controls:
        count=len(json.dumps(cases[control['id']],ensure_ascii=False))
        if batch and (len(batch)>=size or chars+count>char_limit):
            yield batch
            batch=[];chars=0
        batch.append(control);chars+=count
    if batch:yield batch


def batch_workers():
    try:
        workers=int(os.getenv('CODEX_BATCH_WORKERS','2'))
        if not 1<=workers<=2:raise ValueError()
        return workers
    except ValueError:raise HTTPException(503,'Invalid Codex batch worker configuration')


def assess_batch(provider,cases):
    worker=copy(provider)
    worker.usage_calls=[]
    started=time.perf_counter()
    error=None
    rows=[]
    assessment_jobs.check_cancelled()
    try:
        if callable(getattr(worker,'assess_controls',None)):
            rows=worker.assess_controls(cases)
            if not isinstance(rows,list):raise codex_provider.CodexError('codex_batch_json_invalid')
        else:rows=[worker.assess_control(cases[0])]
        assessment_jobs.check_cancelled()
    except assessment_jobs.AssessmentCancelled:raise
    except Exception as exc:
        assessment_jobs.check_cancelled()
        error=exc.code if isinstance(exc,codex_provider.CodexError) else 'codex_validation_failed'
    usage=worker.usage_calls or [safe_usage({'control_ids':[case['control_id'] for case in cases]})]
    return rows,error,usage,time.perf_counter()-started


def batch_results(executor,batches,provider,cases,workers,remaining_attempts):
    if workers==1:
        for batch in batches:
            yield batch,assess_batch(provider,[cases[c['id']] for c in batch])
        return
    while wave:=list(islice(batches,min(workers,remaining_attempts()))):
        futures=[executor.submit(copy_context().run,assess_batch,provider,[cases[c['id']] for c in batch])
                 for batch in wave]
        for batch,future in zip(wave,futures):
            yield batch,future.result()


def assess_repair(provider,case):
    worker=copy(provider)
    worker.usage_calls=[]
    started=time.perf_counter()
    error=None
    finding=None
    assessment_jobs.check_cancelled()
    try:
        if callable(getattr(worker,'assess_control',None)):
            row=worker.assess_control(case)
        else:
            rows=worker.assess_controls([case])
            if not isinstance(rows,list) or len(rows)!=1:
                raise codex_provider.CodexError('codex_batch_coverage_invalid')
            row=rows[0]
        finding=codex_provider.validate_result({'finding':row},case)
        assessment_jobs.check_cancelled()
    except assessment_jobs.AssessmentCancelled:raise
    except Exception as exc:
        assessment_jobs.check_cancelled()
        error=exc.code if isinstance(exc,codex_provider.CodexError) else 'codex_validation_failed'
    usage=[safe_usage(item) for item in worker.usage_calls] or [safe_usage({'control_ids':[case['control_id']],'request_seconds':time.perf_counter()-started})]
    return finding,error,usage


def run(db,assessment_id,body,provider_factory=None):
    a=get(db,Assessment,assessment_id)
    if not body.authorize_external:
        raise HTTPException(422,'ต้องอนุญาตส่งข้อมูลไปยัง OpenAI สำหรับการประเมินทดลองรอบนี้ก่อน')
    readiness=codex_provider.health()
    if not readiness['available']:raise HTTPException(503,'Codex unavailable: '+readiness['code'])
    try:
        bundle=codex_provider.skill_bundle()
        provider=(provider_factory or codex_provider.CodexProvider)(bundle)
    except (ValueError,OSError,KeyError):raise HTTPException(503,'Codex skill or model configuration is invalid')
    size,char_limit=batch_limits()
    workers=batch_workers()
    if not callable(getattr(provider,'assess_controls',None)):size=workers=1
    s=get(db,Standard,a.standard_id)
    profile=standard_profiles.resolve(s)
    std_meta=standard_profiles.standard_metadata(s,profile)
    prof_snap=standard_profiles.profile_snapshot(s,profile)
    inputs=client_inputs(db,a);controls=inputs['controls'];revision=a.current_version
    started=time.perf_counter()
    grading_cases,grading_snapshot=grading_knowledge.load() if profile.grading_knowledge_enabled else (None,None)
    std_knowledge=knowledge.prepare_for_standard(s,controls)
    cases={c['id']:make_case(inputs,c,approved_examples(db,a.id,c,bundle),grading_cases,std_knowledge,profile,std_meta) for c in controls}
    hashes={cid:digest({'case':case,'provider':provider.name,'cli_version':readiness['version'],'skills':bundle,'context_protocol':codex_provider.CONTEXT_PROTOCOL,'standard_profile':prof_snap}) for cid,case in cases.items()}
    values={c['id']:pending(c['id'],questions=cases[c['id']]['control'].get('assessment_questions',[])) for c in controls};reused=[]
    history=db.scalars(select(AgentRun).where(AgentRun.assessment_id==a.id).order_by(AgentRun.created_at.desc(),AgentRun.id.desc())).all()
    if not body.full_reassessment:
        for old in history:
            if old.provider!=provider.name or old.snapshot.get('context_protocol')!=codex_provider.CONTEXT_PROTOCOL:continue
            allowed=set(old.snapshot.get('assessment_plan',{}).get('ai_control_ids',[]))
            for row in old.results:
                cid=row['control_id']
                if cid in reused or cid not in cases or cid not in allowed or hashes[cid]!=old.snapshot.get('input_fingerprints',{}).get(cid):continue
                try:values[cid]=codex_provider.validate_result({'finding':row},cases[cid]);reused.append(cid)
                except (ValueError,TypeError):continue
    plan={'incremental':True,'assessment_unit':'control','assessment_control_count':len(controls),
        'requests':0,'repair_requests':0,'retry_count':0,'ai_control_ids':[],'reused_control_ids':reused[:],
        'failed_control_ids':[],'unanswered_control_ids':[],'warnings':[],'control_metrics':[],
        'batch_size':size,'batch_workers':workers,'batch_max_chars':char_limit,'batch_metrics':[],'provider_config':getattr(provider,'cache_config',{})}
    snapshot={'context_protocol':codex_provider.CONTEXT_PROTOCOL,'experimental':True,'assessment_basis':assessment_policy.SC_ANSWERS,'grading_knowledge':grading_snapshot,'standard_knowledge':profile.knowledge_ref,'standard_profile':prof_snap,'standard':std_meta,'inputs':inputs,'client_hash':digest(assessed_inputs(inputs)),'input_fingerprints':hashes,
        'skills':bundle,'assessment_plan':plan,'codex_health':readiness,
        'authorization':{'external_data_authorized':True,'revision':revision,'client_hash':digest(assessed_inputs(inputs)),'at':datetime.now(timezone.utc).isoformat(),
                         'destination':'OpenAI via official saved-login Codex CLI','payload':'Scoped requirements, context, recorded customer answers and approved same-assessment answer examples; archived evidence and policy attachments are excluded'}}
    record=AgentRun(assessment_id=a.id,revision=revision,provider=provider.name,status='running',snapshot=deepcopy(snapshot),results=[values[c['id']] for c in controls])
    db.add(record);db.commit();run_id=record.id
    assessment_jobs.publish(phase='codex',total=len(controls),completed_ids=reused,validated_ids=reused)
    failures=0
    executor=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='codex-batch') if workers>1 else None
    def save(status):
        snapshot['assessment_plan']=deepcopy(plan);snapshot['usage']=process_usage(provider,time.perf_counter()-started)
        record.status=status;record.snapshot=deepcopy(snapshot);record.results=[deepcopy(values[c['id']]) for c in controls]
        db.add(record);db.commit()
    try:
        work=[]
        for control in controls:
            cid=control['id']
            if cid in reused:continue
            if not cases[cid]['answer'].get('text','').strip():
                plan['unanswered_control_ids'].append(cid)
                assessment_jobs.publish(completed_ids=[cid])
            else:work.append(control)
        batches=control_batches(work,cases,size,char_limit)
        for batch,(rows,batch_error,usage,seconds) in batch_results(executor,batches,provider,cases,workers,lambda:3-failures):
            assessment_jobs.check_cancelled()
            ids=[c['id'] for c in batch]
            step=next(i for i,c in enumerate(controls,1) if c['id']==ids[0])
            plan['requests']+=1
            provider.usage_calls.extend(usage)
            failed=[];valid=[]
            for cid in ids:
                assessment_jobs.check_cancelled()
                try:
                    if batch_error:raise codex_provider.CodexError(batch_error)
                    matches=[row for row in rows if isinstance(row,dict) and row.get('control_id')==cid]
                    if len(matches)!=1:raise codex_provider.CodexError('codex_batch_coverage_invalid')
                    raw_row=matches[0]
                    conflict=audit_classification.has_conflict(raw_row,answer_based=True)
                    validated=codex_provider.validate_result({'finding':raw_row},cases[cid])
                    if not conflict and (validated.get('audit') or {}).get('reason')==audit_classification.CONFLICT_REASON:
                        conflict=audit_classification.CONFLICT_REASON
                    if conflict:
                        repair_case={
                            **cases[cid],
                            'elevate_reasoning':True,
                            'repair_instruction':(
                                f'REPAIR PASS: Your previous classification had conflicting fields ({conflict}). '
                                'Re-evaluate this single control carefully and return a strictly consistent finding '
                                'where status, audit.category, audit.requirement_gap, audit.system_impact, and '
                                'recommendation obey all contract rules.'
                            ),
                        }
                        plan['repair_requests']+=1
                        plan['requests']+=1
                        repaired,rep_err,rep_usage=assess_repair(provider,repair_case)
                        provider.usage_calls.extend(rep_usage)
                        if repaired is not None and not rep_err and not audit_classification.has_conflict(repaired,answer_based=True):
                            validated=repaired
                        elif rep_err:
                            plan['warnings'].append({'control_ids':[cid],'code':rep_err,'stage':'codex_repair','step':step})
                    values[cid]=validated
                    plan['ai_control_ids'].append(cid);valid.append(cid)
                    assessment_jobs.publish(completed_ids=[cid],validated_ids=[cid])
                except (ValueError,TypeError,KeyError) as exc:
                    code=exc.code if isinstance(exc,codex_provider.CodexError) else 'codex_validation_failed'
                    plan['failed_control_ids'].append(cid);failed.append(cid)
                    plan['warnings'].append({'control_ids':[cid],'code':code,'stage':'codex','step':step})
                    values[cid]=pending(cid,'Codex assessment failed ('+code+').',cases[cid]['control'].get('assessment_questions',[]))
                    assessment_jobs.publish(completed_ids=[cid],failed_ids=[cid])
            plan['batch_metrics'].append({'control_ids':ids,'seconds':seconds,'validated':len(valid),'failed':len(failed)})
            plan['control_metrics'].extend({'control_id':cid,'seconds':seconds/len(ids),'timing_basis':'shared_batch_average'} for cid in ids)
            failures=failures+1 if failed and not valid else 0
            save('running')
            if failures>=3:
                processed=set(reused+plan['ai_control_ids']+plan['failed_control_ids']+plan['unanswered_control_ids'])
                remaining=[c['id'] for c in controls if c['id'] not in processed]
                plan['warnings'].append({'control_ids':remaining,'code':'codex_failure_circuit_open','stage':'codex','step':step+len(batch)})
                plan['skipped_control_ids']=remaining
                break
        plan['ai_control_ids']=list(dict.fromkeys(reused+plan['ai_control_ids']))
        with assessment_jobs.commit_guard():save('completed_with_warnings' if plan['warnings'] else 'completed')
    except assessment_jobs.AssessmentCancelled:
        # Successfully validated rows are durable, but the run remains explicitly partial.
        plan['ai_control_ids']=list(dict.fromkeys(reused+plan['ai_control_ids']))
        save('cancelled');raise
    finally:
        if executor is not None:executor.shutdown(cancel_futures=True)
    db.refresh(a)
    return run_json(db,a,record)


def feedback_json(row):
    return {'id':row.id,'run_id':row.run_id,'run_mode':row.run_mode,'control_id':row.control_id,'provider':row.provider,
        'category':row.category,'explanation':row.explanation,'reviewer':row.reviewer,'decision':row.decision,
        'prediction':row.prediction,'created_at':row.created_at,'reviewed_at':row.snapshot.get('review',{}).get('at'),'skill_versions':row.snapshot.get('skill_versions',{})}


def add_feedback(db,assessment_id,body):
    a=get(db,Assessment,assessment_id)
    if not body.explanation.strip():raise HTTPException(422,'Auditor explanation is required')
    if body.run_mode=='codex':
        source=get(db,AgentRun,body.run_id);inputs=source.snapshot['inputs'];skills=source.snapshot['skills']
    else:
        from .models import Run
        source=get(db,Run,body.run_id)
        if source.assessment_id!=assessment_id:raise HTTPException(404,'Run not found for assessment')
        if source.revision!=a.current_version:raise HTTPException(409,'Review the current answer revision')
        inputs=client_inputs(db,a);skills={'colab_skill':{'version':source.skill.get('version'),'sha256':digest(source.skill)}}
    if source.assessment_id!=assessment_id:raise HTTPException(404,'Run not found for assessment')
    control=next((c for c in inputs['controls'] if c['id']==body.control_id),None)
    prediction=next((f for f in source.results if f['control_id']==body.control_id),None)
    if not control or not prediction:raise HTTPException(422,'Control is not in this run')
    case=make_case(inputs,control,[])
    ids=set(body.evidence_ids)
    archived=[e for e in inputs.get('evidence',[]) if e['control_id']==body.control_id]
    if not ids<={e['id'] for e in archived}:raise HTTPException(422,'Evidence references must belong to this control snapshot')
    snapshot={'standard_id':inputs['standard_id'],'requirement':control['requirement'],'requirement_hash':digest(control['requirement']),
        'answer':case['answer'],'evidence':[e for e in archived if e['id'] in ids],
        'skill_versions':{name:{k:v.get(k) for k in ('version','sha256')} for name,v in skills.items()}}
    snapshot['case_hash']=digest({k:v for k,v in snapshot.items() if k!='skill_versions'})
    row=AuditorFeedback(assessment_id=assessment_id,run_id=source.id,run_mode=body.run_mode,control_id=body.control_id,
        provider=source.provider,snapshot=snapshot,prediction=deepcopy(prediction),category=body.category,
        explanation=body.explanation,decision='pending',reviewer='')
    db.add(row);db.commit();return feedback_json(row)


def review_feedback(db,assessment_id,feedback_id,body):
    row=get(db,AuditorFeedback,feedback_id)
    if row.assessment_id!=assessment_id:raise HTTPException(404,'Feedback not found')
    if not body.reviewer.strip():raise HTTPException(422,'Reviewer name is required')
    if row.decision!='pending':raise HTTPException(409,'Feedback already reviewed; create a new correction to revise it')
    review={'reviewer':body.reviewer.strip(),'decision':body.decision,'at':datetime.now(timezone.utc).isoformat(),'rule_hashes':{name:value['sha256'] for name,value in codex_provider.skill_bundle().items() if name in {'evidence-validator','finding-classifier'}}}
    changed=db.execute(update(AuditorFeedback).where(AuditorFeedback.id==feedback_id,AuditorFeedback.assessment_id==assessment_id,AuditorFeedback.decision=='pending').values(decision=body.decision,reviewer=body.reviewer.strip(),snapshot={**row.snapshot,'review':review}).execution_options(synchronize_session=False))
    if changed.rowcount!=1:
        db.rollback();raise HTTPException(409,'Feedback was already reviewed')
    db.commit();db.refresh(row);return feedback_json(row)


def export_run(db,assessment_id,run_id):
    a=get(db,Assessment,assessment_id);row=get(db,AgentRun,run_id)
    if row.assessment_id!=assessment_id:raise HTTPException(404,'Experimental run not found')
    s=get(db,Standard,a.standard_id);inputs=row.snapshot['inputs'];plan=row.snapshot['assessment_plan']
    return excel.export(s.template,s.mapping,inputs['controls'],inputs['answers'],
        {'assessment_id':a.id,'standard_id':s.id,'version':row.revision,'experimental':True,'agent_run_id':row.id},
        row.results,inputs['questions'],context={**inputs['context'],**inputs.get('audit_participants',{})},failed_ids=plan['failed_control_ids'],
        unreviewed_ids=[c['id'] for c in inputs['controls'] if c['id'] not in set(plan['ai_control_ids'])|set(plan['failed_control_ids'])],failure_warnings=plan['warnings'])


def feedback_summary(db,assessment_id):
    from collections import Counter
    get(db,Assessment,assessment_id)
    rows=db.scalars(select(AuditorFeedback).where(AuditorFeedback.assessment_id==assessment_id,AuditorFeedback.decision=='approved')).all()
    changes=Counter(((row.prediction.get('audit') or {}).get('category','unclassified'),row.category) for row in rows if (row.prediction.get('audit') or {}).get('category')!=row.category)
    return {'scope':'approved_feedback_only_not_a_representative_accuracy_benchmark','approved_count':len(rows),
        'correction_count':sum(changes.values()),'recurring_changes':[{'predicted':before,'approved':after,'count':count} for (before,after),count in changes.most_common()]}
