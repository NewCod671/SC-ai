import pytest
from fastapi import HTTPException
from .test_gateway import gateway
from .test_inference_diagnostics import fake_gateway


def note(grade='OFI',status='met',gap='NONE',impact='does_not_affect'):
    return '\n'.join([f'GRADE: {grade}',f'STATUS: {status}',
        'FINDING: Staff use the "Finance" role; paths include C:\\Reports\\2026.',
        'The note includes {literal braces} and a separate line.',
        'REASON: Required arrangements operate; a clearer view is optional.',
        f'GAP: {gap}',f'IMPACT: {impact}','ACTION: Consider a consolidated view.'])


def test_finding_quotes_paths_braces_and_multiline_need_no_json_escaping():
    result=gateway.assessment_lines_output(note(),{'id':'A.8.3'})[0]
    assert result['control_id']=='A.8.3'
    assert '"Finance"' in result['rationale']
    assert 'C:\\Reports\\2026' in result['rationale']
    assert '\nThe note includes {literal braces}' in result['rationale']
    assert result['audit']['category']=='ofi'
    assert result['audit']['requirement_gap']==''


@pytest.mark.parametrize('grade,status,gap,impact,expected',[
    ('C','met','NONE','does_not_affect','comply'),
    ('NC-Minor','partial','Required review was missed.','does_not_affect','nc_minor'),
    ('NC-Major','gap','Required protection is absent.','affects','nc_major'),
    ('Needs Review','gap','Required jobs failed.','unknown','needs_review')])
def test_labels_preserve_grade_and_confirmed_gap(grade,status,gap,impact,expected):
    result=gateway.assessment_lines_output(note(grade,status,gap,impact),{'id':'4.3'})[0]
    assert result['audit']['category']==expected
    assert result['status']==status


@pytest.mark.parametrize('text,reason',[
    (note().replace('IMPACT: does_not_affect',''),'assessment_line_missing'),
    (note()+'\nGRADE: C','assessment_line_duplicate'),
    ('Here is my assessment:\n'+note(),'assessment_line_preamble')])
def test_incomplete_or_ambiguous_text_is_not_accepted(text,reason):
    with pytest.raises(gateway.OutputFormatError) as exc:gateway.assessment_lines_output(text,{'id':'4.3'})
    assert exc.value.reason==reason


def body(retry=False):
    return gateway.Request(operation='assess',model='test-model',system='Grade actual notes only.',
        prompt_profile='compact-v1',payload={'assessment_basis':'sc_recorded_answers_v1',
            'controls':[{'id':'A.8.3'}],'format_retry':retry})


def test_gateway_builds_api_json_from_model_text(fake_gateway):
    fake_gateway['text']=note()
    result=gateway.infer(body(),'Bearer test-key')
    assert result['output'][0]['audit']['category']=='ofi'
    assert result['output'][0]['control_id']=='A.8.3'
    assert fake_gateway['generation_options']['max_new_tokens']==768
    assert 'WIRE OUTPUT CONTRACT' in fake_gateway['messages'][0]['content']
    assert gateway.last_inference_stats['response_format']=='assessment-lines-v1'


def test_bad_text_returns_content_free_reason_for_backend_retry(fake_gateway):
    fake_gateway['text']=note()+'\nGRADE: C'
    with pytest.raises(HTTPException) as exc:gateway.infer(body(True),'Bearer test-key')
    assert exc.value.headers['X-Inference-Format-Reason']=='assessment_line_duplicate'
    assert fake_gateway['generation_options']['max_new_tokens']==1024


def test_summary_is_saved_without_appending_grade_reason_or_action(fake_gateway):
    summary='Access is approved by system owners and reviewed quarterly. One temporary account review was overdue; the account had expired and login was blocked.'
    fake_gateway['text']='\n'.join(['GRADE: NC-Minor','STATUS: partial','FINDING: '+summary,
        'REASON: One bounded review lapse with effective account expiry.',
        'GAP: One required account review was overdue.','IMPACT: does_not_affect',
        'ACTION: Complete the outstanding review.'])
    result=gateway.infer(body(),'Bearer test-key')['output'][0]
    assert result['rationale']==summary
    assert result['audit']['category']=='nc_minor'
    assert result['recommendation']=='Complete the outstanding review.'
    assert result['audit']['reason'] not in result['rationale']


def test_workbook_topic_lines_and_separate_recorded_attribution_are_preserved(fake_gateway):
    finding='Internal issues: on-call staffing capacity.\nExternal issues: cloud-provider dependencies.\n\nConfirmed by interview with the recorded ISMS owner.'
    fake_gateway['text']='\n'.join(['GRADE: C','STATUS: met','FINDING: '+finding,
        'REASON: Recorded issues and ownership address the requirement.','GAP: NONE',
        'IMPACT: does_not_affect','ACTION: NONE'])
    result=gateway.infer(body(),'Bearer test-key')['output'][0]
    assert result['rationale']==finding
    assert result['audit']['category']=='comply'
    assert not result['rationale'].startswith('Document reference:')


def test_two_control_blocks_bind_different_grades_to_exact_input_ids(fake_gateway):
    first=note('C','met','NONE','does_not_affect')
    second=note('NC-Major','gap','Required recovery process absent.','affects')
    fake_gateway['text']='CONTROL: 1\n'+first+'\nCONTROL: 2\n'+second
    request=body()
    request.payload['controls']=[{'id':'4.1'},{'id':'A.8.13'}]
    result=gateway.infer(request,'Bearer test-key')['output']
    assert [r['control_id'] for r in result]==['4.1','A.8.13']
    assert [r['audit']['category'] for r in result]==['comply','nc_major']
    assert fake_gateway['generation_options']['max_new_tokens']==1536


@pytest.mark.parametrize('indexes',[(1,),(1,1),(2,1),(1,3)])
def test_bad_batch_indexes_are_rejected_for_single_control_recovery(indexes):
    text='\n'.join(f'CONTROL: {index}\n'+note() for index in indexes)
    with pytest.raises(gateway.OutputFormatError) as error:
        gateway.assessment_batch_lines_output(text,[{'id':'4.1'},{'id':'4.2'}])
    assert error.value.reason=='assessment_batch_coverage_invalid'
