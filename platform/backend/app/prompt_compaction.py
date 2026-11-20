"""Remove repeated canonical instructions from SC transport, never customer data."""
from copy import deepcopy
import json
from .finding_style import INSTRUCTION

VERSION='sc-exact-dedup-1'


def compact(system,payload):
    wire=deepcopy(payload)
    def remove(value):
        if isinstance(value,str):return value.replace(INSTRUCTION,'').strip()
        if isinstance(value,list):return [remove(item) for item in value if item!=INSTRUCTION]
        if isinstance(value,dict):return {key:remove(item) for key,item in value.items()}
        return value
    skill=wire.get('skill')
    if isinstance(skill,dict):
        for field in ('core_instructions','review_procedure'):
            if isinstance(skill.get(field),str):skill[field]=remove(skill[field])
        instructions=skill.get('instructions')
        if isinstance(instructions,str):
            try:structured=json.loads(instructions)
            except (ValueError,TypeError):skill['instructions']=remove(instructions)
            else:skill['instructions']=json.dumps(remove(structured),ensure_ascii=False,separators=(',',':'))
    controls={control['id']:control for control in wire.get('controls',[])}
    if isinstance(wire.get('questions'),list):
        kept=[]
        for question in wire['questions']:
            targets=question.get('control_ids',[question.get('control_id')])
            text=question.get('text','')
            duplicate=(question.get('source')=='checklist' and isinstance(text,str) and text.strip()
                and not question.get('mapping_reason') and targets
                and all(cid in controls and not controls[cid].get('assessment_questions')
                    and text in controls[cid].get('requirement','') for cid in targets))
            if not duplicate:kept.append(question)
        # Exact imported wording already remains in each target requirement.
        # Supplemental questions, raw answers and all distinct wording remain.
        wire['questions']=kept
    # All shared project skills remain represented; identical style guidance
    # is rendered once, after their specific instructions.
    concise_aux=('Keep audit.reason, requirement_gap and recommendation concise, normally one clause each. '
        'Avoid repeating the finding in these auxiliary fields; retain the facts needed to justify severity. '
        'Do not shorten the factual finding merely to meet this preference.')
    return system.replace(INSTRUCTION,'').strip()+'\n'+concise_aux+'\n'+INSTRUCTION,wire
