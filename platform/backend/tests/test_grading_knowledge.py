from copy import deepcopy
import pytest
from app import ai, agent_mode, assessment_policy, grading_knowledge, incremental
from app.assessment_batches import compact_batch
from .test_standard_skills import db, standard, assessment


def test_knowledge_has_three_grades_and_explicit_conditions_without_guessing_unknown_ids():
    knowledge,snapshot=grading_knowledge.load()
    assert len(snapshot['sha256'])==64
    assert len(knowledge['sources'])==3
    for group in knowledge['groups']:
        cases=grading_knowledge.examples({'id':group['control_ids'][0]},knowledge)
        assert {c['category'] for c in cases}=={'ofi','nc_minor','nc_major'}
        assert all(c['synthetic'] and c['requirement_condition'] and c['distinction'] for c in cases)
    assert grading_knowledge.examples({'id':'UNKNOWN-CONTROL'},knowledge)==[]


def test_colab_and_codex_receive_same_examples_scoped_to_actual_control():
    controls=[{'id':'A.8.13','requirement':'Imported backup requirement'}, {'id':'9.2','requirement':'Imported audit requirement'}]
    answers={c['id']:{'status':'unanswered','text':'Actual recorded customer note'} for c in controls}
    payload={'assessment_basis':assessment_policy.SC_ANSWERS,
        'controls':[{**c,'grading_examples':grading_knowledge.examples(c)} for c in controls],
        'answers':answers,'context':{},'questions':[],'evidence':[]}
    scoped=compact_batch(payload,payload['controls'][:1])
    case=agent_mode.make_case({'context':{},'questions':[],'answers':answers},controls[0],[])
    assert scoped['controls'][0]['grading_examples']==case['control']['grading_examples']
    assert len(scoped['controls'])==1
    assert scoped['answers']['A.8.13']['text']=='Actual recorded customer note'
    assert 'assessment_questions' not in scoped['controls'][0]
    assert 'internal audits' not in str(scoped['controls'][0]['grading_examples'])


def test_changed_calibration_or_shared_skill_hash_invalidates_cached_finding():
    control={'id':'A.8.13','requirement':'Imported backup requirement','grading_examples':grading_knowledge.examples({'id':'A.8.13'})}
    payload={'assessment_basis':assessment_policy.SC_ANSWERS,'controls':[control],
        'answers':{'A.8.13':{'text':'Recorded note'}},'context':{},'evidence':[]}
    before=incremental.fingerprints(payload,'http:test')
    changed=deepcopy(payload);changed['controls'][0]['grading_examples'][0]['distinction']='Refined interpretation'
    assert incremental.fingerprints(changed,'http:test')!=before
    changed=deepcopy(payload);changed['codex_agent_skills']={'finding-classifier':{'sha256':'new-skill-hash'}}
    assert incremental.fingerprints(changed,'http:test')!=before


def test_clause_8_3_never_receives_annex_a_8_3_access_examples():
    clause=grading_knowledge.examples({'id':'8.3'})
    annex=grading_knowledge.examples({'id':'A.8.3'})
    assert len(clause)==len(annex)==3
    assert any('risk treatment' in c['requirement_condition'].lower() for c in clause)
    assert all('RBAC' not in c['note'] for c in clause)
    assert any('RBAC' in c['note'] for c in annex)
    assert grading_knowledge.examples({'id':'8.13'})==[]


@pytest.mark.parametrize('category,status,impact,gap',[
    ('ofi','met','does_not_affect',''),
    ('nc_minor','partial','does_not_affect','A required review was missed.'),
    ('nc_major','gap','affects','The required recovery process is absent across scope.')])
def test_all_three_grade_contracts_accept_recorded_notes_without_archived_attachments(category,status,impact,gap):
    row={'control_id':'A.8.13','status':status,'rationale':'Recorded process facts and extent.',
        'recommendation':'Address the specific issue.','evidence_ids':[],
        'audit':{'category':category,'reason':'Recorded facts support this assessment.',
            'requirement_gap':gap,'system_impact':impact}}
    checked=ai.validate_findings([row],[{'id':'A.8.13'}],[],
        {'A.8.13':{'text':'Actual SC onsite note with the relevant facts.'}},assessment_policy.SC_ANSWERS)
    assert checked[0]['audit']['category']==category


def test_sc_run_includes_scoped_knowledge_and_remains_current_without_question_expansion(db,monkeypatch):
    from app import main
    from app.schemas import ReplaceAnswers,RunInput
    monkeypatch.setenv('SC_ASSESS_BATCH_SIZE','1')
    monkeypatch.setenv('AI_ASSESS_CONCURRENCY','1')
    s=standard(db)
    s.mapping={**s.mapping,'assessment_basis':assessment_policy.SC_ANSWERS}
    s.controls=[{**c,'id':cid} for c,cid in zip(s.controls,['A.8.13','9.2'])]
    db.add(s);db.commit()
    a=assessment(db,s.id)
    main.save_answers(a['id'],ReplaceAnswers(expected_version=1,answers={
        c['id']:{'status':'unanswered','text':'Actual customer short note.'} for c in s.controls}),db)
    delegate=ai.HTTPProvider(url='https://synthetic.example',model='test-model');calls=[]
    def request(operation,payload):
        calls.append(payload)
        assert len(payload['controls'])==1
        control=payload['controls'][0]
        assert len(control['grading_examples'])==3
        assert 'assessment_questions' not in control
        return [{'control_id':control['id'],'status':'needs_review','rationale':'Clarify actual customer note.',
            'recommendation':'Record missing practice detail.','evidence_ids':[]}]
    monkeypatch.setattr(delegate,'request',request)
    monkeypatch.setattr(ai,'provider',lambda:ai.ValidatedProvider(delegate))
    run=main.run_assessment(a['id'],RunInput(),db)
    assert len(calls)==2 and len(run['results'])==2
    assert run['skill']['grading_knowledge']==grading_knowledge.load()[1]
    assert run['analysis_plan']['assessment_unit']=='control'
    assert run['stale'] is False
