import pytest
from app import ai
from app.assessment_batches import assess_batches


def payload():
    return {'controls':[{'id':'4.1','requirement':'Identify context','department':'IT'},
                        {'id':'4.2','requirement':'Identify interested parties','department':'IT'}],
            'answers':{'4.1':{'text':'SC recorded findings'},'4.2':{'text':'SC recorded findings'}},
            'context':{},'evidence':[]}


def finding(cid):
    return {'control_id':cid,'status':'gap','rationale':'Recorded gap','recommendation':'Address the gap','evidence_ids':[]}


def test_missing_control_retries_only_that_control_once_by_default(monkeypatch):
    monkeypatch.delenv('AI_ASSESS_COVERAGE_REPAIR_ATTEMPTS',raising=False)
    monkeypatch.setenv('AI_ASSESS_FAILURE_MODE','continue')
    calls=[]
    def request(operation,batch):
        calls.append(batch)
        if len(calls)==1:return [finding('4.1')]
        assert batch['required_control_ids']==['4.2']
        assert batch['validation_feedback']['code']=='control_coverage_invalid'
        return [finding('4.2')]
    result,plan=assess_batches(request,payload(),ai.validate_findings)
    assert [[c['id'] for c in b['controls']] for b in calls]==[['4.1','4.2'],['4.2']]
    assert [f['control_id'] for f in result]==['4.1','4.2']
    assert plan['coverage_repair_requests']==plan['repair_requests']==1
    assert plan['failed_control_ids']==[] and plan['completion_status']=='completed'


def test_wrong_id_is_not_relabelled_and_retry_is_bounded(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_COVERAGE_REPAIR_ATTEMPTS','1')
    monkeypatch.setenv('AI_ASSESS_FAILURE_MODE','continue')
    calls=[]
    def request(operation,batch):
        calls.append(batch)
        return [finding('4.1')] if len(calls)==1 else [finding('A.4.2')]
    result,plan=assess_batches(request,payload(),ai.validate_findings)
    assert len(calls)==2
    assert plan['failed_control_ids']==['4.2']
    assert result[0]['status']=='gap' and result[1]['status']=='needs_review'
    assert plan['warnings'][0]['code']=='control_coverage_invalid'


def test_coverage_retry_can_be_disabled(monkeypatch):
    monkeypatch.setenv('AI_ASSESS_COVERAGE_REPAIR_ATTEMPTS','0')
    monkeypatch.setenv('AI_ASSESS_FAILURE_MODE','continue')
    calls=[]
    def request(operation,batch):calls.append(batch);return [finding('4.1')]
    result,plan=assess_batches(request,payload(),ai.validate_findings)
    assert len(calls)==1 and plan['coverage_repair_requests']==0
    assert plan['failed_control_ids']==['4.2']
