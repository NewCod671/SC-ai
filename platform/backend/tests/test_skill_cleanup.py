import importlib.util
from copy import deepcopy
from pathlib import Path
import pytest
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import Session
from app import main
from app.db import Base
from app.models import Skill,SkillFamily,StandardSkillVersion,Standard,Run,Assessment,Revision,Audit

spec=importlib.util.spec_from_file_location('keep_current_sc_skill',Path(__file__).resolve().parents[2]/'scripts/keep_current_sc_skill.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_cleanup_keeps_current_skill_and_historical_snapshots():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.connection().exec_driver_sql('PRAGMA foreign_keys=ON')
        assert db.connection().exec_driver_sql('PRAGMA foreign_keys').scalar()==1
        standard=Standard(name='ISO/IEC 27001:2022 (SC Audit Checklist)',version='SC202512-03-V1',controls=[{'id':'C','requirement':'Example obligation','department':'IT','row':2}],mapping={},template=b'',changes={})
        other=Standard(name='Other',version='1',controls=[],mapping={},template=b'',changes={})
        db.add_all([standard,other]);db.flush()
        family=SkillFamily(standard_id=standard.id,name='SC ISO 27001:2022 audit review',core_instructions='Fixed core')
        retired_family=SkillFamily(standard_id=other.id,name='Old review',core_instructions='Old core')
        db.add_all([family,retired_family]);db.flush()
        parent=StandardSkillVersion(family_id=family.id,version='1.0',instructions='Old guidance')
        old_other=StandardSkillVersion(family_id=retired_family.id,version='1',instructions='Other guidance')
        db.add_all([parent,old_other]);db.flush()
        keep=StandardSkillVersion(family_id=family.id,parent_id=parent.id,version='1.1',instructions='Current SC guidance')
        legacy=Skill(name='Demo',version='1',instructions='Demo')
        db.add_all([keep,legacy]);db.flush()
        assessment=Assessment(name='History',standard_id=standard.id,context={'company':'X','departments':['IT']},questions=[],current_version=1)
        db.add(assessment);db.flush()
        snapshot={'id':parent.id,'instructions':'Old guidance','version':'1.0'}
        run=Run(assessment_id=assessment.id,revision=1,provider='http:model',skill=deepcopy(snapshot),results=[{'control_id':'C','status':'needs_review','rationale':'Pending','recommendation':'Review','evidence_ids':[]}])
        db.add(run);db.add(Revision(assessment_id=assessment.id,version=1,answers={'C':{'status':'unanswered','text':''}},source='test'));db.commit()
        keepid=keep.id;runid=run.id
        result=module.cleanup(db,keepid);db.commit()
        assert result['removed_versions']==2 and result['removed_legacy']==1
        assert db.scalar(select(func.count()).select_from(StandardSkillVersion))==1
        assert db.scalar(select(func.count()).select_from(SkillFamily))==1
        assert db.scalar(select(func.count()).select_from(Skill))==0
        kept=db.get(StandardSkillVersion,keepid)
        assert kept.parent_id is None and kept.instructions=='Current SC guidance'
        assert db.get(Run,runid).skill==snapshot
        assert db.scalar(select(func.count()).select_from(Standard))==2
        assert main.selected_skill(db,None,standard.id)['id']==keepid
        assert main.assessment_json(db,assessment)['selected_skill_id']==keepid
        assert module.cleanup(db,keepid)['removed_versions']==0
        assert db.scalar(select(func.count()).select_from(Audit))==1
    engine.dispose()


def test_missing_keep_target_does_not_remove_anything():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Skill(name='Demo',version='1',instructions='Keep until valid target exists'));db.commit()
        with pytest.raises(ValueError,match='no changes made'):module.cleanup(db,'missing')
        assert db.scalar(select(func.count()).select_from(Skill))==1
    engine.dispose()
