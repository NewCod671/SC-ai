from copy import deepcopy
import json
from app.finding_style import INSTRUCTION
from app.prompt_compaction import compact


def test_style_is_rendered_once_without_modifying_customer_inputs_or_custom_rules():
    payload={'controls':[{'id':'A.8.3','requirement':'Exact imported requirement.'}],
        'answers':{'A.8.3':{'text':'[Q1] Exact question\nExact recorded answer.','status':'unanswered'}},
        'questions':[{'id':'q1','text':'Exact question','control_ids':['A.8.3']}],
        'standard_knowledge':{'text':'Operator-supplied reference'},
        'approved_examples':{'A.8.3':[{'historical_answer':'Approved answer'}]},
        'skill':{'core_instructions':'Custom immutable rule. '+INSTRUCTION,
            'review_procedure':'Apply the grade criteria. '+INSTRUCTION,
            'instructions':json.dumps({'review_steps':['Custom local rule.',INSTRUCTION],
                'control_rules':{'A.8.3':'Custom control rule.'}})}}
    original=deepcopy(payload)
    system,wire=compact('Shared skill A\n'+INSTRUCTION+'\nShared skill B\n'+INSTRUCTION,payload)
    assert system.count(INSTRUCTION)==1
    assert 'Shared skill A' in system and 'Shared skill B' in system
    assert wire['skill']['core_instructions']=='Custom immutable rule.'
    assert wire['skill']['review_procedure']=='Apply the grade criteria.'
    parsed=json.loads(wire['skill']['instructions'])
    assert parsed['review_steps']==['Custom local rule.']
    assert parsed['control_rules']=={'A.8.3':'Custom control rule.'}
    for key in ('controls','answers','questions','standard_knowledge','approved_examples'):
        assert wire[key]==original[key]
    assert payload==original


def test_non_json_operator_refinements_and_similar_custom_prose_are_preserved():
    prose='Keep this operator-specific scope limit. Similar wording is not a duplicate.'
    _,wire=compact('Safety.',{'skill':{'instructions':prose,'core_instructions':'Custom core'}})
    assert wire['skill']['instructions']==prose
    assert wire['skill']['core_instructions']=='Custom core'


def test_only_exact_repeated_checklist_text_is_removed_not_supplemental_questions():
    payload={'controls':[{'id':'4.1','requirement':'Scope heading\n\nExact original checklist question?'}],
        'answers':{'4.1':{'text':'Exact recorded answer.'}},'questions':[
            {'source':'checklist','text':'Exact original checklist question?','control_ids':['4.1']},
            {'source':'user','text':'Exact original checklist question?','control_ids':['4.1']},
            {'source':'checklist','text':'A distinct checklist follow-up?','control_ids':['4.1']}]}
    _,wire=compact('Safety.',payload)
    assert len(wire['questions'])==2
    assert wire['questions'][0]['source']=='user'
    assert wire['questions'][1]['text']=='A distinct checklist follow-up?'
    assert wire['controls']==payload['controls']
    assert wire['answers']==payload['answers']


def test_shared_question_is_retained_if_one_target_requirement_does_not_contain_it():
    payload={'controls':[{'id':'A','requirement':'Exact question?'},{'id':'B','requirement':'Other requirement'}],
        'questions':[{'source':'checklist','text':'Exact question?','control_ids':['A','B']}]}
    _,wire=compact('Safety.',payload)
    assert wire['questions']==payload['questions']
