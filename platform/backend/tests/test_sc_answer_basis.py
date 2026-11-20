import importlib.util
import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy import select,func
from app import ai,main,assessment_policy,incremental
from app.models import Evidence,Policy,SkillFamily,StandardSkillVersion,Run
from app.schemas import ReplaceAnswers,RunInput
from app.usage import safe_usage
from .test_standard_skills import db,standard,assessment  # noqa: F401
from .test_inference_diagnostics import fake_gateway  # noqa: F401
from .test_gateway import gateway

BASIS=assessment_policy.SC_ANSWERS


def finding(cid='C'):
    return {'control_id':cid,'status':'met','interpreted_answer':'SC records that the practice is implemented',
        'rationale':'The recorded answer addresses the requirement','recommendation':'Continue the process','evidence_ids':[],
        'audit':{'category':'comply','reason':'SC recorded complete implementation','requirement_gap':'','system_impact':'does_not_affect'}}


def test_sc_answer_can_pass_without_attachment_but_default_policy_still_requires_proof():
    controls=[{'id':'C'}];answers={'C':{'status':'unanswered','text':'SC reviewed the process and records full implementation'}}
    assert ai.validate_findings([finding()],controls,[],answers,BASIS)[0]['status']=='met'
    assert ai.validate_findings([finding()],controls,[],answers)[0]['status']=='needs_review'
    answers['C']['text']=''
    assert ai.validate_findings([finding()],controls,[],answers,BASIS)[0]['status']=='needs_review'


def test_archived_files_do_not_enter_assessment_inputs_or_cache_dependencies():
    payload={'assessment_basis':BASIS,'controls':[{'id':'C','requirement':'Review scope','department':'IT'}],
             'answers':{'C':{'status':'unanswered','text':'SC records annual review'}},'context':{},'skill':{},
             'evidence':[{'id':'e','control_id':'C','text':'ARCHIVED FILE CONTENT','sha256':'a','filename':'proof.txt'}],
             'policies':[{'id':'p','control_ids':['C'],'text':'ARCHIVED POLICY CONTENT'}]}
    assert assessment_policy.prepare(payload)['evidence']==[]
    first=incremental.fingerprints(payload,'http:test')
    payload['evidence'][0]['sha256']='b';payload['policies'][0]['text']='Updated archive'
    assert incremental.fingerprints(payload,'http:test')==first
    payload['answers']['C']['text']='The process is no longer implemented'
    assert incremental.fingerprints(payload,'http:test')!=first


def test_archive_citations_are_not_treated_as_verified_proof():
    f=finding();f['evidence_ids']=['archive-id']
    with pytest.raises(ValueError,match='unknown or incorrectly linked'):
        ai.validate_findings([f],[{'id':'C'}],[{'id':'archive-id','control_id':'C','text':'Text'}],{'C':{'text':'SC recorded implementation'}},BASIS)


def test_gateway_answer_based_prompt_omits_strict_evidence_requirement_and_file_contents(fake_gateway):
    fake_gateway['text']=json.dumps([finding()])
    result=gateway.infer(gateway.Request(operation='assess',model='test-model',system=assessment_policy.REVIEW_PROCEDURE,
        payload={'assessment_basis':BASIS,'controls':[{'id':'C'}],
                 'evidence':[{'text':'PRIVATE_ARCHIVE_CONTENT'}],'policies':[{'text':'PRIVATE_POLICY_CONTENT'}]}),'Bearer test-key')
    prompt=fake_gateway['messages'][0]['content']
    assert 'Attachments are optional archival files' in prompt
    assert 'Use met only when the full requirement is implemented and supported by cited readable evidence' not in prompt
    assert 'PRIVATE_ARCHIVE_CONTENT' not in fake_gateway['messages'][1]['content']
    assert 'PRIVATE_POLICY_CONTENT' not in fake_gateway['messages'][1]['content']
    assert result['output'][0]['status']=='met'
    assert gateway.GATEWAY_VERSION=='qwen35-v14'


def test_ollama_answer_based_prompt_has_no_conflicting_proof_rule():
    p=ai.OllamaProvider(model='synthetic')
    with patch('app.ai.build_opener') as opener:
        opener.return_value.open.return_value=BytesIO(json.dumps({'done':True,'message':{'content':json.dumps({'output':[finding()]})}}).encode())
        p.request('assess',{'assessment_basis':BASIS,'controls':[{'id':'C'}],'evidence':[{'text':'PRIVATE_ARCHIVE_CONTENT'}]})
        body=json.loads(opener.return_value.open.call_args.args[0].data)
    assert 'An attachment, uploaded file or separate proof is not required' in body['messages'][0]['content']
    assert 'Use met only when the explicit implementation answer and cited readable evidence' not in body['messages'][0]['content']
    assert 'PRIVATE_ARCHIVE_CONTENT' not in body['messages'][1]['content']


def test_sc_archive_upload_keeps_current_results_and_does_not_call_ai_again(db,monkeypatch):
    s=standard(db);s.mapping={**s.mapping,'assessment_basis':BASIS};db.commit()
    a=assessment(db,s.id)
    answers={c['id']:{'status':'unanswered','text':'SC records full implementation with annual owner review'} for c in s.controls}
    main.save_answers(a['id'],ReplaceAnswers(expected_version=1,answers=answers),db)
    calls=[]
    class Provider(ai.HTTPProvider):
        def __init__(self):super().__init__(url='https://synthetic.example',model='test',api_key='')
        def request(self,operation,payload):
            calls.append(payload);self.usage_calls.append(safe_usage({'prompt_tokens':10,'completion_tokens':5,'generation_seconds':.1}))
            assert operation=='assess' and payload['assessment_basis']==BASIS
            assert payload['evidence']==[] and payload['policies']==[]
            return [finding(c['id']) for c in payload['controls']]
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(Provider()))
    first=main.run_assessment(a['id'],RunInput(),db)
    assert first['summary']['status']=='passed'
    assert first['summary']['rule']['evidence_role']=='archive_only'
    db.add(Evidence(assessment_id=a['id'],control_id=s.controls[0]['id'],filename='screen.png',sha256='a'*64,content=b'ARCHIVED_IMAGE',text=''))
    db.add(Policy(assessment_id=a['id'],name='Archived policy',version='1',text='ARCHIVED_POLICY',control_ids=[s.controls[0]['id']],changes={}))
    db.commit()
    assert not main.get_assessment(a['id'],db)['evaluation']['stale']
    second=main.run_assessment(a['id'],RunInput(),db)
    assert len(calls)==1 and second['usage']['total_tokens']==0
    assert len(second['analysis_plan']['reused_control_ids'])==len(s.controls)
    assert main.list_evidence(a['id'],db)[0]['filename']=='screen.png'
    assert db.scalar(select(func.count()).select_from(Evidence))==1


def test_policy_migration_retains_one_current_skill_and_historical_run_snapshots(db):
    s=standard(db);s.name='ISO/IEC 27001:2022 (SC Audit Checklist)';s.version='SC202512-03-V1';db.commit()
    family=SkillFamily(standard_id=s.id,name='SC ISO 27001:2022 audit review',core_instructions='Old evidence-required core')
    db.add(family);db.flush()
    old=StandardSkillVersion(family_id=family.id,version='1.1',instructions='Old evidence-required refinements')
    db.add(old);db.commit();oldid=old.id
    a=assessment(db,s.id,oldid)
    snapshot=main.selected_skill(db,oldid,s.id)
    run=Run(assessment_id=a['id'],revision=1,provider='http:test',skill=deepcopy(snapshot),results=[])
    db.add(run);db.commit();rid=run.id
    spec=importlib.util.spec_from_file_location('configure_sc_answer_review',Path(__file__).resolve().parents[2]/'scripts/configure_sc_answer_review.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    current,changed=module.configure(db);db.commit()
    assert changed and current!=oldid
    assert db.scalar(select(func.count()).select_from(StandardSkillVersion))==1
    assert db.get(Run,rid).skill==snapshot
    assert s.mapping['assessment_basis']==BASIS
    selected=main.selected_skill(db,None,s.id)
    assert selected['id']==current and selected['version']=='2.1'
    assert 'sole assessment basis for C, OFI, NC-Minor and NC-Major' in selected['core_instructions']
    assert module.configure(db)==(current,False)
