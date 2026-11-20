from copy import deepcopy
from types import SimpleNamespace
import pytest
from app import ai
from app.reporting import excel
from app.audit_classification import complete, normalize
from app.outcomes import summarize
from .test_excel_assessment_status import sample
from .fixtures import patch_cell


def result(status,category,impact='does_not_affect',gap=''):
    return {'control_id':'C','status':status,'rationale':'Observed facts','recommendation':'Specific optional improvement',
            'evidence_ids':['e'],'audit':{'category':category,'reason':'Facts supporting classification',
            'requirement_gap':gap,'system_impact':impact}}


@pytest.mark.parametrize('status,category,impact,gap,expected',[
    ('met','comply','does_not_affect','','comply'),
    ('met','ofi','does_not_affect','','ofi'),
    ('gap','ofi','does_not_affect','Unmet obligation','needs_review'),
    ('partial','ofi','does_not_affect','Unmet obligation','needs_review'),
    ('partial','nc_minor','does_not_affect','Isolated missed review','nc_minor'),
    ('gap','nc_major','affects','Systemic process failure','nc_major'),
    ('gap','nc_minor','unknown','Unmet obligation','needs_review'),
    ('gap','nc_major','unknown','Unmet obligation','needs_review'),
    ('gap','nc_major','does_not_affect','Unmet obligation','needs_review'),
    ('gap','nc_minor','affects','Unmet obligation','needs_review'),
    ('gap','nc_major','affects','','needs_review'),
    ('met','nc_minor','does_not_affect','Unmet obligation','needs_review'),
    ('needs_review','nc_major','affects','Unmet obligation','needs_review'),
    ('not_applicable','not_applicable','unknown','','not_applicable'),
])
def test_classification_consistency(status,category,impact,gap,expected):
    assert normalize(result(status,category,impact,gap))['audit']['category']==expected


def test_met_guard_downgrades_audit_when_evidence_is_unreadable():
    f=result('met','ofi')
    checked=ai.validate_findings([f],[{'id':'C'}],[{'id':'e','control_id':'C','text':''}],{'C':{'text':'We do this'}})
    assert checked[0]['status']=='needs_review'
    assert checked[0]['audit']['category']=='needs_review'


def test_legacy_findings_are_not_guessed_as_minor_or_ofi():
    legacy={'control_id':'C','status':'partial'}
    original=deepcopy(legacy)
    checked=complete([legacy])[0]
    assert checked['audit']['category']=='needs_review'
    assert legacy==original


def test_nc_minor_is_not_a_partial_pass_and_ofi_is_not_a_failure():
    s=SimpleNamespace(id='s',name='Standard',version='1',controls=[{'id':'C'}])
    minor=summarize(s,[result('partial','nc_minor',gap='Missed required step')])
    assert minor['status']=='failed' and minor['audit']['counts']['nc_minor']==1
    assert summarize(s,[result('met','ofi')])['status']=='passed'
    missing=summarize(s,complete([{'control_id':'C','status':'met'}]))
    assert missing['status']=='needs_review'


def test_audit_grades_export_and_roundtrip_and_cannot_be_forged():
    template, controls, mapping, answers, findings, manifest=sample()
    categories=['comply','nc_major','nc_minor','needs_review','not_applicable']
    for f,category in zip(findings,categories):
        f['audit']={'category':category,'reason':'Audit reason','requirement_gap':'Gap' if category.startswith('nc_') else '',
                    'system_impact':'affects' if category=='nc_major' else 'does_not_affect'}
    data=excel.export(template,mapping,controls,answers,manifest,results=findings)
    wb=excel.read(data)
    assert [wb['Checklist'].cell(c['row'],mapping['indices']['status']).value for c in controls]==[
        'C','NC-Major','NC-Minor','ต้องตรวจเพิ่ม','ไม่เกี่ยวข้อง']
    wb.close()
    assert excel.import_answers(data,mapping,controls,manifest,answers)==answers
    forged=patch_cell(data,controls[1]['row'],mapping['indices']['status'],'Comply')
    with pytest.raises(ValueError,match='assessment result'):
        excel.import_answers(forged,mapping,controls,manifest,answers)
    stale=excel.export(template,mapping,controls,answers,manifest,results=findings,stale=True)
    assert excel.read(stale)['Checklist']['E2'].value=='ต้องประเมินใหม่'
from .test_inference_diagnostics import fake_gateway  # noqa: F401
from .test_gateway import gateway
import json


def test_gateway_preserves_audit_output_and_requests_explicit_grades(fake_gateway):
    finding=result('gap','nc_major','affects','Demonstrated systemic process failure')
    fake_gateway['text']=json.dumps([finding])
    response=gateway.infer(gateway.Request(operation='assess',model='test-model',
        system=ai.REVIEW_PROCEDURE,payload={'controls':[{'id':'C'}]}),'Bearer test-key')
    assert response['output'][0]['audit']==finding['audit']
    prompt=fake_gateway['messages'][0]['content']
    assert 'nc_major' in prompt and 'requirement_gap' in prompt and 'system_impact' in prompt
    assert gateway.health('Bearer test-key')['gateway_version']==gateway.GATEWAY_VERSION
