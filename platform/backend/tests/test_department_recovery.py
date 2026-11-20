import httpx
import pytest
from app import answer_departments as mapping, assessment_jobs as jobs
from .test_answer_departments import CONTROLS, CONTEXT, ANSWERS, provider


def test_department_gateway_502_splits_once_and_recovers(monkeypatch):
    calls=[]
    def handler(payload):
        if len(payload['controls'])>1:
            request=httpx.Request('POST','https://synthetic.example/infer')
            raise httpx.HTTPStatusError('failed',request=request,response=httpx.Response(502,request=request))
        return [{'control_id':c['id'],'departments':['HR'],'reason':'HR handles this work'} for c in payload['controls']]
    context,plan,_=mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:provider(calls,handler))
    assert [len(c['controls']) for c in calls]==[2,1,1]
    assert len(context['answer_department_map'])==2 and not plan['warnings']
    assert plan['requests']==3


def test_department_timeout_continues_after_bounded_split():
    calls=[]
    def handler(payload):raise httpx.ReadTimeout('private')
    context,plan,_=mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:provider(calls,handler))
    assert len(calls)==3 and not context['answer_department_map']
    assert plan['pending_control_ids']==['C1','C2']
    assert all(w['code']=='inference_timeout' for w in plan['warnings'])


@pytest.mark.parametrize('fails',[False,True])
def test_cancel_during_department_request_never_splits_or_continues(fails):
    job=jobs.reserve('dept-test')
    calls=[]
    def handler(payload):
        jobs.cancel(job['id'])
        if fails:raise httpx.ReadTimeout('private')
        return []
    with jobs.generation(job['id'],'dept-test'):
        with pytest.raises(jobs.AssessmentCancelled):
            mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:provider(calls,handler))
    assert len(calls)==1


def test_later_department_batches_continue_after_failed_halves(monkeypatch):
    monkeypatch.setenv('AI_DEPARTMENT_BATCH_SIZE','2')
    controls=[{'id':f'C{i}','requirement':'Review access','department':'All'} for i in range(1,5)]
    answers={c['id']:{'text':'HR verifies access','status':'unanswered'} for c in controls}
    calls=[]
    def handler(payload):
        if any(c['id'] in {'C1','C2'} for c in payload['controls']):raise httpx.ReadTimeout('private')
        return [{'control_id':c['id'],'departments':['HR'],'reason':'HR verifies access'} for c in payload['controls']]
    context,plan,_=mapping.refresh(controls,answers,CONTEXT,[],lambda:provider(calls,handler))
    assert len(calls)==4
    assert set(context['answer_department_map'])=={'C3','C4'}
    assert plan['pending_control_ids']==['C1','C2']
