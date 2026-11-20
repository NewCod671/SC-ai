import pytest
from fastapi import HTTPException
from app import ai, assessment_policy
from .test_gateway import gateway
from .test_inference_diagnostics import fake_gateway


def compact(status,category,gap='',impact='does_not_affect'):
    return {'c':'4.3','s':status,'i':'Recorded meaning','r':'Required scope elements are complete; a dependency view could simplify use.',
        'n':'Consider an optional dependency view.',
        'a':{'c':category,'r':'Recorded facts support the grade.','g':gap,'i':impact}}


@pytest.mark.parametrize('status,category,gap,impact,expected',[
    ('MET','OFI','','does_not_affect','ofi'),
    ('PARTIAL','NC-Minor','One required review was missed.','does_not_affect','nc_minor'),
    ('GAP','Major','All recovery protection failed.','affects','nc_major'),
    ('Met','Comply','','does_not_affect','comply')])
def test_explicit_grade_survives_wire_casing_and_aliases(status,category,gap,impact,expected):
    row=gateway.expand_compact_assessment([compact(status,category,gap,impact)])[0]
    result=ai.validate_findings([row],[{'id':'4.3'}],[],{'4.3':{'text':'SC onsite note.'}},assessment_policy.SC_ANSWERS)[0]
    assert result['audit']['category']==expected
    assert result['rationale'].startswith('Required scope elements')


def test_short_explicit_ofi_audit_label_is_not_erased():
    row=compact('MET','OFI');row['a']='OFI'
    result=gateway.expand_compact_assessment([row])[0]
    assert result['audit']['category']=='ofi'
    assert result['audit']['reason']==row['r']


def test_full_outer_with_compact_audit_normalizes_both_fields():
    row={'control_id':'4.3','status':'MET','rationale':'Recorded optional improvement.',
        'recommendation':'Consider the optional improvement.','evidence_ids':[],
        'audit':{'c':'OFI','r':'No required gap.','g':'','i':'NO IMPACT'}}
    result=gateway.expand_compact_assessment([row])[0]
    assert result['audit']['category']=='ofi'
    assert result['audit']['system_impact']=='does_not_affect'


def test_malformed_grade_raises_for_retry_instead_of_silent_semantic_downgrade():
    row=compact('MET','not-a-real-grade')
    with pytest.raises(gateway.OutputFormatError) as error:
        gateway.expand_compact_assessment([row])
    assert error.value.reason=='assessment_category_invalid'


def test_contradictory_nc_grade_raises_for_retry():
    with pytest.raises(gateway.OutputFormatError) as error:
        gateway.expand_compact_assessment([compact('MET','NC-Major','Missing process.','affects')])
    assert error.value.reason=='assessment_grade_conflict'


def test_confirmed_gap_is_retained_when_only_severity_is_uncertain():
    row={'control_id':'A.8.13','status':'gap','rationale':'Backup jobs failed; scope of affected systems is not recorded.',
        'recommendation':'Clarify affected scope and recoverability.','evidence_ids':[],
        'audit':{'category':'needs_review','reason':'Confirmed failed jobs; recovery impact needs clarification.',
            'requirement_gap':'Required backups failed.','system_impact':'unknown'}}
    checked=ai.validate_findings([row],[{'id':'A.8.13'}],[],
        {'A.8.13':{'text':'Backups: jobs failed; no independent copy; recovery open.'}},assessment_policy.SC_ANSWERS)[0]
    assert checked['status']=='gap'
    assert checked['audit']['category']=='needs_review'
    assert checked['audit']['requirement_gap']=='Required backups failed.'


def test_bad_grade_is_reported_as_format_failure_without_leaking_note(fake_gateway,monkeypatch):
    import json
    monkeypatch.setenv('AI_ENABLE_THINKING','false')
    fake_gateway['text']=json.dumps([compact('MET','not-a-real-grade')])
    body=gateway.Request(operation='assess',model='test-model',system='Grade the note.',
        payload={'assessment_basis':assessment_policy.SC_ANSWERS,'controls':[{'id':'4.3'}]})
    with pytest.raises(HTTPException) as error:gateway.infer(body,'Bearer test-key')
    assert error.value.status_code==502
    assert error.value.headers['X-Inference-Failure-Code']=='invalid_json'
