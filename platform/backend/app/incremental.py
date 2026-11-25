"""Reuse only successful per-control findings with identical assessment inputs."""
from copy import deepcopy
import os
from . import assessment_policy
from .prompt_compaction import VERSION as PROMPT_COMPACTION_VERSION
from .answer_departments import digest,public_context
from .audit_classification import VERSION as AUDIT_VERSION
VERSION='assessment-inputs-v7-concise-workbook'


def fingerprints(payload,provider):
    payload=assessment_policy.prepare(payload)
    return {c['id']:digest({'version':VERSION,'provider':provider,'prompt_profile':os.getenv('AI_ASSESS_PROMPT_PROFILE','compact-v1'),'audit_version':assessment_policy.SC_GRADING_VERSION if assessment_policy.answer_based(payload) else AUDIT_VERSION,'assessment_basis':payload.get('assessment_basis',assessment_policy.DEFAULT),
        'control':{k:c.get(k) for k in ('id','requirement')},
        'grading_examples':c.get('grading_examples',[]),
        'standard_guidance':c.get('standard_guidance',''),
        'standard':payload.get('standard'),
        'prompt_compaction_version':PROMPT_COMPACTION_VERSION if assessment_policy.answer_based(payload) else None,
        'agent_skill_hashes':{name:item.get('sha256') for name,item in payload.get('codex_agent_skills',{}).items()},
        'answer':payload['answers'].get(c['id'],{}),'context':public_context(payload.get('context',{})),
        'questions':[{'control_ids':[c['id']],'text':q['text']} for q in payload.get('questions',[]) if c['id'] in q.get('control_ids',[q.get('control_id')])],
        'evidence':sorted([{k:e.get(k) for k in ('id','sha256','filename','text')} for e in payload.get('evidence',[]) if e['control_id']==c['id']],key=lambda x:x['id']),
        'policies':sorted([{k:p.get(k) for k in ('id','name','version','text')} for p in payload.get('policies',[]) if c['id'] in p['control_ids']],key=lambda x:x['id']),
        'skill':{k:payload.get('skill',{}).get(k) for k in ('id','name','version','instructions','core_instructions','review_procedure')}
    }) for c in payload['controls']}


def select_reusable(controls,hashes,history,provider,force=False):
    reused={};sources={}
    if force or provider.startswith('offline'):return reused,sources
    for run in history:
        if run.provider!=provider:continue
        plan=run.skill.get('assessment_plan',{})
        excluded=set(plan.get('failed_control_ids',[]))|set(plan.get('unanswered_control_ids',[]))
        old=run.skill.get('input_fingerprints',{})
        # Require unambiguous, versioned current findings; legacy runs bootstrap once.
        if run.skill.get('input_fingerprint_version')!=VERSION:continue
        ids=[f['control_id'] for f in run.results]
        for f in run.results:
            cid=f['control_id']
            if cid in reused or cid in excluded or ids.count(cid)!=1:continue
            if cid in hashes and hashes[cid]==old.get(cid):
                reused[cid]=deepcopy(f);sources[cid]=run.skill.get('reused_from_runs',{}).get(cid,run.id)
    return reused,sources


def merge_plan(controls,answers,plan,reused):
    return {**plan,'incremental':True,'reused_control_ids':[c['id'] for c in controls if c['id'] in reused],
            'ai_control_ids':plan.get('ai_control_ids',[]),
            'unanswered_control_ids':[c['id'] for c in controls if not answers[c['id']].get('text','').strip()],
            'failed_control_ids':plan.get('failed_control_ids',[]),'warnings':plan.get('warnings',[]),
            'requests':plan.get('requests',0),'repair_requests':plan.get('repair_requests',0),
            'completion_status':plan.get('completion_status','completed')}
