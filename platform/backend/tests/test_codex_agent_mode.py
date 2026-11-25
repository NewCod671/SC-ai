"""Experimental workflows use mock providers; no tests consume Codex quota."""
from copy import deepcopy
import json
from types import SimpleNamespace
from pathlib import Path
import pytest
from fastapi import HTTPException
from sqlalchemy import select,func
from app import main,ai,agent_mode,codex_provider,assessment_jobs as jobs
from app.models import AgentRun,AuditorFeedback,Evidence,Run,Revision,Assessment
from app.schemas import RunInput,ReplaceAnswers,ProcessingModeInput,FeedbackInput,FeedbackReviewInput,DeleteAssessmentInput,NoteReviewInput
from .test_standard_skills import db,standard,assessment
from app.codex_context_mcp import dispatch


def workbook_parts(data):
    from io import BytesIO
    from zipfile import ZipFile
    # ZIP container timestamps are not workbook content or formatting.
    with ZipFile(BytesIO(data)) as archive:return {name:archive.read(name) for name in archive.namelist()}


def fixture(db,monkeypatch,evidence=True):
    monkeypatch.setattr(codex_provider,'health',lambda:{'available':True,'version':'synthetic-cli','code':'ready'})
    s=standard(db);a=assessment(db,s.id)
    a=main.save_answers(a['id'],ReplaceAnswers(expected_version=1,answers={c['id']:{'text':'Implemented practice with isolated exception where described','status':'unanswered'} for c in s.controls}),db)
    for c in s.controls:
        if evidence:db.add(Evidence(assessment_id=a['id'],control_id=c['id'],filename='observations.txt',sha256='a'*64,content=b'Observed actual operation',text='Auditor observed actual operation and recorded bounded exceptions with system capability unaffected.'))
    db.commit()
    calls=[]
    class Fake:
        def __init__(self,bundle=None):self.name='codex:synthetic';self.usage_calls=[]
        def assess_control(self,case):
            calls.append(deepcopy(case))
            self.usage_calls.append({'prompt_tokens':10,'completion_tokens':5,'generation_seconds':None})
            return finding(case)
    monkeypatch.setattr(codex_provider,'CodexProvider',Fake)
    return s,a,calls


def finding(case,category='comply'):
    return {'control_id':case['control_id'],'status':'met' if category in {'comply','ofi'} else 'gap','rationale':'Verified observations','recommendation':'Improve optional usability' if category=='ofi' else 'Correct missing required step' if category.startswith('nc_') else '',
        'interpreted_answer':'Recorded practice','evidence_ids':[e['id'] for e in case['evidence']],
        'audit':{'category':category,'reason':'Verified full implementation' if category in {'comply','ofi'} else 'Recorded gap affects system capability' if category=='nc_major' else 'Recorded isolated gap without system impact',
            'requirement_gap':'Required step absent' if category.startswith('nc_') else '',
            'system_impact':'affects' if category=='nc_major' else 'does_not_affect'}}


def test_default_colab_and_mode_switch_preserve_all_answers_and_exports(db,monkeypatch):
    s,a,calls=fixture(db,monkeypatch)
    assert main.get_assessment(a['id'],db)['processing_mode']=='colab'
    original=deepcopy(main.get_assessment(a['id'],db)['answers'])
    production=main.run_assessment(a['id'],RunInput(),db)
    workbook=main.assessment_workbook(a['id'],db)[1]
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    with pytest.raises(HTTPException) as missing_consent:main.run_assessment(a['id'],RunInput(),db)
    assert missing_consent.value.status_code==422 and not calls
    experimental=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert experimental['experimental'] and len(calls)==2
    assert experimental['summary']['status']=='passed'
    assert workbook_parts(main.assessment_workbook(a['id'],db)[1])==workbook_parts(workbook)
    assert db.scalar(select(func.count()).select_from(Run))==1
    assert db.scalar(select(func.count()).select_from(AgentRun))==1
    assert main.get_assessment(a['id'],db)['answers']==original
    assert [r['id'] for r in main.runs(a['id'],db)]==[experimental['id']]
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='colab',expected_version=2),db)
    assert [r['id'] for r in main.runs(a['id'],db)]==[production['id']]


def test_recorded_answer_supports_grades_without_archived_evidence(db,monkeypatch):
    s,a,_=fixture(db,monkeypatch,evidence=False)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    result=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert all(f['status']=='met' and f['audit']['category']=='comply' for f in result['results'])
    case=agent_mode.make_case(agent_mode.client_inputs(db,db.get(Assessment,a['id'])),s.controls[0],[])
    for category in ('ofi','nc_minor','nc_major'):
        output=finding(case,category)
        output['evidence_ids']=[]
        assert codex_provider.validate_result({'finding':output},case)['audit']['category']==category


@pytest.mark.parametrize('category',['nc_minor','nc_major','ofi'])
def test_verified_classifications_and_exact_id_validation(db,monkeypatch,category):
    s,a,_=fixture(db,monkeypatch)
    case=agent_mode.make_case(agent_mode.client_inputs(db,db.get(Assessment,a['id'])),s.controls[0],[])
    output=finding(case,category)
    assert codex_provider.validate_result({'finding':output},case)['audit']['category']==category
    bad=deepcopy(output);bad['control_id']='WRONG'
    with pytest.raises(ValueError):codex_provider.validate_result({'finding':bad},case)
    with_archive_id=deepcopy(output);with_archive_id['evidence_ids']=['OTHER-CLIENT']
    assert codex_provider.validate_result({'finding':with_archive_id},case)['evidence_ids']==[]


def test_saved_note_review_runs_again_even_with_a_completed_assessment(db, monkeypatch):
    s,a,calls=fixture(db,monkeypatch,evidence=False)
    original=codex_provider.CodexProvider
    complete=False

    class Provider(original):
        def assess_control(self, case):
            if case.get('note_review_instruction'):
                super().assess_control(case)
                if complete:
                    return {'coverage_topics':[{'topic':'Check owner','status':'covered',
                                               'quote':case['answer']['text'],'source_control_id':case['control_id'],
                                               'missing_detail':''}]}
                return {'coverage_topics':[{'topic':'Draft detail','status':'missing',
                                           'missing_detail':'The note does not identify who performs the check.'}]}
            return super().assess_control(case)

    monkeypatch.setattr(codex_provider,'CodexProvider',Provider)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    run=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    cid=s.controls[0]['id']
    assert run['results'][0]['audit']['category']=='comply'
    note=a['answers'][cid]['text']
    body=NoteReviewInput(expected_version=2,control_id=cid,text=note,
                         processing_mode='codex',authorize_external=True)
    calls.clear()
    for _ in range(2):
        result=main.review_clause_note(a['id'],body,db)
        assert result['requires_review'] is True
        assert result['missing_details']=='The note does not identify who performs the check.'
        assert result['question_coverage'][0]['status']=='missing'
    assert len(calls)==2
    assert all(call['answer']['text']==note and call.get('note_review_instruction') for call in calls)
    assert main.runs(a['id'],db)[-1]['results']==run['results']
    reassessed=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert reassessed['results'][0]['audit']['category']=='comply'
    assert set(reassessed['analysis_plan']['reused_control_ids'])=={control['id'] for control in s.controls}
    complete=True
    assert main.review_clause_note(a['id'],body,db)['requires_review'] is False
    cleared=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert cleared['results'][0]['audit']['category']=='comply'

    complete=False
    draft='Changed note with an unresolved detail'
    main.review_clause_note(a['id'],body.model_copy(update={'text':draft}),db)
    still_saved=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert still_saved['results'][0]['audit']['category']=='comply'
    answers=deepcopy(a['answers'])
    answers[cid]['text']=draft
    main.save_answers(a['id'],ReplaceAnswers(expected_version=2,answers=answers),db)
    saved_draft=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert saved_draft['results'][0]['audit']['category']=='comply'


def test_pending_feedback_not_retrieved_approved_feedback_is_scoped(db,monkeypatch):
    s,a,_=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    result=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    cid=s.controls[0]['id'];e=db.scalar(select(Evidence).where(Evidence.assessment_id==a['id'],Evidence.control_id==cid))
    correction=main.add_auditor_feedback(a['id'],FeedbackInput(run_id=result['id'],run_mode='codex',control_id=cid,category='ofi',explanation='Full implementation; optional usability improvement',evidence_ids=[]),db)
    assert not agent_mode.approved_examples(db,a['id'],s.controls[0])
    approved=main.review_auditor_feedback(a['id'],correction['id'],FeedbackReviewInput(decision='approved',reviewer='Local auditor'),db)
    assert approved['decision']=='approved'
    examples=agent_mode.approved_examples(db,a['id'],s.controls[0])
    assert len(examples)==1 and examples[0]['approved_category']=='ofi'
    assert main.feedback_summary(a['id'],db)['correction_count']==1
    bundle=codex_provider.skill_bundle();bundle['finding-classifier']['sha256']='changed-rule'
    assert agent_mode.approved_examples(db,a['id'],s.controls[0],bundle)==[]
    other=assessment(db,s.id)
    assert not agent_mode.approved_examples(db,other['id'],s.controls[0])
    with pytest.raises(HTTPException) as cross:main.review_auditor_feedback(other['id'],correction['id'],FeedbackReviewInput(decision='approved',reviewer='Other'),db)
    assert cross.value.status_code==404
    assert db.get(AgentRun,result['id']).results[0]['audit']['category']=='comply'
    with pytest.raises(HTTPException):main.add_auditor_feedback(other['id'],FeedbackInput(run_id=result['id'],run_mode='codex',control_id=cid,category='ofi',explanation='Not ours'),db)


def test_cancellation_saves_validated_control_and_resume_skips_it(db,monkeypatch):
    s,a,calls=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    job=jobs.reserve(a['id'])
    Original=codex_provider.CodexProvider
    class Stop(Original):
        def assess_control(self,case):
            if case['control_id']==s.controls[1]['id']:jobs.cancel(job['id']);jobs.check_cancelled()
            return super().assess_control(case)
    monkeypatch.setattr(codex_provider,'CodexProvider',Stop)
    with pytest.raises(HTTPException) as cancelled:main.run_assessment(a['id'],RunInput(job_id=job['id'],authorize_external=True),db)
    assert cancelled.value.status_code==409
    partial=db.scalar(select(AgentRun));assert partial.status=='cancelled'
    assert partial.results[0]['status']=='met' and partial.results[1]['status']=='needs_review'
    assert db.scalar(select(func.count()).select_from(Run))==0
    calls.clear();monkeypatch.setattr(codex_provider,'CodexProvider',Original)
    resumed=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert [case['control_id'] for case in calls]==[s.controls[1]['id']]
    assert resumed['analysis_plan']['reused_control_ids']==[s.controls[0]['id']]


def test_unavailable_codex_never_falls_back_to_colab(db,monkeypatch):
    s,a,calls=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    monkeypatch.setattr(codex_provider,'health',lambda:{'available':False,'code':'codex_not_authenticated'})
    monkeypatch.setattr(ai,'provider',lambda:pytest.fail('No fallback allowed'))
    with pytest.raises(HTTPException) as error:main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert error.value.status_code==503 and not calls
    assert db.scalar(select(func.count()).select_from(AgentRun))==0


def test_readonly_mcp_rejects_cross_control_and_tool_paths():
    snapshot={'control_id':'A.5.18','control':{'requirement':'Read scoped control'},'evidence':[],'approved_examples':[]}
    assert len(dispatch(snapshot,{'method':'tools/list'})['tools'])==3
    for name in ('get_control','get_evidence','get_approved_examples'):
        result=dispatch(snapshot,{'method':'tools/call','params':{'name':name,'arguments':{'control_id':'OTHER'}}})
        assert result['isError']
    assert dispatch(snapshot,{'method':'tools/call','params':{'name':'get_control','arguments':{'control_id':'A.5.18','path':'C:/secret'}}})['isError']
    assert dispatch(snapshot,{'method':'tools/call','params':{'name':'get_control','arguments':{'control_id':'A.5.18'}}})['isError'] is False


def test_delete_also_removes_experimental_and_feedback_data(db,monkeypatch):
    s,a,_=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    main.delete_assessment(a['id'],DeleteAssessmentInput(expected_version=2,confirm_name=a['name']),db)
    assert db.scalar(select(func.count()).select_from(AgentRun))==0
    assert db.scalar(select(func.count()).select_from(AuditorFeedback))==0


def test_control_failure_is_isolated_and_only_failed_control_is_retried(db,monkeypatch):
    s,a,calls=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    Original=codex_provider.CodexProvider
    class Bad(Original):
        def assess_control(self,case):
            if case['control_id']==s.controls[0]['id']:raise codex_provider.CodexError('codex_json_invalid')
            return super().assess_control(case)
    monkeypatch.setattr(codex_provider,'CodexProvider',Bad)
    first=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert first['status']=='completed_with_warnings'
    assert first['results'][0]['status']=='needs_review' and first['results'][1]['status']=='met'
    calls.clear();monkeypatch.setattr(codex_provider,'CodexProvider',Original)
    second=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert [c['control_id'] for c in calls]==[s.controls[0]['id']]
    assert len(second['results'])==2


def test_experimental_export_reuses_original_template_and_does_not_change_production(db,monkeypatch):
    from io import BytesIO
    from openpyxl import load_workbook
    s,a,_=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    before=main.assessment_workbook(a['id'],db)[1]
    result=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    exported=agent_mode.export_run(db,a['id'],result['id'])
    original=load_workbook(BytesIO(s.template))
    experimental=load_workbook(BytesIO(exported))
    assert set(original.sheetnames)<=set(experimental.sheetnames)
    assert list(original[original.sheetnames[0]].merged_cells.ranges)==list(experimental[original.sheetnames[0]].merged_cells.ranges)
    assert workbook_parts(main.assessment_workbook(a['id'],db)[1])==workbook_parts(before)
    preview=main.preview_agent_run(a['id'],result['id'],sheet=None,page=1,db=db)
    assert preview['version']=='experimental' and preview['rows']


def test_stale_client_snapshot_and_missing_evidence_feedback_cannot_be_approved(db,monkeypatch):
    s,a,_=fixture(db,monkeypatch,evidence=False)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    run=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    entry=main.add_auditor_feedback(a['id'],FeedbackInput(run_id=run['id'],run_mode='codex',control_id=s.controls[0]['id'],category='comply',explanation='Unverified assertion'),db)
    with pytest.raises(HTTPException) as error:main.review_auditor_feedback(a['id'],entry['id'],FeedbackReviewInput(decision='approved',reviewer='Auditor'),db)
    assert error.value.status_code==422
    changed=deepcopy(a['answers']);changed[s.controls[0]['id']]['text']='Changed answer'
    main.save_answers(a['id'],ReplaceAnswers(expected_version=2,answers=changed),db)
    assert agent_mode.run_json(db,db.get(Assessment,a['id']),db.get(AgentRun,run['id']))['stale']


def test_provider_switch_race_fails_closed_instead_of_using_another_provider(db,monkeypatch):
    s,a,calls=fixture(db,monkeypatch)
    with pytest.raises(HTTPException) as error:main.run_assessment(a['id'],RunInput(processing_mode='codex',authorize_external=True),db)
    assert error.value.status_code==409 and not calls


def test_answers_changed_during_agent_execution_are_not_overwritten_and_run_is_stale(db,monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy import update
    s,a,calls=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    Original=codex_provider.CodexProvider
    class Changing(Original):
        def assess_control(self,case):
            if not calls:
                with Session(db.get_bind()) as other:
                    answers=deepcopy(a['answers']);answers[s.controls[0]['id']]['text']='New work recorded during inference'
                    other.add(Revision(assessment_id=a['id'],version=3,answers=answers,source='web'))
                    other.execute(update(Assessment).where(Assessment.id==a['id']).values(current_version=3));other.commit()
            return super().assess_control(case)
    monkeypatch.setattr(codex_provider,'CodexProvider',Changing)
    result=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert result['revision']==2 and result['stale']
    assert main.get_assessment(a['id'],db)['answers'][s.controls[0]['id']]['text']=='New work recorded during inference'


def batch_provider(monkeypatch):
    calls=[]
    class Batch:
        name='codex:synthetic'
        def __init__(self,bundle=None):self.usage_calls=[]
        def assess_controls(self,cases):
            calls.append(deepcopy(cases))
            self.usage_calls.append({'prompt_tokens':120,'completion_tokens':30,'generation_seconds':None,'control_ids':[c['control_id'] for c in cases]})
            return [finding(c) for c in cases]
    monkeypatch.setattr(codex_provider,'CodexProvider',Batch)
    return Batch,calls


def test_batch_workflow_reduces_calls_and_reuses_prior_successes(db,monkeypatch):
    s,a,_=fixture(db,monkeypatch)
    Original,calls=batch_provider(monkeypatch)
    monkeypatch.setenv('CODEX_BATCH_SIZE','5')
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    run=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert len(calls)==1 and len(calls[0])==2
    assert run['analysis_plan']['requests']==1 and run['analysis_plan']['batch_size']==5
    assert run['usage']['total_tokens']==150 and run['usage']['requests']==1
    calls.clear()
    resumed=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert not calls and len(resumed['analysis_plan']['reused_control_ids'])==2
    changed=deepcopy(a['answers']);changed[s.controls[0]['id']]['text']='Changed only this answer'
    main.save_answers(a['id'],ReplaceAnswers(expected_version=2,answers=changed),db)
    updated=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert len(calls)==1 and [c['control_id'] for c in calls[0]]==[s.controls[0]['id']]
    assert updated['analysis_plan']['reused_control_ids']==[s.controls[1]['id']]


@pytest.mark.parametrize('error',['missing','duplicate','evidence'])
def test_batch_bad_row_does_not_discard_valid_sibling_and_retry_is_targeted(db,monkeypatch,error):
    s,a,_=fixture(db,monkeypatch)
    Original,calls=batch_provider(monkeypatch)
    class Bad(Original):
        def assess_controls(self,cases):
            rows=super().assess_controls(cases)
            if error=='missing':return rows[:1]
            if error=='duplicate':return rows+[deepcopy(rows[1])]
            rows[1]['evidence_ids']=rows[0]['evidence_ids'];return rows
    monkeypatch.setattr(codex_provider,'CodexProvider',Bad)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    run=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert run['analysis_plan']['ai_control_ids']==[s.controls[0]['id']]
    assert run['analysis_plan']['failed_control_ids']==[s.controls[1]['id']]
    assert run['results'][0]['status']=='met' and run['results'][1]['status']=='needs_review'
    monkeypatch.setattr(codex_provider,'CodexProvider',Original);calls.clear()
    main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert len(calls)==1 and [c['control_id'] for c in calls[0]]==[s.controls[1]['id']]


def test_batch_sizing_respects_count_and_context_budget_without_truncation(monkeypatch):
    controls=[{'id':str(i)} for i in range(12)]
    cases={c['id']:{'text':'x'*600} for c in controls}
    assert [len(b) for b in agent_mode.control_batches(controls,cases,5,48000)]==[5,5,2]
    assert [len(b) for b in agent_mode.control_batches(controls,cases,5,1000)]==[1]*12
    monkeypatch.setenv('CODEX_BATCH_SIZE','0')
    with pytest.raises(HTTPException):agent_mode.batch_limits()


def test_codex_runs_two_batches_concurrently_and_saves_in_checklist_order(db,monkeypatch):
    from threading import Barrier, Event

    s,a,_=fixture(db,monkeypatch)
    Original,_=batch_provider(monkeypatch)
    ids=[c['id'] for c in s.controls]
    both_running=Barrier(2)
    second_done=Event()
    class Concurrent(Original):
        def assess_controls(self,cases):
            both_running.wait(timeout=5)
            rows=super().assess_controls(cases)
            if cases[0]['control_id']==ids[0]:
                assert second_done.wait(timeout=5)
            else:
                second_done.set()
            return rows

    monkeypatch.setattr(codex_provider,'CodexProvider',Concurrent)
    monkeypatch.setenv('CODEX_BATCH_SIZE','1')
    monkeypatch.setenv('CODEX_BATCH_WORKERS','2')
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    result=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert result['analysis_plan']['ai_control_ids']==ids
    assert result['usage']['requests']==2 and result['usage']['total_tokens']==300


def test_batch_cancellation_preserves_previous_batch_and_resume_only_remaining(db,monkeypatch):
    s,a,_=fixture(db,monkeypatch)
    Original,calls=batch_provider(monkeypatch)
    monkeypatch.setenv('CODEX_BATCH_SIZE','1')
    monkeypatch.setenv('CODEX_BATCH_WORKERS','1')
    job=jobs.reserve(a['id'])
    class Stopping(Original):
        def assess_controls(self,cases):
            if calls:jobs.cancel(job['id']);jobs.check_cancelled()
            return super().assess_controls(cases)
    monkeypatch.setattr(codex_provider,'CodexProvider',Stopping)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    with pytest.raises(HTTPException) as error:main.run_assessment(a['id'],RunInput(authorize_external=True,job_id=job['id']),db)
    assert error.value.status_code==409
    saved=db.scalar(select(AgentRun))
    assert saved.status=='cancelled' and saved.snapshot['assessment_plan']['ai_control_ids']==[s.controls[0]['id']]
    monkeypatch.setattr(codex_provider,'CodexProvider',Original);calls.clear()
    main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert [c['control_id'] for batch in calls for c in batch]==[s.controls[1]['id']]


def test_three_failed_batches_stop_further_calls_and_record_unknown_usage_once_per_batch(db,monkeypatch):
    s,a,_=fixture(db,monkeypatch,evidence=False)
    controls=deepcopy(s.controls)
    for i in range(4):controls.append(dict(controls[0],id='EXTRA-'+str(i)))
    s.controls=controls;db.commit()
    answers={c['id']:{'status':'unanswered','text':'Synthetic answer'} for c in controls}
    main.save_answers(a['id'],ReplaceAnswers(expected_version=2,answers=answers),db)
    Original,calls=batch_provider(monkeypatch)
    class Broken(Original):
        def assess_controls(self,cases):
            calls.append(cases)
            raise codex_provider.CodexError('codex_exec_failed')
    monkeypatch.setattr(codex_provider,'CodexProvider',Broken)
    monkeypatch.setenv('CODEX_BATCH_SIZE','1')
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=3),db)
    result=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert len(calls)==3 and result['usage']['requests']==3
    assert result['usage']['total_tokens'] is None and result['usage']['unmeasured_requests']==3
    assert len(result['analysis_plan']['failed_control_ids'])==3
    assert len(result['analysis_plan']['skipped_control_ids'])==3
    assert all(f['status']=='needs_review' for f in result['results'])


def test_unverified_legacy_results_are_stale_and_not_reused(db,monkeypatch):
    s,a,calls=fixture(db,monkeypatch)
    main.set_processing_mode(a['id'],ProcessingModeInput(mode='codex',expected_version=2),db)
    first=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    old=db.get(AgentRun,first['id']);snapshot=deepcopy(old.snapshot);snapshot.pop('context_protocol')
    old.snapshot=snapshot;db.commit();calls.clear()
    assert agent_mode.run_json(db,db.get(Assessment,a['id']),old)['stale']
    second=main.run_assessment(a['id'],RunInput(authorize_external=True),db)
    assert len(calls)==2 and not second['analysis_plan']['reused_control_ids']
