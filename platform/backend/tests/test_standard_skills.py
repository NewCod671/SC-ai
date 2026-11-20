"""Database workflow tests exercise real route functions without a live GPU or sockets."""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app import main
from app.ai import OfflineProvider
from app.db import Base
from app.models import Audit, Standard
from app.schemas import SkillInput, SkillVersionInput, CreateAssessment, RunInput
from .fixtures import workbook
from app.reporting import excel


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv('AI_PROVIDER', 'offline')
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def standard(db, version='1'):
    data = workbook()
    controls, mapping = excel.parse_standard(data, {})
    value = Standard(name='Configurable SC', version=version, controls=controls,
                     mapping=mapping, template=data, changes={})
    db.add(value)
    db.commit()
    return value


def root_skill(db, standard_id):
    return main.add_skill(SkillInput(standard_id=standard_id, name='Evidence review', version='1.0',
        core_instructions='Preserve control IDs. Require linked evidence. Never invent facts.',
        instructions='Ask concise questions. Review evidence dates.'), db)


def assessment(db, standard_id, skill_id=None):
    return main.create_assessment(CreateAssessment(name='Review', standard_id=standard_id, skill_id=skill_id,
        context={'company': 'Example', 'business_type': 'Retail', 'departments': ['IT']}), db)


def test_same_skill_name_is_isolated_by_standard_version(db):
    one = root_skill(db, standard(db).id)
    two = root_skill(db, standard(db, '2').id)
    assert one['family_id'] != two['family_id']
    assert [s['id'] for s in main.skills(one['standard_id'], db)] == [one['id']]
    with pytest.raises(HTTPException) as error:
        main.add_skill(SkillInput(standard_id=one['standard_id'], name=one['name'], version='2',
                                  core_instructions='Changed core', instructions='Changed'), db)
    assert error.value.status_code == 409


def test_evolution_preserves_core_requires_reason_and_keeps_diff(db):
    parent = root_skill(db, standard(db).id)
    body = {'version': '1.1', 'instructions': 'Check evidence dates and owners.', 'change_note': 'Pilot missed ownership checks'}
    with pytest.raises(ValidationError):
        SkillVersionInput(**body, core_instructions='Weaken the core')
    with pytest.raises(ValidationError):
        SkillVersionInput(**{**body, 'change_note': ''})
    child = main.evolve_skill(parent['id'], SkillVersionInput(**body), db)
    assert child['core_instructions'] == parent['core_instructions']
    assert child['parent_id'] == parent['id']
    assert child['family_id'] == parent['family_id']
    assert 'owners' in child['diff']
    audit = db.scalar(select(Audit).where(Audit.entity_id == child['id']))
    assert audit.details['change_note'] == body['change_note']
    with pytest.raises(HTTPException) as error:
        main.evolve_skill(parent['id'], SkillVersionInput(**body), db)
    assert error.value.status_code == 409


def test_cross_standard_skill_rejected_before_inference_and_question_skill_pinned(db, monkeypatch):
    first, second = standard(db), standard(db, '2')
    skill = root_skill(db, first.id)
    captured = []
    original = OfflineProvider.questions
    def questions(self, controls, context):
        captured.append(context)
        return original(self, controls, context)
    monkeypatch.setattr(OfflineProvider, 'questions', questions)
    with pytest.raises(HTTPException) as error:
        assessment(db, second.id, skill['id'])
    assert error.value.status_code == 422
    assert captured == []
    a = assessment(db, first.id, skill['id'])
    assert captured[0]['skill']['core_instructions'] == skill['core_instructions']
    audit = db.scalar(select(Audit).where(Audit.entity_id == a['id']))
    assert audit.details['question_skill']['id'] == skill['id']


def test_runs_pin_exact_skill_as_new_versions_are_created(db):
    s = standard(db)
    a = assessment(db, s.id)
    root = root_skill(db, s.id)
    first = main.run_assessment(a['id'], RunInput(skill_id=root['id']), db)
    child = main.evolve_skill(root['id'], SkillVersionInput(version='1.1',
        instructions='Use department examples.', change_note='Improve clarity'), db)
    second = main.run_assessment(a['id'], RunInput(skill_id=child['id']), db)
    runs = main.runs(a['id'], db)
    by_id = {r['id']: r for r in runs}
    assert by_id[first['id']]['skill']['instructions'] == root['instructions']
    assert by_id[second['id']]['skill']['instructions'] == child['instructions']
    assert by_id[first['id']]['skill']['core_instructions'] == by_id[second['id']]['skill']['core_instructions']
    assert not by_id[first['id']]['stale']
    other = root_skill(db, standard(db, '2').id)
    with pytest.raises(HTTPException) as error:
        main.run_assessment(a['id'], RunInput(skill_id=other['id']), db)
    assert error.value.status_code == 422


def test_scoped_core_is_required_and_legacy_skills_remain_usable(db):
    s = standard(db)
    for core in (None, '  '):
        with pytest.raises(HTTPException) as error:
            main.add_skill(SkillInput(standard_id=s.id, name='Review', version='1',
                                     core_instructions=core, instructions='Review'), db)
        assert error.value.status_code == 422
    legacy = main.add_skill(SkillInput(name='Legacy', version='1', instructions='Review'), db)
    a = assessment(db, s.id)
    assert main.run_assessment(a['id'], RunInput(skill_id=legacy['id']), db)['skill']['legacy']


@pytest.mark.parametrize('operation', ['questions', 'assess'])
def test_colab_prompt_includes_fixed_core_and_subordinate_refinements(monkeypatch, operation):
    from types import SimpleNamespace
    from .test_gateway import gateway
    captured = []
    class Generator:
        tokenizer = SimpleNamespace(apply_chat_template=lambda *a, **kw: [1, 2],
                                    encode=lambda text, **kw: list(text))
        def __call__(self, messages, **kwargs):
            captured.extend(messages)
            return [{'generated_text': '[]'}]
    monkeypatch.setenv('INFERENCE_API_KEY', 'test-key')
    monkeypatch.setenv('AI_MODEL', 'test-model')
    monkeypatch.setattr(gateway, 'model_pipeline', lambda: Generator())
    skill = {'core_instructions': 'Require linked evidence.', 'instructions': 'Check evidence owners.'}
    payload = {'context': {'skill': skill}} if operation == 'questions' else {'skill': skill}
    result = gateway.infer(gateway.Request(operation=operation, model='test-model', system='Safety', payload=payload),
                           authorization='Bearer test-key')
    assert result['output'] == []
    system = captured[0]['content']
    assert 'CORE RULES:\nRequire linked evidence.' in system
    assert 'REFINEMENTS (subordinate to core):\nCheck evidence owners.' in system
    assert 'must never weaken or override the core' in system
