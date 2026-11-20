from copy import deepcopy
from types import SimpleNamespace
import pytest
from app import ai, main, incremental
from app.schemas import CreateAssessment, ReplaceAnswers, RunInput, AutoDepartmentsInput, PolicyInput, DepartmentAssignments
from app.models import Evidence, Run, Assessment
from app.usage import safe_usage
from .test_standard_skills import db,standard  # noqa: F401


def setup(db):
    s=standard(db)
    a=main.create_assessment(CreateAssessment(name='Changed-only test',standard_id=s.id,
        context={'company':'Example','business_type':'Services','departments':['IT','HR']}),db)
    return s,a


def attach_provider(monkeypatch,calls,fail=None):
    class Provider(ai.HTTPProvider):
        def __init__(self):super().__init__(url='https://synthetic.example',model='test',api_key='')
        def request(self,operation,payload):
            calls.append((operation,[c['id'] for c in payload['controls']]))
            self.usage_calls.append(safe_usage({'prompt_tokens':10,'completion_tokens':5,'generation_seconds':.1}))
            if fail and fail(operation,payload):raise ValueError('synthetic failure')
            if operation=='departments':
                return [{'control_id':c['id'],'departments':['HR'] if 'HR' in payload['answers'][c['id']] else ['IT'],
                         'reason':'Department responsibility stated in answer'} for c in payload['controls']]
            assert operation=='assess'
            return [{'control_id':c['id'],'status':'gap','rationale':'Confirmed requirement gap','recommendation':'Perform missing step','evidence_ids':[],
                'audit':{'category':'nc_minor','reason':'Isolated lapse with system outcome unaffected',
                         'requirement_gap':'Required step absent','system_impact':'does_not_affect'}} for c in payload['controls']]
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(Provider()))


def save(db,a,s,version=1,change=None):
    answers={c['id']:{'status':'unanswered','text':'IT has not performed this step'} for c in s.controls}
    if change:answers.update(change)
    return main.save_answers(a['id'],ReplaceAnswers(expected_version=version,answers=answers),db)


def test_save_maps_only_changed_answers_and_incremental_run_reuses_unchanged(db,monkeypatch):
    s,a=setup(db);calls=[];attach_provider(monkeypatch,calls)
    saved=save(db,a,s)
    assert calls==[('departments',['SC-A','SC-B'])]
    first=main.run_assessment(a['id'],RunInput(),db)
    assert calls[-1]==('assess',['SC-A','SC-B'])
    original=deepcopy(first['results'])
    nochange=main.run_assessment(a['id'],RunInput(),db)
    assert nochange['analysis_plan']['reused_control_ids']==['SC-A','SC-B']
    assert len(calls)==2 and nochange['usage']['total_tokens']==0
    saved=save(db,a,s,version=2,change={'SC-B':{'status':'unanswered','text':'HR now handles this task but has not done it'}})
    assert calls[-1]==('departments',['SC-B'])
    assert saved['department_assignments']['SC-B']['departments']==['HR']
    second=main.run_assessment(a['id'],RunInput(),db)
    assert calls[-1]==('assess',['SC-B'])
    assert second['analysis_plan']['reused_control_ids']==['SC-A']
    assert second['results'][0]==original[0]
    assert len(second['results'])==len(s.controls)
    assert db.get(Run,first['id']).results==original


def test_linked_evidence_and_policy_invalidate_only_impacted_controls(db,monkeypatch):
    s,a=setup(db);calls=[];attach_provider(monkeypatch,calls);save(db,a,s)
    main.run_assessment(a['id'],RunInput(),db)
    calls.clear()
    db.add(Evidence(assessment_id=a['id'],control_id='SC-B',filename='record.txt',sha256='a'*64,content=b'record',text='record'));db.commit()
    r=main.run_assessment(a['id'],RunInput(),db)
    assert calls==[('assess',['SC-B'])] and r['analysis_plan']['reused_control_ids']==['SC-A']
    main.add_policy(a['id'],PolicyInput(name='Access',version='1',text='New access requirement',control_ids=['SC-A']),db)
    calls.clear();main.run_assessment(a['id'],RunInput(),db)
    assert calls==[('assess',['SC-A'])]


def test_cleared_answer_does_not_keep_old_finding_or_call_ai(db,monkeypatch):
    s,a=setup(db);calls=[];attach_provider(monkeypatch,calls);save(db,a,s);main.run_assessment(a['id'],RunInput(),db)
    save(db,a,s,version=2,change={'SC-B':{'status':'unanswered','text':''}})
    calls.clear();r=main.run_assessment(a['id'],RunInput(),db)
    assert calls==[]
    assert r['results'][1]['status']=='needs_review'
    assert r['analysis_plan']['unanswered_control_ids']==['SC-B']
    assert r['analysis_plan']['reused_control_ids']==['SC-A']


def test_department_failure_preserves_answers_and_warns_without_accepting_old_mapping(db,monkeypatch):
    s,a=setup(db);calls=[];attach_provider(monkeypatch,calls)
    save(db,a,s)
    attach_provider(monkeypatch,calls,fail=lambda op,payload:op=='departments')
    saved=save(db,a,s,version=2,change={'SC-B':{'status':'unanswered','text':'Updated ambiguous responsibility'}})
    assert saved['answers']['SC-B']['text']=='Updated ambiguous responsibility'
    assert saved['department_mapping']['warnings']
    assert saved['department_assignments']['SC-B']['source']!='ai_answer'


def test_full_reassessment_bypasses_caches_and_force_is_explicit(db,monkeypatch):
    s,a=setup(db);calls=[];attach_provider(monkeypatch,calls);save(db,a,s);main.run_assessment(a['id'],RunInput(),db)
    calls.clear();r=main.run_assessment(a['id'],RunInput(full_reassessment=True),db)
    assert calls==[('departments',['SC-A','SC-B']),('assess',['SC-A','SC-B'])]
    assert r['analysis_plan']['reused_control_ids']==[]


def test_failed_controls_retry_while_successful_peers_are_reused(db,monkeypatch):
    s,a=setup(db);calls=[]
    attach_provider(monkeypatch,calls,fail=lambda op,p:op=='assess' and p['controls'][0]['id']=='SC-B')
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE','1')
    save(db,a,s);first=main.run_assessment(a['id'],RunInput(),db)
    assert first['analysis_plan']['failed_control_ids']==['SC-B']
    attach_provider(monkeypatch,calls);calls.clear()
    second=main.run_assessment(a['id'],RunInput(),db)
    assert calls==[('assess',['SC-B'])]
    assert second['analysis_plan']['reused_control_ids']==['SC-A']


def test_auto_department_endpoint_does_not_change_revision_and_noops_when_cached(db,monkeypatch):
    s,a=setup(db)
    save(db,a,s)  # offline: no invented AI mapping
    calls=[];attach_provider(monkeypatch,calls)
    r=main.auto_departments(a['id'],AutoDepartmentsInput(expected_version=2),db)
    assert r['current_version']==2 and r['department_assignments']['SC-A']['source']=='ai_answer'
    main.auto_departments(a['id'],AutoDepartmentsInput(expected_version=2),db)
    assert len(calls)==1


def test_manual_department_changes_invalidate_related_assessment_only(db,monkeypatch):
    s,a=setup(db);calls=[];attach_provider(monkeypatch,calls);save(db,a,s);main.run_assessment(a['id'],RunInput(),db)
    main.save_departments(a['id'],DepartmentAssignments(expected_version=2,assignments={'SC-A':['HR']}),db)
    calls.clear();main.run_assessment(a['id'],RunInput(),db)
    assert calls==[('assess',['SC-A'])]


def test_provider_or_context_change_invalidates_results():
    payload={'controls':[{'id':'C','requirement':'R','department':'IT'}],'answers':{'C':{'text':'A'}},'context':{'company':'A'},'skill':{}}
    hashes=incremental.fingerprints(payload,'http:model1')
    old=SimpleNamespace(id='r',provider='http:model1',skill={'input_fingerprint_version':incremental.VERSION,'input_fingerprints':hashes},results=[{'control_id':'C','status':'gap'}])
    assert incremental.select_reusable(payload['controls'],hashes,[old],'http:model1')[0]
    assert not incremental.select_reusable(payload['controls'],hashes,[old],'http:model2')[0]
    payload['context']['company']='Changed'
    assert not incremental.select_reusable(payload['controls'],incremental.fingerprints(payload,'http:model1'),[old],'http:model1')[0]

def test_unlinked_evidence_is_not_a_dependency_and_skill_changes_are():
    payload={'controls':[{'id':'C','requirement':'R','department':'IT'}],'answers':{'C':{'text':'A'}},
             'context':{'company':'A'},'skill':{'version':'1','instructions':'Original'}}
    first=incremental.fingerprints(payload,'http:test')
    payload['evidence']=[{'id':'e','control_id':'OTHER','text':'Unrelated','sha256':'a','filename':'other.txt'}]
    assert incremental.fingerprints(payload,'http:test')==first
    payload['skill']['version']='2'
    assert incremental.fingerprints(payload,'http:test')!=first


def test_context_edit_during_inference_is_not_overwritten(db,monkeypatch):
    s,a=setup(db);calls=[];attach_provider(monkeypatch,calls);save(db,a,s)
    original=ai.HTTPProvider.assess
    def concurrent(self,payload):
        current=main.get_assessment(a['id'],db)
        main.save_departments(a['id'],DepartmentAssignments(expected_version=current['current_version'],assignments={'SC-A':['HR']}),db)
        return original(self,payload)
    monkeypatch.setattr(ai.HTTPProvider,'assess',concurrent)
    result=main.run_assessment(a['id'],RunInput(),db)
    assert result['stale'] is True
    actual=main.get_assessment(a['id'],db)
    assert actual['department_assignments']['SC-A']['departments']==['HR']
    assert actual['current_version']==3
