import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import ai, knowledge, main
from app.assessment_batches import compact_batch
from app.schemas import ReplaceAnswers, RunInput
from app.skill_templates import load_template, template_skill
from .test_standard_skills import db, standard, assessment  # noqa: F401

TEMPLATE = 'iso27001-2022-knowledge-review'
REFERENCE = load_template(TEMPLATE)['instructions']['knowledge_ref']


def controls(*ids):
    return [{'id': cid, 'requirement': 'Imported requirement', 'department': 'IT'} for cid in ids]


def skill():
    return template_skill(TEMPLATE, SimpleNamespace(id='standard', name='ISO checklist', version='2022',
                                                   controls=controls('A.8.5', 'A.8.13', '6.1.3', 'CUSTOM')))


def test_real_pdf_bundle_covers_all_93_annex_ids_and_page_cited_clauses():
    bundle = knowledge.load_knowledge(REFERENCE)
    entries = bundle['entries']
    expected = {f'A.{group}.{i}' for group, count in [(5, 37), (6, 8), (7, 14), (8, 34)] for i in range(1, count+1)}
    assert {ref for ref, item in entries.items() if item['kind'] == 'annex_a'} == expected
    assert bundle['source']['pdf_page_count'] == 26
    assert bundle['source']['amendments_included'] == []
    assert entries['A.8.13']['pdf_pages'] == [22]
    assert entries['A.8.13']['printed_pages'] == [16]
    assert entries['clause:6.1.3']['pdf_pages'] == [10, 11]
    assert 'Statement of Applicability' in entries['clause:6.1.3']['text']
    assert 'Bibliography' not in entries['A.8.34']['text']
    for entry in entries.values():
        assert entry['text'] and entry['title']
        assert all(7 <= page <= 24 for page in entry['pdf_pages'])
        assert '--``' not in entry['text']
        assert 'All rights reserved' not in entry['text']


def test_exact_ids_do_not_confuse_annex_with_management_clause():
    entries = knowledge.load_knowledge(REFERENCE)['entries']
    assert knowledge.matched_references('A.6.1', entries) == ['A.6.1']
    assert set(knowledge.matched_references('6.1', entries)) == {'clause:6.1', 'clause:6.1.1', 'clause:6.1.2', 'clause:6.1.3'}
    assert knowledge.matched_references('Clause 6.1.3', entries) == ['clause:6.1.3']
    assert knowledge.matched_references('A.8.99', entries) == []
    assert knowledge.matched_references('CUSTOM', entries) == []


def test_batch_retrieval_retains_reference_pages_and_no_unrelated_controls():
    cs = controls('A.8.5', 'A.8.13', 'CUSTOM')
    prepared = knowledge.prepare_knowledge(skill(), cs)
    payload = {'controls': cs, 'answers': {c['id']: {'text': 'Answer'} for c in cs},
               'skill': skill(), 'standard_knowledge': prepared}
    before = json.dumps(payload, sort_keys=True)
    batch = compact_batch(payload, cs[:1])
    kb = batch['standard_knowledge']
    assert set(kb['entries']) == {'A.8.5', 'clause:1', 'clause:6.1.3'}
    assert kb['control_references'] == {'A.8.5': ['A.8.5']}
    assert kb['entries']['A.8.5']['pdf_pages'] == [22]
    assert kb['source']['sha256'] == REFERENCE['source_sha256']
    assert batch['evidence'] == []
    assert batch['allowed_evidence_ids'] == {'A.8.5': []}
    # Source text stays in untrusted payload, never promoted into skill/core.
    assert kb['entries']['A.8.5']['text'] not in batch['skill']['instructions']
    custom = compact_batch(payload, cs[2:])['standard_knowledge']
    assert custom['entries'] == {}
    assert custom['unmatched_control_ids'] == ['CUSTOM']
    assert json.dumps(payload, sort_keys=True) == before


def test_question_index_has_titles_and_pages_without_all_control_statements():
    cs = controls('A.8.5', 'A.8.13')
    kb = knowledge.prepare_knowledge(skill(), cs, questions=True)
    assert kb['mode'] == 'question_reference_index'
    assert 'text' not in kb['entries']['A.8.13']
    assert kb['entries']['A.8.13']['title'] == 'Information backup'
    assert kb['entries']['clause:6.1.3']['text']


@pytest.mark.parametrize('change', ['missing', 'bundle', 'source', 'traversal'])
def test_unavailable_tampered_or_unsafe_knowledge_fails_before_inference(tmp_path, monkeypatch, change):
    original = knowledge.KNOWLEDGE_ROOT
    monkeypatch.setattr(knowledge, 'KNOWLEDGE_ROOT', tmp_path)
    ref = dict(REFERENCE)
    (tmp_path / 'sources').mkdir()
    bundle = original / (ref['id'] + '.json')
    (tmp_path / bundle.name).write_bytes(bundle.read_bytes())
    source_name = ref['source_sha256'] + '.pdf'
    (tmp_path / 'sources' / source_name).write_bytes((original / 'sources' / source_name).read_bytes())
    if change == 'missing':
        (tmp_path / bundle.name).unlink()
    elif change == 'bundle':
        (tmp_path / bundle.name).write_bytes(b'{}')
    elif change == 'source':
        (tmp_path / 'sources' / source_name).write_bytes(b'changed')
    else:
        ref['id'] = '../outside'
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.load_knowledge(ref)


@pytest.mark.parametrize('provider_type', [ai.HTTPProvider, ai.OllamaProvider])
def test_both_providers_send_full_reference_text_for_assessment_only_as_data(provider_type, monkeypatch):
    monkeypatch.setenv('AI_ASSESS_BATCH_SIZE', '1')
    provider = provider_type(url='https://synthetic.example', model='test')
    calls = []
    def request(operation, batch):
        calls.append(batch)
        return [{'control_id': c['id'], 'status': 'needs_review', 'interpreted_answer': 'Self-report only',
                 'rationale': 'Need operational evidence', 'recommendation': 'Supply records', 'evidence_ids': []}
                for c in batch['controls']]
    monkeypatch.setattr(provider, 'request', request)
    cs = controls('A.8.5', 'A.8.13')
    result = provider.assess({'controls': cs, 'answers': {c['id']: {'text': 'Implemented'} for c in cs}, 'skill': skill()})
    assert len(result) == len(calls) == 2
    for batch in calls:
        cid = batch['controls'][0]['id']
        assert batch['standard_knowledge']['entries'][cid]['text']
        assert len(batch['standard_knowledge']['control_references']) == 1
    assert set(provider.standard_knowledge_snapshot['control_references']) == {'A.8.5', 'A.8.13'}


def test_http_question_generation_attaches_reference_index(monkeypatch):
    provider = ai.HTTPProvider(url='https://synthetic.example', model='test')
    cs = controls('A.8.5')
    calls = []
    def request(operation, payload):
        calls.append(payload)
        return [{'control_ids': ['A.8.5'], 'departments': ['IT'], 'text': 'How do you manage sign-in?', 'mapping_reason': 'Authentication'}]
    monkeypatch.setattr(provider, 'request', request)
    provider.questions(cs, {'skill': skill()})
    assert calls[0]['standard_knowledge']['mode'] == 'question_reference_index'


def test_run_pins_complete_knowledge_snapshot_and_separates_customer_evidence(db, monkeypatch):
    s = standard(db)
    s.controls = controls('A.8.5', 'A.8.13')
    db.commit()
    installed = main.install_skill_template(s.id, TEMPLATE, db)
    a = assessment(db, s.id)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1,
        answers={c['id']: {'text': 'Implemented'} for c in s.controls}), db)
    delegate = ai.HTTPProvider(url='https://synthetic.example', model='test')
    monkeypatch.setattr(delegate, 'request', lambda operation, batch: [
        {'control_id': c['id'], 'status': 'needs_review', 'rationale': 'Need evidence',
         'recommendation': 'Supply records', 'evidence_ids': []} for c in batch['controls']])
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))
    result = main.run_assessment(a['id'], RunInput(skill_id=installed['id']), db)
    kb = result['skill']['standard_knowledge_snapshot']
    assert kb['reference'] == REFERENCE
    assert kb['entries']['A.8.13']['text']
    assert kb['entries']['A.8.13']['pdf_pages'] == [22]
    assert result['skill']['evidence_sha256'] == {}
    assert result['summary']['status'] != 'passed'
    assert main.runs(a['id'], db)[0]['skill']['standard_knowledge_snapshot'] == kb
    shown = main.get_assessment(a['id'], db)['evaluation']['standard_knowledge']
    assert shown['matched_control_count'] == 2
    assert shown['unmatched_control_ids'] == []
    assert any(ref['id'] == 'A.8.13' and ref['pdf_pages'] == [22] for ref in shown['references'])
    assert all('text' not in ref for ref in shown['references'])


def test_core_and_existing_skills_are_not_rewritten_by_knowledge_template(db):
    s = standard(db)
    base = main.install_skill_template(s.id, 'iso27001-2022-review', db)
    derived = main.install_skill_template(s.id, TEMPLATE, db)
    assert base['id'] != derived['id']
    assert base['core_instructions'] == derived['core_instructions']
    assert knowledge.knowledge_reference(base) is None
    assert knowledge.knowledge_reference(derived) == REFERENCE


def test_integrity_error_is_a_safe_configuration_error():
    def fail():
        raise knowledge.KnowledgeError('Standard knowledge is unavailable')
    with pytest.raises(HTTPException) as error:
        main.ai_call(fail)
    assert error.value.status_code == 422


def test_builder_reproduces_bundle_from_local_source_without_network(tmp_path):
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'build_iso27001_knowledge.py'
    spec = importlib.util.spec_from_file_location('knowledge_builder', path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    source = knowledge.KNOWLEDGE_ROOT / 'sources' / (REFERENCE['source_sha256'] + '.pdf')
    reference = builder.build(source, tmp_path)
    # Filename is provenance metadata; rebuilding from a renamed source changes
    # the bundle hash, but source bytes, exact IDs and page citations stay stable.
    assert reference['source_sha256'] == REFERENCE['source_sha256']
    rebuilt = json.loads((tmp_path / (reference['id'] + '.json')).read_text(encoding='utf-8'))
    assert rebuilt['entries'] == knowledge.load_knowledge(REFERENCE)['entries']
    with pytest.raises(ValueError, match='Annex A'):
        builder.extract_entries(['No text'] * 26)
