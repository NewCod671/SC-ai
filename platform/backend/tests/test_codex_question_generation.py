from copy import deepcopy
import json
from pathlib import Path
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app import ai, codex_provider, main
from app.models import Assessment, Audit, Revision
from app.schemas import CreateAssessment
from .test_standard_skills import db, standard


CONTEXT={'company':'Example','business_type':'Logistics','departments':['IT','HR'],
         'it_systems':['ERP'],'data_types':['Customer records'],'question_language':'th'}


@pytest.mark.parametrize(('message','code'),[
    ('No prompt provided via stdin.','codex_prompt_unreadable'),
    ('Invalid JSON schema for response format','codex_output_schema_invalid'),
    ('The requested model is not available','codex_model_unavailable'),
    ('Failed to connect to service','codex_network_error'),
])
def test_codex_cli_errors_are_reduced_to_safe_diagnostic_codes(message,code):
    assert codex_provider.execution_error(message)==code


def test_codex_question_provider_uses_shared_batch_validation_and_exact_mapping(monkeypatch):
    provider=codex_provider.CodexQuestionProvider.__new__(codex_provider.CodexQuestionProvider)
    provider.name='codex-questions:test';provider.usage_calls=[];provider.question_plan={}
    seen=[]
    def execute(payload):
        seen.append(payload)
        return {'questions':[{'control_ids':[c['id'] for c in payload['controls']],
            'departments':['IT','HR'],'text':'แต่ละทีมตรวจสอบสิทธิ์การเข้าถึงอย่างไร?',
            'mapping_reason':'ทั้งสองข้อเป็นกระบวนการตรวจทานสิทธิ์เดียวกัน'}]}
    monkeypatch.setattr(provider,'_execute_questions',execute)
    monkeypatch.setenv('CODEX_QUESTION_BATCH_SIZE','2')
    controls=[{'id':f'SC-{i}','requirement':'Review access permissions','department':d}
              for i,d in enumerate(['IT','HR','IT'],1)]
    result=provider.questions(controls,CONTEXT)
    assert len(seen)==2
    assert provider.question_plan['batch_size']==2
    assert {cid for q in result for cid in q['control_ids']}=={'SC-1','SC-2','SC-3'}
    assert result[0]['control_ids']==['SC-1','SC-2']
    assert result[0]['mapping_reason']


def test_codex_question_provider_maps_a_custom_question_to_imported_controls(monkeypatch):
    provider=codex_provider.CodexQuestionProvider.__new__(codex_provider.CodexQuestionProvider)
    provider.name='codex-questions:test';provider.usage_calls=[]
    seen=[]
    def execute(payload):
        seen.append(payload)
        return [{'control_id':payload['controls'][0]['id'],'score':.91,
                 'reason':'The question asks who reviews access, matching this control.'}]
    monkeypatch.setattr(provider,'_execute_question_mapping',execute)
    controls=[{'id':'SC-A','requirement':'Review user access','department':'IT'},
              {'id':'SC-B','requirement':'Test backup restoration','department':'IT'}]
    result=provider.map_question('Who checks user access?',controls,CONTEXT,None,['IT'],'IT reviews monthly.')
    assert len(seen)==1 and seen[0]['question']['answer_text']=='IT reviews monthly.'
    assert [item['control_id'] for item in result]==['SC-A']


def test_codex_question_cli_receives_skill_prompt_schema_and_records_usage(monkeypatch):
    provider=codex_provider.CodexQuestionProvider()
    monkeypatch.setattr(codex_provider,'cli_path',lambda:'codex.exe')
    payload={'controls':[{'id':'SC-A','requirement':'Review access','department':'IT','departments':['IT']}],
             'required_control_ids':['SC-A'],'context':{'departments':['IT'],'question_language':'en'}}
    generated={'questions':[{'control_ids':['SC-A'],'departments':['IT'],
        'text':'How does your team review access?', 'mapping_reason':''}]}
    class Process:
        pid=1
        returncode=0
        def poll(self):return self.returncode
    def spawn(command,stdin,stdout,stderr,cwd,**kwargs):
        prompt=stdin.read().decode('utf-8')
        assert 'Invoke the project skill $question-generator' in prompt
        schema=Path(command[command.index('--output-schema')+1])
        output=Path(command[command.index('-o')+1])
        schema_data=json.loads(schema.read_text(encoding='utf-8'))
        assert schema_data['properties']['questions']['items']['properties']['control_ids']['items']['enum']==['SC-A']
        assert 'uniqueItems' not in json.dumps(schema_data)
        output.write_text(json.dumps(generated),encoding='utf-8')
        stdout.write(b'{"type":"turn.completed","usage":{"input_tokens":21,"output_tokens":13}}\n')
        return Process()
    monkeypatch.setattr(codex_provider.subprocess,'Popen',spawn)
    result=provider._execute_questions(payload)
    assert result==generated
    assert provider.usage_calls[0]['prompt_tokens']==21
    assert provider.usage_calls[0]['completion_tokens']==13


def test_codex_question_generation_requires_explicit_external_consent(db,monkeypatch):
    s=standard(db)
    monkeypatch.setattr(ai,'provider',lambda:pytest.fail('inference provider should not run'))
    with pytest.raises(HTTPException) as error:
        main.create_assessment(CreateAssessment(name='Codex questions',standard_id=s.id,
            context=CONTEXT,question_provider='codex'),db)
    assert error.value.status_code==422
    assert db.scalar(select(func.count()).select_from(Assessment))==0
    assert db.scalar(select(func.count()).select_from(Revision))==0


def test_codex_question_generation_is_selected_and_audited_after_full_success(db,monkeypatch):
    s=standard(db)
    monkeypatch.setattr(codex_provider,'health',lambda:{'available':True,'code':'ready'})
    class FakeCodexQuestions:
        name='codex-questions:synthetic'
        usage_calls=[{'prompt_tokens':12,'completion_tokens':8,'generation_seconds':None}]
        question_plan={'batch_size':5,'requests':1,'successful_batches':1}
        question_skill_snapshot={'name':'question-generator','version':'1.0.0','sha256':'synthetic'}
        def questions(self,controls,context):
            return [{'control_ids':[c['id']],'departments':[c.get('department') or 'IT'],
                'text':'ทีมของคุณทำงานตามข้อกำหนดนี้อย่างไร?', 'mapping_reason':''} for c in controls]
    monkeypatch.setattr(codex_provider,'CodexQuestionProvider',FakeCodexQuestions)
    monkeypatch.setattr(ai,'provider',lambda:pytest.fail('inference provider should not run'))
    result=main.create_assessment(CreateAssessment(name='Codex questions',standard_id=s.id,
        context=CONTEXT,question_provider='codex',authorize_external=True),db)
    assert result['questions']
    assert result['question_usage']['provider']=='codex-questions:synthetic'
    saved=db.scalar(select(Audit).where(Audit.entity_id==result['id'],Audit.action=='assessment.create'))
    assert saved.details['question_provider']=='codex-questions:synthetic'
    assert saved.details['question_external_authorized'] is True
    assert saved.details['question_plan']['successful_batches']==1
    assert saved.details['codex_question_skill']['version']=='1.0.0'


def test_codex_question_generation_fails_closed_when_codex_unavailable(db,monkeypatch):
    s=standard(db)
    monkeypatch.setattr(codex_provider,'health',lambda:{'available':False,'code':'codex_disabled'})
    with pytest.raises(HTTPException) as error:
        main.create_assessment(CreateAssessment(name='Codex questions',standard_id=s.id,
            context=CONTEXT,question_provider='codex',authorize_external=True),db)
    assert error.value.status_code==503
    assert db.scalar(select(func.count()).select_from(Assessment))==0
