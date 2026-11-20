import json
import httpx
from app import ai, assessment_policy
from app.assessment_batches import assess_batches
from .test_gateway import gateway
from .test_standard_skills import db, standard, assessment
from .test_inference_diagnostics import fake_gateway


def setup_payload(monkeypatch):
    monkeypatch.setenv('SC_ASSESS_BATCH_SIZE','1')
    monkeypatch.setenv('AI_ASSESS_CONCURRENCY','1')
    monkeypatch.setenv('AI_ASSESS_FAILURE_MODE','continue')
    monkeypatch.setenv('AI_ASSESS_FORMAT_RETRIES','1')
    monkeypatch.setenv('AI_ASSESS_TRANSIENT_RETRIES','0')
    units=[{'question_id':f'q{i}','question_text':f'Question {i}?','answer_text':f'Onsite note {i}'} for i in range(3)]
    return {'controls':[{'id':'4.1','requirement':'Imported requirement','assessment_questions':units}],
        'answers':{'4.1':{'status':'unanswered','text':'Recorded onsite notes'}},'evidence':[],
        'assessment_basis':assessment_policy.SC_ANSWERS}


def finding(units):
    audit={'category':'comply','reason':'Recorded practice conforms.','requirement_gap':'','system_impact':'does_not_affect'}
    return {'control_id':'4.1','status':'met','rationale':'Recorded practice conforms.',
        'recommendation':'','evidence_ids':[],'audit':audit,
        'question_findings':[{'question_id':u['question_id'],'question_text':u['question_text'],
            'status':'met','rationale':u['answer_text'],'recommendation':'','evidence_ids':[],'audit':audit} for u in units]}


def validator(raw, controls, evidence, answers):
    return ai.validate_findings(raw,controls,evidence,answers,assessment_policy.SC_ANSWERS)


def test_missing_questions_retries_only_missing_and_preserves_valid_peer(monkeypatch):
    payload=setup_payload(monkeypatch); calls=[]
    def request(operation,batch):
        units=batch['controls'][0]['assessment_questions']; calls.append([u['question_id'] for u in units])
        return [finding(units[:1] if len(units)>1 else units)]
    rows,plan=assess_batches(request,payload,validator)
    assert calls==[['q0','q1','q2'],['q1'],['q2']]
    assert [q['question_id'] for q in rows[0]['question_findings']]==['q0','q1','q2']
    assert rows[0]['audit']['category']=='comply'
    assert not plan['warnings']


def test_failed_question_keeps_successful_peers_and_is_not_reusable(monkeypatch):
    payload=setup_payload(monkeypatch)
    def request(operation,batch):
        units=batch['controls'][0]['assessment_questions']
        if len(units)==1 and units[0]['question_id']=='q1':raise TimeoutError()
        return [finding(units[:1] if len(units)>1 else units)]
    rows,plan=assess_batches(request,payload,validator)
    assert [q['audit']['category'] for q in rows[0]['question_findings']]==['comply','needs_review','comply']
    assert plan['failed_control_ids']==['4.1']
    assert plan['warnings'][0]['question_id']=='q1'


def test_repeated_invalid_json_recovers_single_questions_from_parent_only_output(monkeypatch):
    payload=setup_payload(monkeypatch); calls=[]
    def request(operation,batch):
        units=batch['controls'][0]['assessment_questions']; calls.append(len(units))
        if len(units)>1:
            response=httpx.Response(502,headers={'X-Inference-Failure-Code':'invalid_json'},request=httpx.Request('POST','https://synthetic.test/infer'))
            raise httpx.HTTPStatusError('Synthetic invalid JSON',request=response.request,response=response)
        row=finding(units); row.pop('question_findings'); return [row]
    rows,plan=assess_batches(request,payload,validator)
    assert calls==[3,3,1,1,1]
    assert len(rows[0]['question_findings'])==3
    assert not plan['warnings']


def test_complete_compact_singleton_is_accepted_without_recovering_nested_fragments():
    row={'c':'4.1','s':'met','r':'Recorded note','a':{'c':'comply'}}
    assert gateway.extract_json(json.dumps(row),list,'assess')==[row]


def test_default_run_combines_all_questions_and_added_answer_per_control(db,monkeypatch):
    from app import main
    from app.schemas import ReplaceAnswers, AddControlQuestionInput, RunInput
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE','5')
    monkeypatch.setenv('AI_ASSESS_CONCURRENCY','1')
    s=standard(db); a=assessment(db,s.id); cid=s.controls[0]['id']
    a=main.save_answers(a['id'],ReplaceAnswers(expected_version=1,answers={
        c['id']:{'status':'unanswered','text':'[Q1] Practice?\nRecorded onsite practice'} for c in s.controls}),db)
    a=main.add_control_question(a['id'],AddControlQuestionInput(expected_version=a['current_version'],
        control_id=cid,text='Additional company question?',answer_text='Additional onsite detail'),db)
    delegate=ai.HTTPProvider(url='https://synthetic.example',model='test-model'); calls=[]
    def request(operation,payload):
        calls.append(payload)
        assert all('assessment_questions' not in c for c in payload['controls'])
        assert 'Additional onsite detail' in payload['answers'][cid]['text']
        assert any(q['text']=='Additional company question?' for q in payload['questions'])
        return [{'control_id':c['id'],'status':'needs_review','rationale':'Combined control finding.',
            'recommendation':'Clarify practice.','evidence_ids':[]} for c in payload['controls']]
    monkeypatch.setattr(delegate,'request',request)
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(delegate))
    run=main.run_assessment(a['id'],RunInput(),db)
    assert len(calls)==1
    assert len(run['results'])==len(s.controls)
    assert run['analysis_plan']['assessment_unit']=='control'
    assert run['analysis_plan']['source_question_count']>len(s.controls)


def test_codex_case_keeps_all_notes_without_expanding_question_units():
    from app import agent_mode, codex_provider
    control={'id':'4.1','requirement':'Imported requirement'}
    questions=[{'id':f'q{i}','control_ids':['4.1'],'text':f'Interview question {i}?','source':'user'} for i in range(6)]
    inputs={'context':{},'questions':questions,'answers':{'4.1':{'status':'unanswered','text':'All six recorded responses'}}}
    case=agent_mode.make_case(inputs,control,[])
    assert len(case['questions'])==6
    assert case['answer']['text']=='All six recorded responses'
    assert 'assessment_questions' not in case['control']
    assert 'question_findings' not in codex_provider.output_schema()['properties']['finding']['properties']


def test_colab_combined_mode_does_not_require_nested_question_output(fake_gateway,monkeypatch):
    setup_payload(monkeypatch)
    payload={'controls':[{'id':'4.1','requirement':'Imported requirement'}],
        'answers':{'4.1':{'text':'Recorded answers'}},'assessment_basis':assessment_policy.SC_ANSWERS,
        'skill':{'review_procedure':'Use recorded notes.'}}
    gateway.infer(gateway.Request(operation='assess',model='test-model',system='Assess one result per control.',
        prompt_profile='compact-v1',payload=payload),'Bearer test-key')
    system=fake_gateway['messages'][0]['content']
    assert 'WIRE OUTPUT CONTRACT' in system
    assert 'Do not output JSON, control IDs, evidence IDs or per-question grades' in system
    assert 'q=one object for every assessment_questions' not in system
