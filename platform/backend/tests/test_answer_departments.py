from copy import deepcopy
from types import SimpleNamespace
import pytest
from app import answer_departments as mapping, departments

CONTROLS=[{'id':'C1','requirement':'Review access','department':'All'},{'id':'C2','requirement':'Handle onboarding','department':'All'}]
CONTEXT={'company':'Example','business_type':'Services','departments':['IT','HR','Risk'],'question_language':'th'}
ANSWERS={'C1':{'text':'IT and HR collaborate','status':'unanswered'},'C2':{'text':'HR verifies onboarding','status':'unanswered'}}


def provider(calls,handler=None):
    def method(payload):
        calls.append(payload)
        if handler:return handler(payload)
        return [{'control_id':c['id'],'departments':['IT','HR'] if c['id']=='C1' else ['HR'],'reason':'Supported collaboration'} for c in payload['controls']]
    return SimpleNamespace(name='http:test',map_departments=method,usage_calls=[])


def test_maps_multiple_actual_company_departments_and_only_changed_answers():
    calls=[];p=provider(calls)
    context,plan,_=mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:p)
    assert plan['mapped_control_ids']==['C1','C2']
    routing=departments.assignments(CONTROLS,context,[],ANSWERS)
    assert routing['C1']['departments']==['IT','HR'] and routing['C1']['source']=='ai_answer'
    context,again,_=mapping.refresh(CONTROLS,ANSWERS,context,[],lambda:p)
    assert len(calls)==1 and again['requests']==0
    changed=deepcopy(ANSWERS);changed['C2']['text']='HR and IT now handle onboarding'
    updated,plan,_=mapping.refresh(CONTROLS,changed,context,[],lambda:p)
    assert [c['id'] for c in calls[-1]['controls']]==['C2']
    assert plan['reused_control_ids']==['C1']
    assert 'answer_department_map' not in calls[-1]['context']
    assert mapping.current_entry(CONTROLS[1],changed['C2'],context,[]) is None
    assert mapping.current_entry(CONTROLS[1],changed['C2'],updated,[]) is not None


@pytest.mark.parametrize('kind',['invented','duplicate','missing','wrong_id'])
def test_invalid_model_departments_are_not_saved(kind):
    def bad(payload):
        rows=[{'control_id':c['id'],'departments':['Made up'] if kind=='invented' else ['HR','HR'] if kind=='duplicate' else ['HR'],'reason':'Why'} for c in payload['controls']]
        if kind=='missing':rows.pop()
        if kind=='wrong_id':rows[0]['control_id']='OTHER'
        return rows
    calls=[];context,plan,_=mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:provider(calls,bad))
    assert context['answer_department_map']=={}
    assert plan['warnings'] and plan['pending_control_ids']==['C1','C2']
    assert ANSWERS['C1']['text']=='IT and HR collaborate'


def test_manual_override_wins_and_blank_answer_invalidates_old_mapping():
    calls=[];context,_,_=mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:provider(calls))
    context['department_overrides']={'C1':['Risk']}
    changed=deepcopy(ANSWERS);changed['C2']['text']=''
    current,plan,_=mapping.refresh(CONTROLS,changed,context,[],lambda:provider(calls))
    assert len(calls)==1 and plan['requests']==0
    routing=departments.assignments(CONTROLS,current,[],changed)
    assert routing['C1']['departments']==['Risk']
    assert 'C2' not in current['answer_department_map']


def test_explicit_ambiguity_is_cached_without_assigning_every_department():
    calls=[]
    def unclear(payload):return [{'control_id':c['id'],'departments':[],'reason':'Answer does not identify responsible work'} for c in payload['controls']]
    p=provider(calls,unclear)
    context,plan,_=mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:p)
    assert plan['pending_control_ids']==['C1','C2']
    assert departments.assignments(CONTROLS,context,[],ANSWERS)['C1']['departments']==[]
    mapping.refresh(CONTROLS,ANSWERS,context,[],lambda:p)
    assert len(calls)==1


def test_one_company_department_needs_no_model():
    context,plan,p=mapping.refresh(CONTROLS,ANSWERS,{**CONTEXT,'departments':['IT']},[],lambda:pytest.fail('No inference needed'))
    assert plan['requests']==0 and p is None
    assert departments.assignments(CONTROLS,context,[],ANSWERS)['C1']['departments']==['IT']


def test_context_or_question_change_invalidates_mapping():
    context,_,_=mapping.refresh(CONTROLS,ANSWERS,CONTEXT,[],lambda:provider([]))
    assert mapping.current_entry(CONTROLS[0],ANSWERS['C1'],{**context,'company':'Other'},[]) is None
    assert mapping.current_entry(CONTROLS[0],ANSWERS['C1'],context,[{'control_id':'C1','text':'New scope?'}]) is None

def test_gateway_department_contract_and_operation_budget(fake_gateway):
    from .test_gateway import gateway
    import json
    rows=[{'control_id':'C1','departments':['IT','HR'],'reason':'Both teams perform the described process'}]
    fake_gateway['text']=json.dumps(rows)
    result=gateway.infer(gateway.Request(operation='departments',model='test-model',system=mapping.INSTRUCTION,
        payload={'controls':[{'id':'C1'}],'allowed_departments':['IT','HR']}),'Bearer test-key')
    assert result['output']==rows
    assert fake_gateway['generation_options']['max_new_tokens']<=2048
    assert 'allowed_departments' in fake_gateway['messages'][0]['content']
    assert gateway.health('Bearer test-key')['gateway_version']==gateway.GATEWAY_VERSION


from .test_inference_diagnostics import fake_gateway  # noqa: F401

def test_ollama_department_schema_resolves_its_item_definition():
    import json
    from io import BytesIO
    from unittest.mock import patch
    from app import ai
    p=ai.OllamaProvider(model='synthetic-model')
    rows=[{'control_id':'C1','departments':['HR'],'reason':'HR owns the work'}]
    with patch('app.ai.build_opener') as opener:
        opener.return_value.open.return_value=BytesIO(json.dumps({'done':True,'message':{'content':json.dumps({'output':rows})}}).encode())
        result=p.map_departments({'controls':[CONTROLS[0]],'answers':{'C1':'HR handles this'},'allowed_departments':['HR']})
        body=json.loads(opener.return_value.open.call_args.args[0].data)
    assert result==rows
    assert body['format']['properties']['output']['items']['$ref']=='#/$defs/AnswerDepartmentMapping'
    assert 'AnswerDepartmentMapping' in body['format']['$defs']
    assert body['options']['num_predict']==2048
