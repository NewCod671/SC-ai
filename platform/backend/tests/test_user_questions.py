import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, func
from app import ai, main, agent_mode
from app.reporting import excel
from app.models import Assessment, Audit, Revision
from app.schemas import QuestionMappingInput, AddQuestionInput, ReplaceAnswers, RunInput, ProcessingModeInput
from app.question_mapping import question_mapping, validate_question_mapping
from app.questionnaire import text_by_control
from .test_standard_skills import db, standard, assessment, root_skill  # noqa: F401


@pytest.fixture
def setup(db, monkeypatch):
    s = standard(db)
    a = assessment(db, s.id)
    p = ai.HTTPProvider(url='https://synthetic.example', model='test')
    calls = []
    def request(operation, batch):
        calls.append((operation, batch))
        from app.usage import safe_usage
        p.usage_calls.append(safe_usage({'prompt_tokens':100,'completion_tokens':20,'generation_seconds':.1}))
        if operation == 'map_question':
            return [{'control_id': c['id'], 'score':.8, 'reason':'The question addresses this requirement'} for c in batch['controls']]
        return [{'control_id':c['id'], 'status':'needs_review', 'rationale':'Review evidence',
                 'recommendation':'Supply records', 'evidence_ids':[]} for c in batch['controls']]
    monkeypatch.setattr(p, 'request', request)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(p))
    return s, a, p, calls


def preview(db, a, **kw):
    return main.map_user_question(a['id'], QuestionMappingInput(expected_version=a['current_version'],
        text='Who approves access when someone joins or leaves?', **kw), db)


def test_preview_maps_multiple_controls_records_provider_usage_but_does_not_change_assessment(db, setup):
    s,a,p,calls = setup
    result = preview(db,a)
    assert len(result['candidates']) == len(s.controls)
    assert result['provider'] == 'http:test'
    assert result['usage']['requests'] == 1
    assert result['usage']['total_tokens'] == 120
    assert main.get_assessment(a['id'], db)['questions'] == a['questions']
    assert main.get_assessment(a['id'], db)['current_version'] == 1
    assert db.get(Audit,result['preview_id']).action == 'question.mapping.preview'
    assert calls[0][0] == 'map_question'


def test_codex_processing_mode_routes_custom_question_mapping_to_codex_with_consent(db,setup,monkeypatch):
    s,a,p,calls=setup
    main.set_processing_mode(a['id'],ProcessingModeInput(expected_version=1,mode='codex'),db)
    monkeypatch.setattr('app.codex_provider.health',lambda:{'available':True,'code':'ready'})
    class FakeCodex:
        name='codex-questions:synthetic'
        usage_calls=[{'prompt_tokens':20,'completion_tokens':8,'generation_seconds':None}]
        def map_question(self,text,controls,context,skill,departments,answer_text=''):
            return [{'control_id':controls[0]['id'],'score':.9,'reason':'The wording asks about this requirement.'}]
    monkeypatch.setattr('app.codex_provider.CodexQuestionProvider',FakeCodex)
    with pytest.raises(HTTPException) as error:
        main.map_user_question(a['id'],QuestionMappingInput(expected_version=1,text='Who checks access?'),db)
    assert error.value.status_code==422
    result=main.map_user_question(a['id'],QuestionMappingInput(expected_version=1,text='Who checks access?',
        answer_text='IT reviews access monthly.',authorize_external=True),db)
    assert result['provider']=='codex-questions:synthetic'
    assert result['candidates'][0]['control_id']==s.controls[0]['id']
    assert calls==[]


def test_confirmed_question_preserves_original_questions_answers_and_advances_revision(db, setup):
    s,a,p,calls = setup
    saved = main.save_answers(a['id'], ReplaceAnswers(expected_version=1,
        answers={c['id']:{'text':'Existing department answer','status':'partial'} for c in s.controls}), db)
    existing_answers = saved['answers']
    result = preview(db,saved)
    added = main.add_user_question(a['id'], AddQuestionInput(expected_version=2, preview_id=result['preview_id'],
        control_ids=[c['id'] for c in s.controls]), db)
    assert added['current_version'] == 3
    assert added['answers'] == existing_answers
    assert added['questions'][:-1] == a['questions']
    custom = added['questions'][-1]
    assert custom['source'] == 'user'
    assert custom['text'] == result['text']
    assert custom['control_ids'] == [c['id'] for c in s.controls]
    assert custom['mapping_preview_id'] == result['preview_id']
    assert len(custom['control_mappings']) == len(s.controls)
    assert db.scalar(select(Revision).where(Revision.assessment_id==a['id'],Revision.version==3)).source == 'question_added'
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='question.add')) == 1


def test_user_can_confirm_only_a_subset_and_wording_is_never_rewritten(db, setup):
    s,a,p,calls = setup
    text='Consider systems: Is this wording kept as my actual question?'
    result = main.map_user_question(a['id'],QuestionMappingInput(expected_version=1,text=text),db)
    added = main.add_user_question(a['id'],AddQuestionInput(expected_version=1,preview_id=result['preview_id'],
                                                          control_ids=[s.controls[0]['id']]),db)
    assert added['questions'][-1]['text'] == text
    assert added['questions'][-1]['control_ids'] == [s.controls[0]['id']]


def test_company_interview_question_and_answer_are_saved_together_to_selected_controls(db, setup):
    s,a,p,calls=setup
    saved=main.save_answers(a['id'],ReplaceAnswers(expected_version=1,
        answers={c['id']:{'text':f'Original answer for {c["id"]}','status':'partial'} for c in s.controls}),db)
    answer='IT removed Entra ID access; the legacy account is still under review.'
    result=preview(db,saved,answer_text=answer,departments=['IT'])
    assert calls[-1][1]['question']['answer_text']==answer
    assert calls[-1][1]['question']['departments']==['IT']
    assert result['answer_text']==answer
    added=main.add_user_question(a['id'],AddQuestionInput(expected_version=2,preview_id=result['preview_id'],
        control_ids=[s.controls[0]['id']]),db)
    assert added['current_version']==3
    question=added['questions'][-1]
    assert question['initial_answer']==answer
    assert question['initial_answer_control_ids']==[s.controls[0]['id']]
    cid,other=s.controls[0]['id'],s.controls[1]['id']
    assert added['answers'][cid]['text'].startswith(saved['answers'][cid]['text'])
    assert result['text'] in added['answers'][cid]['text']
    assert added['answers'][cid]['text'].endswith(answer)
    assert added['answers'][cid]['status']=='partial'
    assert added['answers'][other]==saved['answers'][other]
    refreshed=main.get_assessment(a['id'],db)
    assert refreshed['questions'][-1]['initial_answer']==answer
    assert refreshed['answers']==added['answers']
    codex_case=agent_mode.make_case(agent_mode.client_inputs(db,db.get(Assessment,a['id'])),s.controls[0],[])
    assert answer in codex_case['answer']['text']
    assert any(q.get('source')=='user' and q['text']==result['text'] for q in codex_case['questions'])
    # This is an interview record; relevance mapping never sets a compliance verdict.
    assert refreshed['evaluation'] is None
    main.run_assessment(a['id'],RunInput(),db)
    assess_payload=[data for op,data in calls if op=='assess'][-1]
    assert answer in assess_payload['answers'][cid]['text']
    assert answer not in assess_payload['answers'][other]['text']
    _,binary=main.assessment_workbook(a['id'],db)
    book=excel.read(binary)
    assert answer in book[s.mapping['sheet']].cell(s.controls[0]['row'],s.mapping['indices']['answer']).value


def test_long_initial_answer_rolls_back_question_and_all_answer_updates(db, setup):
    s,a,p,calls=setup
    saved=main.save_answers(a['id'],ReplaceAnswers(expected_version=1,answers={
        s.controls[0]['id']:{'text':'Short original','status':'partial'},
        s.controls[1]['id']:{'text':'x'*19990,'status':'unanswered'}}),db)
    result=preview(db,saved,answer_text='Interview response')
    with pytest.raises(HTTPException) as error:
        main.add_user_question(a['id'],AddQuestionInput(expected_version=2,preview_id=result['preview_id'],
            control_ids=[c['id'] for c in s.controls]),db)
    assert error.value.status_code==422
    refreshed=main.get_assessment(a['id'],db)
    assert refreshed['current_version']==2
    assert refreshed['questions']==a['questions']
    assert refreshed['answers']==saved['answers']
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='question.add'))==0


def test_answer_cannot_be_substituted_when_confirming_an_ai_preview():
    with pytest.raises(ValidationError):
        AddQuestionInput(expected_version=1,preview_id='preview',control_ids=['SC-A'],answer_text='Different answer')


@pytest.mark.parametrize('ids', [['unknown'],['SC-A','SC-A']])
def test_unapproved_or_duplicate_ids_never_create_a_question(db, setup, ids):
    s,a,p,calls = setup
    result=preview(db,a)
    with pytest.raises(HTTPException) as error:
        main.add_user_question(a['id'],AddQuestionInput(expected_version=1,preview_id=result['preview_id'],control_ids=ids),db)
    assert error.value.status_code==422
    assert main.get_assessment(a['id'],db)['questions']==a['questions']
    assert main.get_assessment(a['id'],db)['current_version']==1


def test_preview_cannot_be_used_for_another_assessment(db, setup, monkeypatch):
    s,a,p,calls=setup
    result=preview(db,a)
    monkeypatch.setenv('AI_PROVIDER','offline')
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(ai.OfflineProvider()))
    other=assessment(db,s.id)
    with pytest.raises(HTTPException) as error:
        main.add_user_question(other['id'],AddQuestionInput(expected_version=1,preview_id=result['preview_id'],control_ids=[s.controls[0]['id']]),db)
    assert error.value.status_code==422


def test_stale_preview_cannot_overwrite_newer_answers(db, setup):
    s,a,p,calls=setup
    result=preview(db,a)
    saved=main.save_answers(a['id'],ReplaceAnswers(expected_version=1,
        answers={c['id']:{'text':'Newer answer'} for c in s.controls}),db)
    for version in (1,2):
        with pytest.raises(HTTPException) as error:
            main.add_user_question(a['id'],AddQuestionInput(expected_version=version,preview_id=result['preview_id'],control_ids=[s.controls[0]['id']]),db)
        assert error.value.status_code==409
    assert main.get_assessment(a['id'],db)['answers']==saved['answers']
    assert main.get_assessment(a['id'],db)['questions']==a['questions']


def test_old_run_and_export_become_stale_and_question_reaches_assessment(db, setup):
    s,a,p,calls=setup
    saved=main.save_answers(a['id'],ReplaceAnswers(expected_version=1,
        answers={c['id']:{'text':'Existing answer'} for c in s.controls}),db)
    run=main.run_assessment(a['id'],RunInput(),db)
    manifest=main.manifest(db.get(Assessment,a['id']))
    result=preview(db,saved)
    added=main.add_user_question(a['id'],AddQuestionInput(expected_version=2,preview_id=result['preview_id'],control_ids=[s.controls[0]['id']]),db)
    assert main.runs(a['id'],db)[0]['stale'] is True
    assert main.manifest(db.get(Assessment,a['id']))['version'] != manifest['version']
    main.run_assessment(a['id'],RunInput(),db)
    assess_payloads=[payload for op,payload in calls if op=='assess']
    assert any(q['text']==result['text'] for q in assess_payloads[-1]['questions'])
    assert len(run['skill']['questionnaire'])==len(a['questions'])


def test_excel_keeps_both_original_and_added_question_for_each_control(db, setup):
    s,a,p,calls=setup
    result=preview(db,a)
    added=main.add_user_question(a['id'],AddQuestionInput(expected_version=1,preview_id=result['preview_id'],control_ids=[s.controls[0]['id']]),db)
    grouped=text_by_control(added['questions'])
    cid=s.controls[0]['id']
    assert a['questions'][0]['text'] in grouped[cid]
    assert result['text'] in grouped[cid]
    # Fixture mapping has no Question column by default; explicitly enable its existing G column.
    mapping={**s.mapping,'columns':{**s.mapping['columns'],'question':'Question'},
             'indices':{**s.mapping['indices'],'question':7}}
    content=excel.export(s.template,mapping,s.controls,added['answers'],{'assessment_id':a['id'],'standard_id':s.id,'version':2},questions=added['questions'])
    book=excel.read(content)
    assert book[mapping['sheet']].cell(s.controls[0]['row'],7).value==grouped[cid]
    assert excel.import_answers(content,mapping,s.controls,{'assessment_id':a['id'],'standard_id':s.id,'version':2})==added['answers']


def test_offline_invalid_scope_and_wrong_skill_are_rejected_before_inference(db, setup, monkeypatch):
    s,a,p,calls=setup
    with pytest.raises(HTTPException) as error:
        preview(db,a,departments=['Not a department'])
    assert error.value.status_code==422 and not calls
    other=root_skill(db,standard(db,'2').id)
    with pytest.raises(HTTPException) as error:
        preview(db,a,skill_id=other['id'])
    assert error.value.status_code==422 and not calls
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(ai.OfflineProvider()))
    with pytest.raises(HTTPException) as error:
        preview(db,a)
    assert error.value.status_code==422 and not calls


def test_failed_mapping_does_not_create_a_partial_preview_or_question(db, setup, monkeypatch):
    s,a,p,calls=setup
    monkeypatch.setenv('AI_QUESTION_MAPPING_BATCH_SIZE','1')
    seen=[]
    def request(operation,payload):
        seen.append(payload)
        if len(seen)==2: raise TimeoutError('private-url secret-key')
        return [{'control_id':payload['controls'][0]['id'],'score':.8,'reason':'Relevant'}]
    monkeypatch.setattr(p,'request',request)
    with pytest.raises(HTTPException) as error: preview(db,a)
    assert error.value.status_code==502
    assert 'secret-key' not in str(error.value.detail)
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='question.mapping.preview'))==0
    assert main.get_assessment(a['id'],db)['questions']==a['questions']


def test_no_match_is_explicit_and_cannot_be_confirmed(db, setup, monkeypatch):
    s,a,p,calls=setup
    monkeypatch.setattr(p,'request',lambda *args:[])
    result=preview(db,a)
    assert result['candidates']==[]
    with pytest.raises(ValidationError): AddQuestionInput(expected_version=1,preview_id=result['preview_id'],control_ids=[])


def test_changed_assessment_during_ai_mapping_does_not_produce_a_stale_preview(db, setup, monkeypatch):
    s,a,p,calls=setup
    def request(operation,payload):
        main.save_answers(a['id'],ReplaceAnswers(expected_version=1,
            answers={c['id']:{'text':'Updated while AI ran'} for c in s.controls}),db)
        return [{'control_id':s.controls[0]['id'],'score':.8,'reason':'Relevant'}]
    monkeypatch.setattr(p,'request',request)
    with pytest.raises(HTTPException) as error: preview(db,a)
    assert error.value.status_code==409
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='question.mapping.preview'))==0


def test_new_gateway_health_advertises_mapping_operation(monkeypatch):
    from .test_gateway import gateway
    monkeypatch.setenv('INFERENCE_API_KEY','test-key')
    assert 'map_question' in gateway.health('Bearer test-key')['supported_operations']


def test_ollama_transport_supports_question_mapping_without_five_match_limit(monkeypatch):
    from io import BytesIO
    seen=[]
    result=[{'control_id':f'C-{i}','score':.8,'reason':'Relevant'} for i in range(6)]
    def open_request(request,**kwargs):
        seen.append(json.loads(request.data))
        return BytesIO(json.dumps({'done':True,'message':{'role':'assistant','content':json.dumps({'output':result})}}).encode())
    monkeypatch.setattr(ai,'build_opener',lambda *args:SimpleNamespace(open=open_request))
    p=ai.OllamaProvider(model='test')
    assert p.request('map_question',{'question':{'text':'Question'},'controls':[]})==result
    assert 'maxItems' not in seen[0]['format']['properties']['output']
    assert 'independently checking every requirement' in seen[0]['messages'][0]['content']


@pytest.mark.parametrize('bad', [
    [{'control_id':'unknown','score':.8,'reason':'Relevant'}],
    [{'control_id':'SC-A','score':True,'reason':'Relevant'}],
    [{'control_id':'SC-A','score':float('nan'),'reason':'Relevant'}],
    [{'control_id':'SC-A','score':.8,'reason':' '}],
    [{'control_id':'SC-A','score':.8,'reason':'Relevant'}]*2,
])
def test_mapping_validation_rejects_untrusted_invalid_outputs(bad):
    with pytest.raises(ValueError): validate_question_mapping(bad,[{'id':'SC-A'}])


def test_full_standard_mapping_is_not_limited_to_five_candidates_and_bounds_input(monkeypatch):
    monkeypatch.setenv('AI_QUESTION_MAPPING_BATCH_SIZE','24')
    controls=[{'id':f'C-{i}','requirement':'Full requirement '*100,'department':'IT'} for i in range(123)]
    calls=[]
    def request(operation,payload):
        calls.append(payload)
        return [{'control_id':c['id'],'score':.8,'reason':'Relevant'} for c in payload['controls']]
    result=question_mapping(request,'Question',controls,{},None,[])
    assert len(result)==123
    assert [c['id'] for call in calls for c in call['controls']]==[c['id'] for c in controls]
    assert all(len(call['controls'])<=24 for call in calls)
    assert all(c['requirement']==controls[0]['requirement'] for call in calls for c in call['controls'])


def test_gateway_accepts_map_question_without_exposing_user_question_as_system_instruction(monkeypatch):
    from .test_gateway import gateway
    seen=[]
    class Generator:
        tokenizer=SimpleNamespace(apply_chat_template=lambda *a,**kw:[1,2],encode=lambda *a,**kw:[1,2])
        def __call__(self,messages,**kw):
            seen.extend(messages)
            return [{'generated_text':'[]'}]
    monkeypatch.setattr(gateway,'model_pipeline',lambda:Generator())
    monkeypatch.setenv('INFERENCE_API_KEY','test-key')
    result=gateway.infer(gateway.Request(operation='map_question',system='Safety',payload={'question':{'text':'PRIVATE_QUESTION','answer_text':'PRIVATE_ANSWER'}}),'Bearer test-key')
    assert result['output']==[]
    assert 'PRIVATE_QUESTION' not in seen[0]['content']
    assert 'PRIVATE_QUESTION' in seen[1]['content']
    assert 'PRIVATE_ANSWER' not in seen[0]['content']
    assert 'PRIVATE_ANSWER' in seen[1]['content']
