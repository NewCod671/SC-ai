import json
import pytest
from fastapi import HTTPException
from app import ai, main
from app.assessment_batches import compact_batch, assess_batches
from app.schemas import SkillInput, SkillVersionInput, RunInput
from app.skill_templates import load_template, template_catalog, template_skill, batch_instructions
from .test_standard_skills import db, standard, assessment  # noqa: F401


def test_bundled_templates_are_valid_and_installed_against_exact_standard(db):
    s = standard(db)
    assert {x['id'] for x in template_catalog()} == {'sc-iso27001-audit-review'}
    for item in template_catalog():
        body = SkillInput(**template_skill(item['id'], s))
        instructions = json.loads(body.instructions)
        assert instructions['standard_scope'] == {'id': s.id, 'name': s.name, 'version': s.version}
        assert instructions['template_version'] == item['version']
        assert 'FIXED DECISION RULES' in body.core_instructions
        assert 'decision_rules' not in instructions
        assert 'never certify' in body.core_instructions.lower()


def test_install_is_explicit_scoped_and_never_overwrites_existing_version(db):
    first, second = standard(db), standard(db, '2')
    one = main.install_skill_template(first.id, 'iso27001-2022-review', db)
    two = main.install_skill_template(second.id, 'iso27001-2022-review', db)
    assert one['family_id'] != two['family_id']
    assert one['standard_id'] == first.id
    with pytest.raises(HTTPException) as error:
        main.install_skill_template(first.id, 'iso27001-2022-review', db)
    assert error.value.status_code == 409
    assert len(main.skills(first.id, db)) == 1


def test_unknown_templates_and_standards_return_404(db):
    s = standard(db)
    for standard_id, template_id in [(s.id, '../standard-review'), ('missing', 'standard-review')]:
        with pytest.raises(HTTPException) as error:
            main.install_skill_template(standard_id, template_id, db)
        assert error.value.status_code == 404


def test_only_exact_imported_ids_receive_supplemental_guidance(db):
    s = standard(db)
    s.controls = [{'id': cid, 'requirement': 'Imported requirement', 'department': 'IT'}
                  for cid in ['A.8.5', 'A.8.13', 'CUSTOM-1', '8.15']]
    value = template_skill('iso27001-2022-review', s)
    instructions = json.loads(value['instructions'])
    assert set(instructions['control_rules']) == {'A.8.5', 'A.8.13'}
    assert instructions['review_steps']  # Baseline applies to every control.
    assert 'Do not claim full ISO coverage' in instructions['scope_note']


def test_batch_only_sends_relevant_control_rules_and_never_mutates_snapshot(db):
    s = standard(db)
    s.controls = [{'id': cid, 'requirement': 'Imported requirement', 'department': 'IT'}
                  for cid in ['A.8.5', 'A.8.13', 'CUSTOM-1']]
    skill = template_skill('iso27001-2022-review', s)
    data = {'controls': s.controls, 'answers': {c['id']: {'text': 'Answer'} for c in s.controls}, 'skill': skill}
    original = json.dumps(data, sort_keys=True)
    batch = compact_batch(data, s.controls[:1])
    full, scoped = json.loads(skill['instructions']), json.loads(batch['skill']['instructions'])
    assert set(scoped['control_rules']) == {'A.8.5'}
    assert scoped['review_steps'] == full['review_steps']
    assert scoped['scope_note'] == full['scope_note']
    assert batch['skill']['core_instructions'] == skill['core_instructions']
    assert json.dumps(data, sort_keys=True) == original
    # Custom IDs still get all baseline checks, without unrelated supplemental rules.
    custom = json.loads(compact_batch(data, s.controls[2:])['skill']['instructions'])
    assert custom['control_rules'] == {}
    assert custom['review_steps'] == full['review_steps']


@pytest.mark.parametrize('text', ['Existing operator instructions', '[]', '{broken', '{"format":"other","control_rules":{}}'])
def test_existing_prose_and_other_formats_are_preserved(text):
    assert batch_instructions(text, {'A.8.5'}) == text


def test_template_core_and_full_source_are_pinned_in_runs_when_refinements_evolve(db):
    s = standard(db)
    skill = main.install_skill_template(s.id, 'standard-review', db)
    a = assessment(db, s.id)
    first = main.run_assessment(a['id'], RunInput(skill_id=skill['id']), db)
    child = main.evolve_skill(skill['id'], SkillVersionInput(version='1.1',
        instructions=skill['instructions'] + '\nUse concise recommendations.', change_note='Improve clarity'), db)
    assert child['core_instructions'] == skill['core_instructions']
    assert first['skill']['instructions'] == skill['instructions']
    assert first['skill']['version'] == '1.0'
    assert first['skill']['standard_id'] == s.id
    # Installing a template does not silently replace default operation instructions.
    assert main.selected_skill(db, None, s.id)['name'] == 'default'


def test_93_controls_keep_baseline_rules_in_all_12_batches(db, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '8')
    s = standard(db)
    s.controls = [{'id': f'C-{i}', 'requirement': 'Imported requirement', 'department': 'IT'} for i in range(93)]
    skill = template_skill('iso27001-2022-review', s)
    data = {'controls': s.controls, 'answers': {c['id']: {'text': 'Not implemented', 'status': 'no'} for c in s.controls},
            'skill': skill}
    calls = []
    def request(operation, batch):
        calls.append(batch)
        assert batch['skill']['core_instructions'] == skill['core_instructions']
        assert json.loads(batch['skill']['instructions'])['review_steps']
        return [{'control_id': c['id'], 'status': 'gap', 'rationale': 'Explicit absence',
                 'recommendation': 'Implement', 'evidence_ids': []} for c in batch['controls']]
    results, plan = assess_batches(request, data, ai.validate_findings)
    assert len(calls) == 12
    assert len(results) == len(plan['ai_control_ids']) == 93
    assert not plan['warnings']


def test_iso_template_contains_only_original_supplemental_guidance():
    template = load_template('iso27001-2022-review')
    assert len(template['instructions']['control_rules']) == 12
    assert 'normative ISO text' in template['instructions']['scope_note']
