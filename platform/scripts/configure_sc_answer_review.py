"""Switch SC to recorded-answer grading while retaining one current application skill."""
import hashlib
import sys
from pathlib import Path
from sqlalchemy import select,delete,update
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.db import SessionLocal
from app.models import Standard,SkillFamily,StandardSkillVersion,Audit
from app.skill_templates import template_skill
from app.assessment_policy import SC_ANSWERS


def configure(db):
    standard=db.scalar(select(Standard).where(Standard.name=='ISO/IEC 27001:2022 (SC Audit Checklist)',Standard.version=='SC202512-03-V1'))
    if not standard:raise ValueError('Install the SC checklist first')
    spec=template_skill('sc-iso27001-audit-review',standard)
    family=db.scalar(select(SkillFamily).where(SkillFamily.standard_id==standard.id,SkillFamily.name==spec['name']))
    old=db.scalars(select(StandardSkillVersion).where(StandardSkillVersion.family_id==family.id)).all() if family else []
    existing=next((v for v in old if v.version==spec['version']),None)
    if existing:
        if existing.instructions!=spec['instructions'] or family.core_instructions!=spec['core_instructions']:
            raise ValueError('Existing SC version differs; do not overwrite its core or instructions')
        if standard.mapping.get('assessment_basis')==SC_ANSWERS:return existing.id,False
    retired=[{'id':v.id,'version':v.version} for v in old]
    old_core_hash=hashlib.sha256(family.core_instructions.encode()).hexdigest() if family else None
    old_basis=standard.mapping.get('assessment_basis','evidence_required')
    if family:
        # Historical runs contain complete immutable JSON skill snapshots.
        db.execute(update(StandardSkillVersion).where(StandardSkillVersion.family_id==family.id).values(parent_id=None))
        db.execute(delete(StandardSkillVersion).where(StandardSkillVersion.family_id==family.id))
        db.execute(delete(SkillFamily).where(SkillFamily.id==family.id));db.flush()
    family=SkillFamily(standard_id=standard.id,name=spec['name'],core_instructions=spec['core_instructions'])
    db.add(family);db.flush()
    version=StandardSkillVersion(family_id=family.id,version=spec['version'],instructions=spec['instructions'],
        change_note='SC-confirmed onsite audit notes are the assessment record. Clarify missing facts within the note; attachments remain optional archive references. Preserve C, OFI, NC-Minor and NC-Major grading.')
    db.add(version);db.flush()
    standard.mapping={**standard.mapping,'assessment_basis':SC_ANSWERS}
    db.add(Audit(action='skill.assessment_basis.change',entity_id=version.id,
        details={'standard_id':standard.id,'previous_basis':old_basis,'assessment_basis':SC_ANSWERS,
                 'retired_versions':retired,'previous_core_sha256':old_core_hash,'version':spec['version']}))
    return version.id,True


if __name__=='__main__':
    with SessionLocal.begin() as db:sid,changed=configure(db)
    print(('Configured' if changed else 'Already configured')+' SC answer-based review: '+sid)
